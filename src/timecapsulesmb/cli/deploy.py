from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import load_env_values
from timecapsulesmb.core.config import parse_bool
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.deploy.artifact_resolver import resolve_payload_artifacts
from timecapsulesmb.deploy.artifacts import validate_artifacts
from timecapsulesmb.deploy.dry_run import deployment_plan_to_jsonable, format_deployment_plan
from timecapsulesmb.deploy.executor import (
    remote_ensure_adisk_uuid,
    remote_install_auth_files,
    run_remote_actions,
    upload_deployment_payload,
)
from timecapsulesmb.deploy.planner import build_deployment_plan
from timecapsulesmb.deploy.templates import build_template_bundle, render_template, write_boot_asset
from timecapsulesmb.deploy.verify import (
    verify_managed_runtime,
)
from timecapsulesmb.device.compat import is_netbsd4_payload_family, payload_family_description, render_compatibility_message
from timecapsulesmb.device.probe import build_device_paths, discover_volume_root_conn, wait_for_ssh_state_conn
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.transport.ssh import run_ssh
from timecapsulesmb.cli.util import NETBSD4_REBOOT_FOLLOWUP, NETBSD4_REBOOT_GUIDANCE, color_green, color_red


REPO_ROOT = Path(__file__).resolve().parents[3]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy the checked-in Samba 4 payload to a Time Capsule.")
    parser.add_argument("--no-reboot", action="store_true", help="Do not reboot after deployment")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before reboot")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without making changes")
    parser.add_argument("--json", action="store_true", help="Output the dry-run deployment plan as JSON")
    parser.add_argument("--allow-unsupported", action="store_true", help="Proceed even if the detected device is not currently supported")
    parser.add_argument("--install-nbns", action="store_true", help="Enable the bundled NBNS responder on the next boot")
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
        target = command_context.resolve_validated_managed_target(profile="deploy", include_probe=True)
        connection = target.connection
        host = connection.host
        smb_password = connection.password

        artifact_results = validate_artifacts(REPO_ROOT)
        failures = [message for _, ok, message in artifact_results if not ok]
        if failures:
            raise SystemExit("; ".join(failures))
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
        plan = build_deployment_plan(
            host,
            device_paths,
            smbd_path,
            mdns_path,
            nbns_path,
            install_nbns=args.install_nbns,
            activate_netbsd4=is_netbsd4,
            reboot_after_deploy=not args.no_reboot,
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
                command_context.add_debug_context()
                return 0

        run_remote_actions(connection, plan.pre_upload_actions)
        adisk_uuid = remote_ensure_adisk_uuid(connection, plan.private_dir)
        template_bundle = build_template_bundle(
            values,
            adisk_disk_key=plan.disk_key,
            adisk_uuid=adisk_uuid,
            payload_family=payload_family,
            debug_logging=args.debug_logging,
            data_root=device_paths.data_root,
            share_use_disk_root=share_use_disk_root,
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

        remote_install_auth_files(connection, plan.private_dir, values["TC_SAMBA_USER"], smb_password)
        run_remote_actions(connection, plan.post_auth_actions)

        print(f"Deployed Samba payload to {plan.payload_dir}")
        print("Updated /mnt/Flash boot files.")

        if is_netbsd4:
            print("Activating NetBSD4 payload without reboot.")
            run_remote_actions(connection, plan.activation_actions)
            if not verify_managed_runtime(connection, timeout_seconds=180, heading="Waiting for NetBSD 4 device activation, this can take a few minutes for Samba to start up..."):
                print("NetBSD4 activation failed.")
                command_context.fail_with_error("NetBSD4 activation failed.")
                command_context.add_debug_context()
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
            answer = input("This will reboot the Time Capsule now. Continue? [Y/n]: ").strip().lower()
            if answer not in {"", "y", "yes"}:
                print("Deployment complete without reboot.")
                command_context.cancel_with_error("Cancelled by user at reboot confirmation prompt.")
                command_context.add_debug_context()
                return 0

        run_ssh(connection, "/sbin/reboot", check=False)
        command_context.update_fields(reboot_was_attempted=True)
        print("Reboot requested. Waiting for the device to go down...")
        wait_for_ssh_state_conn(connection, expected_up=False, timeout_seconds=60)
        print("Waiting for the device to come back up...")
        if wait_for_ssh_state_conn(connection, expected_up=True, timeout_seconds=240):
            command_context.update_fields(device_came_back_after_reboot=True)
            print("Device is back online.")
            print("Waiting for managed runtime to finish starting...")
            if not verify_managed_runtime(connection, timeout_seconds=180, heading="Wait for device to finish loading; it can take a few minutes for Samba to start up..."):
                print("Managed runtime did not become ready after reboot.")
                command_context.fail_with_error("Managed runtime did not become ready after reboot.")
                command_context.add_debug_context()
                return 1
            print(color_green("Deploy Finished."))
            command_context.succeed()
            return 0

        print("Timed out waiting for SSH after reboot.")
        command_context.fail_with_error("Timed out waiting for SSH after reboot.")
        command_context.add_debug_context()
        return 1
    return 1
