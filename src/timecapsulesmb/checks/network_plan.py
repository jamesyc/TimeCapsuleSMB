from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal


NetworkFamily = Literal["ipv4", "ipv6"]


@dataclass(frozen=True)
class NetworkFamilyPlan:
    family: NetworkFamily
    remote_addresses: tuple[str, ...] = ()
    remote_cidrs: tuple[str, ...] = ()
    local_sources: tuple[str, ...] = ()
    mdns_expected: bool = False
    samba_expected: bool = False
    nbns_expected: bool = False

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
) -> NetworkCheckPlan:
    bind_interfaces = parse_bind_interfaces(smb_bind_interfaces)
    mdns = set(normalize_family_tokens(mdns_families))
    # RFC NBNS NB records carry only IPv4 addresses. Keep this independent
    # from Samba/mDNS, which can be dual-stack.
    nbns = {family for family in normalize_family_tokens(nbns_families) if family == "ipv4"}

    def family_plan(family: NetworkFamily) -> NetworkFamilyPlan:
        remote_addresses: list[str] = []
        remote_cidrs: list[str] = []
        for interface in bind_interfaces:
            if interface.family != family:
                continue
            if interface.address not in remote_addresses:
                remote_addresses.append(interface.address)
            if interface.cidr not in remote_cidrs:
                remote_cidrs.append(interface.cidr)
        return NetworkFamilyPlan(
            family=family,
            remote_addresses=tuple(remote_addresses),
            remote_cidrs=tuple(remote_cidrs),
            local_sources=local_sources_for_remote_cidrs(remote_cidrs, family=family, local_addresses=local_addresses),
            mdns_expected=family in mdns,
            samba_expected=bool(remote_addresses),
            nbns_expected=family in nbns,
        )

    return NetworkCheckPlan(ipv4=family_plan("ipv4"), ipv6=family_plan("ipv6"))
