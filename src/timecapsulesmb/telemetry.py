from __future__ import annotations

import json
import os
import platform
import subprocess
import threading
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from timecapsulesmb.cli.util import CLI_VERSION, RELEASE_TAG, SAMBA_VERSION
from timecapsulesmb.identity import BOOTSTRAP_PATH, load_install_identity


SCHEMA_VERSION = 2
DEFAULT_TELEMETRY_URL = "https://timecapsulesmb.jamesyc.com/v1/events"
TELEMETRY_URL_ENV = "TCAPSULE_TELEMETRY_URL"
TELEMETRY_TOKEN_ENV = "TCAPSULE_TELEMETRY_TOKEN"
DEFAULT_TELEMETRY_TOKEN = "d65373762e893ae18c8aaa95a8f1b3a3464611f33b30983909543535fa8b0733"
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_SEND_ATTEMPTS = 2


@dataclass(frozen=True)
class TelemetryContext:
    install_id: str
    cli_version: str
    release_tag: str
    samba_version: str
    host_os: str
    host_os_version: str
    configure_id: str | None = None
    device_model: str | None = None
    device_syap: str | None = None
    nbns_enabled: bool | None = None


class TelemetryClient:
    def __init__(self, *, endpoint: str, token: str | None, context: TelemetryContext | None, enabled: bool) -> None:
        self.endpoint = endpoint
        self.token = token
        self.context = context
        self.enabled = enabled and context is not None and bool(token)

    @classmethod
    def from_values(
        cls,
        values: Optional[dict[str, str]] = None,
        *,
        nbns_enabled: bool | None = None,
        bootstrap_path: Path = BOOTSTRAP_PATH,
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
            configure_id=(values or {}).get("TC_CONFIGURE_ID") or None,
            device_model=(values or {}).get("TC_MDNS_DEVICE_MODEL") or None,
            device_syap=(values or {}).get("TC_AIRPORT_SYAP") or None,
            nbns_enabled=nbns_enabled,
        )
        return cls(endpoint=endpoint, token=token, context=context, enabled=identity.telemetry_enabled)

    def emit(self, event: str, *, synchronous: bool = False, **fields: object) -> None:
        if not self.enabled or self.context is None:
            return
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
        if self.context.configure_id:
            payload["configure_id"] = self.context.configure_id
        if self.context.device_model:
            payload["device_model"] = self.context.device_model
        if self.context.device_syap:
            payload["device_syap"] = self.context.device_syap
        if self.context.nbns_enabled is not None:
            payload["nbns_enabled"] = self.context.nbns_enabled
        for key, value in fields.items():
            if value is not None:
                payload[key] = value
        if synchronous:
            self._send_payload(payload)
            return
        self._dispatch_payload_async(payload)

    def _dispatch_payload_async(self, payload: dict[str, object]) -> None:
        thread = threading.Thread(target=self._send_payload, args=(payload,), daemon=True)
        thread.start()

    def _send_payload(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        for attempt in range(MAX_SEND_ATTEMPTS):
            try:
                request = urllib.request.Request(
                    self.endpoint,
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.token}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS):
                    return
            except urllib.error.HTTPError as exc:
                if exc.code < 500 or attempt + 1 >= MAX_SEND_ATTEMPTS:
                    return
            except (OSError, urllib.error.URLError, ValueError):
                if attempt + 1 >= MAX_SEND_ATTEMPTS:
                    return


def build_device_os_version(os_name: str | None, os_release: str | None, arch: str | None) -> str | None:
    if not os_name or not os_release or not arch:
        return None
    return f"{os_name} {os_release} ({arch})"


def detect_device_family(payload_family: str | None) -> str | None:
    if payload_family in {"netbsd4le_samba4", "netbsd4be_samba4"}:
        return "netbsd4"
    if payload_family == "netbsd6_samba4":
        return "netbsd6"
    return None


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
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError:
        return None
    value = proc.stdout.strip()
    return value or None


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
