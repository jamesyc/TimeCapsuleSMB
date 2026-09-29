from __future__ import annotations

import errno
import ipaddress
import socket
from dataclasses import dataclass
from typing import Literal

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.core.net import (
    endpoint_host,
    ipv6_literal,
    ipv6_scope_index,
    scoped_ip_literal,
)
from timecapsulesmb.device.probe import probe_ssh_command_conn
from timecapsulesmb.transport.local import tcp_connect_error
from timecapsulesmb.transport.ssh import SshConnection


RouteState = Literal["available", "unavailable", "unknown"]


@dataclass(frozen=True)
class RouteSelection:
    state: RouteState
    source: str | None = None
    error: str | None = None
    error_number: int | None = None


def _adapter_ip_text(value: object) -> str | None:
    scope = ""
    if isinstance(value, tuple):
        if len(value) >= 3 and value[2]:
            scope = str(value[2])
        value = value[0] if value else ""
    if not isinstance(value, str):
        return None
    if "%" in value:
        value, explicit_scope = value.split("%", 1)
        scope = explicit_scope or scope
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return None
    if parsed.version == 6 and parsed.is_link_local and scope:
        return f"{parsed}%{scope}"
    return str(parsed)


def local_interface_addresses() -> tuple[str, ...]:
    try:
        import ifaddr
        adapters = ifaddr.get_adapters()
    except Exception:
        return ()

    addresses: list[str] = []
    for adapter in adapters:
        adapter_name = str(getattr(adapter, "name", "") or getattr(adapter, "nice_name", ""))
        if adapter_name in {"lo", "lo0"}:
            continue
        for adapter_ip in getattr(adapter, "ips", []):
            ip_text = _adapter_ip_text(getattr(adapter_ip, "ip", None))
            if not ip_text:
                continue
            try:
                ip_obj = ipaddress.ip_address(ip_text.split("%", 1)[0])
            except ValueError:
                continue
            if ip_obj.is_loopback:
                continue
            if ip_obj.version == 6 and ip_obj.is_link_local and adapter_name:
                ip_text = f"{ip_obj}%{adapter_name}"
            if ip_text not in addresses:
                addresses.append(ip_text)
    return tuple(addresses)


_ROUTE_UNAVAILABLE_ERRNOS = {
    errno.EADDRNOTAVAIL,
    errno.EAFNOSUPPORT,
    errno.EHOSTUNREACH,
    errno.ENETDOWN,
    errno.ENETUNREACH,
}


def select_route_to_address(address: str, *, port: int = 445) -> RouteSelection:
    address_base, _, scope = address.partition("%")
    try:
        ip_obj = ipaddress.ip_address(address_base)
    except ValueError as exc:
        return RouteSelection("unknown", error=str(exc))

    family = socket.AF_INET6 if ip_obj.version == 6 else socket.AF_INET
    scope_id = ipv6_scope_index(scope) if family == socket.AF_INET6 and scope else 0
    if scope_id is None or (ip_obj.version == 6 and ip_obj.is_link_local and not scope_id):
        return RouteSelection("unavailable", error="no usable local IPv6 scope", error_number=errno.EADDRNOTAVAIL)
    destination = (address_base, port, 0, scope_id) if family == socket.AF_INET6 else (address_base, port)
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.connect(destination)
            sockname = sock.getsockname()
            source = scoped_ip_literal(sockname[0], scope_id=sockname[3] if family == socket.AF_INET6 else 0)
    except OSError as exc:
        state: RouteState = "unavailable" if exc.errno in _ROUTE_UNAVAILABLE_ERRNOS else "unknown"
        return RouteSelection(state, error=str(exc) or exc.__class__.__name__, error_number=exc.errno)

    if source is None or ipaddress.ip_address(source.split("%", 1)[0]).is_unspecified:
        return RouteSelection("unknown", error="kernel did not select a source address")
    return RouteSelection("available", source=source)


def find_relocated_device_addresses(
    configured_host: str,
    *,
    timeout_sec: float = 3.0,
) -> tuple[str, tuple[str, ...], ...]:
    """Find devices that advertise over mDNS but no longer answer at a host.

    A DHCP lease renewal moves the device to a new address while the
    configured one stops answering. The device stays healthy and keeps
    advertising, so browsing _smb._tcp can name the address the
    configuration lost. Returns (instance name, advertised addresses)
    pairs for every advertised device, newest browse result first.
    """
    from timecapsulesmb.discovery.bonjour import SMB_SERVICE
    from timecapsulesmb.discovery.native_dns_sd import (
        discover_native_dns_sd_snapshot_detailed,
    )

    discovered = discover_native_dns_sd_snapshot_detailed(
        SMB_SERVICE,
        timeout_sec=timeout_sec,
    )
    if discovered is None:
        return ()
    snapshot, _diagnostics = discovered
    found: list[tuple[str, tuple[str, ...]]] = []
    for service in snapshot.resolved:
        addresses: list[str] = []
        for candidate in list(service.ipv4) + list(service.ipv6):
            address = scoped_ip_literal(candidate) or ipv6_literal(candidate)
            if address and address not in addresses:
                addresses.append(address)
        if addresses:
            found.append((service.name, tuple(addresses)))
    return tuple(found)


def check_ssh_login(
    connection: SshConnection,
    *,
    relocation_hint: bool = False,
) -> CheckResult:
    result = probe_ssh_command_conn(
        connection,
        "/bin/echo ok",
        timeout=30,
        expected_stdout_suffix="ok",
    )
    if result.ok:
        return CheckResult("PASS", f"SSH command works for {connection.host}")
    if not result.detail.startswith("Connecting to the device failed, SSH error:"):
        failure = CheckResult("FAIL", f"SSH command failed for {connection.host}: {result.detail}")
        if relocation_hint and result.detail.startswith("Timed out"):
            hint = _relocated_device_hint(connection.host)
            if hint:
                return CheckResult("FAIL", f"{failure.message}. {hint}")
        return failure
    if relocation_hint and "Timed out" in result.detail:
        hint = _relocated_device_hint(connection.host)
        if hint:
            return CheckResult("FAIL", f"{result.detail}. {hint}")
    return CheckResult("FAIL", result.detail)


def _relocated_device_hint(configured: str) -> str:
    """Name the address a still-advertising device uses after a DHCP change."""
    host = endpoint_host(configured)
    for name, addresses in find_relocated_device_addresses(host):
        if host in addresses:
            continue
        return (
            f"The device is still advertising over mDNS as {name!r} at "
            f"{', '.join(addresses)}, so its address changed. Update TC_HOST "
            f"from {configured} to {addresses[0]}."
        )
    return ""


def check_smb_port(host: str) -> CheckResult:
    connect_error = tcp_connect_error(host, 445)
    if connect_error is None:
        return CheckResult("PASS", f"SMB reachable at {host}:445")
    return CheckResult("WARN", f"SMB not reachable at {host}:445 ({connect_error})", {"error": connect_error})
