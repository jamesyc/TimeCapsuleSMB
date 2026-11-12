from __future__ import annotations

import ipaddress
import os
import socket
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal, Union

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.core.install import running_in_app, this_computer
from timecapsulesmb.core.net import (
    endpoint_host,
    ipv4_literal,
    ipv6_literal,
    ipv6_scope_index,
    is_link_local_ipv6,
    resolve_host_ips,
)
from timecapsulesmb.device import acpd
from timecapsulesmb.device.probe import SshCommandProbeResult, probe_ssh_command_conn
from timecapsulesmb.integrations.acp import ACP_PORT
from timecapsulesmb.transport.errors import (
    SshAlgorithmNegotiationError,
    SshAuthenticationError,
    SshClientError,
    SshLocalNetworkFilteredError,
    TransportError,
    local_network_filtered_message,
)
from timecapsulesmb.transport.local import (
    Router,
    connect_answered,
    connect_error_kind,
    connect_unanswered,
    lan_routers,
    normalize_hardware_address,
    tcp_connect_error,
)
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


PlaceVerdict = Literal["same", "moved", "offline", "unknown"]


@dataclass(frozen=True)
class NetworkPlace:
    """Where this computer is: its LAN networks (local_lan_networks) and the
    default router on each LAN interface (transport.local.lan_routers), as
    read at one moment."""

    networks: tuple[LocalInterfaceNetwork, ...]
    routers: tuple[Router, ...]

    @property
    def router_macs(self) -> frozenset[str]:
        return frozenset(router.mac for router in self.routers if router.mac)

    def router_toward(self, device_networks: Iterable[IpNetwork]) -> Router | None:
        """The router on an interface whose network holds one of the device's
        addresses: the network this computer reaches the device on. A
        full-tunnel VPN takes the system default route, never this one."""
        device = tuple(device_networks)
        interfaces = [
            item.interface for item in self.networks
            if any(item.network.version == network.version and item.network.overlaps(network) for network in device)
        ]
        for interface in interfaces:
            for router in self.routers:
                if router.interface == interface:
                    return router
        return None

    def routes_through(self, device_networks: Iterable[IpNetwork]) -> bool:
        """Whether one of this computer's routers holds one of the device's
        addresses: a Time Capsule in router mode, whose restart takes the route
        toward it away."""
        addresses = {str(network.network_address) for network in device_networks}
        return any(router.ip in addresses for router in self.routers)


def read_network_place(
    *,
    networks: Callable[[], tuple[LocalInterfaceNetwork, ...]] | None = None,
    routers: Callable[[], tuple[Router, ...]] | None = None,
) -> NetworkPlace | None:
    """This computer's place now; None when its interfaces cannot be read.
    VPN and virtual interfaces' routers are left out, as their networks are
    (interface_kind)."""
    try:
        lan = (networks or local_lan_networks)()
        found = (routers or lan_routers)()
    except Exception:
        return None
    return NetworkPlace(
        tuple(lan),
        tuple(router for router in found if interface_kind(router.interface) not in ("vpn", "virtual")),
    )


@dataclass(frozen=True)
class PlaceComparison:
    """Whether this computer is on the network it reached the device on.

    `verdict`: `same`, the router toward the device is the one it was;
    `moved`, no network holds the device's address, or that network's router
    is another one this computer did not already have; `offline`, this
    computer has no LAN address at all; `unknown`, nothing settles it, such as
    an unreadable router. `evidence` names what decided it.
    """

    verdict: PlaceVerdict
    evidence: str | None = None
    router_before: str | None = None
    router_now: str | None = None

    def telemetry(self) -> dict[str, object]:
        fields: dict[str, object] = {"client_place": self.verdict}
        if self.evidence is not None:
            fields["place_evidence"] = self.evidence
        if self.router_before is not None:
            fields["router_mac_before"] = self.router_before
        if self.router_now is not None:
            fields["router_mac_now"] = self.router_now
        return fields


def compare_place(
    now: NetworkPlace | None,
    device_networks: Iterable[IpNetwork],
    *,
    router_mac: str | None,
    known_router_macs: frozenset[str] = frozenset(),
    known_networks: Iterable[IpNetwork] | None = None,
    device_is_router: bool = False,
) -> PlaceComparison:
    """Compare this computer's place `now` with where it reached the device:
    `router_mac`, the router toward the device then. `known_router_macs` and
    `known_networks` are every router and network it had then, when known:
    leaving for one of them is not moving, as a Mac on the device's Ethernet
    and on another Wi-Fi network is only left on that Wi-Fi when the restart
    drops the Ethernet link. `device_is_router`: the device was this
    computer's router (router mode), so while it is down there is no router
    toward it, and a computer that stayed has no other."""
    if now is None:
        return PlaceComparison("unknown", router_before=router_mac)
    if not now.networks:
        return PlaceComparison("offline", "no_address", router_before=router_mac)
    device = tuple(device_networks)
    if not device:
        # A saved name that no longer resolves, as a .local name does while
        # the device is off, and also while it is up on a network that drops
        # multicast: the router alone would tell a working device to be
        # unplugged.
        return PlaceComparison("unknown", "unresolved", router_before=router_mac)
    link = classify_network_link(device, [item.network for item in now.networks], source="place")
    if link.verdict == "separate":
        local, _device = link.compared()
        if known_networks is not None and set(local) <= set(known_networks):
            return PlaceComparison("unknown", "known_networks", router_before=router_mac)
        return PlaceComparison("moved", "subnet", router_before=router_mac)
    router = now.router_toward(device)
    current = router.mac if router is not None else None
    if device_is_router:
        if current is None:
            # Still routed through the device, which does not answer for its
            # address: this computer has no other router to have moved to.
            return PlaceComparison("same", "device_is_router", router_mac, current)
        if router_mac is None:
            # A router answers, and nothing says whether it is the device's.
            return PlaceComparison("unknown", "device_is_router", router_mac, current)
        if current == router_mac or current in known_router_macs:
            return PlaceComparison("same", "device_is_router", router_mac, current)
        return PlaceComparison("moved", "router", router_mac, current)
    if current is None or router_mac is None:
        return PlaceComparison("unknown", router_before=router_mac, router_now=current)
    if current == router_mac:
        return PlaceComparison("same", "router", router_mac, current)
    if current in known_router_macs:
        return PlaceComparison("unknown", "known_router", router_mac, current)
    return PlaceComparison("moved", "router", router_mac, current)


PORT_CHECK_TIMEOUT_SECONDS = 2.0
SSH_PORT = 22
# Apple's ssh, asked whether the device is up when this program's own
# connections get no route (local_network_block's proof): a reachable device
# answers within seconds. Its login has transport.ssh.SSH_LOGIN_SECONDS on top.
SSH_PROOF_TIMEOUT_SECONDS = 15


def macos_may_hold_this_process(*, platform: str | None = None, environ: dict[str, str] | None = None) -> bool:
    """Whether macOS's Local Network access may refuse this process's
    connections to the LAN: it holds apps, this one included, and every
    terminal app but Terminal, a command's connections counting as its
    terminal app's (CLI_TERMINAL_APP_BLOCKED). A command run over SSH is not
    held, and Linux has no such access."""
    if (platform or sys.platform) != "darwin":
        return False
    if running_in_app():
        return True
    env = os.environ if environ is None else environ
    return env.get("TERM_PROGRAM") != "Apple_Terminal" and not env.get("SSH_CONNECTION")


# What a terminal app macOS holds gets for every connect to the LAN: no route,
# and a .local name that does not resolve, its mDNS lookups being refused too.
_HELD_TERMINAL_KINDS = frozenset({"no_route", "name_lookup_failed"})


def terminal_may_be_held(
    errors: Iterable[str | None], *, platform: str | None = None, environ: dict[str, str] | None = None,
) -> bool:
    """Whether a command outside the app got connect errors that its terminal
    app being held by Local Network access would give
    (macos_may_hold_this_process): they say nothing about whether the device
    is up, so they are not a silent device. Nothing outside the app can
    prove such a block (local_network_block_possible), so the error keeps its
    generic code, whose card names the terminal app. The app proves or rules
    out its own block before it judges a silent device."""
    if running_in_app() or not macos_may_hold_this_process(platform=platform, environ=environ):
        return False
    kinds = {kind for error in errors if error for kind in (connect_error_kind(error) or "").split("+")}
    return bool(kinds & _HELD_TERMINAL_KINDS)


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
    def silent(self) -> bool:
        """Nothing answered at the address: a refusal on the ACP port is a host
        that is up (the device with ACPd gone, or another one that took the
        address), so only a device that is not answered at all is judged by
        where this computer is (silent_device)."""
        return (
            self.code == ErrorCode.DEVICE_UNREACHABLE and not connect_answered(self.acp_error)
            and not terminal_may_be_held((self.acp_error,))
        )

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


@dataclass(frozen=True)
class SilentDevice:
    """A device that answers nothing at its address (ClosedSshPort's
    device_unreachable), with where this computer is against the network it
    last reached the device on (compare_place against `router_mac`, the
    router toward the device then)."""

    closed: ClosedSshPort
    place: PlaceComparison
    platform: str | None = None

    @property
    def code(self) -> ErrorCode:
        if self.place.verdict == "same":
            return ErrorCode.DEVICE_SILENT
        if self.place.verdict == "moved":
            return ErrorCode.DEVICE_OFF_NETWORK
        return ErrorCode.DEVICE_UNREACHABLE

    @property
    def place_sentence(self) -> str:
        computer = this_computer(self.platform)
        capitalized = this_computer(self.platform, capitalized=True)
        if self.place.verdict == "same":
            return f"{capitalized} is on the same network as when it last reached the device."
        if self.place.verdict == "moved" and self.place.evidence == "router":
            return (
                f"{capitalized} is on a different network than when it last reached the device, "
                "although the addresses look alike."
            )
        if self.place.verdict == "moved":
            return f"The device's address is not on {computer}'s network."
        if self.place.verdict == "offline":
            return f"{capitalized} has no network connection."
        return ""

    @property
    def message(self) -> str:
        if self.place.verdict == "same":
            return "The device is not answering on this network."
        if self.place.verdict == "moved":
            return self.place_sentence
        if self.place.verdict == "offline":
            return f"The device is not answering, and {this_computer(self.platform)} has no network connection."
        return self.closed.message

    @property
    def detail(self) -> str:
        return f"{self.closed.detail} {self.place_sentence}".rstrip()

    @property
    def line(self) -> str:
        """Checkup's result line."""
        return f"{self.closed.line} {self.place_sentence}".rstrip()

    @property
    def details(self) -> dict[str, object]:
        return {**self.closed.details, **self.place.telemetry()}


def saved_router_mac(config: AppConfig | None) -> str | None:
    """This computer's router toward the device when it last reached it
    (TC_LAN_ROUTER_MAC: configure's, or the app's newer one), for telling a
    device that answers nothing from this computer being on another network.
    None without a config (a command that has not loaded one)."""
    value = config.get("TC_LAN_ROUTER_MAC") if config is not None else None
    return normalize_hardware_address(value) if value else None


def router_mac_toward(host: str, *, place: NetworkPlace | None = None) -> str | None:
    """The hardware address of this computer's router toward `host` now: the
    default router on the interface whose network holds it. None for a host
    on none of this computer's networks, or when the router cannot be read."""
    now = place if place is not None else read_network_place()
    if now is None:
        return None
    try:
        device = host_networks(host)
    except Exception:
        return None
    router = now.router_toward(device)
    return router.mac if router is not None else None


def silent_device(
    closed: ClosedSshPort,
    router_mac: str | None,
    *,
    place: NetworkPlace | None = None,
    platform: str | None = None,
) -> SilentDevice:
    """Where this computer is, for a device that answers nothing at
    `closed.host`. `place` is this computer's place now (read_network_place
    by default). A device whose address is one of this computer's routers is
    in router mode: while it is down there is no router toward it."""
    now = place if place is not None else read_network_place()
    try:
        device = host_networks(closed.host)
    except Exception:
        device = []
    device_is_router = now is not None and now.routes_through(device)
    return SilentDevice(
        closed, compare_place(now, device, router_mac=router_mac, device_is_router=device_is_router), platform,
    )


def link_local_interface_gone(host: str, addresses: Iterable[str] | None = None) -> str | None:
    """The interface a link-local IPv6 address names, when this computer no
    longer has it or it has no IPv6 link-local address of its own: a USB
    Ethernet adapter unplugged since the device was added, say. Connects to
    such an address fail with No route to host, as a Local Network block's
    do. None for any other address, or when the interfaces cannot be read.
    `addresses` is local_interface_addresses()."""
    base, _, scope = endpoint_host(host).strip("[]").partition("%")
    if not scope or not is_link_local_ipv6(base):
        return None
    if scope.isdigit():
        try:
            scope = socket.if_indextoname(int(scope))
        except (OSError, ValueError, OverflowError):
            return scope
    if ipv6_scope_index(scope) is None:
        return scope
    addresses = local_interface_addresses() if addresses is None else tuple(addresses)
    if not addresses:
        return None
    for address in addresses:
        local_base, _, local_scope = address.partition("%")
        if local_scope == scope and is_link_local_ipv6(local_base):
            return None
    return scope


@dataclass(frozen=True)
class LinkLocalInterfaceGone:
    """A link-local address on an interface this computer no longer
    has (link_local_interface_gone)."""

    host: str
    interface: str
    platform: str | None = None
    code = ErrorCode.LINK_LOCAL_INTERFACE_GONE

    @property
    def _computer(self) -> str:
        return this_computer(self.platform)

    @property
    def message(self) -> str:
        return f"The address {self.host} is on a network connection {self._computer} no longer has ({self.interface})."

    @property
    def detail(self) -> str:
        return f"{self.interface} is not one of {self._computer}'s network interfaces with an IPv6 link-local address."

    @property
    def line(self) -> str:
        """Checkup's result line."""
        return (
            f"{self.message[:-1]}: reconnect the adapter or cable used when the device was added, or add the "
            "device again so its current address is saved"
        )


def ssh_reached_device(result: SshCommandProbeResult) -> bool:
    """Whether ssh reached the device's TCP stack: it logged in (the command
    may still have failed), the device rejected the login or offered no
    algorithm both ends accept, or it refused the port (SSH off). In
    telemetry, /usr/bin/ssh reached devices that this program's own
    connections could not (Errno 65), so it is independent evidence that the
    device is up."""
    if result.ok or result.error is None:
        return True
    if isinstance(result.error, (SshAuthenticationError, SshAlgorithmNegotiationError)):
        return True
    return "refused" in (connect_error_kind(str(result.error)) or "").split("+")


# What a command outside the app says when AirPort Utility may see a device it
# cannot reach. Terminal.app and SSH are exempt from Local Network access;
# another terminal app is not, and a command's connections count as that app's.
CLI_TERMINAL_APP_BLOCKED = (
    "If AirPort Utility can see the device but this command cannot, and it runs in a terminal app other than "
    "Terminal, such as iTerm2 or Visual Studio Code, macOS is blocking that app: turn it on in System Settings > "
    "Privacy & Security > Local Network"
)


# What shows the device is up when this program's own connections get no route.
LOCAL_NETWORK_BLOCK_PROOFS = {
    "ssh": "SSH reaches it",
    "bonjour": "Bonjour finds it",
}


@dataclass(frozen=True)
class LocalNetworkBlock:
    """macOS refuses the app's own connections to the device while something
    else shows the device is up (local_network_block). macOS holds the app
    responsible for its helper's connections."""

    host: str
    proof: str
    errors: tuple[str, ...]
    code = ErrorCode.ACP_NO_ROUTE

    @property
    def message(self) -> str:
        return (
            "macOS refuses TimeCapsuleSMB's connections to the device, although the device is on the network "
            f"({LOCAL_NETWORK_BLOCK_PROOFS[self.proof]})."
        )

    @property
    def detail(self) -> str:
        return f"No route to {self.host} from this Mac ({'; '.join(self.errors)}), while {LOCAL_NETWORK_BLOCK_PROOFS[self.proof]}."

    @property
    def line(self) -> str:
        """Checkup's result line."""
        return (
            f"macOS refuses this Mac's connections to {self.host} ([Errno 65] No route to host), although "
            f"{LOCAL_NETWORK_BLOCK_PROOFS[self.proof]}: Local Network access is blocked for TimeCapsuleSMB. Turn "
            "it off and on in System Settings > Privacy & Security > Local Network, then quit and reopen "
            "TimeCapsuleSMB"
        )

    @property
    def details(self) -> dict[str, object]:
        return {"code": self.code, "local_network_block_proof": self.proof, "connect_errors": list(self.errors)}


def local_network_block_possible(
    host: str,
    errors: Iterable[str],
    *,
    networks: Iterable[LocalInterfaceNetwork] | None = None,
) -> bool:
    """Whether local_network_block could find a block behind these connects,
    before any proof or retry: in the macOS app, every connect got no route,
    and the address is not on a separate network. A proof that costs time
    (ssh) is asked only when this is True.

    macOS applies Local Network access to apps. A terminal app it blocks
    (Terminal.app and SSH are exempt) has ssh and Bonjour refused too, so
    nothing could prove the device is up: outside the app, no route is never
    a proven block."""
    errors = [error for error in errors if error]
    if not running_in_app() or not errors:
        return False
    if any(connect_error_kind(error) != "no_route" for error in errors):
        return False
    local = [item.network for item in (local_lan_networks() if networks is None else networks)]
    return classify_network_link(host_networks(host), local, source="local_network_block").verdict != "separate"


def local_network_block(
    host: str,
    errors: Iterable[str],
    *,
    proof: str | None,
    connect_error: Callable[[str, int, float], str | None] | None = None,
    networks: Iterable[LocalInterfaceNetwork] | None = None,
) -> LocalNetworkBlock | None:
    """The block, when macOS refuses this program's connections to a device
    that is up: in the macOS app, every connect this work made to the device
    got No route to host (Errno 65), `proof` (LOCAL_NETWORK_BLOCK_PROOFS)
    shows the device is up, and one more connect to port 22 gets no route too.

    No route alone never decides it: a device that is off or gone gives it
    too, and Linux reports a failed address lookup on the link the same way
    (and has no Local Network privacy). A connect that gets through on the
    last try was a fault that has passed. An address on none of this
    computer's networks is never a block: its route goes through a router.
    `networks` is local_lan_networks()."""
    errors = tuple(dict.fromkeys(error for error in errors if error))
    if proof is None or not local_network_block_possible(host, errors, networks=networks):
        return None
    retry = (connect_error or tcp_connect_error)(host, SSH_PORT, PORT_CHECK_TIMEOUT_SECONDS)
    if connect_error_kind(retry) != "no_route":
        return None
    return LocalNetworkBlock(host, proof, tuple(dict.fromkeys((*errors, retry))))


def check_ssh_login(connection: SshConnection, *, router_mac: str | None = None) -> CheckResult:
    """SSH logs in at `connection`. A device that answers nothing is judged
    against `router_mac`, this computer's router toward it when it last
    reached it (silent_device)."""
    result = probe_ssh_command_conn(
        connection,
        "/bin/echo ok",
        timeout=30,
        expected_stdout_suffix="ok",
    )
    if result.ok:
        return CheckResult("PASS", f"SSH command works for {connection.host}")
    details = {"code": ssh_login_failure_code(result.error)}
    if details["code"] == "ssh_unreachable" and (
        unanswered := _unanswered_ssh_port(
            connection.host, ssh_reached=ssh_reached_device(result), router_mac=router_mac,
        )
    ) is not None:
        return unanswered
    if result.detail.startswith(("Connecting to the device failed, SSH error:", local_network_filtered_message())):
        return CheckResult("FAIL", result.detail, details)
    return CheckResult("FAIL", f"SSH command failed for {connection.host}: {result.detail}", details)


def _unanswered_ssh_port(target: str, *, ssh_reached: bool = False, router_mac: str | None = None) -> CheckResult | None:
    """Why SSH did not get through, when its port does not answer.

    ssh waits out its own connect timeout, so an address nothing answers at
    otherwise looks like a command that timed out. None when port 22 answers:
    the session itself is what hung. A saved link-local address on an
    interface this computer no longer has is said as such. `ssh_reached`:
    ssh itself reached the device (ssh_reached_device), as when SSH is off
    and the device refuses the port, which shows a Local Network block when
    this program's own connects get no route.
    """
    host = endpoint_host(target)
    if (interface := link_local_interface_gone(host)) is not None:
        gone = LinkLocalInterfaceGone(host, interface)
        return CheckResult("FAIL", gone.line, {"code": gone.code})
    ssh_error = tcp_connect_error(host, SSH_PORT, PORT_CHECK_TIMEOUT_SECONDS)
    if ssh_error is None:
        return None
    closed = closed_ssh_port_error(host, ssh_error=ssh_error)
    if closed.code == ErrorCode.DEVICE_UNREACHABLE and (
        block := local_network_block(host, (ssh_error, closed.acp_error), proof="ssh" if ssh_reached else None)
    ) is not None:
        return CheckResult("FAIL", block.line, block.details)
    if closed.silent:
        # Nothing answers: where this computer is says whether to unplug the
        # device or to reconnect this computer.
        silent = silent_device(closed, router_mac)
        return CheckResult("FAIL", silent.line, {"code": silent.code, "ssh_port_error": ssh_error, **silent.details})
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
