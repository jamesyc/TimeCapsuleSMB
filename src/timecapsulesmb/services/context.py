from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from timecapsulesmb.telemetry.debug import debug_summary, render_debug_mapping

if TYPE_CHECKING:
    from timecapsulesmb.core.config import AppConfig
    from timecapsulesmb.device.probe import ProbedDeviceState
    from timecapsulesmb.transport.ssh import SshConnection


COMMAND_VALUE_BLACKLIST = {
    "TC_PASSWORD",
    # Removed naming keys may still exist in old .env files. They are
    # intentionally ignored and should not appear as command inputs.
    "TC_SAMBA_USER",
    "TC_PAYLOAD_DIR_NAME",
    "TC_MDNS_HOST_LABEL",
    "TC_MDNS_INSTANCE_NAME",
    "TC_NETBIOS_NAME",
    # These are already first-class operation fields.
    "TC_CONFIGURE_ID",
    "TC_MDNS_DEVICE_MODEL",
    "TC_AIRPORT_SYAP",
}
COMMAND_FIELD_BLACKLIST = {
    # These are already first-class operation fields.
    "configure_id",
    "device_model",
    "device_syap",
    "device_os_version",
    "device_family",
    "nbns_enabled",
    "reboot_was_attempted",
    "device_came_back_after_reboot",
}


def _render_connection_debug_lines(connection: SshConnection | None, values: Mapping[str, str] | None) -> list[str]:
    host = None
    ssh_opts = None
    if connection is not None:
        host = connection.host
        ssh_opts = connection.ssh_opts
    elif values is not None:
        host = values.get("TC_HOST") or None
        ssh_opts = values.get("TC_SSH_OPTS") or None
    lines: list[str] = []
    if host:
        lines.append(f"host={host}")
    if ssh_opts:
        lines.append(f"ssh_opts={ssh_opts}")
    return lines


def render_operation_debug_lines(
    *,
    operation_name: str,
    stage: str | None,
    connection: SshConnection | None,
    values: Mapping[str, str] | None,
    preflight_error: str | None,
    finish_fields: Mapping[str, object],
    probe_state: ProbedDeviceState | None,
    debug_fields: Mapping[str, object],
    config: AppConfig | None = None,
) -> list[str]:
    debug_values = config.values if config is not None else values
    lines = ["Debug context:", f"command={operation_name}"]
    if stage:
        lines.append(f"stage={stage}")
    if config is not None:
        lines.append(f"env_path={config.path}")
    lines.extend(_render_connection_debug_lines(connection, debug_values))
    if debug_values is not None:
        lines.extend(render_debug_mapping(debug_values, blacklist=COMMAND_VALUE_BLACKLIST))
    if preflight_error:
        lines.append(f"preflight_error={preflight_error}")
    lines.extend(render_debug_mapping(finish_fields, blacklist=COMMAND_FIELD_BLACKLIST))
    if probe_state is not None:
        lines.extend(render_debug_mapping(debug_summary(probe_state), blacklist=COMMAND_FIELD_BLACKLIST))
    lines.extend(render_debug_mapping(debug_fields, blacklist=COMMAND_FIELD_BLACKLIST))
    return lines


class OperationContext:
    """Shared operation diagnostics used by CLI and app/API entrypoints."""

    def __init__(
        self,
        operation_name: str,
        *,
        values: Mapping[str, str] | None = None,
        config: AppConfig | None = None,
    ) -> None:
        self.operation_name = operation_name
        self.values = values
        self.config = config
        self.finish_fields: dict[str, object] = {}
        self.error_lines: list[str] = []
        self.preflight_error: str | None = None
        self.debug_stage: str | None = None
        self.debug_fields: dict[str, object] = {}
        self.connection: SshConnection | None = None
        self.probe_state: ProbedDeviceState | None = None

    def update_fields(self, **fields: object) -> None:
        for key, value in fields.items():
            if value is not None:
                self.finish_fields[key] = value

    def set_stage(self, stage: str) -> None:
        self.debug_stage = stage

    def add_debug_fields(self, **fields: object) -> None:
        for key, value in fields.items():
            if value is not None:
                self.debug_fields[key] = debug_summary(value)

    def set_error(self, message: str) -> None:
        self.error_lines = [line.rstrip() for line in message.splitlines() if line.strip()]

    def build_error(self) -> str | None:
        if not self.error_lines:
            return None
        return "\n".join([
            *self.error_lines,
            "",
            *render_operation_debug_lines(
                operation_name=self.operation_name,
                stage=self.debug_stage,
                connection=self.connection,
                values=self.values,
                preflight_error=self.preflight_error,
                finish_fields=self.finish_fields,
                probe_state=self.probe_state,
                debug_fields=self.debug_fields,
                config=self.config,
            ),
        ])
