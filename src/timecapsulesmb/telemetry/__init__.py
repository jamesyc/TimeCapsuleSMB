from __future__ import annotations

import json
import os
import platform
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.core.process import run_process
from timecapsulesmb.core.release import CLI_VERSION, RELEASE_TAG, SAMBA_VERSION
from timecapsulesmb.identity import load_install_identity
from timecapsulesmb.transport.http import HttpError, http_post_json


SCHEMA_VERSION = 5
DEFAULT_TELEMETRY_URL = "https://timecapsulesmb.jamesyc.com/v1/events"
TELEMETRY_URL_ENV = "TCAPSULE_TELEMETRY_URL"
TELEMETRY_TOKEN_ENV = "TCAPSULE_TELEMETRY_TOKEN"
DEFAULT_TELEMETRY_TOKEN = "d65373762e893ae18c8aaa95a8f1b3a3464611f33b30983909543535fa8b0733"
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_SEND_ATTEMPTS = 2
# How long a synchronous send waits for the background sends before it. One
# send's attempts take at most this long unless the server trickles its reply.
PENDING_SEND_WAIT_SECONDS = REQUEST_TIMEOUT_SECONDS * MAX_SEND_ATTEMPTS


@dataclass(frozen=True)
class TelemetryContext:
    install_id: str
    cli_version: str
    release_tag: str
    samba_version: str
    host_os: str
    host_os_version: str
    configure_id: str | None = None


class TelemetryClient:
    def __init__(self, *, endpoint: str, token: str | None, context: TelemetryContext | None, enabled: bool) -> None:
        self.endpoint = endpoint
        self.token = token
        self.context = context
        self.enabled = enabled and context is not None and bool(token)
        self._pending_lock = threading.Lock()
        self._pending: list[threading.Thread] = []

    @classmethod
    def from_config(
        cls,
        config: AppConfig,
        *,
        bootstrap_path: Path | None = None,
    ) -> "TelemetryClient":
        identity = load_install_identity(bootstrap_path)
        endpoint = os.getenv(TELEMETRY_URL_ENV, DEFAULT_TELEMETRY_URL)
        token = os.getenv(TELEMETRY_TOKEN_ENV, DEFAULT_TELEMETRY_TOKEN).strip() or None
        if not identity.install_id:
            return cls(endpoint=endpoint, token=token, context=None, enabled=False)
        context = TelemetryContext(
            install_id=identity.install_id,
            cli_version=CLI_VERSION,
            release_tag=RELEASE_TAG,
            samba_version=SAMBA_VERSION,
            host_os=detect_host_os(),
            host_os_version=detect_host_os_version(),
            configure_id=config.get("TC_CONFIGURE_ID") or None,
        )
        return cls(endpoint=endpoint, token=token, context=context, enabled=identity.telemetry_enabled)

    def emit(
        self,
        event: str,
        *,
        synchronous: bool = False,
        operation: str | None = None,
        phase: str | None = None,
        operation_id: str | None = None,
        entrypoint: str | None = None,
        client: str | None = None,
        options: dict[str, object] | None = None,
        details: dict[str, object] | None = None,
        **fields: object,
    ) -> None:
        if not self.enabled or self.context is None:
            return
        try:
            inferred_operation, inferred_phase = infer_operation_phase(event)
            operation = operation or inferred_operation
            phase = phase or inferred_phase
            payload: dict[str, object] = {
                "schema_version": SCHEMA_VERSION,
                "event": event,
                "event_id": str(uuid.uuid4()),
                "occurred_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "install_id": self.context.install_id,
                "cli_version": self.context.cli_version,
                "release_tag": self.context.release_tag,
                "samba_version": self.context.samba_version,
                "host_os": self.context.host_os,
                "host_os_version": self.context.host_os_version,
            }
            if operation:
                payload["operation"] = operation
            if phase:
                payload["phase"] = phase
            if operation_id:
                payload["operation_id"] = operation_id
            if entrypoint:
                payload["entrypoint"] = entrypoint
            if client:
                payload["client"] = client
            if self.context.configure_id:
                payload["configure_id"] = self.context.configure_id
            if options is not None:
                payload["options"] = options
            if details is not None:
                payload["details"] = details
            for key, value in fields.items():
                if value is not None:
                    payload[key] = value
            if synchronous:
                self._wait_for_pending_sends()
                self._send_payload(payload)
                return
            self._dispatch_payload_async(payload)
        except Exception:
            return

    def _dispatch_payload_async(self, payload: dict[str, object]) -> None:
        thread = threading.Thread(target=self._send_payload, args=(payload,), daemon=True)
        thread.start()
        with self._pending_lock:
            self._pending = [pending for pending in self._pending if pending.is_alive()]
            self._pending.append(thread)

    def _wait_for_pending_sends(self) -> None:
        """Let the background sends finish before the synchronous one.

        The synchronous send is an operation's last event, and the process
        exits after it. A background send still running then is cut off
        mid-request (the server gets headers and no body), and the
        interpreter shuts down around its daemon thread. Waiting also keeps
        an operation's started event ahead of its finished one.
        """
        with self._pending_lock:
            pending, self._pending = self._pending, []
        deadline = time.monotonic() + PENDING_SEND_WAIT_SECONDS
        for thread in pending:
            thread.join(max(0.0, deadline - time.monotonic()))

    def _send_payload(self, payload: dict[str, object]) -> None:
        try:
            body = json.dumps(payload, default=str).encode("utf-8")
        except Exception:
            return
        for attempt in range(MAX_SEND_ATTEMPTS):
            try:
                status = http_post_json(
                    self.endpoint,
                    body,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                    headers={"Authorization": f"Bearer {self.token}"},
                )
            except HttpError:
                continue
            except Exception:
                return
            # Only a server error is worth another try.
            if status < 500:
                return


def build_device_os_version(os_name: str | None, os_release: str | None, arch: str | None) -> str | None:
    if not os_name or not os_release or not arch:
        return None
    return f"{os_name} {os_release} ({arch})"


def detect_host_os() -> str:
    if sys_platform_is_macos():
        return "macOS"
    if sys_platform_is_linux():
        return detect_linux_id() or "Linux"
    return platform.system() or "unknown"


def detect_host_os_version() -> str:
    if sys_platform_is_macos():
        version = run_text_command(["sw_vers", "-productVersion"])
        if version:
            return version
        return platform.mac_ver()[0] or "unknown"
    if sys_platform_is_linux():
        return detect_linux_version_id() or platform.release() or "unknown"
    return platform.release() or "unknown"


def sys_platform_is_macos() -> bool:
    return platform.system() == "Darwin"


def sys_platform_is_linux() -> bool:
    return platform.system() == "Linux"


def run_text_command(command: list[str]) -> str | None:
    try:
        proc = run_process(command, capture_output=True, text=True, check=False)
    except OSError:
        return None
    value = proc.stdout.strip()
    return value or None


def infer_operation_phase(event: str) -> tuple[str | None, str | None]:
    if event.endswith("_started"):
        return event.removesuffix("_started").replace("_", "-"), "started"
    if event.endswith("_finished"):
        return event.removesuffix("_finished").replace("_", "-"), "finished"
    return None, None


def parse_os_release() -> dict[str, str]:
    path = Path("/etc/os-release")
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"').strip("'")
    return values


def detect_linux_id() -> str | None:
    values = parse_os_release()
    return values.get("ID") or None


def detect_linux_version_id() -> str | None:
    values = parse_os_release()
    return values.get("VERSION_ID") or None
