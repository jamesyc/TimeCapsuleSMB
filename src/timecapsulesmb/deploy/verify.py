from __future__ import annotations

import ipaddress
import time

from timecapsulesmb.checks.bonjour import run_bonjour_checks
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.checks.smb import try_authenticated_smb_listing
from timecapsulesmb.deploy.planner import UninstallPlan
from timecapsulesmb.device.probe import (
    netbsd4_runtime_services_healthy_conn,
    probe_managed_mdns_takeover_conn,
    probe_managed_smbd_conn,
    probe_paths_absent_conn,
    probe_netbsd4_activation_status_conn,
)
from timecapsulesmb.transport.local import command_exists
from timecapsulesmb.transport.ssh import SshConnection


def _connection_from_args(connection_or_host: SshConnection | str, password: str | None, ssh_opts: str | None) -> SshConnection:
    if isinstance(connection_or_host, SshConnection):
        return connection_or_host
    if password is None or ssh_opts is None:
        raise TypeError("password and ssh_opts are required when passing host string")
    return SshConnection(connection_or_host, password, ssh_opts)


def _configured_smb_server(host_label: str) -> str:
    value = host_label.strip()
    if not value:
        return value
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if "." in value:
        return value
    return f"{value}.local"


def wait_for_post_reboot_smbd(connection: SshConnection | str, password: str | None = None, ssh_opts: str | None = None, *, timeout_seconds: int = 120) -> bool:
    connection = _connection_from_args(connection, password, ssh_opts)
    return probe_managed_smbd_conn(connection, timeout_seconds=timeout_seconds).ready


def wait_for_post_reboot_mdns_takeover(connection: SshConnection | str, password: str | None = None, ssh_opts: str | None = None, *, timeout_seconds: int = 120) -> bool:
    connection = _connection_from_args(connection, password, ssh_opts)
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if probe_managed_mdns_takeover_conn(connection, timeout_seconds=min(20, max(5, int(remaining) + 1))).ready:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(5.0, remaining))


def wait_for_post_reboot_mdns_ready(
    connection: SshConnection | str,
    password: str | None = None,
    ssh_opts: str | None = None,
    expected_instance_name: str | None = None,
    *,
    timeout_seconds: float = 120.0,
    poll_interval_seconds: float = 5.0,
) -> bool:
    connection = _connection_from_args(connection, password, ssh_opts)
    if expected_instance_name is None:
        raise TypeError("expected_instance_name is required")
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False

        if probe_managed_mdns_takeover_conn(
            connection,
            timeout_seconds=min(20, max(5, int(remaining) + 1)),
        ).ready:
            browse_timeout = min(5.0, remaining)
            _, discovered_instance, target = run_bonjour_checks(
                expected_instance_name,
                timeout=browse_timeout,
            )
            if discovered_instance and target:
                return True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(poll_interval_seconds, remaining))


def wait_for_post_reboot_bonjour(
    expected_instance_name: str,
    *,
    timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 2.0,
) -> tuple[list[CheckResult], str | None, str | None]:
    deadline = time.monotonic() + timeout_seconds
    last_results = []
    last_instance = None
    last_target = None

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return last_results, last_instance, last_target

        browse_timeout = min(5.0, remaining)
        results, discovered_instance, target = run_bonjour_checks(
            expected_instance_name,
            timeout=browse_timeout,
        )
        last_results, last_instance, last_target = results, discovered_instance, target
        if discovered_instance and target:
            return results, discovered_instance, target

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return last_results, last_instance, last_target
        time.sleep(min(poll_interval_seconds, remaining))


def verify_post_deploy(values: dict[str, str]) -> None:
    samba_user = values["TC_SAMBA_USER"]
    password = values["TC_PASSWORD"]
    host_label = values["TC_MDNS_HOST_LABEL"]

    print("Post-deploy verification:")

    try:
        _, discovered_instance, target = wait_for_post_reboot_bonjour(values["TC_MDNS_INSTANCE_NAME"])
        if discovered_instance:
            print(f"  Advertised service name: {discovered_instance}")
        else:
            print("  Advertised service name: not found")
        if target:
            print(f"  Advertised hostname: {target}")
        else:
            print("  Advertised hostname: not resolved")
    except Exception as e:
        print(f"  Bonjour verification failed: {e}")

    if command_exists("smbclient"):
        servers = [_configured_smb_server(host_label)]
        tc_host = values.get("TC_HOST", "")
        if "@" in tc_host:
            tc_host = tc_host.split("@", 1)[1]
        if tc_host and tc_host not in servers:
            servers.append(tc_host)
        result = try_authenticated_smb_listing(samba_user, password, servers)
        if result.status == "PASS":
            server = result.message.removeprefix("authenticated SMB listing works for ")
            print(f"  Authenticated SMB listing: ok ({server})")
        else:
            failure = result.message.removeprefix("authenticated SMB listing failed: ")
            print(f"  Authenticated SMB listing: failed ({failure})")
    else:
        print("  SMB listing verification skipped: smbclient not found")


def verify_netbsd4_activation(connection: SshConnection | str, password: str | None = None, ssh_opts: str | None = None, *, timeout_seconds: int = 180) -> bool:
    connection = _connection_from_args(connection, password, ssh_opts)
    print("NetBSD4 activation verification:")
    proc = probe_netbsd4_activation_status_conn(connection, timeout_seconds=timeout_seconds)
    for line in proc.stdout.strip().splitlines():
        if line.startswith("PASS:"):
            print(f"  ok: {line.removeprefix('PASS:')}")
        elif line.startswith("FAIL:"):
            print(f"  failed: {line.removeprefix('FAIL:')}")
        elif line:
            print(f"  {line}")
    return proc.returncode == 0


def netbsd4_activation_is_already_healthy(connection: SshConnection | str, password: str | None = None, ssh_opts: str | None = None) -> bool:
    connection = _connection_from_args(connection, password, ssh_opts)
    return netbsd4_runtime_services_healthy_conn(connection)


def verify_post_uninstall(connection: SshConnection | str, password_or_plan, ssh_opts: str | None = None, plan: UninstallPlan | None = None) -> bool:
    if isinstance(connection, SshConnection):
        resolved_connection = connection
        resolved_plan = password_or_plan
    else:
        resolved_connection = _connection_from_args(connection, password_or_plan, ssh_opts)
        if plan is None:
            raise TypeError("plan is required when passing host string")
        resolved_plan = plan
    print("Post-uninstall verification:")
    proc = probe_paths_absent_conn(resolved_connection, resolved_plan.verify_absent_targets)

    ok = proc.returncode == 0
    for line in proc.stdout.strip().splitlines():
        if line.startswith("ABSENT:"):
            print(f"  ok: removed {line.removeprefix('ABSENT:')}")
        elif line.startswith("PRESENT:"):
            print(f"  failed: still present {line.removeprefix('PRESENT:')}")
    return ok
