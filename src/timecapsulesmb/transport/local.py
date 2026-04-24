from __future__ import annotations

import shlex
import socket
import subprocess


def command_exists(name: str) -> bool:
    return subprocess.run(
        ["/bin/sh", "-c", f"command -v {shlex.quote(name)} >/dev/null 2>&1"]
    ).returncode == 0


def tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        for family, socktype, proto, _, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
            with socket.socket(family, socktype, proto) as sock:
                sock.settimeout(timeout)
                try:
                    sock.connect(sockaddr)
                    return True
                except OSError:
                    continue
    except Exception:
        return False
    return False


def find_free_local_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def run_local_capture(cmd: list[str], timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
