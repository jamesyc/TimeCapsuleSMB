from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import tempfile

from timecapsulesmb.app.contracts import deploy_plan_payload, deploy_result_payload
from timecapsulesmb.app.confirmations import build_confirmation, require_confirmation
from timecapsulesmb.app.events import EventSink
from timecapsulesmb.core.config import MANAGED_PAYLOAD_DIR_NAME, AppConfig, airport_family_display_name_from_identity
from timecapsulesmb.core.messages import NETBSD4_REBOOT_FOLLOWUP
from timecapsulesmb.core.net import extract_host
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.deploy.artifact_resolver import resolve_payload_artifacts
from timecapsulesmb.deploy.artifacts import validate_artifacts
from timecapsulesmb.deploy.auth import render_smbpasswd
from timecapsulesmb.deploy.boot_assets import boot_asset_path
from timecapsulesmb.deploy.dry_run import deployment_plan_to_jsonable
from timecapsulesmb.deploy.executor import (
    flush_remote_filesystem_writes,
    remote_request_reboot,
    remote_request_shutdown_reboot,
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
)
from timecapsulesmb.deploy.verify import (
    managed_runtime_ready,
    render_managed_runtime_verification,
    verify_managed_runtime,
)
from timecapsulesmb.device.compat import (
    is_netbsd4_payload_family,
    payload_family_description,
    render_compatibility_message,
    require_compatibility,
)
from timecapsulesmb.device.probe import wait_for_ssh_state_conn
from timecapsulesmb.device.storage import (
    MAST_DISCOVERY_ATTEMPTS,
    MAST_DISCOVERY_DELAY_SECONDS,
    build_dry_run_payload_home,
    select_payload_home_with_diagnostics_conn,
    verify_payload_home_conn,
    wait_for_mast_volumes_conn,
)
from timecapsulesmb.integrations.acp import ACPError, reboot as acp_reboot
from timecapsulesmb.services.app import (
    AppOperationError,
    OperationResult,
    bool_param,
    config_path,
    int_param,
)
from timecapsulesmb.services.credentials import overlay_request_credentials
from timecapsulesmb.services.deploy import (
    DEPLOY_REBOOT_NO_DOWN_MESSAGE,
    DEPLOY_REBOOT_UP_TIMEOUT_MESSAGE,
    no_mast_volumes_message,
    no_writable_mast_volumes_message,
    payload_verification_error,
    render_flash_runtime_config,
)
from timecapsulesmb.services.runtime import load_env_config, resolve_validated_managed_target
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, SshError


ACP_REBOOT_REQUEST_TIMEOUT_SECONDS = 10


def require_supported_payload(target, *, allow_unsupported: bool) -> object:
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


def load_config_and_target(
    operation: str,
    params: dict[str, object],
    sink: EventSink,
    *,
    profile: str,
    include_probe: bool,
) -> tuple[AppConfig, object]:
    sink.stage(operation, "load_config")
    config = overlay_request_credentials(load_env_config(env_path=config_path(params)), params)
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
    nbns_enabled = bool_param(params, "nbns_enabled", True)
    dry_run = bool_param(params, "dry_run")
    no_reboot = bool_param(params, "no_reboot")
    no_wait = bool_param(params, "no_wait")
    mount_wait = int_param(params, "mount_wait", DEFAULT_APPLE_MOUNT_WAIT_SECONDS)
    allow_unsupported = bool_param(params, "allow_unsupported")
    debug_logging = bool_param(params, "debug_logging")

    config, target = load_config_and_target(operation, params, sink, profile="deploy", include_probe=True)
    connection = target.connection
    app_paths = resolve_app_paths(config_path=config_path(params))

    sink.stage(operation, "validate_artifacts")
    failures = [message for _, ok, message in validate_artifacts(app_paths.distribution_root) if not ok]
    if failures:
        raise AppOperationError("; ".join(failures), code="validation_failed")

    sink.stage(operation, "check_compatibility")
    compatibility = require_supported_payload(target, allow_unsupported=allow_unsupported)
    payload_family = compatibility.payload_family
    is_netbsd4 = is_netbsd4_payload_family(payload_family)
    sink.log(operation, f"Using {payload_family_description(payload_family)} payload.")
    resolved_artifacts = resolve_payload_artifacts(app_paths.distribution_root, payload_family)
    if not dry_run:
        confirmation_plan = build_deployment_plan(
            connection.host,
            build_dry_run_payload_home(MANAGED_PAYLOAD_DIR_NAME),
            resolved_artifacts["smbd"].absolute_path,
            resolved_artifacts["mdns-advertiser"].absolute_path,
            resolved_artifacts["nbns-advertiser"].absolute_path,
            activate_netbsd4=is_netbsd4,
            reboot_after_deploy=not no_reboot,
            apple_mount_wait_seconds=mount_wait,
        )
        device_name = airport_family_display_name_from_identity(
            model=target.probe_state.probe_result.airport_model if target.probe_state else None,
            syap=target.probe_state.probe_result.airport_syap if target.probe_state else None,
        )
        if is_netbsd4:
            title = "Confirm NetBSD4 deployment"
            message = f"Deploy and activate the NetBSD4 payload on this {device_name}. Remote services will be changed."
            action_title = "Deploy and activate"
            risk = "destructive"
            summary = "NetBSD4 deployment with service activation"
        elif no_reboot:
            title = "Confirm deployment"
            message = f"Deploy TimeCapsuleSMB to this {device_name} without rebooting it."
            action_title = "Deploy"
            risk = "remote_write"
            summary = "Deployment without reboot"
        else:
            title = "Confirm deployment and reboot"
            message = f"Deploy TimeCapsuleSMB and reboot this {device_name}."
            action_title = "Deploy and reboot"
            risk = "reboot"
            summary = "Deployment with reboot request"
        require_confirmation(
            params,
            build_confirmation(
                operation=operation,
                params=params,
                title=title,
                message=message,
                action_title=action_title,
                risk=risk,
                summary=summary,
                context={
                    "host": connection.host,
                    "payload_family": payload_family,
                    "netbsd4": is_netbsd4,
                    "requires_reboot": bool(confirmation_plan.reboot_required),
                    "no_reboot": no_reboot,
                    "no_wait": no_wait,
                },
            ),
            legacy_names=(
                ("confirm_deploy", "confirm_netbsd4_activation")
                if is_netbsd4
                else ("confirm_deploy",) if no_reboot else ("confirm_deploy", "confirm_reboot")
            ),
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
                no_mast_volumes_message(
                    attempts=MAST_DISCOVERY_ATTEMPTS,
                    delay_seconds=MAST_DISCOVERY_DELAY_SECONDS,
                ),
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
                no_writable_mast_volumes_message(len(mast_discovery.volumes)),
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
        return OperationResult(True, deploy_plan_payload(
            deployment_plan_to_jsonable(plan),
            payload_family=payload_family,
            netbsd4=is_netbsd4,
        ))

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
    verify_payload_upload(operation, sink, connection, payload_home, wait_seconds=mount_wait)
    sink.stage(operation, "flush_payload_upload")
    sink.log(operation, "Flushing deployed payload to disk...")
    flush_remote_filesystem_writes(connection)
    verify_payload_upload(operation, sink, connection, payload_home, wait_seconds=mount_wait, post_sync=True)

    if is_netbsd4:
        sink.stage(operation, "netbsd4_activation")
        run_remote_actions(connection, plan.activation_actions)
        verify_runtime(operation, sink, connection, stage="verify_runtime_activation", timeout_seconds=180)
        return OperationResult(True, deploy_result_payload(
            payload_dir=plan.payload_dir,
            netbsd4=True,
            reboot_requested=False,
            waited=False,
            verified=True,
            message=f"NetBSD4 activation complete. {NETBSD4_REBOOT_FOLLOWUP}",
            payload_family=payload_family,
        ))

    if no_reboot:
        return OperationResult(True, deploy_result_payload(
            payload_dir=plan.payload_dir,
            rebooted=False,
            reboot_requested=False,
            waited=False,
            verified=False,
            payload_family=payload_family,
        ))

    if no_wait:
        request_reboot(
            operation,
            sink,
            connection,
            strategy="ssh_shutdown_then_reboot",
            require_request_success=True,
        )
        return OperationResult(True, deploy_result_payload(
            payload_dir=plan.payload_dir,
            reboot_requested=True,
            waited=False,
            verified=False,
            payload_family=payload_family,
        ))

    request_reboot_and_wait(
        operation,
        sink,
        connection,
        strategy="ssh_shutdown_then_reboot",
        reboot_no_down_message=DEPLOY_REBOOT_NO_DOWN_MESSAGE,
    )
    verify_runtime(operation, sink, connection, stage="verify_runtime_reboot", timeout_seconds=240)
    return OperationResult(True, deploy_result_payload(
        payload_dir=plan.payload_dir,
        rebooted=True,
        reboot_requested=True,
        waited=True,
        verified=True,
        payload_family=payload_family,
    ))


def verify_payload_upload(
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
        raise AppOperationError(payload_verification_error(payload_home, verification), code="remote_error")


def verify_runtime(
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


def request_reboot_and_wait(
    operation: str,
    sink: EventSink,
    connection: SshConnection,
    *,
    strategy: str,
    reboot_no_down_message: str,
    down_timeout_seconds: int = 60,
    up_timeout_seconds: int = 240,
) -> None:
    request_reboot(operation, sink, connection, strategy=strategy)

    sink.stage(operation, "wait_for_reboot_down")
    sink.log(operation, "Waiting for the device to go down...")
    if not wait_for_ssh_state_conn(connection, expected_up=False, timeout_seconds=down_timeout_seconds):
        raise AppOperationError(reboot_no_down_message, code="remote_error")
    sink.stage(operation, "wait_for_reboot_up")
    sink.log(operation, "Waiting for the device to come back up...")
    if not wait_for_ssh_state_conn(connection, expected_up=True, timeout_seconds=up_timeout_seconds):
        raise AppOperationError(DEPLOY_REBOOT_UP_TIMEOUT_MESSAGE, code="remote_error")
    sink.log(operation, "Device is back online.")


def request_reboot(
    operation: str,
    sink: EventSink,
    connection: SshConnection,
    *,
    strategy: str,
    require_request_success: bool = False,
) -> None:
    sink.stage(operation, "reboot")
    if strategy == "acp_then_ssh":
        try:
            acp_reboot(extract_host(connection.host), connection.password, timeout=ACP_REBOOT_REQUEST_TIMEOUT_SECONDS)
            sink.log(operation, "ACP reboot requested.")
        except ACPError as exc:
            sink.log(operation, f"ACP reboot request failed; trying SSH reboot request: {exc}", level="warning")
            request_ssh_reboot(
                operation,
                sink,
                connection,
                shutdown=False,
                require_request_success=require_request_success,
            )
    else:
        request_ssh_reboot(
            operation,
            sink,
            connection,
            shutdown=True,
            require_request_success=require_request_success,
        )


def request_ssh_reboot(
    operation: str,
    sink: EventSink,
    connection: SshConnection,
    *,
    shutdown: bool,
    require_request_success: bool = False,
) -> None:
    try:
        if shutdown:
            remote_request_shutdown_reboot(connection)
        else:
            remote_request_reboot(connection)
    except SshCommandTimeout as exc:
        sink.log(operation, f"SSH reboot request timed out; checking whether the device is rebooting: {exc}", level="warning")
        return
    except SshError as exc:
        if require_request_success:
            raise AppOperationError(f"SSH reboot request failed: {exc}", code="remote_error") from exc
        sink.log(operation, f"SSH reboot request failed; checking whether the device is rebooting anyway: {exc}", level="warning")
        return
    sink.log(operation, "SSH reboot requested.")
