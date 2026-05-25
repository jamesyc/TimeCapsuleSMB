from __future__ import annotations

import ipaddress
import socket


def extract_host(target: str) -> str:
    return target.split("@", 1)[1] if "@" in target else target


def ipv4_literal(value: str) -> str | None:
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


def ipv6_literal(value: str) -> str | None:
    value = value.strip()
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return None
    if parsed.version != 6:
        return None
    return str(parsed)


def is_link_local_ipv4(value: str) -> bool:
    literal = ipv4_literal(value)
    return literal is not None and literal.startswith("169.254.")


def is_loopback_ipv4(value: str) -> bool:
    literal = ipv4_literal(value)
    return literal is not None and literal.startswith("127.")


def resolve_host_ipv4s(host: str) -> tuple[str, ...]:
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
        ip_addr = ipv4_literal(sockaddr[0])
        if ip_addr and ip_addr not in ordered:
            ordered.append(ip_addr)
    return tuple(ordered)


def resolve_host_ipv6s(host: str) -> tuple[str, ...]:
    if not host:
        return ()
    try:
        results = socket.getaddrinfo(host, None, socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        return ()
    ordered: list[str] = []
    for result in results:
        sockaddr = result[4]
        if not sockaddr:
            continue
        ip_addr = ipv6_literal(sockaddr[0].split("%", 1)[0])
        if ip_addr and ip_addr not in ordered:
            ordered.append(ip_addr)
    return tuple(ordered)


def resolve_host_ips(host: str) -> tuple[str, ...]:
    ordered: list[str] = []
    for ip in resolve_host_ipv4s(host) + resolve_host_ipv6s(host):
        if ip not in ordered:
            ordered.append(ip)
    return tuple(ordered)
