"""tests/loopback.py: the port and listener helpers other tests share."""

import socket

import pytest

from tests.loopback import accept_loop, has_ipv6_loopback, held_refusing_ports

FAMILIES = [(socket.AF_INET, "127.0.0.1")] + ([(socket.AF_INET6, "::1")] if has_ipv6_loopback() else [])


def connect(family, host, port):
    with socket.socket(family) as sock:
        sock.settimeout(5)
        try:
            sock.connect((host, port))
        except ConnectionRefusedError:
            return "refused"
        return sock.recv(16)


@pytest.mark.parametrize("family,host", FAMILIES)
def test_a_held_port_refuses_serves_a_listener_and_refuses_again_once_it_stops(family, host):
    with held_refusing_ports(1) as [port]:
        assert connect(family, host, port) == "refused"
        listener = socket.socket(family)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        listener.bind((host, port))
        listener.listen(4)

        def hello(conn):
            with conn:
                conn.sendall(b"hello")
        stop = accept_loop(listener, hello)
        assert connect(family, host, port) == b"hello"
        # stop() returns only once the accept thread has exited, so nothing
        # is left to take the next connection: it is refused again.
        stop()
        assert connect(family, host, port) == "refused"
