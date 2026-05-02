from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import load_env_values
from timecapsulesmb.core.config import airport_family_display_name, parse_bool
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.deploy.artifact_resolver import resolve_payload_artifacts
from timecapsulesmb.deploy.artifacts import validate_artifacts
from timecapsulesmb.deploy.dry_run import deployment_plan_to_jsonable, format_deployment_plan
from timecapsulesmb.deploy.executor import (
    remote_ensure_adisk_uuid,
    remote_install_auth_files,
    remote_request_reboot,
    run_remote_actions,
    upload_deployment_payload,
)
from timecapsulesmb.deploy.planner import build_deployment_plan
from timecapsulesmb.deploy.templates import (
    DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
    build_template_bundle,
    render_template,
    write_boot_asset,
)
from timecapsulesmb.deploy.verify import (
    verify_managed_runtime,
)
from timecapsulesmb.device.compat import is_netbsd4_payload_family, payload_family_description, render_compatibility_message
from timecapsulesmb.device.probe import build_device_paths, discover_volume_root_conn, wait_for_ssh_state_conn
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.cli.util import NETBSD4_REBOOT_FOLLOWUP, NETBSD4_REBOOT_GUIDANCE, color_green, color_red


REPO_ROOT = Path(__file__).resolve().parents[3]


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
    parser.add_argument("--no-reboot", action="store_true", help="Do not reboot after deployment")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before reboot")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without making changes")
    parser.add_argument("--json", action="store_true", help="Output the dry-run deployment plan as JSON")
    parser.add_argument("--allow-unsupported", action="store_true", help="Proceed even if the detected device is not currently supported")
    parser.add_argument("--install-nbns", action="store_true", help="Enable the bundled NBNS responder on the next boot")
    parser.add_argument(
        "--mount-wait",
        type=_non_negative_int,
        default=DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
        metavar="SECONDS",
        help=f"Seconds to wait for Apple firmware to mount the data disk before manual mount fallback (default: {DEFAULT_APPLE_MOUNT_WAIT_SECONDS})",
    )
    parser.add_argument("--debug-logging", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.json and not args.dry_run:
        parser.error("--json currently requires --dry-run")

    if not args.json:
        print("Deploying...")

    ensure_install_id()
    values = load_env_values()
    telemetry = TelemetryClient.from_values(values, nbns_enabled=args.install_nbns)
    with CommandContext(telemetry, "deploy", "deploy_started", "deploy_finished", values=values, args=args) as command_context:
        command_context.update_fields(
            nbns_enabled=args.install_nbns,
            reboot_was_attempted=False,
            device_came_back_after_reboot=False,
        )
        command_context.set_stage("resolve_managed_target")
        target = command_context.resolve_validated_managed_target(profile="deploy", include_probe=True)
        connection = target.connection
        host = connection.host
        smb_password = connection.password

        command_context.set_stage("validate_artifacts")
        artifact_results = validate_artifacts(REPO_ROOT)
        failures = [message for _, ok, message in artifact_results if not ok]
        if failures:
            raise SystemExit("; ".join(failures))
        command_context.set_stage("check_compatibility")
        compatibility = command_context.require_compatibility()
        compatibility_message = render_compatibility_message(compatibility)
        if not compatibility.supported:
            if not args.allow_unsupported:
                raise SystemExit(compatibility_message)
            if not args.json:
                print(f"Warning: {compatibility_message}")
                print("Continuing because --allow-unsupported was provided.")
        elif not args.json:
            print(compatibility_message)
        if not compatibility.payload_family:
            raise SystemExit(f"{compatibility_message}\nNo deployable payload is available for this detected device.")
        payload_family = compatibility.payload_family
        is_netbsd4 = is_netbsd4_payload_family(payload_family)
        if not args.json:
            print(f"Using {payload_family_description(payload_family)} payload...")
        apple_mount_wait_seconds = args.mount_wait
        command_context.set_stage("discover_volume_root")
        volume_root = discover_volume_root_conn(connection)
        share_use_disk_root = parse_bool(values.get("TC_SHARE_USE_DISK_ROOT", "false"))
        resolved_artifacts = resolve_payload_artifacts(REPO_ROOT, payload_family)
        smbd_path = resolved_artifacts["smbd"].absolute_path
        mdns_path = resolved_artifacts["mdns-advertiser"].absolute_path
        nbns_path = resolved_artifacts["nbns-advertiser"].absolute_path
        device_paths = build_device_paths(
            volume_root,
            values["TC_PAYLOAD_DIR_NAME"],
            share_use_disk_root=share_use_disk_root,
        )
        command_context.set_stage("build_deployment_plan")
        plan = build_deployment_plan(
            host,
            device_paths,
            smbd_path,
            mdns_path,
            nbns_path,
            install_nbns=args.install_nbns,
            activate_netbsd4=is_netbsd4,
            reboot_after_deploy=not args.no_reboot,
            apple_mount_wait_seconds=apple_mount_wait_seconds,
        )

        if args.dry_run:
            if args.json:
                print(json.dumps(deployment_plan_to_jsonable(plan), indent=2, sort_keys=True))
            else:
                print(format_deployment_plan(plan))
            command_context.succeed()
            return 0

        if is_netbsd4 and not args.yes:
            print("Deploy will activate Samba immediately without rebooting.")
            print(color_red(NETBSD4_REBOOT_GUIDANCE))
            print(NETBSD4_REBOOT_FOLLOWUP)
            answer = input("Continue with NetBSD 4 deploy + activation? [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                print("Deployment cancelled.")
                command_context.cancel_with_error("Cancelled by user at NetBSD4 deploy confirmation prompt.")
                return 0

        command_context.set_stage("pre_upload_actions")
        run_remote_actions(connection, plan.pre_upload_actions)
        command_context.set_stage("ensure_adisk_uuid")
        adisk_uuid = remote_ensure_adisk_uuid(connection, plan.private_dir)
        command_context.set_stage("render_templates")
        template_bundle = build_template_bundle(
            values,
            adisk_disk_key=plan.disk_key,
            adisk_uuid=adisk_uuid,
            payload_family=payload_family,
            debug_logging=args.debug_logging,
            data_root=device_paths.data_root,
            share_use_disk_root=share_use_disk_root,
            apple_mount_wait_seconds=apple_mount_wait_seconds,
        )

        with tempfile.TemporaryDirectory(prefix="tc-deploy-") as tmp:
            tmpdir = Path(tmp)
            rendered_rc_local = tmpdir / "rc.local"
            rendered_common = tmpdir / "common.sh"
            rendered_start = tmpdir / "start-samba.sh"
            rendered_dfree = tmpdir / "dfree.sh"
            rendered_watchdog = tmpdir / "watchdog.sh"
            rendered_smbconf = tmpdir / "smb.conf.template"
            write_boot_asset("rc.local", rendered_rc_local)
            write_boot_asset("common.sh", rendered_common)
            rendered_start.write_text(render_template("start-samba.sh", template_bundle.start_script_replacements))
            write_boot_asset("dfree.sh", rendered_dfree)
            rendered_watchdog.write_text(render_template("watchdog.sh", template_bundle.watchdog_replacements))
            rendered_smbconf.write_text(render_template("smb.conf.template", template_bundle.smbconf_replacements))

            command_context.set_stage("upload_payload")
            upload_deployment_payload(
                plan,
                connection=connection,
                rc_local=rendered_rc_local,
                common_sh=rendered_common,
                rendered_start=rendered_start,
                rendered_dfree=rendered_dfree,
                rendered_watchdog=rendered_watchdog,
                rendered_smbconf=rendered_smbconf,
            )

        command_context.set_stage("install_auth_files")
        remote_install_auth_files(connection, plan.private_dir, values["TC_SAMBA_USER"], smb_password)
        command_context.set_stage("post_auth_actions")
        run_remote_actions(connection, plan.post_auth_actions)

        print(f"Deployed Samba payload to {plan.payload_dir}")
        print("Updated /mnt/Flash boot files.")

        if is_netbsd4:
            print("Activating NetBSD4 payload without reboot.")
            command_context.set_stage("netbsd4_activation")
            run_remote_actions(connection, plan.activation_actions)
            command_context.set_stage("verify_runtime_activation")
            if not verify_managed_runtime(connection, timeout_seconds=180, heading="Waiting for NetBSD 4 device activation, this can take a few minutes for Samba to start up..."):
                print("NetBSD4 activation failed.")
                command_context.fail_with_error("NetBSD4 activation failed.")
                return 1
            print(f"NetBSD4 activation complete. {NETBSD4_REBOOT_FOLLOWUP}")
            print(color_green("Deploy Finished."))
            command_context.succeed()
            return 0

        if args.no_reboot:
            print("Skipping reboot.")
            print(color_green("Deploy Finished."))
            command_context.succeed()
            return 0

        if not args.yes:
            device_name = airport_family_display_name(values)
            answer = input(f"This will reboot the {device_name} now. Continue? [Y/n]: ").strip().lower()
            if answer not in {"", "y", "yes"}:
                print("Deployment complete without reboot.")
                command_context.cancel_with_error("Cancelled by user at reboot confirmation prompt.")
                return 0

        command_context.set_stage("reboot")
        command_context.update_fields(reboot_was_attempted=True)
        remote_request_reboot(connection)
        print("Reboot requested. Waiting for the device to go down...")
        command_context.set_stage("wait_for_reboot_down")
        wait_for_ssh_state_conn(connection, expected_up=False, timeout_seconds=60)
        print("Waiting for the device to come back up...")
        command_context.set_stage("wait_for_reboot_up")
        if wait_for_ssh_state_conn(connection, expected_up=True, timeout_seconds=240):
            command_context.update_fields(device_came_back_after_reboot=True)
            print("Device is back online.")
            print("Waiting for managed runtime to finish starting...")
            command_context.set_stage("verify_runtime_reboot")
            if not verify_managed_runtime(connection, timeout_seconds=240, heading="Wait for device to finish loading; it can take a few minutes for Samba to start up..."):
                print("Managed runtime did not become ready after reboot.")
                command_context.fail_with_error("Managed runtime did not become ready after reboot.")
                return 1
            print(color_green("Deploy Finished."))
            command_context.succeed()
            return 0

        print("Timed out waiting for SSH after reboot.")
        command_context.fail_with_error("Timed out waiting for SSH after reboot.")
        return 1
    return 1
