from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, Union

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.core.net import endpoint_host, ipv4_literal, ipv6_literal, resolve_host_ips
from timecapsulesmb.device import acpd
from timecapsulesmb.device.probe import probe_ssh_command_conn
from timecapsulesmb.integrations.acp import ACP_PORT
from timecapsulesmb.transport.errors import (
    SshAlgorithmNegotiationError,
    SshAuthenticationError,
    SshClientError,
    SshLocalNetworkFilteredError,
    TransportError,
    local_network_filtered_message,
)
from timecapsulesmb.transport.local import connect_answered, connect_unanswered, tcp_connect_error
from timecapsulesmb.transport.ssh import SshConnection


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


@dataclass(frozen=True)
class LocalInterfaceNetwork:
    interface: str
    address: str
    network: ipaddress.IPv4Network | ipaddress.IPv6Network


def local_interface_networks(adapters: Iterable[object] | None = None) -> tuple[LocalInterfaceNetwork, ...]:
    """The networks this computer's interfaces are on, in both families.

    Loopback and link-local networks are left out: every interface has a
    link-local network, so they say nothing about which LAN it is on.
    """
    if adapters is None:
        try:
            import ifaddr
            adapters = ifaddr.get_adapters()
        except Exception:
            return ()

    networks: list[LocalInterfaceNetwork] = []
    for adapter in adapters:
        adapter_name = str(getattr(adapter, "name", "") or getattr(adapter, "nice_name", ""))
        for adapter_ip in getattr(adapter, "ips", []):
            ip_text = _adapter_ip_text(getattr(adapter_ip, "ip", None))
            prefix = getattr(adapter_ip, "network_prefix", None)
            if not ip_text or not isinstance(prefix, int):
                continue
            base = ip_text.split("%", 1)[0]
            try:
                interface = ipaddress.ip_interface(f"{base}/{prefix}")
            except ValueError:
                continue
            if interface.ip.is_loopback or interface.ip.is_link_local:
                continue
            entry = LocalInterfaceNetwork(adapter_name, str(interface.ip), interface.network)
            if entry not in networks:
                networks.append(entry)
    return tuple(networks)


def interface_kind(name: str) -> str:
    lowered = name.lower()
    if lowered.startswith(("utun", "ipsec", "ppp", "wg", "tun", "tap", "tailscale", "zt")):
        return "vpn"
    if lowered.startswith(("bridge", "vmnet", "vboxnet", "docker", "veth", "virbr", "br-")):
        return "virtual"
    if lowered.startswith(("en", "eth", "wl")):
        return "lan"
    return "other"


def local_lan_networks(adapters: Iterable[object] | None = None) -> tuple[LocalInterfaceNetwork, ...]:
    """This computer's networks without its VPN tunnels and virtual machine or
    container bridges.

    Their networks can overlap one of a device's (a VPN's 100.64.0.0/10 and a
    carrier-NAT WAN address, say) without putting this computer on it.
    Interfaces of unknown kind stay: Linux names a plain bridge `br0`, and
    Windows names every adapter by GUID.
    """
    return tuple(
        item for item in local_interface_networks(adapters)
        if interface_kind(item.interface) not in ("vpn", "virtual")
    )


IpNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]
NetworkLinkVerdict = Literal["shared", "separate", "unknown"]
_SHARED_IPV4 = ipaddress.ip_network("100.64.0.0/10")
_UNIQUE_LOCAL_IPV6 = ipaddress.ip_network("fc00::/7")


@dataclass(frozen=True)
class NetworkLinkResult:
    """Whether this computer is on any network the device is on.

    Bonjour (multicast DNS) and Apple's NBNS only reach clients on the
    device's own networks, unless a router relays them. `separate` means some
    address family has networks on both sides and no pair overlaps;
    `unknown` means no family could be compared.
    """

    verdict: NetworkLinkVerdict
    device_networks: tuple[IpNetwork, ...] = ()
    local_networks: tuple[IpNetwork, ...] = ()
    source: str = "none"
    detail: str | None = None

    @property
    def families(self) -> tuple[str, ...]:
        device = {network.version for network in self.device_networks}
        local = {network.version for network in self.local_networks}
        return tuple(f"ipv{version}" for version in sorted(device & local))

    def compared(self) -> tuple[tuple[IpNetwork, ...], tuple[IpNetwork, ...]]:
        """Both sides' networks in the families that were compared."""
        versions = {int(family[-1]) for family in self.families}
        return (
            tuple(network for network in self.local_networks if network.version in versions),
            tuple(network for network in self.device_networks if network.version in versions),
        )

    def telemetry(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "source": self.source,
            "families": list(self.families),
            "device_networks": [reportable_network(network) for network in self.device_networks],
            # A full-length local prefix is this computer's own address.
            "local_networks": [reportable_network(network, hide_hosts=True) for network in self.local_networks],
            "detail": self.detail,
        }


def reportable_network(network: IpNetwork, *, hide_hosts: bool = False) -> str:
    """A network as telemetry carries it: private, shared and unique local
    ranges in full, public ones by family and prefix length only."""
    address = network.network_address
    private = address in _UNIQUE_LOCAL_IPV6 if address.version == 6 else (address in _SHARED_IPV4 or address.is_private)
    if private and not (hide_hosts and network.prefixlen == network.max_prefixlen):
        return str(network)
    kind = "public" if not private else "host"
    return f"ipv{address.version}-{kind}/{network.prefixlen}"


def network_display(network: IpNetwork) -> str:
    return str(network.network_address) if network.prefixlen == network.max_prefixlen else str(network)


def host_networks(host: str) -> list[IpNetwork]:
    """A device address, or every address its hostname resolves to, as
    host-length networks. Link-local addresses are left out: every interface
    has a link-local network, so they cannot say which network a host is on."""
    text = host.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    literal = ipv4_literal(text) or ipv6_literal(text)
    addresses = [literal] if literal is not None else [address.partition("%")[0] for address in resolve_host_ips(host)]
    return [
        network for network in (ipaddress.ip_network(address) for address in addresses)
        if not network.network_address.is_link_local
    ]


def classify_network_link(
    device_networks: Iterable[IpNetwork],
    local_networks: Iterable[IpNetwork],
    *,
    source: str,
    detail: str | None = None,
) -> NetworkLinkResult:
    device = tuple(dict.fromkeys(device_networks))
    local = tuple(dict.fromkeys(local_networks))
    comparable = {network.version for network in device} & {network.version for network in local}
    verdict: NetworkLinkVerdict
    if any(left.version == right.version and left.overlaps(right) for left in device for right in local):
        verdict = "shared"
    else:
        verdict = "separate" if comparable else "unknown"
    return NetworkLinkResult(verdict, device, local, source, detail)


PORT_CHECK_TIMEOUT_SECONDS = 2.0
SSH_PORT = 22


@dataclass(frozen=True)
class ClosedSshPort:
    """Why SSH port 22 is closed at `host`: SSH is off while ACP answers
    (`acp_error` None); the device answers on port 22 but ACP's port goes
    unanswered (`acp_stuck`, with `ssh_accepted` when port 22 connected rather
    than refused), so Apple's ACPd is stuck; or nothing answers there at all."""

    host: str
    acp_error: str | None
    acp_stuck: bool = False
    ssh_accepted: bool = False

    @property
    def code(self) -> ErrorCode:
        if self.acp_error is None:
            return ErrorCode.SSH_DISABLED
        return ErrorCode.ACPD_BROKEN if self.acp_stuck else ErrorCode.DEVICE_UNREACHABLE

    @property
    def _ssh_answer(self) -> str:
        return f"SSH port {SSH_PORT} {'accepted' if self.ssh_accepted else 'refused'} a connection"

    @property
    def message(self) -> str:
        """An error's message."""
        if self.acp_error is None:
            return "SSH is turned off on the device."
        if self.acp_stuck:
            return "The device answers on the network, but its AirPort service does not."
        return "The device is not answering."

    @property
    def detail(self) -> str:
        """An error's facts."""
        if self.acp_error is None:
            return f"AirPort ACP answers at {self.host} on port {ACP_PORT}, but SSH port {SSH_PORT} is closed."
        if self.acp_stuck:
            return f"AirPort ACP port {ACP_PORT} does not answer at {self.host} ({self.acp_error}), while {self._ssh_answer}."
        return f"Neither SSH port {SSH_PORT} nor AirPort ACP port {ACP_PORT} is reachable at {self.host} ({self.acp_error})."

    @property
    def line(self) -> str:
        """Checkup's result line."""
        if self.acp_error is None:
            return f"SSH is turned off on {self.host}: AirPort ACP answers on port {ACP_PORT}, but SSH port {SSH_PORT} is closed."
        if self.acp_stuck:
            broken = acpd.acpd_broken_message("the AirPort service does not answer the network", note=self.detail)
            return f"{broken} {acpd.ACPD_FIX}, then check again"
        return (
            f"The device is not answering at {self.host}: neither SSH port {SSH_PORT} nor AirPort ACP port {ACP_PORT} "
            f"is reachable ({self.acp_error})."
        )

    @property
    def details(self) -> dict[str, object]:
        if self.acp_error is None:
            return {}
        if self.acp_stuck:
            return {"acp_port_error": self.acp_error, "acpd_problem": "acp_port_unanswered"}
        return {"acp_port_error": self.acp_error}


def closed_ssh_port_error(host: str, *, ssh_error: str | None = None) -> ClosedSshPort:
    """Why SSH port 22 is closed.

    The user fixes the three causes differently: SSH is off (after a reset, or
    turned off in SSH Access) while the device still answers AirPort ACP; ACPd
    is stuck, which only unplugging the device clears; or nothing answers at
    this address at all (the device is off, or DHCP gave it another address).
    One TCP connect to the ACP port, and to SSH's when ACP's goes unanswered
    (transport.local.connect_unanswered), tells them apart.

    `ssh_error` is port 22's failed connect when the caller has one. Unlike
    tcp_connect_error's None, None here means there is none: port 22 is then
    tried again, and a connect that now succeeds still shows the device is up.
    """
    acp_error = tcp_connect_error(host, ACP_PORT, PORT_CHECK_TIMEOUT_SECONDS)
    if not connect_unanswered(acp_error):
        return ClosedSshPort(host, acp_error)
    if ssh_error is None:
        ssh_error = tcp_connect_error(host, SSH_PORT, PORT_CHECK_TIMEOUT_SECONDS)
    return ClosedSshPort(host, acp_error, acp_stuck=connect_answered(ssh_error), ssh_accepted=ssh_error is None)


def check_ssh_login(connection: SshConnection) -> CheckResult:
    result = probe_ssh_command_conn(
        connection,
        "/bin/echo ok",
        timeout=30,
        expected_stdout_suffix="ok",
    )
    if result.ok:
        return CheckResult("PASS", f"SSH command works for {connection.host}")
    details = {"code": ssh_login_failure_code(result.error)}
    if details["code"] == "ssh_unreachable" and (unanswered := _unanswered_ssh_port(connection.host)) is not None:
        return unanswered
    if result.detail.startswith(("Connecting to the device failed, SSH error:", local_network_filtered_message())):
        return CheckResult("FAIL", result.detail, details)
    return CheckResult("FAIL", f"SSH command failed for {connection.host}: {result.detail}", details)


def _unanswered_ssh_port(target: str) -> CheckResult | None:
    """Why SSH did not get through, when its port does not answer.

    ssh waits out its own connect timeout, so an address nothing answers at
    otherwise looks like a command that timed out. None when port 22 answers:
    the session itself is what hung.
    """
    host = endpoint_host(target)
    ssh_error = tcp_connect_error(host, SSH_PORT, PORT_CHECK_TIMEOUT_SECONDS)
    if ssh_error is None:
        return None
    closed = closed_ssh_port_error(host, ssh_error=ssh_error)
    # A stuck ACPd gets the ACPd step's own code, which needs SSH and does not run.
    code = acpd.APPLE_ACPD_BROKEN_CODE if closed.acp_stuck else closed.code
    return CheckResult("FAIL", closed.line, {"code": code, "ssh_port_error": ssh_error, **closed.details})


def ssh_login_failure_code(error: TransportError | None) -> str:
    """Name who an SSH login failure points at, for Checkup and telemetry:
    a VPN or security app on this computer (a crashed ssh included), this
    computer's ssh itself, the saved password, no algorithm both ends accept,
    or a device or network that did not answer. Without an error, ssh ran and
    the command failed.
    """
    if isinstance(error, SshLocalNetworkFilteredError):
        return "local_network_filtered"
    if isinstance(error, SshClientError):
        return "ssh_client_failed"
    if isinstance(error, SshAuthenticationError):
        return "ssh_auth_rejected"
    if isinstance(error, SshAlgorithmNegotiationError):
        return "ssh_algorithm_negotiation_failed"
    if error is not None:
        return "ssh_unreachable"
    return "ssh_command_failed"


def check_smb_port(host: str) -> CheckResult:
    connect_error = tcp_connect_error(host, 445)
    if connect_error is None:
        return CheckResult("PASS", f"SMB reachable at {host}:445")
    return CheckResult("WARN", f"SMB not reachable at {host}:445 ({connect_error})", {"error": connect_error})
