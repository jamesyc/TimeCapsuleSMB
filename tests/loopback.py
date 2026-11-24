"""Whether this computer has an IPv6 loopback, for dual-stack tests."""

import socket


def has_ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as sock:
            sock.bind(("::1", 0))
    except OSError:
        return False
    return True
