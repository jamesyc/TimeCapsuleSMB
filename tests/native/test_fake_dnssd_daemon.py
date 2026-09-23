"""The fake mDNSResponder survives a client reset between registrations."""

import errno
import os
import socket
import struct
import tempfile

from tests.native.integration.fake_dnssd_daemon import (
    FakeDnssdDaemon,
    HEADER,
    REG_SERVICE_REQUEST,
)


def test_client_reset_keeps_daemon_serving(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="tcdnssd") as directory:
        path = os.path.join(directory, "mDNSResponder")
        daemon = FakeDnssdDaemon(path)
        original_recv = daemon._recv_exact
        reset_once = True

        def recv_or_reset(sock, size):
            nonlocal reset_once
            if reset_once:
                reset_once = False
                raise ConnectionResetError(errno.ECONNRESET, "peer reset")
            return original_recv(sock, size)

        monkeypatch.setattr(daemon, "_recv_exact", recv_or_reset)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as first:
                first.connect(path)
                first.sendall(b"x")
                assert daemon.wait_for(lambda rows: any(row["op"] == "close" for row in rows), timeout=2)

            payload = struct.pack(">II", 0, 9) + b"\0_smb._tcp\0local.\0\0" + struct.pack(">HH", 445, 0)
            request = HEADER.pack(1, len(payload), 0, REG_SERVICE_REQUEST, 0, 0, 0) + payload
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as second:
                second.settimeout(2)
                second.connect(path)
                second.sendall(request)
                assert daemon.wait_for(lambda rows: any(row["op"] == "register" for row in rows), timeout=2)
                assert second.recv(4) == b"\0\0\0\0"
        finally:
            daemon.close()
