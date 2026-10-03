from __future__ import annotations

import socket
from collections.abc import Sequence
from timecapsulesmb.core.net import select_route_to_address
from timecapsulesmb.discovery.models import MDNS_PORT

def interface_index_for_target(target_ip: str | None, interfaces: Sequence[str] | None = None) -> int | None:
    import ifaddr
    source = next(iter(interfaces), None) if interfaces else None
    if source is None and target_ip:
        source = select_route_to_address(target_ip, port=MDNS_PORT).source
    if source is None:
        return None
    if "%" in source:
        zone = source.partition("%")[2]
        if zone.isdigit():
            return int(zone)
        try:
            return socket.if_nametoindex(zone)
        except OSError:
            return None
    for adapter in ifaddr.get_adapters():
        for address in adapter.ips:
            value = address.ip[0] if isinstance(address.ip, tuple) else address.ip
            if value == source:
                try:
                    return socket.if_nametoindex(adapter.name)
                except OSError:
                    return None
    return None


def ipv4_interface_addresses(index: int) -> list[str]:
    import ifaddr
    for adapter in ifaddr.get_adapters():
        try:
            matched = socket.if_nametoindex(adapter.name) == index
        except OSError:
            matched = False
        if matched:
            return [address.ip for address in adapter.ips if isinstance(address.ip, str)]
    return []
