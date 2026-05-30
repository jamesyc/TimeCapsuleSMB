from __future__ import annotations

import argparse
from contextlib import ExitStack
import tempfile
from pathlib import Path
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.flows import request_deploy_reboot_and_wait, verify_managed_runtime_flow
from timecapsulesmb.cli.runtime import (
    add_config_argument,
    add_no_input_argument,
    no_input_enabled,
    print_json,
    require_supported_device_compatibility,
)
from timecapsulesmb.core.config import (
    MANAGED_PAYLOAD_DIR_NAME,
    airport_family_display_name_from_identity,
)
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.deploy.artifact_resolver import resolve_payload_artifacts
from timecapsulesmb.deploy.artifacts import validate_artifacts
from timecapsulesmb.deploy.auth import render_smbpasswd
from timecapsulesmb.deploy.commands import RemoteAction, StopProcessAction
from timecapsulesmb.deploy.dry_run import deployment_plan_to_jsonable, format_deployment_plan
from timecapsulesmb.deploy.executor import flush_remote_filesystem_writes, run_remote_actions, upload_deployment_payload
from timecapsulesmb.deploy.planner import (
    BINARY_MDNS_SOURCE,
    BINARY_NBNS_SOURCE,
    BINARY_SMBD_SOURCE,
    DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
    DEPLOY_STARTUP_ACTIVATE_NOW,
    DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
    FileTransfer,
    GENERATED_FLASH_CONFIG_SOURCE,
    GENERATED_SMBPASSWD_SOURCE,
    GENERATED_USERNAME_MAP_SOURCE,
    PACKAGED_BOOT_SOURCE,
    PACKAGED_COMMON_SH_SOURCE,
    PACKAGED_DFREE_SH_SOURCE,
    PACKAGED_MANAGER_SOURCE,
    PACKAGED_RC_LOCAL_SOURCE,
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
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.cli.util import color_green
from timecapsulesmb.services.activation import decide_netbsd4_post_reboot_activation
from timecapsulesmb.services.deploy import (
    DEPLOY_REBOOT_NO_DOWN_MESSAGE,
    activation_complete_message,
    no_mast_volumes_message,
    no_writable_mast_volumes_message,
    payload_verification_error,
    render_flash_runtime_config,
    startup_mode_for_deploy,
)
from timecapsulesmb.services.runtime import load_env_config


def _target_family_display_name(target) -> str:
    probe = target.probe_state.probe_result if target.probe_state is not None else None
    return airport_family_display_name_from_identity(
        model=None if probe is None else probe.airport_model,
        syap=None if probe is None else probe.airport_syap,
    )

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
    parser.add_argument("--no-reboot", action="store_true", help="Do not reboot; activate the deployed runtime in place")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before reboot")
    add_no_input_argument(parser)
    parser.add_argument("--dry-run", action="store_true", help="Print actions without making changes")
    parser.add_argument("--json", action="store_true", help="Output the dry-run deployment plan as JSON")
    parser.add_argument("--allow-unsupported", action="store_true", help="Proceed even if the detected device is not currently supported")
    parser.add_argument("--no-nbns", action="store_true", help="Disable the bundled NBNS responder on the next boot")
    parser.add_argument(
        "--mount-wait",
        type=_non_negative_int,
        default=DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
        metavar="SECONDS",
        help=f"Seconds for each deployment-time diskd.useVolume mount guard attempt to wait (default: {DEFAULT_APPLE_MOUNT_WAIT_SECONDS})",
    )
    parser.add_argument("--debug-logging", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.json and not args.dry_run:
        parser.error("--json currently requires --dry-run")

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
        if no_input_enabled(args) and not args.yes and not args.no_reboot and not args.dry_run:
            command_context.set_stage("noninteractive_confirmation")
            message = (
                "Running `deploy` with reboot in non-interactive mode requires `--yes` "
                "to approve the reboot or `--no-reboot` to avoid it."
            )
            print(message)
            command_context.fail_with_error(message)
            return 1
        command_context.set_stage("resolve_managed_target")
        if not args.json:
            print("Resolving deployment target...", flush=True)
        target = command_context.resolve_validated_managed_target(profile="deploy", include_probe=True)
        connection = target.connection
        host = connection.host
        smb_password = connection.password

        command_context.set_stage("validate_artifacts")
        if not args.json:
            print("Validating local artifacts...", flush=True)
        artifact_results = validate_artifacts(app_paths.distribution_root)
        failures = [message for _, ok, message in artifact_results if not ok]
        if failures:
            raise SystemExit("; ".join(failures))
        command_context.set_stage("check_compatibility")
        if not args.json:
            print("Checking device compatibility...", flush=True)
        compatibility, compatibility_message = require_supported_device_compatibility(
            command_context,
            allow_unsupported=args.allow_unsupported,
            json_output=args.json,
        )
        if not compatibility.payload_family:
            raise SystemExit(f"{compatibility_message}\nNo deployable payload is available for this detected device.")
        payload_family = compatibility.payload_family
        is_netbsd4 = is_netbsd4_payload_family(payload_family)
        if is_netbsd4:
            # Apple NetBSD 4 firmware can expose /usr/bin/scp but hang after
            # writing the file. Use the SSH pipe upload fallback consistently.
            connection.remote_has_scp = False
        startup_mode = startup_mode_for_deploy(no_reboot=args.no_reboot, is_netbsd4=is_netbsd4)
        command_context.update_fields(deploy_startup_mode=startup_mode)
        if not args.json:
            print(f"Using {payload_family_description(payload_family)} payload.", flush=True)
        apple_mount_wait_seconds = args.mount_wait
        resolved_artifacts = resolve_payload_artifacts(app_paths.distribution_root, payload_family)
        smbd_path = resolved_artifacts["smbd"].absolute_path
        mdns_path = resolved_artifacts["mdns-advertiser"].absolute_path
        nbns_path = resolved_artifacts["nbns-advertiser"].absolute_path
        if args.dry_run:
            payload_home = build_dry_run_payload_home(MANAGED_PAYLOAD_DIR_NAME)
        else:
            if not args.json:
                print("Finding payload volume...", flush=True)
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
            if not args.json:
                print(f"Using payload directory {payload_home.payload_dir}.", flush=True)
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

        print("Deleting old deployed files...", flush=True)
        print("Stopping existing runtime...", flush=True)

        def report_pre_upload_action(action: RemoteAction, _index: int, _total: int) -> None:
            if isinstance(action, StopProcessAction) and action.name == "nbns-advertiser":
                print("Cleaning up previous deployment files...", flush=True)

        command_context.set_stage("pre_upload_actions")
        run_remote_actions(connection, plan.pre_upload_actions, on_action_done=report_pre_upload_action)
        command_context.set_stage("prepare_deployment_files")
        flash_config_text = render_flash_runtime_config(
            config,
            payload_home,
            nbns_enabled=nbns_enabled,
            debug_logging=args.debug_logging,
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
                PACKAGED_BOOT_SOURCE: boot_assets.enter_context(boot_asset_path("boot.sh")),
                PACKAGED_MANAGER_SOURCE: boot_assets.enter_context(boot_asset_path("manager.sh")),
                PACKAGED_DFREE_SH_SOURCE: boot_assets.enter_context(boot_asset_path("dfree.sh")),
            }

            def report_uploaded_file(transfer: FileTransfer) -> None:
                message = None
                if transfer.source_id == BINARY_SMBD_SOURCE:
                    message = "Uploaded smbd."
                elif transfer.source_id == BINARY_MDNS_SOURCE and transfer.mode == "flash_atomic":
                    message = "Uploaded mdns-advertiser."
                elif transfer.source_id == BINARY_NBNS_SOURCE:
                    message = "Uploaded nbns-advertiser."
                elif transfer.source_id == PACKAGED_DFREE_SH_SOURCE:
                    message = "Uploaded boot files."
                elif transfer.source_id == GENERATED_FLASH_CONFIG_SOURCE:
                    message = "Uploaded runtime config."
                elif transfer.source_id == GENERATED_USERNAME_MAP_SOURCE:
                    message = "Uploaded Samba account files."
                if message is not None:
                    print(message, flush=True)

            command_context.set_stage("upload_payload")
            print("Uploading deployment payload...", flush=True)
            upload_deployment_payload(
                plan,
                connection=connection,
                source_resolver=upload_sources,
                on_uploaded=report_uploaded_file,
            )
            print("Upload phase complete.", flush=True)

        command_context.set_stage("post_upload_actions")
        print("Applying file permissions...", flush=True)
        run_remote_actions(connection, plan.post_upload_actions)

        command_context.set_stage("verify_payload_upload")
        print("Verifying uploaded payload...", flush=True)
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
            print("Flushing payload to disk...", flush=True)
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

        print("Verified uploaded payload.", flush=True)
        print(f"Deployed Samba payload to {plan.payload_dir}", flush=True)
        print("Updated /mnt/Flash boot files.", flush=True)

        if startup_mode == DEPLOY_STARTUP_ACTIVATE_NOW:
            command_context.set_stage("activate_runtime")
            print("Starting deployed runtime without reboot.")
            run_remote_actions(connection, plan.activation_actions)
            if not verify_managed_runtime_flow(
                connection,
                command_context,
                stage="verify_runtime_activation",
                timeout_seconds=180,
                heading="Waiting for managed runtime to finish starting...",
                failure_message="Managed runtime activation failed.",
            ):
                return 1
            print(activation_complete_message(is_netbsd4=is_netbsd4))
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
                allow_prompt=not no_input_enabled(args),
            )
            if proceed is None:
                return 1
            if not proceed:
                print("Deployment complete without reboot.", flush=True)
                command_context.cancel_with_error("Cancelled by user at reboot confirmation prompt.")
                return 0

        print("Requesting reboot...", flush=True)
        if not request_deploy_reboot_and_wait(
            connection,
            command_context,
            reboot_no_down_message=DEPLOY_REBOOT_NO_DOWN_MESSAGE,
        ):
            return 1

        if startup_mode == DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE:
            command_context.set_stage("probe_runtime")
            decision = decide_netbsd4_post_reboot_activation(connection)
            command_context.add_debug_fields(
                activation_decision=decision.reason,
                manual_activation_required=decision.run_actions,
            )
            print(decision.detail)
            if decision.run_actions:
                command_context.set_stage("post_reboot_activation")
                print("Activating deployed runtime after reboot.")
                run_remote_actions(connection, plan.activation_actions)
            else:
                print("NetBSD4 firmware autostart is enabled; waiting for managed runtime.")
            if not verify_managed_runtime_flow(
                connection,
                command_context,
                stage="verify_runtime_activation",
                timeout_seconds=180,
                heading="Waiting for NetBSD 4 device activation, this can take a few minutes for Samba to start up...",
                failure_message="NetBSD4 activation failed.",
            ):
                return 1
            print(activation_complete_message(is_netbsd4=is_netbsd4))
            print(color_green("Deploy Finished."))
            command_context.succeed()
            return 0

        print("Waiting for managed runtime to finish starting...", flush=True)
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
