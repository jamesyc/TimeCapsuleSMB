from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Optional

from timecapsulesmb.core.config import AppConfig, ENV_PATH, parse_env_values, validate_airport_syap
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
    verify_netbsd4_activation,
    verify_post_deploy,
    wait_for_post_reboot_mdns_takeover,
    wait_for_post_reboot_smbd,
)
from timecapsulesmb.device.compat import probe_device_compatibility
from timecapsulesmb.device.probe import build_device_paths, discover_volume_root, wait_for_ssh_state
from timecapsulesmb.telemetry import TelemetryClient, build_device_os_version, detect_device_family
from timecapsulesmb.transport.ssh import run_ssh
from timecapsulesmb.cli.util import NETBSD4_REBOOT_FOLLOWUP, NETBSD4_REBOOT_GUIDANCE, color_red, resolve_env_connection


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
    values = parse_env_values(ENV_PATH)
    telemetry = TelemetryClient.from_values(values, nbns_enabled=args.install_nbns)
    command_telemetry = telemetry.begin_command("deploy_started", "deploy_finished")
    result = "failure"
    finish_fields: dict[str, object] = {
        "nbns_enabled": args.install_nbns,
        "reboot_was_attempted": False,
        "device_came_back_after_reboot": False,
    }
    try:
        AppConfig(values).require(
            "TC_AIRPORT_SYAP",
            messageafter="\nPlease run the `configure` command before running `deploy`.",
        )
        syap_error = validate_airport_syap(values["TC_AIRPORT_SYAP"], "TC_AIRPORT_SYAP")
        if syap_error:
            raise SystemExit(syap_error)
        host, password, ssh_opts = resolve_env_connection(values)

        artifact_results = validate_artifacts(REPO_ROOT)
        failures = [message for _, ok, message in artifact_results if not ok]
        if failures:
            raise SystemExit("; ".join(failures))
        volume_root = discover_volume_root(host, password, ssh_opts)
        compatibility = probe_device_compatibility(host, password, ssh_opts)
        finish_fields["device_os_version"] = build_device_os_version(
            compatibility.os_name,
            compatibility.os_release,
            compatibility.arch,
        )
        finish_fields["device_family"] = detect_device_family(compatibility.payload_family)
        if not compatibility.supported:
            if not args.allow_unsupported:
                raise SystemExit(compatibility.message)
            if not args.json:
                print(f"Warning: {compatibility.message}")
                print("Continuing because --allow-unsupported was provided.")
        elif not args.json:
            print(compatibility.message)
        payload_family = compatibility.payload_family or "netbsd6_samba4"
        is_netbsd4 = payload_family == "netbsd4_samba4"
        resolved_artifacts = resolve_payload_artifacts(REPO_ROOT, payload_family)
        smbd_path = resolved_artifacts["smbd"].absolute_path
        mdns_path = resolved_artifacts["mdns-advertiser"].absolute_path
        nbns_path = resolved_artifacts["nbns-advertiser"].absolute_path
        device_paths = build_device_paths(volume_root, values["TC_PAYLOAD_DIR_NAME"])
        plan = build_deployment_plan(
            host,
            device_paths,
            smbd_path,
            mdns_path,
            nbns_path,
            install_nbns=args.install_nbns,
            activate_netbsd4=is_netbsd4,
        )

        if args.dry_run:
            if args.json:
                print(json.dumps(deployment_plan_to_jsonable(plan), indent=2, sort_keys=True))
            else:
                print(format_deployment_plan(plan))
            result = "success"
            return 0

        if is_netbsd4 and not args.yes:
            print("Detected NetBSD 4 Time Capsule.")
            print("Deploy will activate Samba immediately without rebooting.")
            print(color_red(NETBSD4_REBOOT_GUIDANCE))
            print(NETBSD4_REBOOT_FOLLOWUP)
            answer = input("Continue with NetBSD4 deploy + activation? [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                print("Deployment cancelled.")
                result = "cancelled"
                return 0

        run_remote_actions(host, password, ssh_opts, plan.pre_upload_actions)
        adisk_uuid = remote_ensure_adisk_uuid(host, password, ssh_opts, plan.private_dir)
        template_bundle = build_template_bundle(
            values,
            adisk_disk_key=plan.disk_key,
            adisk_uuid=adisk_uuid,
            payload_family=payload_family,
            debug_logging=args.debug_logging,
            data_root=device_paths.data_root,
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
                host=host,
                password=password,
                ssh_opts=ssh_opts,
                rc_local=rendered_rc_local,
                common_sh=rendered_common,
                rendered_start=rendered_start,
                rendered_dfree=rendered_dfree,
                rendered_watchdog=rendered_watchdog,
                rendered_smbconf=rendered_smbconf,
            )

        remote_install_auth_files(host, password, ssh_opts, plan.private_dir, values["TC_SAMBA_USER"], password)
        run_remote_actions(host, password, ssh_opts, plan.post_auth_actions)

        print(f"Deployed Samba payload to {plan.payload_dir}")
        print("Updated /mnt/Flash boot files.")

        if is_netbsd4:
            print("Activating NetBSD4 payload without reboot.")
            run_remote_actions(host, password, ssh_opts, plan.activation_actions)
            if not verify_netbsd4_activation(host, password, ssh_opts):
                print("NetBSD4 activation failed.")
                return 1
            print(f"NetBSD4 activation complete. {NETBSD4_REBOOT_FOLLOWUP}")
            result = "success"
            return 0

        if args.no_reboot:
            print("Skipping reboot.")
            result = "success"
            return 0

        if not args.yes:
            answer = input("This will reboot the Time Capsule now. Continue? [Y/n]: ").strip().lower()
            if answer not in {"", "y", "yes"}:
                print("Deployment complete without reboot.")
                result = "cancelled"
                return 0

        run_ssh(host, password, ssh_opts, "/sbin/reboot", check=False)
        finish_fields["reboot_was_attempted"] = True
        print("Reboot requested. Waiting for the device to go down...")
        wait_for_ssh_state(host, password, ssh_opts, expected_up=False, timeout_seconds=60)
        print("Waiting for the device to come back up...")
        if wait_for_ssh_state(host, password, ssh_opts, expected_up=True, timeout_seconds=240):
            finish_fields["device_came_back_after_reboot"] = True
            print("Device is back online.")
            print("Waiting for managed smbd to finish starting...")
            if not wait_for_post_reboot_smbd(host, password, ssh_opts):
                print("Managed smbd did not become ready after reboot.")
                return 1
            print("Waiting for managed mDNS takeover to finish...")
            if not wait_for_post_reboot_mdns_takeover(host, password, ssh_opts):
                print("Managed mDNS did not become ready after reboot.")
                return 1
            verify_post_deploy(values)
            result = "success"
            return 0

        print("Timed out waiting for SSH after reboot.")
        return 1
    finally:
        command_telemetry.finish(result=result, **finish_fields)
