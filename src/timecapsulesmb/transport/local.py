from __future__ import annotations

import shlex
import errno
import os
import selectors
import shutil
import socket
import subprocess
import time
from collections.abc import Mapping, Sequence

from timecapsulesmb.core.net import ipv6_scope_index


def find_command(name: str) -> str | None:
    return shutil.which(name)


def command_exists(name: str) -> bool:
    if find_command(name):
        return True
    return subprocess.run(
        ["/bin/sh", "-c", f"command -v {shlex.quote(name)} >/dev/null 2>&1"]
    ).returncode == 0


def tcp_connect_error(host: str, port: int, timeout: float = 2.0) -> str | None:
    errors: list[str] = []
    try:
        for family, socktype, proto, _, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
            with socket.socket(family, socktype, proto) as sock:
                sock.settimeout(timeout)
                try:
                    sock.connect(sockaddr)
                    return None
                except OSError as exc:
                    message = str(exc) or exc.__class__.__name__
                    if message not in errors:
                        errors.append(message)
                    continue
    except Exception as exc:
        return str(exc) or exc.__class__.__name__
    return "; ".join(errors) if errors else "connection failed"


def tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    return tcp_connect_error(host, port, timeout=timeout) is None


def scoped_tcp_connect_errors(hosts: Sequence[str], port: int, *, timeout: float = 2.0) -> dict[str, str | None]:
    """Probe IPv6 scope alternatives concurrently within one connection budget."""
    results: dict[str, str | None] = {}
    sockets: list[socket.socket] = []
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        try:
            for host in dict.fromkeys(hosts):
                base, _, scope = host.partition("%")
                index = ipv6_scope_index(scope)
                if index is None:
                    results[host] = "no usable local IPv6 scope"
                    continue
                try:
                    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                    sockets.append(sock)
                    sock.setblocking(False)
                    code = sock.connect_ex((base, port, 0, index))
                    if code in (0, errno.EISCONN):
                        results[host] = None
                    elif code in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR):
                        selector.register(sock, selectors.EVENT_WRITE, host)
                    else:
                        results[host] = os.strerror(code)
                except OSError as exc:
                    results[host] = str(exc)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                for key, _events in selector.select(remaining):
                    try:
                        code = key.fileobj.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                        results[key.data] = os.strerror(code) if code else None
                    except OSError as exc:
                        results[key.data] = str(exc)
                    selector.unregister(key.fileobj)
            for key in selector.get_map().values():
                results[key.data] = "connection timed out"
        finally:
            for sock in sockets:
                sock.close()
    return results


def find_free_local_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def run_local_capture(
    cmd: list[str],
    timeout: int = 15,
    *,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
