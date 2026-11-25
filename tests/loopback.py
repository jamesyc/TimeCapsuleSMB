"""Loopback helpers for dual-stack tests."""

from __future__ import annotations

import contextlib
import select
import socket
import threading
from typing import Callable


def has_ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as sock:
            sock.bind(("::1", 0))
    except OSError:
        return False
    return True


@contextlib.contextmanager
def held_refusing_ports(count: int):
    """Yields count port numbers nothing listens on, on 127.0.0.1 and on ::1
    where the host has it, so a connect to them is refused.

    Each port is held while the block runs by the local end of a connection
    to a helper listener: no bind to port 0, which is how every test here
    picks its ports, can be given it in any worker, and a SYN to
    it from anywhere else matches no socket and gets a reset. A test may
    listen on a held port with SO_REUSEADDR meanwhile; once its accept_loop
    stops, a connect gets a reset again. (A socket that
    is only bound would make BSD kernels drop the SYN, a timeout, and a
    port released after picking can be taken by another worker.)"""
    ipv6 = has_ipv6_loopback()
    helpers: dict[int, socket.socket] = {}
    held: list[socket.socket] = []

    def hold(family: int, host: str, port: int) -> int | None:
        if family not in helpers:
            helper = socket.socket(family)
            helper.bind((host, 0))
            helper.listen(8)
            helpers[family] = helper
        sock = socket.socket(family)
        # A test may still listen on a held port for a while: with
        # SO_REUSEADDR on this socket and the listener (Linux wants both),
        # as after a server restart beside its old connections. A connect
        # gets the listener then, and a reset again once it closes.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        try:
            sock.bind((host, port))
            sock.connect(helpers[family].getsockname()[:2])
        except OSError:
            sock.close()
            return None
        held.extend([sock, helpers[family].accept()[0]])
        return sock.getsockname()[1]

    ports: list[int] = []
    try:
        for _ in range(count * 50):
            if len(ports) == count:
                break
            port = hold(socket.AF_INET, "127.0.0.1", 0)
            if port is None or (ipv6 and hold(socket.AF_INET6, "::1", port) is None):
                continue
            ports.append(port)
        assert len(ports) == count, "no free loopback port pair"
        yield ports
    finally:
        for sock in held + list(helpers.values()):
            sock.close()


def fill_listen_queue(listener: socket.socket) -> list[socket.socket]:
    """Makes a bound listener that never accepts look like a daemon that
    stopped accepting: its queue filled by connections of our own until the
    kernel drops a new one's SYN, so a client's connect gets no answer at
    all. Returns the filling sockets, for the caller to close.

    Loopback connects complete at once; only a full queue leaves one
    waiting, and 1.5 s is far past what a loaded host takes."""
    listener.listen(1)
    address = listener.getsockname()[:2]
    made: list[socket.socket] = []
    for _ in range(64):
        client = socket.socket(listener.family, socket.SOCK_STREAM)
        client.setblocking(False)
        made.append(client)
        client.connect_ex(address)
        _, writable, _ = select.select([], [client], [], 1.5)
        if not writable:
            return made
    for sock in made:
        sock.close()
    raise AssertionError("the listen queue never filled")


def accept_loop(listener: socket.socket, handle: Callable[[socket.socket], None]) -> Callable[[], None]:
    """Accepts on a listening socket in a thread, handing each connection to
    handle in a thread of its own, until the returned stop() is called.

    stop() shuts the listener down before closing it: on Linux close() alone
    does not wake a thread blocked in accept(), which would keep accepting,
    through descriptor reuse, connections meant for a later test. It returns
    once the accept thread has exited."""
    def loop() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()

    def stop() -> None:
        with contextlib.suppress(OSError):
            listener.shutdown(socket.SHUT_RDWR)
        listener.close()
        thread.join(timeout=10)
        assert not thread.is_alive(), "accept loop did not stop"

    return stop
