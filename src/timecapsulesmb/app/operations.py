from __future__ import annotations

import argparse
import shlex
import sys
import tempfile
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

from timecapsulesmb.app.events import EventSink
from timecapsulesmb.checks.doctor import run_doctor_checks
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.cli import repair_xattrs as repair_xattrs_cli
from timecapsulesmb.cli.deploy import render_flash_runtime_config
from timecapsulesmb.cli.doctor import build_doctor_error
from timecapsulesmb.cli.fsck import (
    FSCK_REBOOT_NO_DOWN_MESSAGE,
    FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS,
    build_remote_fsck_script,
    select_fsck_target,
    _target_from_volume,
)
from timecapsulesmb.cli.runtime import (
    load_env_config,
    load_optional_env_config,
    resolve_env_connection,
    resolve_validated_managed_target,
    ssh_target_link_local_resolution_error,
)
from timecapsulesmb.core.config import (
    DEFAULTS,
    MANAGED_PAYLOAD_DIR_NAME,
    AppConfig,
    airport_family_display_name_from_identity,
    parse_bool,
    parse_env_file,
    write_env_file,
)
from timecapsulesmb.core.errors import system_exit_message
from timecapsulesmb.core.messages import NETBSD4_REBOOT_FOLLOWUP
from timecapsulesmb.core.net import extract_host
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.deploy.artifact_resolver import resolve_payload_artifacts
from timecapsulesmb.deploy.artifacts import validate_artifacts
from timecapsulesmb.deploy.auth import render_smbpasswd
from timecapsulesmb.deploy.boot_assets import boot_asset_path
from timecapsulesmb.deploy.dry_run import deployment_plan_to_jsonable, uninstall_plan_to_jsonable
from timecapsulesmb.deploy.executor import (
    flush_remote_filesystem_writes,
    remote_request_reboot,
    remote_request_shutdown_reboot,
    remote_uninstall_payload,
    run_remote_actions,
    upload_deployment_payload,
)
from timecapsulesmb.deploy.planner import (
    BINARY_MDNS_SOURCE,
    BINARY_NBNS_SOURCE,
    BINARY_SMBD_SOURCE,
    DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
    GENERATED_FLASH_CONFIG_SOURCE,
    GENERATED_SMBPASSWD_SOURCE,
    GENERATED_USERNAME_MAP_SOURCE,
    PACKAGED_COMMON_SH_SOURCE,
    PACKAGED_DFREE_SH_SOURCE,
    PACKAGED_RC_LOCAL_SOURCE,
    PACKAGED_START_SAMBA_SOURCE,
    PACKAGED_WATCHDOG_SOURCE,
    build_deployment_plan,
    build_netbsd4_activation_plan,
    build_uninstall_plan,
)
from timecapsulesmb.deploy.verify import (
    managed_runtime_ready,
    render_managed_runtime_verification,
    render_post_uninstall_verification,
    verify_managed_runtime,
    verify_post_uninstall,
)
from timecapsulesmb.device.compat import (
    is_netbsd4_payload_family,
    payload_family_description,
    render_compatibility_message,
    require_compatibility,
)
from timecapsulesmb.device.probe import (
    probe_connection_state,
    probe_managed_runtime_conn,
    wait_for_ssh_state_conn,
)
from timecapsulesmb.device.storage import (
    MAST_DISCOVERY_ATTEMPTS,
    MAST_DISCOVERY_DELAY_SECONDS,
    UNINSTALL_DRY_RUN_VOLUME_ROOT_PLACEHOLDER,
    build_dry_run_payload_home,
    mounted_mast_volumes_conn,
    read_mast_volumes_conn,
    select_payload_home_with_diagnostics_conn,
    verify_payload_home_conn,
    wait_for_mast_volumes_conn,
)
from timecapsulesmb.discovery.bonjour import (
    DEFAULT_BROWSE_TIMEOUT_SEC,
    BonjourDiscoverySnapshot,
    BonjourResolvedService,
    discover_snapshot,
    discovered_record_root_host,
    discovery_record_to_jsonable,
    service_instance_to_jsonable,
)
from timecapsulesmb.install_validation import (
    install_checks_to_jsonable,
    install_ok,
    paths_to_jsonable,
    validate_install,
)
from timecapsulesmb.integrations.acp import ACPAuthError, ACPError, enable_ssh, reboot as acp_reboot
from timecapsulesmb.services.app import (
    AppOperationError,
    OperationResult,
    bool_param as _bool_param,
    config_path as _config_path,
    confirm_param as _confirm_param,
    int_param as _int_param,
    jsonable as _jsonable,
    require_string_param as _require_string_param,
    string_param as _string_param,
)
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, SshError, run_ssh


REBOOT_UP_TIMEOUT_MESSAGE = "Timed out waiting for SSH after reboot."
DEPLOY_REBOOT_NO_DOWN_MESSAGE = (
    "Reboot was requested but the device did not go down.\n"
    "The deploy stopped the managed runtime before reboot; power-cycle or rerun deploy."
)
UNINSTALL_REBOOT_NO_DOWN_MESSAGE = (
    "Reboot was requested but the device did not go down.\n"
    "The uninstall removed managed TimeCapsuleSMB files before reboot; power-cycle or rerun uninstall."
)
ACP_REBOOT_REQUEST_TIMEOUT_SECONDS = 10


def _selected_record_properties(params: dict[str, object]) -> dict[str, str]:
    selected = params.get("selected_record")
    if not isinstance(selected, dict):
        return {}
    properties = selected.get("properties")
    if not isinstance(properties, dict):
        return {}
    return {str(key): str(value) for key, value in properties.items()}


def _selected_record_host(params: dict[str, object]) -> str:
    selected = params.get("selected_record")
    if not isinstance(selected, dict):
        return ""
    record = BonjourResolvedService(
        name=str(selected.get("name") or ""),
        hostname=str(selected.get("hostname") or ""),
        service_type=str(selected.get("service_type") or ""),
        port=int(selected.get("port") or 0),
        ipv4=tuple(str(ip) for ip in selected.get("ipv4", ()) if ip),
        ipv6=tuple(str(ip) for ip in selected.get("ipv6", ()) if ip),
        properties=_selected_record_properties(params),
        fullname=str(selected.get("fullname") or ""),
    )
    return discovered_record_root_host(record) or ""


def _snapshot_payload(snapshot: BonjourDiscoverySnapshot) -> dict[str, object]:
    return {
        "instances": [service_instance_to_jsonable(instance) for instance in snapshot.instances],
        "resolved": [discovery_record_to_jsonable(record) for record in snapshot.resolved],
    }


def discover_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "discover"
    timeout = float(params.get("timeout", DEFAULT_BROWSE_TIMEOUT_SEC))
    sink.stage(operation, "bonjour_discovery")
    snapshot = discover_snapshot(timeout=timeout)
    return OperationResult(True, _snapshot_payload(snapshot))


def paths_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "paths"
    sink.stage(operation, "resolve_paths")
    app_paths = resolve_app_paths(config_path=_config_path(params))
    sink.stage(operation, "summarize_artifacts")
    return OperationResult(True, paths_to_jsonable(app_paths))


def validate_install_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "validate-install"
    sink.stage(operation, "resolve_paths")
    app_paths = resolve_app_paths(config_path=_config_path(params))
    sink.stage(operation, "validate_install")
    checks = validate_install(app_paths)
    ok = install_ok(checks)
    for check in checks:
        sink.check(
            operation,
            status="PASS" if check.ok else "FAIL",
            message=check.message,
            details=check.details,
        )
    return OperationResult(ok, {"ok": ok, "checks": install_checks_to_jsonable(checks)})


def configure_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "configure"
    sink.stage(operation, "load_existing_config")
    app_paths = resolve_app_paths(config_path=_config_path(params))
    env_path = app_paths.config_path
    existing = parse_env_file(env_path)
    configure_id = str(uuid.uuid4())
    ssh_opts = _string_param(params, "ssh_opts", existing.get("TC_SSH_OPTS", DEFAULTS["TC_SSH_OPTS"]))
    host = _string_param(params, "host") or _selected_record_host(params) or existing.get("TC_HOST", "")
    password = _require_string_param(params, "password")
    if not host:
        raise AppOperationError("missing required parameter: host", code="validation_failed")

    resolution_error = ssh_target_link_local_resolution_error(host, ssh_opts)
    if resolution_error is not None:
        raise AppOperationError(resolution_error, code="config_error")

    values = {
        "TC_HOST": host,
        "TC_PASSWORD": password,
        "TC_SSH_OPTS": ssh_opts,
        "TC_INTERNAL_SHARE_USE_DISK_ROOT": "true" if _bool_param(
            params,
            "internal_share_use_disk_root",
            parse_bool(existing.get("TC_INTERNAL_SHARE_USE_DISK_ROOT", DEFAULTS["TC_INTERNAL_SHARE_USE_DISK_ROOT"])),
        ) else "false",
        "TC_ANY_PROTOCOL": "true" if _bool_param(
            params,
            "any_protocol",
            parse_bool(existing.get("TC_ANY_PROTOCOL", DEFAULTS["TC_ANY_PROTOCOL"])),
        ) else "false",
        "TC_CONFIGURE_ID": configure_id,
    }

    sink.stage(operation, "ssh_probe")
    connection = SshConnection(host, password, ssh_opts)
    probed_state = probe_connection_state(connection)
    probe = probed_state.probe_result

    if not probe.ssh_port_reachable:
        if not _bool_param(params, "enable_ssh", True):
            raise AppOperationError("SSH is not reachable and enable_ssh is false.", code="remote_error")
        sink.stage(operation, "acp_enable_ssh")
        try:
            enable_ssh(extract_host(host), password, reboot_device=True, log=lambda message: sink.log(operation, message))
        except ACPAuthError as exc:
            raise AppOperationError("The AirPort admin password did not work.", code="auth_failed", debug=str(exc)) from exc
        except ACPError as exc:
            raise AppOperationError(f"Failed to enable SSH via ACP: {exc}", code="remote_error") from exc

        sink.stage(operation, "wait_for_ssh_after_acp")
        if not _wait_for_ssh_port(host, timeout_seconds=_int_param(params, "ssh_wait_timeout", 180)):
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

    selected_props = _selected_record_properties(params)
    observed_syap = None if compatibility is None else compatibility.exact_syap
    observed_model = None if compatibility is None else compatibility.exact_model
    if observed_syap is None:
        observed_syap = selected_props.get("syAP") or None

    sink.stage(operation, "write_env")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    write_env_file(env_path, values)
    return OperationResult(True, {
        "config_path": str(env_path),
        "host": host,
        "configure_id": configure_id,
        "ssh_authenticated": True,
        "device_syap": observed_syap,
        "device_model": observed_model,
        "compatibility": _jsonable(compatibility) if compatibility is not None else None,
    })


def _wait_for_ssh_port(host: str, *, timeout_seconds: int) -> bool:
    from timecapsulesmb.cli.flows import wait_for_tcp_port_state

    return wait_for_tcp_port_state(
        extract_host(host),
        22,
        expected_state=True,
        timeout_seconds=timeout_seconds,
        verbose=False,
        service_name="SSH port",
    )


def _require_supported_payload(target, *, allow_unsupported: bool) -> object:
    probe_state = target.probe_state
    if probe_state is None:
        raise AppOperationError("Failed to determine remote device OS compatibility.", code="remote_error")
    compatibility = require_compatibility(
        probe_state.compatibility,
        fallback_error=probe_state.probe_result.error or "Failed to determine remote device OS compatibility.",
    )
    if not compatibility.supported and not allow_unsupported:
        raise AppOperationError(render_compatibility_message(compatibility), code="unsupported_device")
    if not compatibility.payload_family:
        raise AppOperationError("No deployable payload is available for this detected device.", code="unsupported_device")
    return compatibility


def _load_config_and_target(
    operation: str,
    params: dict[str, object],
    sink: EventSink,
    *,
    profile: str,
    include_probe: bool,
) -> tuple[AppConfig, object]:
    sink.stage(operation, "load_config")
    config = load_env_config(env_path=_config_path(params))
    sink.stage(operation, "resolve_managed_target")
    target = resolve_validated_managed_target(
        config,
        command_name=operation,
        profile=profile,
        include_probe=include_probe,
    )
    return config, target


def deploy_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "deploy"
    nbns_enabled = _bool_param(params, "nbns_enabled", True)
    dry_run = _bool_param(params, "dry_run")
    no_reboot = _bool_param(params, "no_reboot")
    confirm_deploy = _confirm_param(params, "confirm_deploy")
    confirm_reboot = _confirm_param(params, "confirm_reboot")
    confirm_netbsd4_activation = _confirm_param(params, "confirm_netbsd4_activation")
    mount_wait = _int_param(params, "mount_wait", DEFAULT_APPLE_MOUNT_WAIT_SECONDS)
    allow_unsupported = _bool_param(params, "allow_unsupported")
    debug_logging = _bool_param(params, "debug_logging")

    if not dry_run and not confirm_deploy:
        raise AppOperationError("Deploy requires explicit confirmation.", code="confirmation_required")

    config, target = _load_config_and_target(operation, params, sink, profile="deploy", include_probe=True)
    connection = target.connection
    app_paths = resolve_app_paths(config_path=_config_path(params))

    sink.stage(operation, "validate_artifacts")
    failures = [message for _, ok, message in validate_artifacts(app_paths.distribution_root) if not ok]
    if failures:
        raise AppOperationError("; ".join(failures), code="validation_failed")

    sink.stage(operation, "check_compatibility")
    compatibility = _require_supported_payload(target, allow_unsupported=allow_unsupported)
    payload_family = compatibility.payload_family
    is_netbsd4 = is_netbsd4_payload_family(payload_family)
    sink.log(operation, f"Using {payload_family_description(payload_family)} payload.")
    resolved_artifacts = resolve_payload_artifacts(app_paths.distribution_root, payload_family)
    if not dry_run:
        if is_netbsd4 and not confirm_netbsd4_activation:
            raise AppOperationError(
                "NetBSD 4 deploy requires explicit activation confirmation.",
                code="confirmation_required",
            )
        if not is_netbsd4 and not no_reboot and not confirm_reboot:
            device_name = airport_family_display_name_from_identity(
                model=target.probe_state.probe_result.airport_model if target.probe_state else None,
                syap=target.probe_state.probe_result.airport_syap if target.probe_state else None,
            )
            raise AppOperationError(
                f"Deploy requires confirmation to reboot the {device_name}.",
                code="confirmation_required",
            )

    if dry_run:
        payload_home = build_dry_run_payload_home(MANAGED_PAYLOAD_DIR_NAME)
    else:
        sink.stage(operation, "read_mast")
        mast_discovery = wait_for_mast_volumes_conn(
            connection,
            attempts=MAST_DISCOVERY_ATTEMPTS,
            delay_seconds=MAST_DISCOVERY_DELAY_SECONDS,
        )
        if not mast_discovery.volumes:
            raise AppOperationError(
                f"No deployable HFS disk was found after {MAST_DISCOVERY_ATTEMPTS} MaSt queries "
                f"spaced {MAST_DISCOVERY_DELAY_SECONDS} seconds apart.",
                code="remote_error",
            )
        sink.stage(operation, "select_payload_home")
        selection = select_payload_home_with_diagnostics_conn(
            connection,
            mast_discovery.volumes,
            MANAGED_PAYLOAD_DIR_NAME,
            wait_seconds=mount_wait,
        )
        if selection.payload_home is None:
            raise AppOperationError(
                f"MaSt found {len(mast_discovery.volumes)} deployable HFS volume(s), but deploy could not write to any of them.",
                code="remote_error",
            )
        payload_home = selection.payload_home

    sink.stage(operation, "build_deployment_plan")
    plan = build_deployment_plan(
        connection.host,
        payload_home,
        resolved_artifacts["smbd"].absolute_path,
        resolved_artifacts["mdns-advertiser"].absolute_path,
        resolved_artifacts["nbns-advertiser"].absolute_path,
        activate_netbsd4=is_netbsd4,
        reboot_after_deploy=not no_reboot,
        apple_mount_wait_seconds=mount_wait,
    )
    if dry_run:
        return OperationResult(True, deployment_plan_to_jsonable(plan))

    sink.stage(operation, "pre_upload_actions")
    run_remote_actions(connection, plan.pre_upload_actions)
    sink.stage(operation, "prepare_deployment_files")
    flash_config_text = render_flash_runtime_config(
        config,
        payload_home,
        nbns_enabled=nbns_enabled,
        debug_logging=debug_logging,
    )
    with tempfile.TemporaryDirectory(prefix="tc-deploy-") as tmp, ExitStack() as boot_assets:
        tmpdir = Path(tmp)
        generated_flash_config = tmpdir / "tcapsulesmb.conf"
        generated_smbpasswd = tmpdir / "smbpasswd"
        generated_username_map = tmpdir / "username.map"
        generated_flash_config.write_text(flash_config_text)
        smbpasswd_text, username_map_text = render_smbpasswd(connection.password)
        generated_smbpasswd.write_text(smbpasswd_text)
        generated_username_map.write_text(username_map_text)
        upload_sources = {
            BINARY_SMBD_SOURCE: plan.smbd_path,
            BINARY_MDNS_SOURCE: plan.mdns_path,
            BINARY_NBNS_SOURCE: plan.nbns_path,
            GENERATED_SMBPASSWD_SOURCE: generated_smbpasswd,
            GENERATED_USERNAME_MAP_SOURCE: generated_username_map,
            GENERATED_FLASH_CONFIG_SOURCE: generated_flash_config,
            PACKAGED_RC_LOCAL_SOURCE: boot_assets.enter_context(boot_asset_path("rc.local")),
            PACKAGED_COMMON_SH_SOURCE: boot_assets.enter_context(boot_asset_path("common.sh")),
            PACKAGED_DFREE_SH_SOURCE: boot_assets.enter_context(boot_asset_path("dfree.sh")),
            PACKAGED_START_SAMBA_SOURCE: boot_assets.enter_context(boot_asset_path("start-samba.sh")),
            PACKAGED_WATCHDOG_SOURCE: boot_assets.enter_context(boot_asset_path("watchdog.sh")),
        }
        sink.stage(operation, "upload_payload")
        upload_deployment_payload(plan, connection=connection, source_resolver=upload_sources)

    sink.stage(operation, "post_upload_actions")
    run_remote_actions(connection, plan.post_upload_actions)
    _verify_payload_upload(operation, sink, connection, payload_home, wait_seconds=mount_wait)
    sink.stage(operation, "flush_payload_upload")
    sink.log(operation, "Flushing deployed payload to disk...")
    flush_remote_filesystem_writes(connection)
    _verify_payload_upload(operation, sink, connection, payload_home, wait_seconds=mount_wait, post_sync=True)

    if is_netbsd4:
        sink.stage(operation, "netbsd4_activation")
        run_remote_actions(connection, plan.activation_actions)
        _verify_runtime(operation, sink, connection, stage="verify_runtime_activation", timeout_seconds=180)
        return OperationResult(True, {
            "payload_dir": plan.payload_dir,
            "netbsd4": True,
            "message": f"NetBSD4 activation complete. {NETBSD4_REBOOT_FOLLOWUP}",
        })

    if no_reboot:
        return OperationResult(True, {"payload_dir": plan.payload_dir, "rebooted": False})

    _request_reboot_and_wait(
        operation,
        sink,
        connection,
        strategy="ssh_shutdown_then_reboot",
        reboot_no_down_message=DEPLOY_REBOOT_NO_DOWN_MESSAGE,
    )
    _verify_runtime(operation, sink, connection, stage="verify_runtime_reboot", timeout_seconds=240)
    return OperationResult(True, {"payload_dir": plan.payload_dir, "rebooted": True})


def _verify_payload_upload(
    operation: str,
    sink: EventSink,
    connection: SshConnection,
    payload_home,
    *,
    wait_seconds: int,
    post_sync: bool = False,
) -> None:
    sink.stage(operation, "verify_payload_upload_after_sync" if post_sync else "verify_payload_upload")
    verification = verify_payload_home_conn(connection, payload_home, wait_seconds=wait_seconds)
    sink.log(operation, verification.detail)
    if not verification.ok:
        raise AppOperationError(
            f"managed payload verification failed at {payload_home.payload_dir}: {verification.detail}",
            code="remote_error",
        )


def _verify_runtime(
    operation: str,
    sink: EventSink,
    connection: SshConnection,
    *,
    stage: str,
    timeout_seconds: int,
) -> None:
    sink.stage(operation, stage)
    verification = verify_managed_runtime(connection, timeout_seconds=timeout_seconds)
    for line in render_managed_runtime_verification(
        verification,
        heading="Waiting for managed runtime to finish starting...",
    ):
        sink.log(operation, line)
    if not managed_runtime_ready(verification):
        raise AppOperationError(
            f"Managed runtime did not become ready. {verification.detail.strip()}".strip(),
            code="remote_error",
        )


def _request_reboot_and_wait(
    operation: str,
    sink: EventSink,
    connection: SshConnection,
    *,
    strategy: str,
    reboot_no_down_message: str,
    down_timeout_seconds: int = 60,
    up_timeout_seconds: int = 240,
) -> None:
    sink.stage(operation, "reboot")
    if strategy == "acp_then_ssh":
        try:
            acp_reboot(extract_host(connection.host), connection.password, timeout=ACP_REBOOT_REQUEST_TIMEOUT_SECONDS)
            sink.log(operation, "ACP reboot requested.")
        except ACPError as exc:
            sink.log(operation, f"ACP reboot request failed; trying SSH reboot request: {exc}", level="warning")
            _request_ssh_reboot(operation, sink, connection, shutdown=False)
    else:
        _request_ssh_reboot(operation, sink, connection, shutdown=True)

    sink.stage(operation, "wait_for_reboot_down")
    sink.log(operation, "Waiting for the device to go down...")
    if not wait_for_ssh_state_conn(connection, expected_up=False, timeout_seconds=down_timeout_seconds):
        raise AppOperationError(reboot_no_down_message, code="remote_error")
    sink.stage(operation, "wait_for_reboot_up")
    sink.log(operation, "Waiting for the device to come back up...")
    if not wait_for_ssh_state_conn(connection, expected_up=True, timeout_seconds=up_timeout_seconds):
        raise AppOperationError(REBOOT_UP_TIMEOUT_MESSAGE, code="remote_error")
    sink.log(operation, "Device is back online.")


def _request_ssh_reboot(operation: str, sink: EventSink, connection: SshConnection, *, shutdown: bool) -> None:
    try:
        if shutdown:
            remote_request_shutdown_reboot(connection)
        else:
            remote_request_reboot(connection)
    except SshCommandTimeout as exc:
        sink.log(operation, f"SSH reboot request timed out; checking whether the device is rebooting: {exc}", level="warning")
        return
    except SshError as exc:
        sink.log(operation, f"SSH reboot request failed; checking whether the device is rebooting anyway: {exc}", level="warning")
        return
    sink.log(operation, "SSH reboot requested.")


def activate_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "activate"
    confirm_activation = _confirm_param(params, "confirm_netbsd4_activation")
    dry_run = _bool_param(params, "dry_run")
    _, target = _load_config_and_target(operation, params, sink, profile="activate", include_probe=True)
    compatibility = _require_supported_payload(target, allow_unsupported=False)
    if not is_netbsd4_payload_family(compatibility.payload_family):
        raise AppOperationError(
            "activate is only supported for NetBSD4 AirPort storage devices; use deploy for persistent NetBSD6 installs.",
            code="unsupported_device",
        )
    sink.stage(operation, "build_activation_plan")
    plan = build_netbsd4_activation_plan()
    if dry_run:
        return OperationResult(True, _jsonable(plan))
    if not confirm_activation:
        raise AppOperationError("NetBSD4 activation requires explicit confirmation.", code="confirmation_required")
    connection = target.connection
    sink.stage(operation, "probe_runtime")
    if probe_managed_runtime_conn(connection, timeout_seconds=20).ready:
        return OperationResult(True, {"already_active": True})
    sink.stage(operation, "run_activation")
    run_remote_actions(connection, plan.actions)
    _verify_runtime(operation, sink, connection, stage="verify_runtime_activation", timeout_seconds=180)
    return OperationResult(True, {"already_active": False, "message": f"NetBSD4 activation complete. {NETBSD4_REBOOT_FOLLOWUP}"})


def uninstall_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "uninstall"
    dry_run = _bool_param(params, "dry_run")
    no_reboot = _bool_param(params, "no_reboot")
    confirm_uninstall = _confirm_param(params, "confirm_uninstall")
    confirm_reboot = _confirm_param(params, "confirm_reboot")
    if not dry_run and not confirm_uninstall:
        raise AppOperationError("Uninstall requires explicit confirmation.", code="confirmation_required")
    if not dry_run and not no_reboot and not confirm_reboot:
        raise AppOperationError("Uninstall requires confirmation to reboot the device.", code="confirmation_required")
    sink.stage(operation, "load_config")
    config = load_env_config(env_path=_config_path(params))
    sink.stage(operation, "resolve_connection")
    connection = resolve_env_connection(config, allow_empty_password=True)
    if dry_run:
        volume_roots = [UNINSTALL_DRY_RUN_VOLUME_ROOT_PLACEHOLDER]
        payload_dirs = [f"{UNINSTALL_DRY_RUN_VOLUME_ROOT_PLACEHOLDER}/{MANAGED_PAYLOAD_DIR_NAME}"]
    else:
        sink.stage(operation, "read_mast")
        mast_volumes = read_mast_volumes_conn(connection)
        sink.stage(operation, "mount_mast_volumes")
        mounted_volumes = mounted_mast_volumes_conn(
            connection,
            mast_volumes,
            wait_seconds=DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
        )
        volume_roots = [volume.volume_root for volume in mounted_volumes]
        payload_dirs = [f"{volume_root}/{MANAGED_PAYLOAD_DIR_NAME}" for volume_root in volume_roots]
    sink.stage(operation, "build_uninstall_plan")
    plan = build_uninstall_plan(connection.host, volume_roots, payload_dirs, reboot_after_uninstall=not no_reboot)
    if dry_run:
        return OperationResult(True, uninstall_plan_to_jsonable(plan))
    sink.stage(operation, "uninstall_payload")
    remote_uninstall_payload(connection, plan)
    if no_reboot:
        return OperationResult(True, {"rebooted": False, "verified": False})
    _request_reboot_and_wait(
        operation,
        sink,
        connection,
        strategy="acp_then_ssh",
        reboot_no_down_message=UNINSTALL_REBOOT_NO_DOWN_MESSAGE,
    )
    sink.stage(operation, "verify_post_uninstall")
    verification = verify_post_uninstall(connection, plan)
    for line in render_post_uninstall_verification(verification):
        sink.log(operation, line)
    if not verification:
        raise AppOperationError("Managed TimeCapsuleSMB files are still present after reboot.", code="remote_error")
    return OperationResult(True, {"rebooted": True, "verified": True})


def fsck_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "fsck"
    confirm_fsck = _confirm_param(params, "confirm_fsck")
    no_reboot = _bool_param(params, "no_reboot")
    no_wait = _bool_param(params, "no_wait")
    if not confirm_fsck:
        raise AppOperationError("fsck requires explicit confirmation.", code="confirmation_required")
    sink.stage(operation, "load_config")
    config = load_env_config(env_path=_config_path(params))
    sink.stage(operation, "resolve_connection")
    connection = resolve_env_connection(config, allow_empty_password=True)
    sink.stage(operation, "read_mast")
    mast_volumes = read_mast_volumes_conn(connection)
    sink.stage(operation, "mount_hfs_volumes")
    mounted_volumes = mounted_mast_volumes_conn(
        connection,
        mast_volumes,
        wait_seconds=DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
    )
    sink.stage(operation, "select_fsck_volume")
    try:
        target = select_fsck_target(
            tuple(_target_from_volume(volume) for volume in mounted_volumes),
            _string_param(params, "volume") or None,
            prompt=False,
        )
    except RuntimeError as exc:
        raise AppOperationError(str(exc), code="validation_failed") from exc
    sink.stage(operation, "run_fsck")
    script = build_remote_fsck_script(target.device, target.mountpoint, reboot=not no_reboot)
    proc = run_ssh(
        connection,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
        timeout=FSCK_REMOTE_COMMAND_TIMEOUT_SECONDS,
    )
    if proc.stdout:
        for line in proc.stdout.splitlines():
            sink.log(operation, line)
    if no_reboot:
        return OperationResult(proc.returncode == 0, {
            "device": target.device,
            "mountpoint": target.mountpoint,
            "returncode": proc.returncode,
        })
    if no_wait:
        return OperationResult(True, {"device": target.device, "mountpoint": target.mountpoint, "waited": False})
    _observe_reboot_cycle(
        operation,
        sink,
        connection,
        reboot_no_down_message=FSCK_REBOOT_NO_DOWN_MESSAGE,
        down_timeout_seconds=90,
        up_timeout_seconds=420,
    )
    return OperationResult(True, {"device": target.device, "mountpoint": target.mountpoint, "waited": True})


def _observe_reboot_cycle(
    operation: str,
    sink: EventSink,
    connection: SshConnection,
    *,
    reboot_no_down_message: str,
    down_timeout_seconds: int,
    up_timeout_seconds: int,
) -> None:
    sink.stage(operation, "wait_for_reboot_down")
    if not wait_for_ssh_state_conn(connection, expected_up=False, timeout_seconds=down_timeout_seconds):
        raise AppOperationError(reboot_no_down_message, code="remote_error")
    sink.stage(operation, "wait_for_reboot_up")
    if not wait_for_ssh_state_conn(connection, expected_up=True, timeout_seconds=up_timeout_seconds):
        raise AppOperationError(REBOOT_UP_TIMEOUT_MESSAGE, code="remote_error")


class _RepairContext:
    def __init__(self, operation: str, sink: EventSink) -> None:
        self.operation = operation
        self.sink = sink
        self.result = "failure"
        self.error: str | None = None

    def set_stage(self, stage: str) -> None:
        self.sink.stage(self.operation, stage)

    def update_fields(self, **_fields: object) -> None:
        pass

    def succeed(self) -> None:
        self.result = "success"

    def fail_with_error(self, message: str) -> None:
        self.result = "failure"
        self.error = message


def repair_xattrs_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "repair-xattrs"
    dry_run = _bool_param(params, "dry_run")
    confirm_repair = _confirm_param(params, "confirm_repair")
    if not dry_run and not confirm_repair:
        raise AppOperationError(
            "repair-xattrs requires dry_run or explicit confirmation.",
            code="confirmation_required",
        )
    if sys.platform != "darwin":
        raise AppOperationError(
            "repair-xattrs must be run on macOS because it uses xattr/chflags on the mounted SMB share.",
            code="validation_failed",
        )
    config = load_optional_env_config(env_path=_config_path(params))
    args = argparse.Namespace(
        path=Path(str(params["path"])) if params.get("path") else None,
        dry_run=dry_run,
        yes=confirm_repair,
        recursive=_bool_param(params, "recursive", True),
        max_depth=params.get("max_depth"),
        include_hidden=_bool_param(params, "include_hidden"),
        include_time_machine=_bool_param(params, "include_time_machine"),
        fix_permissions=_bool_param(params, "fix_permissions"),
        verbose=_bool_param(params, "verbose"),
    )
    if args.max_depth is not None:
        args.max_depth = int(args.max_depth)
    context = _RepairContext(operation, sink)
    try:
        result = repair_xattrs_cli.run_repair_structured(
            args,
            context,
            config,
            emit_log=lambda message: sink.log(operation, message),
        )
    except SystemExit as exc:
        message = system_exit_message(exc) or "repair-xattrs failed"
        raise AppOperationError(message, code="operation_failed") from exc
    return OperationResult(result.returncode == 0, {
        "returncode": result.returncode,
        "root": str(result.root),
        "finding_count": len(result.findings),
        "repairable_count": len(result.candidates),
        "summary": _jsonable(result.summary),
        "report": result.report,
        "telemetry_result": context.result,
        "error": context.error,
    })


def doctor_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    operation = "doctor"
    sink.stage(operation, "load_config")
    config = load_env_config(env_path=_config_path(params))
    app_paths = resolve_app_paths(config_path=_config_path(params))
    connection = None
    if not _bool_param(params, "skip_ssh") and config.has_value("TC_HOST"):
        sink.stage(operation, "resolve_connection")
        connection = resolve_env_connection(config, allow_empty_password=True)
    debug_fields: dict[str, object] = {}

    def on_result(result: CheckResult) -> None:
        sink.check(operation, status=result.status, message=result.message, details=result.details)

    sink.stage(operation, "run_checks")
    results, fatal = run_doctor_checks(
        config,
        repo_root=app_paths.distribution_root,
        connection=connection,
        skip_ssh=_bool_param(params, "skip_ssh"),
        skip_bonjour=_bool_param(params, "skip_bonjour"),
        skip_smb=_bool_param(params, "skip_smb"),
        on_result=on_result,
        debug_fields=debug_fields,
    )
    payload = {
        "fatal": fatal,
        "results": [_jsonable(result) for result in results],
        "summary": "doctor found one or more fatal problems." if fatal else "doctor checks passed.",
    }
    if fatal:
        payload["error"] = build_doctor_error(results, debug_fields)
    return OperationResult(not fatal, payload)


OPERATIONS: dict[str, Callable[[dict[str, object], EventSink], OperationResult]] = {
    "activate": activate_operation,
    "configure": configure_operation,
    "deploy": deploy_operation,
    "discover": discover_operation,
    "doctor": doctor_operation,
    "fsck": fsck_operation,
    "paths": paths_operation,
    "repair-xattrs": repair_xattrs_operation,
    "uninstall": uninstall_operation,
    "validate-install": validate_install_operation,
}
