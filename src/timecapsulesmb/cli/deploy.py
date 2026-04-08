from __future__ import annotations

import argparse
import getpass
import json
import tempfile
from pathlib import Path
from typing import Optional

from timecapsulesmb.core.config import ENV_PATH, extract_host, parse_env_values
from timecapsulesmb.deploy.artifact_resolver import resolve_required_artifacts
from timecapsulesmb.deploy.artifacts import validate_artifacts
from timecapsulesmb.deploy.dry_run import deployment_plan_to_jsonable, format_deployment_plan
from timecapsulesmb.deploy.executor import (
    remote_install_auth_files,
    remote_install_permissions,
    remote_prepare_dirs,
    upload_deployment_payload,
)
from timecapsulesmb.deploy.planner import build_deployment_plan
from timecapsulesmb.deploy.templates import build_template_bundle, render_template, write_boot_asset
from timecapsulesmb.deploy.verify import verify_post_deploy
from timecapsulesmb.device.probe import build_device_paths, discover_volume_root, wait_for_ssh_state
from timecapsulesmb.transport.ssh import run_ssh


REPO_ROOT = Path(__file__).resolve().parents[3]


def require(values: dict[str, str], key: str) -> str:
    value = values.get(key, "")
    if not value:
        raise SystemExit(f"Missing required setting in .env: {key}")
    return value


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy the checked-in Samba 4 payload to a Time Capsule.")
    parser.add_argument("--no-reboot", action="store_true", help="Do not reboot after deployment")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before reboot")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without making changes")
    parser.add_argument("--json", action="store_true", help="Output the dry-run deployment plan as JSON")
    args = parser.parse_args(argv)

    if args.json and not args.dry_run:
        parser.error("--json currently requires --dry-run")

    values = parse_env_values(ENV_PATH)
    host = require(values, "TC_HOST")
    password = values.get("TC_PASSWORD", "")
    if not password:
        password = getpass.getpass("Time Capsule root password: ")
    ssh_opts = values["TC_SSH_OPTS"]

    artifact_results = validate_artifacts(REPO_ROOT)
    failures = [message for _, ok, message in artifact_results if not ok]
    if failures:
        raise SystemExit("; ".join(failures))
    resolved_artifacts = resolve_required_artifacts(REPO_ROOT, ["smbd", "mdns-smbd-advertiser"])
    smbd_path = resolved_artifacts["smbd"].absolute_path
    mdns_path = resolved_artifacts["mdns-smbd-advertiser"].absolute_path

    template_bundle = build_template_bundle(values)

    volume_root = discover_volume_root(host, password, ssh_opts)
    device_paths = build_device_paths(volume_root, values["TC_PAYLOAD_DIR_NAME"])
    plan = build_deployment_plan(host, device_paths, smbd_path, mdns_path)

    if args.dry_run:
        if args.json:
            print(json.dumps(deployment_plan_to_jsonable(plan), indent=2, sort_keys=True))
        else:
            print(format_deployment_plan(plan))
        return 0

    remote_prepare_dirs(host, password, ssh_opts, plan.payload_dir)

    with tempfile.TemporaryDirectory(prefix="tc-deploy-") as tmp:
        tmpdir = Path(tmp)
        rendered_rc_local = tmpdir / "rc.local"
        rendered_start = tmpdir / "start-samba.sh"
        rendered_dfree = tmpdir / "dfree.sh"
        rendered_watchdog = tmpdir / "watchdog.sh"
        rendered_smbconf = tmpdir / "smb.conf.template"
        write_boot_asset("rc.local", rendered_rc_local)
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
            rendered_start=rendered_start,
            rendered_dfree=rendered_dfree,
            rendered_watchdog=rendered_watchdog,
            rendered_smbconf=rendered_smbconf,
        )

    remote_install_auth_files(host, password, ssh_opts, plan.private_dir, values["TC_SAMBA_USER"], password)
    remote_install_permissions(host, password, ssh_opts, plan.payload_dir)

    print(f"Deployed Samba payload to {plan.payload_dir}")
    print("Updated /mnt/Flash boot files.")

    if args.no_reboot:
        print("Skipping reboot.")
        return 0

    if not args.yes:
        answer = input("This will reboot the Time Capsule now. Continue? [Y/n]: ").strip().lower()
        if answer not in {"", "y", "yes"}:
            print("Deployment complete without reboot.")
            return 0

    run_ssh(host, password, ssh_opts, "/sbin/reboot", check=False)
    hostname = extract_host(host)
    print("Reboot requested. Waiting for the device to go down...")
    wait_for_ssh_state(hostname, expected_up=False, timeout_seconds=60)
    print("Waiting for the device to come back up...")
    if wait_for_ssh_state(hostname, expected_up=True, timeout_seconds=240):
        print("Device is back online.")
        verify_post_deploy(values)
        return 0

    print("Timed out waiting for SSH after reboot.")
    return 1
