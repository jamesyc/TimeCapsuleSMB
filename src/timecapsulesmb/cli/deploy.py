from __future__ import annotations

import argparse
from contextlib import ExitStack
import tempfile
from pathlib import Path
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.flows import (
    activate_deployed_runtime_flow,
    request_deploy_reboot,
    request_deploy_reboot_and_wait,
    verify_managed_runtime_flow,
)
from timecapsulesmb.cli.runtime import (
    add_mount_wait_argument,
    add_no_wait_argument,
    add_config_argument,
    load_env_config,
    print_json,
    require_supported_device_compatibility,
)
from timecapsulesmb.core.config import (
    MANAGED_PAYLOAD_DIR_NAME,
    airport_family_display_name_from_identity,
)
from timecapsulesmb.core.messages import NETBSD4_REBOOT_FOLLOWUP
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.deploy.artifact_resolver import resolve_payload_artifacts
from timecapsulesmb.deploy.artifacts import validate_artifacts
from timecapsulesmb.deploy.auth import render_smbpasswd
from timecapsulesmb.deploy.dry_run import deployment_plan_to_jsonable, format_deployment_plan
from timecapsulesmb.deploy.executor import flush_remote_filesystem_writes, run_remote_actions, upload_deployment_payload
from timecapsulesmb.deploy.planner import (
    BINARY_MDNS_SOURCE,
    BINARY_NBNS_SOURCE,
    BINARY_SMBD_SOURCE,
    DEPLOY_STARTUP_ACTIVATE_NOW,
    DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
    DEPLOY_STARTUP_REBOOT_THEN_VERIFY,
    DeploymentStartupMode,
    GENERATED_FLASH_CONFIG_SOURCE,
    GENERATED_SMBPASSWD_SOURCE,
    GENERATED_USERNAME_MAP_SOURCE,
    PACKAGED_COMMON_SH_SOURCE,
    PACKAGED_DFREE_SH_SOURCE,
    PACKAGED_START_SAMBA_SOURCE,
    PACKAGED_RC_LOCAL_SOURCE,
    PACKAGED_WATCHDOG_SOURCE,
    build_deployment_plan,
)
from timecapsulesmb.deploy.boot_assets import (
    boot_asset_path,
)
from timecapsulesmb.device.compat import is_netbsd4_payload_family, payload_family_description
from timecapsulesmb.device.storage import (
    MAST_DISCOVERY_ATTEMPTS,
    MAST_DISCOVERY_DELAY_SECONDS,
    build_dry_run_payload_home,
    verify_payload_home_conn,
)
from timecapsulesmb.services.deploy import (
    DEPLOY_REBOOT_NO_DOWN_MESSAGE as REBOOT_NO_DOWN_MESSAGE,
    no_mast_volumes_message,
    no_writable_mast_volumes_message,
    payload_verification_error,
    render_flash_runtime_config,
)
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.cli.util import color_green


def _target_family_display_name(target) -> str:
    probe = target.probe_state.probe_result if target.probe_state is not None else None
    return airport_family_display_name_from_identity(
        model=None if probe is None else probe.airport_model,
        syap=None if probe is None else probe.airport_syap,
    )


def _startup_mode_for_deploy(*, no_reboot: bool, is_netbsd4: bool) -> DeploymentStartupMode:
    if no_reboot:
        return DEPLOY_STARTUP_ACTIVATE_NOW
    if is_netbsd4:
        return DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE
    return DEPLOY_STARTUP_REBOOT_THEN_VERIFY


def _activation_complete_message(*, is_netbsd4: bool) -> str:
    if is_netbsd4:
        return f"NetBSD4 activation complete. {NETBSD4_REBOOT_FOLLOWUP}"
    return "Runtime activation complete."


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("must be an integer") from e
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return parsed


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy the checked-in Samba 4 payload to an AirPort storage device.")
    add_config_argument(parser)
    add_mount_wait_argument(parser)
    add_no_wait_argument(parser)
    parser.add_argument("--no-reboot", action="store_true", help="Do not reboot; activate the deployed runtime in place")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before reboot")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without making changes")
    parser.add_argument("--json", action="store_true", help="Output the dry-run deployment plan as JSON")
    parser.add_argument("--allow-unsupported", action="store_true", help="Proceed even if the detected device is not currently supported")
    parser.add_argument("--no-nbns", action="store_true", help="Disable the bundled NBNS responder on the next boot")
    parser.add_argument("--debug-logging", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.json and not args.dry_run:
        parser.error("--json currently requires --dry-run")

    if not args.json:
        print("Deploying...")

    nbns_enabled = not args.no_nbns
    ensure_install_id()
    app_paths = resolve_app_paths(config_path=args.config)
    config = load_env_config(env_path=args.config)
    telemetry = TelemetryClient.from_config(config, nbns_enabled=nbns_enabled)
    with CommandContext(telemetry, "deploy", "deploy_started", "deploy_finished", config=config, args=args) as command_context:
        command_context.update_fields(
            nbns_enabled=nbns_enabled,
            reboot_was_attempted=False,
            device_came_back_after_reboot=False,
        )
        command_context.set_stage("resolve_managed_target")
        target = command_context.resolve_validated_managed_target(profile="deploy", include_probe=True)
        connection = target.connection
        host = connection.host
        smb_password = connection.password

        command_context.set_stage("validate_artifacts")
        artifact_results = validate_artifacts(app_paths.distribution_root)
        failures = [message for _, ok, message in artifact_results if not ok]
        if failures:
            raise SystemExit("; ".join(failures))
        command_context.set_stage("check_compatibility")
        compatibility, compatibility_message = require_supported_device_compatibility(
            command_context,
            allow_unsupported=args.allow_unsupported,
            json_output=args.json,
        )
        if not compatibility.payload_family:
            raise SystemExit(f"{compatibility_message}\nNo deployable payload is available for this detected device.")
        payload_family = compatibility.payload_family
        is_netbsd4 = is_netbsd4_payload_family(payload_family)
        startup_mode = _startup_mode_for_deploy(no_reboot=args.no_reboot, is_netbsd4=is_netbsd4)
        command_context.update_fields(deploy_startup_mode=startup_mode)
        if not args.json:
            print(f"Using {payload_family_description(payload_family)} payload...")
        apple_mount_wait_seconds = args.mount_wait
        resolved_artifacts = resolve_payload_artifacts(app_paths.distribution_root, payload_family)
        smbd_path = resolved_artifacts["smbd"].absolute_path
        mdns_path = resolved_artifacts["mdns-advertiser"].absolute_path
        nbns_path = resolved_artifacts["nbns-advertiser"].absolute_path
        if args.dry_run:
            payload_home = build_dry_run_payload_home(MANAGED_PAYLOAD_DIR_NAME)
        else:
            mast_discovery = command_context.wait_for_mast_volumes(
                connection,
                attempts=MAST_DISCOVERY_ATTEMPTS,
                delay_seconds=MAST_DISCOVERY_DELAY_SECONDS,
            )
            mast_volumes = mast_discovery.volumes
            if not mast_volumes:
                raise SystemExit(
                    no_mast_volumes_message(
                        attempts=MAST_DISCOVERY_ATTEMPTS,
                        delay_seconds=MAST_DISCOVERY_DELAY_SECONDS,
                    )
                )
            selection = command_context.select_payload_home(
                connection,
                mast_volumes,
                MANAGED_PAYLOAD_DIR_NAME,
                wait_seconds=apple_mount_wait_seconds,
            )
            if selection.payload_home is None:
                raise SystemExit(no_writable_mast_volumes_message(len(mast_volumes)))
            payload_home = selection.payload_home
        command_context.set_stage("build_deployment_plan")
        plan = build_deployment_plan(
            host,
            payload_home,
            smbd_path,
            mdns_path,
            nbns_path,
            startup_mode=startup_mode,
            apple_mount_wait_seconds=apple_mount_wait_seconds,
        )
        command_context.add_debug_fields(
            payload_volume_root=plan.volume_root,
            payload_device_path=plan.device_path,
            payload_dir=plan.payload_dir,
        )

        if args.dry_run:
            if args.json:
                print_json(deployment_plan_to_jsonable(plan))
            else:
                print(format_deployment_plan(plan))
            command_context.succeed()
            return 0

        print("Deleting old deployed files...")
        command_context.set_stage("pre_upload_actions")
        run_remote_actions(connection, plan.pre_upload_actions)
        command_context.set_stage("prepare_deployment_files")
        flash_config_text = render_flash_runtime_config(
            config,
            payload_home,
            nbns_enabled=nbns_enabled,
            debug_logging=True if args.debug_logging else None,
        )

        with tempfile.TemporaryDirectory(prefix="tc-deploy-") as tmp, ExitStack() as boot_assets:
            tmpdir = Path(tmp)
            generated_flash_config = tmpdir / "tcapsulesmb.conf"
            generated_smbpasswd = tmpdir / "smbpasswd"
            generated_username_map = tmpdir / "username.map"
            generated_flash_config.write_text(flash_config_text)
            smbpasswd_text, username_map_text = render_smbpasswd(smb_password)
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

            command_context.set_stage("upload_payload")
            upload_deployment_payload(
                plan,
                connection=connection,
                source_resolver=upload_sources,
            )

        command_context.set_stage("post_upload_actions")
        run_remote_actions(connection, plan.post_upload_actions)

        command_context.set_stage("verify_payload_upload")
        payload_verification = verify_payload_home_conn(
            connection,
            payload_home,
            wait_seconds=apple_mount_wait_seconds,
        )
        command_context.add_debug_fields(payload_upload_verification=payload_verification.detail)
        if not payload_verification.ok:
            raise SystemExit(payload_verification_error(payload_home, payload_verification))

        command_context.set_stage("flush_payload_upload")
        if not args.json:
            print("Flushing deployed payload to disk...")
        flush_remote_filesystem_writes(connection)

        # The immediate verification above can succeed from cache. Flush and
        # verify again before any reboot so dirty HFS metadata cannot disappear
        # under an ACP-triggered restart.
        command_context.set_stage("verify_payload_upload_after_sync")
        payload_verification = verify_payload_home_conn(
            connection,
            payload_home,
            wait_seconds=apple_mount_wait_seconds,
        )
        command_context.add_debug_fields(payload_post_sync_verification=payload_verification.detail)
        if not payload_verification.ok:
            raise SystemExit(payload_verification_error(payload_home, payload_verification))

        print(f"Deployed Samba payload to {plan.payload_dir}")
        print("Updated /mnt/Flash boot files.")

        if startup_mode == DEPLOY_STARTUP_ACTIVATE_NOW:
            if not activate_deployed_runtime_flow(
                connection,
                command_context,
                plan.activation_actions,
                run_actions=run_remote_actions,
                skip_if_ready=False,
                already_active_message="Managed runtime already active; skipping rc.local.",
                startup_in_progress_message="Managed runtime startup is already in progress; waiting for it to finish.",
                activation_message="Starting deployed runtime without reboot.",
                activation_stage="activate_runtime",
                verification_stage="verify_runtime_activation",
                verification_timeout_seconds=180,
                verification_heading="Waiting for managed runtime to finish starting...",
                failure_message="Managed runtime activation failed.",
            ):
                return 1
            print(_activation_complete_message(is_netbsd4=is_netbsd4))
            print(color_green("Deploy Finished."))
            command_context.succeed()
            return 0

        if not args.yes:
            device_name = _target_family_display_name(target)
            if startup_mode == DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE:
                prompt = f"This will reboot the {device_name}, then activate Samba after SSH returns. Continue?"
            else:
                prompt = f"This will reboot the {device_name} now. Continue?"
            proceed = command_context.confirm_or_fail(
                prompt,
                default=True,
                noninteractive_message="Running `deploy` with reboot requires confirmation when stdin is not interactive. Use `deploy --yes` to skip the prompt or `deploy --no-reboot`.",
            )
            if proceed is None:
                return 1
            if not proceed:
                print("Deployment complete without reboot.")
                command_context.cancel_with_error("Cancelled by user at reboot confirmation prompt.")
                return 0

        if args.no_wait:
            request_deploy_reboot(connection, command_context, raise_on_request_error=True)
            print("Reboot requested; not waiting for the device to go down or come back.")
            print(color_green("Deploy Finished."))
            command_context.succeed()
            return 0

        if not request_deploy_reboot_and_wait(
            connection,
            command_context,
            reboot_no_down_message=REBOOT_NO_DOWN_MESSAGE,
        ):
            return 1

        if startup_mode == DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE:
            if not activate_deployed_runtime_flow(
                connection,
                command_context,
                plan.activation_actions,
                run_actions=run_remote_actions,
                skip_if_ready=True,
                already_active_message="Managed runtime already active after reboot; skipping rc.local.",
                startup_in_progress_message="Managed runtime startup is already in progress after reboot; waiting for it to finish.",
                activation_message="Activating deployed runtime after reboot.",
                activation_stage="post_reboot_activation",
                verification_stage="verify_runtime_activation",
                verification_timeout_seconds=180,
                verification_heading="Waiting for NetBSD 4 device activation, this can take a few minutes for Samba to start up...",
                failure_message="NetBSD4 activation failed.",
            ):
                return 1
            print(_activation_complete_message(is_netbsd4=is_netbsd4))
            print(color_green("Deploy Finished."))
            command_context.succeed()
            return 0

        print("Waiting for managed runtime to finish starting...")
        if verify_managed_runtime_flow(
            connection,
            command_context,
            stage="verify_runtime_reboot",
            timeout_seconds=240,
            heading="Wait for device to finish loading; it can take a few minutes for Samba to start up...",
            failure_message="Managed runtime did not become ready after reboot.",
        ):
            print(color_green("Deploy Finished."))
            command_context.succeed()
            return 0

        return 1
    return 1
