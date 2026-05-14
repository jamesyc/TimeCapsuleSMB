from __future__ import annotations

import argparse
from contextlib import ExitStack
import ipaddress
import socket
import tempfile
from pathlib import Path
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.flows import request_reboot_and_wait, verify_managed_runtime_flow
from timecapsulesmb.cli.runtime import (
    add_config_argument,
    load_env_config,
    print_json,
    require_supported_device_compatibility,
)
from timecapsulesmb.core.config import (
    DEFAULTS,
    AppConfig,
    airport_family_display_name_from_config,
    extract_host,
    parse_bool,
    shell_quote,
)
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
    DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
    DEFAULT_ATA_IDLE_SECONDS,
    DEFAULT_DISKD_USE_VOLUME_ATTEMPTS,
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
    PayloadHome,
    PayloadVerificationResult,
    build_dry_run_payload_home,
    verify_payload_home_conn,
)
from timecapsulesmb.device.probe import (
    is_runtime_usable_ipv4,
    read_interface_ipv4_addrs_conn,
    runtime_usable_ipv4s,
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


def _ipv4_literal(value: str) -> str | None:
    value = value.strip()
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        parts = value.split(".")
        if len(parts) != 4 or any(not part.isdigit() for part in parts):
            return None
        octets: list[str] = []
        for part in parts:
            octet = int(part, 10)
            if octet < 0 or octet > 255:
                return None
            octets.append(str(octet))
        return ".".join(octets)
    if parsed.version != 4:
        return None
    return str(parsed)


def _resolve_host_ipv4s(host: str) -> tuple[str, ...]:
    if not host:
        return ()
    try:
        results = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return ()
    ordered: list[str] = []
    for result in results:
        sockaddr = result[4]
        if not sockaddr:
            continue
        ip_addr = sockaddr[0]
        if ip_addr and ip_addr not in ordered:
            ordered.append(ip_addr)
    return tuple(ordered)


def derive_net_ipv4_hint(config: AppConfig, iface_ipv4_addrs: tuple[str, ...]) -> str:
    usable_iface_ips = set(runtime_usable_ipv4s(iface_ipv4_addrs))
    if not usable_iface_ips:
        return ""
    host = extract_host(config.require("TC_HOST"))
    literal = _ipv4_literal(host)
    candidates = (literal,) if literal is not None else _resolve_host_ipv4s(host)
    for ip_addr in candidates:
        if is_runtime_usable_ipv4(ip_addr) and ip_addr in usable_iface_ips:
            return ip_addr
    return ""


def render_flash_runtime_config(
    config: AppConfig,
    payload_home: PayloadHome,
    *,
    nbns_enabled: bool,
    debug_logging: bool,
    net_ipv4_hint: str = "",
    ata_idle_seconds: int = DEFAULT_ATA_IDLE_SECONDS,
    diskd_use_volume_attempts: int = DEFAULT_DISKD_USE_VOLUME_ATTEMPTS,
) -> str:
    internal_root_default = config.get("TC_INTERNAL_SHARE_USE_DISK_ROOT", DEFAULTS["TC_INTERNAL_SHARE_USE_DISK_ROOT"])

    values: list[tuple[str, str | int]] = [
        ("TC_CONFIG_VERSION", 1),
        ("PAYLOAD_DIR_NAME", payload_home.payload_dir_name),
        ("NET_IFACE", config.require("TC_NET_IFACE")),
        ("NET_IPV4_HINT", net_ipv4_hint),
        ("SMB_SAMBA_USER", config.require("TC_SAMBA_USER")),
        ("MDNS_DEVICE_MODEL", config.get("TC_MDNS_DEVICE_MODEL", DEFAULTS["TC_MDNS_DEVICE_MODEL"])),
        ("AIRPORT_SYAP", config.get("TC_AIRPORT_SYAP", DEFAULTS["TC_AIRPORT_SYAP"])),
        ("INTERNAL_SHARE_USE_DISK_ROOT", 1 if parse_bool(internal_root_default) else 0),
        ("DISKD_USE_VOLUME_ATTEMPTS", diskd_use_volume_attempts),
        ("ATA_IDLE_SECONDS", ata_idle_seconds),
        ("NBNS_ENABLED", 1 if nbns_enabled else 0),
        ("SMBD_DEBUG_LOGGING", 1 if debug_logging else 0),
        ("MDNS_DEBUG_LOGGING", 1 if debug_logging else 0),
    ]
    return "\n".join(_render_flash_config_assignment(key, value) for key, value in values) + "\n"


def _payload_verification_error(payload_home: PayloadHome, result: PayloadVerificationResult) -> str:
    return f"managed payload verification failed at {payload_home.payload_dir}: {result.detail}"


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
        help=f"Seconds for deployment-time diskd.useVolume mount guards to wait before their manual fallback (default: {DEFAULT_APPLE_MOUNT_WAIT_SECONDS})",
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
        compatibility, compatibility_message = require_supported_device_compatibility(
            command_context,
            allow_unsupported=args.allow_unsupported,
            json_output=args.json,
        )
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
            mast_discovery = command_context.wait_for_mast_volumes(
                connection,
                attempts=MAST_DISCOVERY_ATTEMPTS,
                delay_seconds=MAST_DISCOVERY_DELAY_SECONDS,
            )
            mast_volumes = mast_discovery.volumes
            if not mast_volumes:
                raise SystemExit(
                    _no_mast_volumes_message(
                        attempts=MAST_DISCOVERY_ATTEMPTS,
                        delay_seconds=MAST_DISCOVERY_DELAY_SECONDS,
                    )
                )
            selection = command_context.select_payload_home(
                connection,
                mast_volumes,
                config.require("TC_PAYLOAD_DIR_NAME"),
                wait_seconds=apple_mount_wait_seconds,
            )
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

        if is_netbsd4 and not args.yes:
            print("Deploy will activate Samba immediately without rebooting.")
            print(color_red(NETBSD4_REBOOT_GUIDANCE))
            print(NETBSD4_REBOOT_FOLLOWUP)
            proceed = command_context.confirm_or_fail(
                "Continue with NetBSD 4 deploy + activation?",
                default=False,
                noninteractive_message="Running `deploy` requires confirmation when stdin is not interactive. Use `deploy --yes` in a non-interactive environment.",
            )
            if proceed is None:
                return 1
            if not proceed:
                print("Deployment cancelled.")
                command_context.cancel_with_error("Cancelled by user at NetBSD4 deploy confirmation prompt.")
                return 0

        command_context.set_stage("pre_upload_actions")
        run_remote_actions(connection, plan.pre_upload_actions)
        command_context.set_stage("prepare_deployment_files")
        iface_ipv4_addrs = read_interface_ipv4_addrs_conn(connection, config.require("TC_NET_IFACE"))
        net_ipv4_hint = derive_net_ipv4_hint(config, iface_ipv4_addrs)
        flash_config_text = render_flash_runtime_config(
            config,
            payload_home,
            nbns_enabled=nbns_enabled,
            debug_logging=args.debug_logging,
            net_ipv4_hint=net_ipv4_hint,
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

        command_context.set_stage("verify_payload_upload")
        payload_verification = verify_payload_home_conn(
            connection,
            payload_home,
            wait_seconds=apple_mount_wait_seconds,
        )
        command_context.add_debug_fields(payload_upload_verification=payload_verification.detail)
        if not payload_verification.ok:
            raise SystemExit(_payload_verification_error(payload_home, payload_verification))

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
            raise SystemExit(_payload_verification_error(payload_home, payload_verification))

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
            proceed = command_context.confirm_or_fail(
                f"This will reboot the {device_name} now. Continue?",
                default=True,
                noninteractive_message="Running `deploy` with reboot requires confirmation when stdin is not interactive. Use `deploy --yes` to skip the prompt or `deploy --no-reboot`.",
            )
            if proceed is None:
                return 1
            if not proceed:
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
