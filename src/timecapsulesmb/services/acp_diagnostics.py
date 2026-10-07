"""Telemetry on how this computer reaches an AirPort's ACP port.

In v3.1.x telemetry many configure and set-ssh runs stopped at the ACP port
probe, mostly against 10.0.1.1 (the router-mode network of an AirPort reset to
factory settings) or while the device was still restarting. The connect error
alone cannot say whether this computer was on the device's network at all.

Every probe records how each known device address relates to this computer's
networks, plus the AirPort record's status flags, so failures have a baseline
to compare with. A failed probe also tries the record's other addresses and
looks the Bonjour name up again. None of this changes what the operation does.

Addresses and prefixes are reported only for private, shared (100.64/10),
unique local and link-local ranges. Public ones are reduced to their family
and scope, and this computer's own addresses are never reported.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Union

from timecapsulesmb.checks.network import LocalInterfaceNetwork, interface_kind, local_interface_networks
from timecapsulesmb.core.net import RouteSelection, select_route_to_address
from timecapsulesmb.core.net import ipv4_literal, ipv6_literal, resolve_host_ips
from timecapsulesmb.discovery.bonjour import (
    BonjourResolvedService,
    BonjourServiceInstance,
    BonjourQuery,
)
from timecapsulesmb.integrations.acp import ACP_PORT
from timecapsulesmb.transport.local import tcp_connect_error


ALT_PROBE_TIMEOUT_SECONDS = 2.0
FRESH_LOOKUP_TIMEOUT_MS = 3000
RECORD_FLAG_KEYS = ("raNA", "raSt", "prob", "syFl")

# A reset AirPort names its network "Apple Network" plus the last six hex
# digits of its radio MAC (raNm "Apple Network b67fdb" for raMA ...-B6-7F-DB).
_DEFAULT_NETWORK_NAME_RE = re.compile(r"^Apple Network [0-9a-f]{6}$", re.IGNORECASE)
_SHARED_IPV4 = ipaddress.ip_network("100.64.0.0/10")
_UNIQUE_LOCAL_IPV6 = ipaddress.ip_network("fc00::/7")
_REPORTED_SCOPES = frozenset({"private", "shared", "ula", "link_local", "loopback"})

# Substrings of connect errors, in the order they are tested. The texts are
# macOS's and Linux's strerror() and Python's socket timeout and gaierror.
_ERROR_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("timeout", ("timed out",)),
    ("refused", ("refused",)),
    ("host_down", ("host is down",)),
    ("no_route", ("no route to host",)),
    ("network_unreachable", ("network is unreachable", "network is down")),
    ("address_unavailable", ("assign requested address",)),
    ("name_lookup_failed", (
        "nodename nor servname",
        "name or service not known",
        "no address associated",
        "temporary failure in name resolution",
    )),
    ("not_permitted", ("operation not permitted", "permission denied")),
)

IpAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
RouteFunc = Callable[[str], RouteSelection]


def connect_error_kind(error: str | None) -> str | None:
    """One word for a connect error; several addresses' errors are joined by '+'."""
    if error is None:
        return None
    kinds: list[str] = []
    for part in str(error).split("; "):
        text = part.lower()
        kind = next((name for name, needles in _ERROR_KINDS if any(needle in text for needle in needles)), "other")
        if kind not in kinds:
            kinds.append(kind)
    return "+".join(kinds) or "other"


def address_scope(ip: IpAddress) -> str:
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"
    if ip.version == 4:
        if ip in _SHARED_IPV4:
            return "shared"
        if ip.is_private:
            return "private"
    elif ip in _UNIQUE_LOCAL_IPV6:
        return "ula"
    if ip.is_global:
        return "global"
    return "other"


def _parse_address(address: str) -> IpAddress | None:
    literal = ipv4_literal(address) or ipv6_literal(address)
    return ipaddress.ip_address(literal) if literal else None


def address_summary(address: str) -> dict[str, object]:
    ip = _parse_address(address)
    if ip is None:
        return {"family": "unknown"}
    scope = address_scope(ip)
    summary: dict[str, object] = {"family": f"ipv{ip.version}", "scope": scope}
    if scope in _REPORTED_SCOPES:
        summary["address"] = address
    return summary


def local_networks_field(networks: Iterable[LocalInterfaceNetwork]) -> list[dict[str, object]]:
    by_interface: dict[str, list[dict[str, object]]] = {}
    for item in networks:
        network = item.network
        scope = address_scope(network.network_address)
        entry: dict[str, object] = {"family": f"ipv{network.version}", "scope": scope, "prefixlen": network.prefixlen}
        # A full-length prefix is this computer's own address.
        if scope in _REPORTED_SCOPES and network.prefixlen < network.max_prefixlen:
            entry["network"] = str(network)
        entries = by_interface.setdefault(item.interface, [])
        if entry not in entries:
            entries.append(entry)
    return [
        {"interface": interface, "kind": interface_kind(interface), "networks": entries}
        for interface, entries in by_interface.items()
    ]


def _route_fields(
    ip: IpAddress,
    scope: str,
    selection: RouteSelection,
    networks: Sequence[LocalInterfaceNetwork],
) -> dict[str, object]:
    if selection.state == "unavailable":
        return {"route": "none"}
    if selection.state != "available" or not selection.source:
        return {"route": "unknown"}
    source, _, source_scope = selection.source.partition("%")
    source_network = next((item for item in networks if item.address == source), None)
    interface = source_network.interface if source_network is not None else (source_scope or None)
    if interface is not None and interface_kind(interface) == "vpn":
        route = "vpn"
    elif scope == "link_local" or (source_network is not None and ip in source_network.network):
        route = "direct"
    else:
        route = "gateway"
    fields: dict[str, object] = {"route": route}
    if interface is not None:
        fields["route_interface"] = interface
    return fields


def address_context(
    address: str,
    role: str,
    networks: Sequence[LocalInterfaceNetwork],
    route: RouteFunc,
) -> dict[str, object]:
    """How this computer would reach one device address."""
    entry: dict[str, object] = {"role": role, **address_summary(address)}
    ip = _parse_address(address)
    if ip is None:
        return entry
    scope = str(entry["scope"])
    family_networks = [item for item in networks if item.network.version == ip.version]
    # Every interface has a link-local network, so prefixes cannot place a
    # link-local address; the route and the alternate probe say more.
    if scope == "link_local" or not family_networks:
        entry["link"] = "unknown"
    elif any(ip in item.network for item in family_networks):
        entry["link"] = "on_link"
    else:
        entry["link"] = "off_link"
    entry.update(_route_fields(ip, scope, route(address), networks))
    return entry


def _same_address(left: str, right: str) -> bool:
    left_ip = _parse_address(left)
    if left_ip is None or left_ip != _parse_address(right):
        return False
    if not left_ip.is_link_local or left_ip.version == 4:
        return True
    return left.partition("%")[2] == right.partition("%")[2]


def candidate_addresses(
    host: str,
    record: BonjourResolvedService | None,
    *,
    resolve: Callable[[str], Sequence[str]] | None = None,
) -> list[tuple[str, str]]:
    """The probed host's addresses ("target"), then the record's others ("record")."""
    resolve = resolve or resolve_host_ips
    targets = [host] if _parse_address(host) is not None else list(resolve(host))
    candidates: list[tuple[str, str]] = []
    for address in targets:
        if not any(_same_address(address, known) for known, _role in candidates):
            candidates.append((address, "target"))
    if record is not None:
        for address in [*record.ipv4, *record.ipv6]:
            ip = _parse_address(address)
            # Without a zone a link-local IPv6 address names no interface, and
            # the record lists each one with its zone as well.
            if ip is None or (ip.version == 6 and ip.is_link_local and "%" not in address):
                continue
            if not any(_same_address(address, known) for known, _role in candidates):
                candidates.append((address, "record"))
    return candidates


def record_flags(record: BonjourResolvedService) -> dict[str, object]:
    properties = record.properties
    flags: dict[str, object] = {key: properties[key] for key in RECORD_FLAG_KEYS if properties.get(key)}
    network_name = (properties.get("raNm") or "").strip()
    flags["default_network_name"] = bool(_DEFAULT_NETWORK_NAME_RE.match(network_name)) if network_name else None
    return flags


def probe_context_fields(
    host: str,
    record: BonjourResolvedService | None,
    *,
    networks: Sequence[LocalInterfaceNetwork] | None = None,
    route: RouteFunc | None = None,
    resolve: Callable[[str], Sequence[str]] | None = None,
) -> dict[str, object]:
    """Fields for every ACP port probe, successful or not."""
    networks = local_interface_networks() if networks is None else networks
    route = route or (lambda address: select_route_to_address(address, port=ACP_PORT))
    fields: dict[str, object] = {
        "acp_target_addresses": [
            address_context(address, role, networks, route)
            for address, role in candidate_addresses(host, record, resolve=resolve)
        ],
        "local_networks": local_networks_field(networks),
    }
    if record is not None:
        fields["acp_record_flags"] = record_flags(record)
    return fields


def fresh_lookup(
    record: BonjourResolvedService,
    *,
    query: BonjourQuery | None = None,
) -> dict[str, object]:
    """Resolve the record's Bonjour name again and compare its addresses."""
    instance = BonjourServiceInstance(record.service_type, record.name, record.fullname, record.interface_index)
    resolved, diagnostics = (query or BonjourQuery()).resolve_detailed(instance, FRESH_LOOKUP_TIMEOUT_MS)
    result: dict[str, object] = {}
    now: list[str] = []
    verified_families: set[int] = set()
    for family, version in (("ipv4", 4), ("ipv6", 6)):
        addresses, error = diagnostics.family_result(resolved, family)
        if error:
            result[family] = "error"
            result[f"{family}_error"] = error[:200]
            continue
        if not addresses:
            result[family] = "no_answer"
            continue
        result[family] = "answered"
        verified_families.add(version)
        for address in addresses:
            if not any(_same_address(address, known) for known in now):
                now.append(address)
    if verified_families:
        before = {str(ip) for ip in map(_parse_address, [*record.ipv4, *record.ipv6]) if ip is not None and ip.version in verified_families}
        after = {str(ip) for ip in map(_parse_address, now) if ip is not None}
        result["addresses_changed"] = before != after
        result["added"] = [address_summary(address) for address in sorted(after - before)]
        result["removed"] = [address_summary(address) for address in sorted(before - after)]
    return result


def failure_fields(
    host: str,
    record: BonjourResolvedService | None,
    *,
    connect: Callable[[str, int, float], str | None] | None = None,
    lookup: Callable[[BonjourResolvedService], dict[str, object]] | None = None,
    resolve: Callable[[str], Sequence[str]] | None = None,
) -> dict[str, object]:
    """Fields for a failed ACP port probe. Takes at most about three seconds."""
    if record is None:
        return {}
    connect = connect or tcp_connect_error
    lookup = lookup or fresh_lookup
    alternates = [address for address, role in candidate_addresses(host, record, resolve=resolve) if role == "record"]
    can_look_up = bool(record.fullname and record.service_type)
    workers = len(alternates) + (1 if can_look_up else 0)
    if workers == 0:
        return {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        probes = [(address, pool.submit(connect, address, ACP_PORT, ALT_PROBE_TIMEOUT_SECONDS)) for address in alternates]
        lookup_future = pool.submit(lookup, record) if can_look_up else None
    fields: dict[str, object] = {}
    if alternates:
        alt_probe: list[dict[str, object]] = []
        for address, future in probes:
            try:
                error = future.result()
            except Exception as exc:
                error = str(exc) or exc.__class__.__name__
            entry = address_summary(address)
            entry["reachable"] = error is None
            if error is not None:
                entry["error_kind"] = connect_error_kind(error)
            alt_probe.append(entry)
        fields["acp_alt_probe"] = alt_probe
    if lookup_future is not None:
        try:
            fields["acp_fresh_lookup"] = lookup_future.result()
        except Exception as exc:
            fields["acp_fresh_lookup"] = {"error": (str(exc) or exc.__class__.__name__)[:200]}
    return fields
