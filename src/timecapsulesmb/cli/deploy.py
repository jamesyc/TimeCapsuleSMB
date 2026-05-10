from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import tempfile
from pathlib import Path
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.flows import request_reboot_and_wait, verify_managed_runtime_flow
from timecapsulesmb.cli.runtime import add_config_argument, load_env_config
from timecapsulesmb.core.config import DEFAULTS, AppConfig, airport_family_display_name_from_config, parse_bool, shell_quote
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.deploy.artifact_resolver import resolve_payload_artifacts
from timecapsulesmb.deploy.artifacts import validate_artifacts
from timecapsulesmb.deploy.auth import render_smbpasswd
from timecapsulesmb.deploy.dry_run import deployment_plan_to_jsonable, format_deployment_plan
from timecapsulesmb.deploy.executor import run_remote_actions, upload_deployment_payload
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
    PACKAGED_START_SAMBA_SOURCE,
    PACKAGED_RC_LOCAL_SOURCE,
    PACKAGED_WATCHDOG_SOURCE,
    build_deployment_plan,
)
from timecapsulesmb.deploy.boot_assets import (
    boot_asset_path,
)
from timecapsulesmb.device.compat import is_netbsd4_payload_family, payload_family_description, render_compatibility_message
from timecapsulesmb.device.storage import (
    MAST_DISCOVERY_ATTEMPTS,
    MAST_DISCOVERY_DELAY_SECONDS,
    PayloadHome,
    build_dry_run_payload_home,
    mast_volumes_debug_summary,
    payload_candidate_checks_debug_summary,
    select_payload_home_with_diagnostics_conn,
    wait_for_mast_volumes_conn,
)
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.cli.util import NETBSD4_REBOOT_FOLLOWUP, NETBSD4_REBOOT_GUIDANCE, color_green, color_red


REBOOT_NO_DOWN_MESSAGE = (
    "Reboot was requested but the device did not go down.\n"
    "The deploy stopped the managed runtime before reboot; power-cycle or rerun deploy."
)


def _no_mast_volumes_message(*, attempts: int, delay_seconds: int) -> str:
    return (
        f"No deployable HFS disk was found after {attempts} MaSt queries "
        f"spaced {delay_seconds} seconds apart."
    )


def _no_writable_mast_volumes_message(volume_count: int) -> str:
    return f"MaSt found {volume_count} deployable HFS volume(s), but deploy could not write to any of them."


def _render_flash_config_assignment(key: str, value: str | int) -> str:
    if isinstance(value, int):
        return f"{key}={value}"
    return f"{key}={shell_quote(value)}"


def render_flash_runtime_config(
    config: AppConfig,
    payload_home: PayloadHome,
    *,
    nbns_enabled: bool,
    debug_logging: bool,
    apple_mount_wait_seconds: int = DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
) -> str:
    internal_root_default = config.get("TC_INTERNAL_SHARE_USE_DISK_ROOT", DEFAULTS["TC_INTERNAL_SHARE_USE_DISK_ROOT"])

    values: list[tuple[str, str | int]] = [
        ("TC_CONFIG_VERSION", 1),
        ("PAYLOAD_DIR_NAME", payload_home.payload_dir_name),
        ("NET_IFACE", config.require("TC_NET_IFACE")),
        ("SMB_SAMBA_USER", config.require("TC_SAMBA_USER")),
        ("MDNS_DEVICE_MODEL", config.get("TC_MDNS_DEVICE_MODEL", DEFAULTS["TC_MDNS_DEVICE_MODEL"])),
        ("AIRPORT_SYAP", config.get("TC_AIRPORT_SYAP", DEFAULTS["TC_AIRPORT_SYAP"])),
        ("INTERNAL_SHARE_USE_DISK_ROOT", 1 if parse_bool(internal_root_default) else 0),
        ("APPLE_MOUNT_WAIT_SECONDS", apple_mount_wait_seconds),
        ("NBNS_ENABLED", 1 if nbns_enabled else 0),
        ("SMBD_DEBUG_LOGGING", 1 if debug_logging else 0),
        ("MDNS_DEBUG_LOGGING", 1 if debug_logging else 0),
    ]
    return "\n".join(_render_flash_config_assignment(key, value) for key, value in values) + "\n"


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
    parser.add_argument("--no-reboot", action="store_true", help="Do not reboot after deployment")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before reboot")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without making changes")
    parser.add_argument("--json", action="store_true", help="Output the dry-run deployment plan as JSON")
    parser.add_argument("--allow-unsupported", action="store_true", help="Proceed even if the detected device is not currently supported")
    parser.add_argument("--no-nbns", action="store_true", help="Disable the bundled NBNS responder on the next boot")
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
        resolved_artifacts = resolve_payload_artifacts(app_paths.distribution_root, payload_family)
        smbd_path = resolved_artifacts["smbd"].absolute_path
        mdns_path = resolved_artifacts["mdns-advertiser"].absolute_path
        nbns_path = resolved_artifacts["nbns-advertiser"].absolute_path
        if args.dry_run:
            payload_home = build_dry_run_payload_home(config.require("TC_PAYLOAD_DIR_NAME"))
        else:
            command_context.set_stage("read_mast")
            mast_discovery = wait_for_mast_volumes_conn(
                connection,
                attempts=MAST_DISCOVERY_ATTEMPTS,
                delay_seconds=MAST_DISCOVERY_DELAY_SECONDS,
            )
            mast_volumes = mast_discovery.volumes
            command_context.add_debug_fields(
                mast_read_attempts=mast_discovery.attempts,
                mast_volume_count=len(mast_volumes),
                mast_candidates=mast_volumes_debug_summary(mast_volumes),
            )
            if not mast_volumes:
                raise SystemExit(
                    _no_mast_volumes_message(
                        attempts=MAST_DISCOVERY_ATTEMPTS,
                        delay_seconds=MAST_DISCOVERY_DELAY_SECONDS,
                    )
                )
            command_context.set_stage("select_payload_home")
            selection = select_payload_home_with_diagnostics_conn(
                connection,
                mast_volumes,
                config.require("TC_PAYLOAD_DIR_NAME"),
                wait_seconds=apple_mount_wait_seconds,
            )
            command_context.add_debug_fields(mast_candidate_checks=payload_candidate_checks_debug_summary(selection.checks))
            if selection.payload_home is None:
                raise SystemExit(_no_writable_mast_volumes_message(len(mast_volumes)))
            payload_home = selection.payload_home
        command_context.set_stage("build_deployment_plan")
        plan = build_deployment_plan(
            host,
            payload_home,
            smbd_path,
            mdns_path,
            nbns_path,
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
        command_context.set_stage("prepare_deployment_files")
        flash_config_text = render_flash_runtime_config(
            config,
            payload_home,
            nbns_enabled=nbns_enabled,
            debug_logging=args.debug_logging,
            apple_mount_wait_seconds=apple_mount_wait_seconds,
        )

        with tempfile.TemporaryDirectory(prefix="tc-deploy-") as tmp, ExitStack() as boot_assets:
            tmpdir = Path(tmp)
            generated_flash_config = tmpdir / "tcapsulesmb.conf"
            generated_smbpasswd = tmpdir / "smbpasswd"
            generated_username_map = tmpdir / "username.map"
            generated_flash_config.write_text(flash_config_text)
            smbpasswd_text, username_map_text = render_smbpasswd(config.require("TC_SAMBA_USER"), smb_password)
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

        print(f"Deployed Samba payload to {plan.payload_dir}")
        print("Updated /mnt/Flash boot files.")

        if is_netbsd4:
            print("Activating NetBSD4 payload without reboot.")
            command_context.set_stage("netbsd4_activation")
            run_remote_actions(connection, plan.activation_actions)
            if not verify_managed_runtime_flow(
                connection,
                command_context,
                stage="verify_runtime_activation",
                timeout_seconds=180,
                heading="Waiting for NetBSD 4 device activation, this can take a few minutes for Samba to start up...",
                failure_message="NetBSD4 activation failed.",
            ):
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
            device_name = airport_family_display_name_from_config(config)
            answer = input(f"This will reboot the {device_name} now. Continue? [Y/n]: ").strip().lower()
            if answer not in {"", "y", "yes"}:
                print("Deployment complete without reboot.")
                command_context.cancel_with_error("Cancelled by user at reboot confirmation prompt.")
                return 0

        if not request_reboot_and_wait(
            connection,
            command_context,
            reboot_no_down_message=REBOOT_NO_DOWN_MESSAGE,
        ):
            return 1
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
