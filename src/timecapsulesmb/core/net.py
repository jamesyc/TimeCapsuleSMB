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


def extract_host(target: str) -> str:
    return endpoint_host(target)


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
