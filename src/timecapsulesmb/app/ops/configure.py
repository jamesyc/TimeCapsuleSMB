from __future__ import annotations

import uuid

from timecapsulesmb.app.contracts import configure_payload
from timecapsulesmb.app.events import EventSink
from timecapsulesmb.app.ops.readiness import selected_record_host, selected_record_properties
from timecapsulesmb.core.config import (
    DEFAULTS,
    parse_bool,
    parse_env_file,
    write_env_file,
)
from timecapsulesmb.core.net import extract_host
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.device.compat import render_compatibility_message
from timecapsulesmb.device.probe import probe_connection_state
from timecapsulesmb.integrations.acp import ACPAuthError, ACPError, enable_ssh
from timecapsulesmb.services.app import (
    AppOperationError,
    OperationResult,
    bool_param,
    config_path,
    int_param,
    jsonable,
    require_string_param,
    string_param,
)
from timecapsulesmb.services.configure import build_configure_env_values
from timecapsulesmb.transport.ssh import SshConnection

from timecapsulesmb.cli.runtime import ssh_target_link_local_resolution_error


def configure_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "configure"
    sink.stage(operation, "load_existing_config")
    app_paths = resolve_app_paths(config_path=config_path(params))
    env_path = app_paths.config_path
    existing = parse_env_file(env_path)
    configure_id = str(uuid.uuid4())
    ssh_opts = string_param(params, "ssh_opts", existing.get("TC_SSH_OPTS", DEFAULTS["TC_SSH_OPTS"]))
    host = string_param(params, "host") or selected_record_host(params) or existing.get("TC_HOST", "")
    password = require_string_param(params, "password")
    if not host:
        raise AppOperationError("missing required parameter: host", code="validation_failed")

    resolution_error = ssh_target_link_local_resolution_error(host, ssh_opts)
    if resolution_error is not None:
        raise AppOperationError(resolution_error, code="config_error")

    values = build_configure_env_values(
        existing,
        host=host,
        password=password,
        ssh_opts=ssh_opts,
        configure_id=configure_id,
        internal_share_use_disk_root=bool_param(
            params,
            "internal_share_use_disk_root",
            parse_bool(existing.get("TC_INTERNAL_SHARE_USE_DISK_ROOT", DEFAULTS["TC_INTERNAL_SHARE_USE_DISK_ROOT"])),
        ),
        any_protocol=bool_param(
            params,
            "any_protocol",
            parse_bool(existing.get("TC_ANY_PROTOCOL", DEFAULTS["TC_ANY_PROTOCOL"])),
        ),
        debug_logging=bool_param(
            params,
            "debug_logging",
            parse_bool(existing.get("TC_DEBUG_LOGGING", DEFAULTS["TC_DEBUG_LOGGING"])),
        ),
    )

    sink.stage(operation, "ssh_probe")
    connection = SshConnection(host, password, ssh_opts)
    probed_state = probe_connection_state(connection)
    probe = probed_state.probe_result

    if not probe.ssh_port_reachable:
        if not bool_param(params, "enable_ssh", True):
            raise AppOperationError("SSH is not reachable and enable_ssh is false.", code="remote_error")
        sink.stage(operation, "acp_enable_ssh")
        try:
            enable_ssh(extract_host(host), password, reboot_device=True, log=lambda message: sink.log(operation, message))
        except ACPAuthError as exc:
            raise AppOperationError("The AirPort admin password did not work.", code="auth_failed", debug=str(exc)) from exc
        except ACPError as exc:
            raise AppOperationError(f"Failed to enable SSH via ACP: {exc}", code="remote_error") from exc

        sink.stage(operation, "wait_for_ssh_after_acp")
        if not wait_for_ssh_port(host, timeout_seconds=int_param(params, "ssh_wait_timeout", 180)):
            raise AppOperationError("SSH did not open after enabling via ACP.", code="remote_error")
        sink.stage(operation, "ssh_probe_after_acp")
        probed_state = probe_connection_state(connection)
        probe = probed_state.probe_result

    if not probe.ssh_authenticated:
        raise AppOperationError(
            probe.error or "The provided AirPort SSH target and password did not work.",
            code="auth_failed",
        )

    compatibility = probed_state.compatibility
    if compatibility is not None and not compatibility.supported:
        raise AppOperationError(render_compatibility_message(compatibility), code="unsupported_device")

    selected_props = selected_record_properties(params)
    observed_syap = None if compatibility is None else compatibility.exact_syap
    observed_model = None if compatibility is None else compatibility.exact_model
    if observed_syap is None:
        observed_syap = selected_props.get("syAP") or None

    sink.stage(operation, "write_env")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    write_env_file(env_path, values)
    return OperationResult(True, configure_payload(
        config_path=str(env_path),
        host=host,
        configure_id=configure_id,
        ssh_authenticated=True,
        device_syap=observed_syap,
        device_model=observed_model,
        compatibility=jsonable(compatibility) if compatibility is not None else None,
    ))


def wait_for_ssh_port(host: str, *, timeout_seconds: int) -> bool:
    from timecapsulesmb.cli.flows import wait_for_tcp_port_state

    return wait_for_tcp_port_state(
        extract_host(host),
        22,
        expected_state=True,
        timeout_seconds=timeout_seconds,
        verbose=False,
        service_name="SSH port",
    )
