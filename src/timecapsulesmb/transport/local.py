from __future__ import annotations

import shlex
import shutil
import socket
import subprocess
from collections.abc import Mapping


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
