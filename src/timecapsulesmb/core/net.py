from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import socket
from urllib.parse import urlparse


@dataclass(frozen=True)
class Endpoint:
    raw: str
    user: str
    host: str
    port: int | None = None
    invalid_port: str | None = None


def parse_endpoint(value: str) -> Endpoint:
    raw = value.strip()
    user = ""
    host = raw
    port: int | None = None
    invalid_port: str | None = None

    parsed = urlparse(raw)
    if parsed.scheme and parsed.hostname:
        user = parsed.username or ""
        host = parsed.hostname
        try:
            port = parsed.port
        except ValueError:
            invalid_port = parsed.netloc.rsplit(":", 1)[-1]
        return Endpoint(raw=raw, user=user, host=normalize_endpoint_host(host), port=port, invalid_port=invalid_port)

    candidate = raw.split("/", 1)[0]
    if "@" in candidate:
        user, candidate = candidate.rsplit("@", 1)

    if candidate.startswith("[") and "]" in candidate:
        end = candidate.index("]")
        host = candidate[1:end]
        suffix = candidate[end + 1:]
        if suffix.startswith(":"):
            port_text = suffix[1:]
            if port_text.isdigit():
                port = int(port_text)
            elif port_text:
                invalid_port = port_text
        elif suffix:
            invalid_port = suffix
    elif candidate.count(":") == 1:
        host_part, port_text = candidate.rsplit(":", 1)
        if port_text.isdigit():
            host = host_part
            port = int(port_text)
        elif port_text:
            host = candidate
            invalid_port = port_text
    else:
        host = candidate

    return Endpoint(raw=raw, user=user, host=normalize_endpoint_host(host), port=port, invalid_port=invalid_port)


def normalize_endpoint_host(value: str) -> str:
    candidate = value.strip().strip("[]")
    if not candidate:
        return ""
    literal = ipv4_literal(candidate) or ipv6_literal(candidate)
    if literal is not None:
        return literal
    return candidate.rstrip(".")


def endpoint_host(value: str) -> str:
    return parse_endpoint(value).host


def canonical_ssh_target(value: str, *, default_user: str = "root") -> str:
    endpoint = parse_endpoint(value)
    if not endpoint.host:
        return ""
    if endpoint.invalid_port:
        raise ValueError(f"invalid SSH target port: {endpoint.invalid_port}")
    if endpoint.port not in (None, 22):
        raise ValueError(
            f"unsupported SSH target port {endpoint.port}; set a custom SSH port in TC_SSH_OPTS instead"
        )
    user = endpoint.user or default_user
    return f"{user}@{endpoint.host}"


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
    value = value.strip().split("%", 1)[0]
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


def is_link_local_ipv6(value: str) -> bool:
    literal = ipv6_literal(value)
    if literal is None:
        return False
    return ipaddress.ip_address(literal).is_link_local


def is_link_local_ip(value: str) -> bool:
    return is_link_local_ipv4(value) or is_link_local_ipv6(value)


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
        ip_addr = scoped_ip_literal(sockaddr[0], scope_id=sockaddr[3] if len(sockaddr) >= 4 else 0)
        if ip_addr and not any(ip_addr == known or same_scoped_ip(ip_addr, known) for known in ordered):
            ordered.append(ip_addr)
    return tuple(ordered)


def ipv6_scope_index(scope: str) -> int | None:
    """Resolve a local zone without falling back to the default interface."""
    try:
        index = int(scope, 10)
    except ValueError:
        try:
            index = socket.if_nametoindex(scope)
        except (OSError, ValueError):
            return None
    return index if 0 < index <= 0xFFFFFFFF else None


def scoped_ip_literal(value: str, *, scope_id: int = 0) -> str | None:
    """Preserve link-local zones from text or a socket's separate scope field."""
    base, _, scope = value.strip().partition("%")
    try:
        address = ipaddress.ip_address(base)
    except ValueError:
        return None
    if address.version != 6 or not address.is_link_local:
        return str(address)
    if scope_id:
        if ipv6_scope_index(str(scope_id)) is None:
            return None
        text_index = ipv6_scope_index(scope) if scope else None
        if text_index is not None and text_index != scope_id:
            return None
        try:
            scope = socket.if_indextoname(scope_id)
        except OSError:
            scope = str(scope_id)
    if scope and ("%" in scope or scope == "0"):
        return None
    return f"{address}%{scope}" if scope else str(address)


def same_scoped_ip(left: str, right: str) -> bool:
    left_base, _, left_scope = left.partition("%")
    right_base, _, right_scope = right.partition("%")
    left_ip = ipv4_literal(left_base) or ipv6_literal(left_base)
    right_ip = ipv4_literal(right_base) or ipv6_literal(right_base)
    if left_ip is None or left_ip != right_ip:
        return False
    if not is_link_local_ipv6(left_ip):
        return True
    # Equal address bytes alone cannot establish which link owns an IPv6 peer.
    if not left_scope or not right_scope:
        return False
    left_index = ipv6_scope_index(left_scope)
    right_index = ipv6_scope_index(right_scope)
    return left_index is not None and left_index == right_index


def resolve_host_ips(host: str) -> tuple[str, ...]:
    ordered: list[str] = []
    for ip in resolve_host_ipv4s(host) + resolve_host_ipv6s(host):
        if ip not in ordered:
            ordered.append(ip)
    return tuple(ordered)
