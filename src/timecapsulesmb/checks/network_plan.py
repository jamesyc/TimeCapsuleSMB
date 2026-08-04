from __future__ import annotations

import errno
import ipaddress
import socket
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Callable, Literal


NetworkFamily = Literal["ipv4", "ipv6"]
RouteState = Literal["available", "unavailable", "unknown"]


@dataclass(frozen=True)
class RouteSelection:
    state: RouteState
    source: str | None = None
    error: str | None = None
    error_number: int | None = None


@dataclass(frozen=True)
class NetworkEndpointPlan:
    address: str
    cidr: str
    family: NetworkFamily
    on_link_sources: tuple[str, ...] = ()
    local_sources: tuple[str, ...] = ()
    route: RouteSelection = field(default_factory=lambda: RouteSelection("unknown"))

    @property
    def applicable(self) -> bool:
        return self.route.state != "unavailable"


@dataclass(frozen=True)
class NetworkFamilyPlan:
    family: NetworkFamily
    endpoints: tuple[NetworkEndpointPlan, ...] = ()
    mdns_expected: bool = False
    samba_expected: bool = False
    nbns_expected: bool = False

    @property
    def remote_addresses(self) -> tuple[str, ...]:
        return tuple(endpoint.address for endpoint in self.endpoints)

    @property
    def remote_cidrs(self) -> tuple[str, ...]:
        cidrs: list[str] = []
        for endpoint in self.endpoints:
            if endpoint.cidr not in cidrs:
                cidrs.append(endpoint.cidr)
        return tuple(cidrs)

    @property
    def local_sources(self) -> tuple[str, ...]:
        sources: list[str] = []
        for endpoint in self.endpoints:
            for source in endpoint.local_sources:
                if source not in sources:
                    sources.append(source)
        return tuple(sources)

    @property
    def on_link_sources(self) -> tuple[str, ...]:
        sources: list[str] = []
        for endpoint in self.endpoints:
            for source in endpoint.on_link_sources:
                if source not in sources:
                    sources.append(source)
        return tuple(sources)

    @property
    def applicable_endpoints(self) -> tuple[NetworkEndpointPlan, ...]:
        return tuple(endpoint for endpoint in self.endpoints if endpoint.applicable)

    @property
    def locally_reachable(self) -> bool:
        return bool(self.local_sources)


@dataclass(frozen=True)
class NetworkCheckPlan:
    ipv4: NetworkFamilyPlan = field(default_factory=lambda: NetworkFamilyPlan("ipv4"))
    ipv6: NetworkFamilyPlan = field(default_factory=lambda: NetworkFamilyPlan("ipv6"))

    def families(self) -> tuple[NetworkFamilyPlan, NetworkFamilyPlan]:
        return (self.ipv4, self.ipv6)


@dataclass(frozen=True)
class BindInterface:
    address: str
    cidr: str
    family: NetworkFamily


def normalize_family_tokens(tokens: Iterable[str]) -> tuple[NetworkFamily, ...]:
    families: list[NetworkFamily] = []
    for raw in tokens:
        token = raw.strip().lower()
        if token not in {"ipv4", "ipv6"}:
            continue
        family = token  # type: ignore[assignment]
        if family not in families:
            families.append(family)
    return tuple(families)


def parse_bind_interfaces(bind_interfaces: str | None) -> tuple[BindInterface, ...]:
    interfaces: list[BindInterface] = []
    for raw in (bind_interfaces or "").split():
        token = raw.strip()
        if not token or "/" not in token:
            continue
        try:
            interface = ipaddress.ip_interface(token)
        except ValueError:
            continue
        network = interface.network
        if network.is_loopback:
            continue
        bind_interface = BindInterface(
            address=str(interface.ip),
            cidr=str(network),
            family="ipv6" if interface.version == 6 else "ipv4",
        )
        if bind_interface not in interfaces:
            interfaces.append(bind_interface)
    return tuple(interfaces)


def parse_bind_cidrs(bind_interfaces: str | None) -> tuple[str, ...]:
    cidrs: list[str] = []
    for interface in parse_bind_interfaces(bind_interfaces):
        if interface.cidr not in cidrs:
            cidrs.append(interface.cidr)
    return tuple(cidrs)


def bind_interface_families(bind_interfaces: str | None) -> tuple[NetworkFamily, ...]:
    families: list[NetworkFamily] = []
    for interface in parse_bind_interfaces(bind_interfaces):
        if interface.family not in families:
            families.append(interface.family)
    return tuple(families)


def cidr_family(cidr: str) -> NetworkFamily | None:
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return None
    return "ipv6" if network.version == 6 else "ipv4"


def _adapter_ip_text(value: object) -> str | None:
    if isinstance(value, tuple):
        value = value[0] if value else ""
    if not isinstance(value, str):
        return None
    value = value.split("%", 1)[0]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def local_interface_addresses() -> tuple[str, ...]:
    try:
        import ifaddr
    except Exception:
        return ()

    try:
        adapters = ifaddr.get_adapters()
    except Exception:
        return ()

    addresses: list[str] = []
    for adapter in adapters:
        for adapter_ip in getattr(adapter, "ips", []):
            ip_text = _adapter_ip_text(getattr(adapter_ip, "ip", None))
            if not ip_text:
                continue
            try:
                ip_obj = ipaddress.ip_address(ip_text)
            except ValueError:
                continue
            if ip_obj.is_loopback:
                continue
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
    try:
        ip_obj = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError as exc:
        return RouteSelection("unknown", error=str(exc))

    family = socket.AF_INET6 if ip_obj.version == 6 else socket.AF_INET
    destination = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.connect(destination)
            source = _adapter_ip_text(sock.getsockname()[0])
    except OSError as exc:
        state: RouteState = "unavailable" if exc.errno in _ROUTE_UNAVAILABLE_ERRNOS else "unknown"
        return RouteSelection(
            state,
            error=str(exc) or exc.__class__.__name__,
            error_number=exc.errno,
        )

    if source is None or ipaddress.ip_address(source).is_unspecified:
        return RouteSelection("unknown", error="kernel did not select a source address")
    return RouteSelection("available", source=source)


def local_sources_for_remote_cidrs(
    remote_cidrs: Sequence[str],
    *,
    family: NetworkFamily,
    local_addresses: Sequence[str] | None = None,
) -> tuple[str, ...]:
    networks = []
    for cidr in remote_cidrs:
        if cidr_family(cidr) != family:
            continue
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue

    if not networks:
        return ()

    sources: list[str] = []
    candidate_addresses = local_addresses if local_addresses is not None else local_interface_addresses()
    for ip_text in candidate_addresses:
        try:
            ip_obj = ipaddress.ip_address(ip_text.split("%", 1)[0])
        except ValueError:
            continue
        if (family == "ipv4" and ip_obj.version != 4) or (family == "ipv6" and ip_obj.version != 6):
            continue
        if any(ip_obj in network for network in networks) and ip_text not in sources:
            sources.append(ip_text)
    return tuple(sources)


def build_network_check_plan(
    *,
    smb_bind_interfaces: str | None,
    mdns_families: Iterable[str],
    nbns_families: Iterable[str],
    local_addresses: Sequence[str] | None = None,
    route_selector: Callable[[str], RouteSelection] | None = None,
) -> NetworkCheckPlan:
    bind_interfaces = parse_bind_interfaces(smb_bind_interfaces)
    candidate_local_addresses = tuple(local_addresses) if local_addresses is not None else local_interface_addresses()
    select_route = route_selector or select_route_to_address
    mdns = set(normalize_family_tokens(mdns_families))
    # RFC NBNS NB records carry only IPv4 addresses. Keep this independent
    # from Samba/mDNS, which can be dual-stack.
    nbns = {family for family in normalize_family_tokens(nbns_families) if family == "ipv4"}

    def family_plan(family: NetworkFamily) -> NetworkFamilyPlan:
        endpoints: list[NetworkEndpointPlan] = []
        for interface in bind_interfaces:
            if interface.family != family:
                continue
            try:
                route = select_route(interface.address)
            except Exception as exc:
                route = RouteSelection("unknown", error=f"{type(exc).__name__}: {exc}")
            matching_sources = local_sources_for_remote_cidrs(
                (interface.cidr,),
                family=family,
                local_addresses=candidate_local_addresses,
            )
            endpoint_sources: list[str] = []
            if route.state == "available" and route.source:
                endpoint_sources.append(route.source)
            elif route.state == "unknown":
                endpoint_sources.extend(matching_sources)
            endpoint = NetworkEndpointPlan(
                address=interface.address,
                cidr=interface.cidr,
                family=family,
                on_link_sources=matching_sources,
                local_sources=tuple(endpoint_sources),
                route=route,
            )
            if endpoint not in endpoints:
                endpoints.append(endpoint)
        return NetworkFamilyPlan(
            family=family,
            endpoints=tuple(endpoints),
            mdns_expected=family in mdns,
            samba_expected=bool(endpoints),
            nbns_expected=family in nbns,
        )

    return NetworkCheckPlan(ipv4=family_plan("ipv4"), ipv6=family_plan("ipv6"))
