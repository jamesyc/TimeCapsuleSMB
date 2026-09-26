"""Durable-handle device suite (Samba patches 0019, 0024, 0029 and the parent
scavenger in 0008), run from a Mac against a deployed device.

    .venv/bin/python -m tests.samba.durable_device --env .env [--stall SECONDS]

A durable handle survives a lost connection while the smbd that owns it is
alive; it does not survive that smbd being killed (as on Windows, that needs a
persistent handle). The SMB2 cases open a file with a lease and a durable v2
request through smbprotocol (a host tool), drop the connection, and reconnect
it from a new connection with the same client GUID:

- fin/rst: the client closes or resets TCP; the old smbd notices and marks the
  open disconnected.
- half-open: the old connection stays up. Without PreviousSessionId the open is
  still live, so after 0024's retry window the answer is FILE_NOT_AVAILABLE;
  naming the old session in the new session setup makes the old smbd close it.

The macOS case holds a file open on a mount, stops the smbd serving it long
enough for macOS to open a new session, resumes it, and checks the pending
write and the data. It works inside a `__tc_durable_test__` folder that it
creates over SSH and removes at the end.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import shlex
import socket
import struct
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from timecapsulesmb.core.config import parse_env_file
from tests.samba.links_device import Device, Results, mount, unmount

TEST_DIR = "__tc_durable_test__"
STATUS_FILE_NOT_AVAILABLE = 0xC0000467
# 0024 retries a live durable open 34 times, 150 ms apart.
LIVE_RETRY_SECONDS = 34 * 0.150


class DurableClient:
    """One SMB2 client identity (client GUID and lease key) over several connections."""

    def __init__(self, device: Device) -> None:
        self.device = device
        self.client_guid = uuid.uuid4()
        self.lease_key = uuid.uuid4().bytes
        self.create_guid = uuid.uuid4()

    def connect(self, previous_session_id: int = 0):
        from smbprotocol.connection import Connection
        from smbprotocol.session import Session
        from smbprotocol.tree import TreeConnect

        conn = Connection(self.client_guid, self.device.host, 445)
        conn.connect()
        with _previous_session(previous_session_id):
            sess = Session(conn, self.device.env.get("TC_SAMBA_USER") or "root",
                           self.device.env["TC_PASSWORD"])
            sess.connect()
        tree = TreeConnect(sess, rf"\\{self.device.host}\{self.device.share}")
        tree.connect()
        return conn, sess, tree

    def _contexts(self, durable):
        from smbprotocol.create_contexts import (CreateContextName, LeaseState,
                                                 SMB2CreateContextRequest, SMB2CreateRequestLeaseV2)

        lease = SMB2CreateRequestLeaseV2()
        lease["lease_key"] = self.lease_key
        lease["parent_lease_key"] = b"\0" * 16
        lease["epoch"] = 0
        lease["lease_state"] = (LeaseState.SMB2_LEASE_READ_CACHING | LeaseState.SMB2_LEASE_HANDLE_CACHING
                                | LeaseState.SMB2_LEASE_WRITE_CACHING)
        contexts = []
        for name, data in ((CreateContextName.SMB2_CREATE_REQUEST_LEASE_V2, lease), durable):
            context = SMB2CreateContextRequest()
            context["buffer_name"] = name
            context["buffer_data"] = data
            contexts.append(context)
        return contexts

    def open_durable(self, tree, path: str):
        """Create path with a durable v2 request; return (open, granted)."""
        from smbprotocol.create_contexts import CreateContextName, SMB2CreateDurableHandleRequestV2
        from smbprotocol.open import (CreateDisposition, CreateOptions, FilePipePrinterAccessMask,
                                      ImpersonationLevel, Open, ShareAccess)

        request = SMB2CreateDurableHandleRequestV2()
        request["timeout"] = 0
        request["create_guid"] = self.create_guid
        handle = Open(tree, path)
        response = handle.create(
            ImpersonationLevel.Impersonation,
            FilePipePrinterAccessMask.GENERIC_READ | FilePipePrinterAccessMask.GENERIC_WRITE,
            0x80, ShareAccess.FILE_SHARE_READ, CreateDisposition.FILE_OVERWRITE_IF,
            CreateOptions.FILE_NON_DIRECTORY_FILE,
            create_contexts=self._contexts((CreateContextName.SMB2_CREATE_DURABLE_HANDLE_REQUEST_V2, request)),
            oplock_level=0xFF)
        granted = any(type(c).__name__ == "SMB2CreateDurableHandleResponseV2" for c in response or [])
        return handle, granted

    def reconnect(self, tree, path: str, file_id: bytes):
        from smbprotocol.create_contexts import CreateContextName, SMB2CreateDurableHandleReconnectV2
        from smbprotocol.open import (CreateDisposition, CreateOptions, FilePipePrinterAccessMask,
                                      ImpersonationLevel, Open, ShareAccess)

        request = SMB2CreateDurableHandleReconnectV2()
        request["file_id"] = file_id
        request["create_guid"] = self.create_guid
        handle = Open(tree, path)
        handle.create(
            ImpersonationLevel.Impersonation,
            FilePipePrinterAccessMask.GENERIC_READ | FilePipePrinterAccessMask.GENERIC_WRITE,
            0x80, ShareAccess.FILE_SHARE_READ, CreateDisposition.FILE_OPEN,
            CreateOptions.FILE_NON_DIRECTORY_FILE,
            create_contexts=self._contexts((CreateContextName.SMB2_CREATE_DURABLE_HANDLE_RECONNECT_V2, request)),
            oplock_level=0xFF)
        return handle


@contextlib.contextmanager
def _previous_session(session_id: int):
    """Send PreviousSessionId in the session setups made inside this block;
    smbprotocol always sends 0."""
    import smbprotocol.session as session_module

    original = session_module.SMB2SessionSetupRequest

    class WithPrevious(original):
        def __init__(self):
            super().__init__()
            self["previous_session_id"] = session_id

    session_module.SMB2SessionSetupRequest = WithPrevious
    try:
        yield
    finally:
        session_module.SMB2SessionSetupRequest = original


def _quiet_dropped_socket(args) -> None:
    """smbprotocol's receive thread fails with EBADF once a test closes the
    socket under it on purpose; that is the point of the test, not an error."""
    if isinstance(args.exc_value, OSError) and args.thread is not None and \
            args.thread.name.startswith("msg_worker"):
        return
    threading.__excepthook__(args)


def drop_case(r: Results, device: Device, mode: str) -> None:
    from smbprotocol.exceptions import SMBResponseException

    client = DurableClient(device)
    path = f"{TEST_DIR}\\{mode}.bin"
    payload = f"durable payload {mode}\n".encode()
    conn1, sess1, tree1 = client.connect()
    handle, granted = client.open_durable(tree1, path)
    r.check(f"{mode}: durable handle granted", lambda: granted)
    handle.write(payload, 0)
    sock = conn1.transport._sock
    if mode == "rst":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    if mode in ("fin", "rst"):
        sock.close()
    time.sleep(2)
    previous = sess1.session_id if mode == "half-open+previous" else 0
    conn2, _, tree2 = client.connect(previous)
    started = time.monotonic()
    try:
        if mode == "half-open":
            def refused() -> bool:
                try:
                    client.reconnect(tree2, path, handle.file_id)
                except SMBResponseException as error:
                    waited = time.monotonic() - started
                    return error.status == STATUS_FILE_NOT_AVAILABLE and waited >= LIVE_RETRY_SECONDS - 1
                return False

            r.check(f"{mode}: a live open is refused after the retry window", refused)
        else:
            def restored() -> bool:
                again = client.reconnect(tree2, path, handle.file_id)
                data = again.read(0, len(payload))
                again.close()
                return data == payload

            r.check(f"{mode}: reconnect restores the open and its data", restored)
    finally:
        with contextlib.suppress(Exception):
            conn2.disconnect()
        if mode.startswith("half-open"):
            with contextlib.suppress(Exception):
                conn1.disconnect()


def mac_stall_case(r: Results, device: Device, mount_dir: Path, stall: int) -> None:
    """Stop the smbd serving the mount while a write is pending; resume it after
    macOS has opened a new session, which must reconnect the held handle."""
    before = _smbd_children(device)
    mount("smb", device, mount_dir)
    try:
        path = mount_dir / TEST_DIR / "stall.txt"
        path.write_text("start\n")
        held = open(path, "r+")
        held.read()
        held.write("before\n")
        held.flush()
        os.fsync(held.fileno())
        mine = sorted(set(_smbd_children(device)) - set(before))
        r.check("mac: the mount has its own smbd", lambda: len(mine) == 1)
        if len(mine) != 1:
            held.close()
            return
        device.sh(f"kill -STOP {mine[0]}")
        outcome: dict[str, str] = {}

        def pending_write() -> None:
            try:
                held.write("during\n")
                held.flush()
                os.fsync(held.fileno())
                outcome["write"] = "ok"
            except OSError as error:
                outcome["write"] = str(error)

        writer = threading.Thread(target=pending_write)
        try:
            writer.start()
            time.sleep(stall)
            during = set(_smbd_children(device)) - set(before) - {mine[0]}
            r.check("mac: macOS opened a new session during the stall", lambda: bool(during))
        finally:
            device.sh(f"kill -CONT {mine[0]}")
        writer.join(240)
        r.check("mac: the pending write completed", lambda: outcome.get("write") == "ok")

        def contents() -> bool:
            held.seek(0)
            return held.read() == "start\nbefore\nduring\n"

        r.check("mac: the held handle reads all writes", contents)
        with contextlib.suppress(OSError):
            held.close()
        r.check("mac: a new open reads all writes", lambda: path.read_text() == "start\nbefore\nduring\n")
    finally:
        unmount(mount_dir)


def _smbd_children(device: Device) -> list[int]:
    """PIDs of the smbd parent's children, by ps (the devices lack pgrep)."""
    rows = []
    for line in device.sh("ps axww -o pid,ppid,command").splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    managers = {pid for pid, _, cmd in rows if cmd.startswith("service: role=manager")}
    parents = {pid for pid, ppid, cmd in rows if ppid in managers and "/sbin/smbd" in cmd}
    return [pid for pid, ppid, _ in rows if ppid in parents]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default=".env")
    parser.add_argument("--share", help="share name (default: TC_SHARE_NAME, else the only share)")
    parser.add_argument("--stall", type=int, default=60,
                        help="seconds to stop the Mac mount's smbd (0 skips the macOS case)")
    args = parser.parse_args()
    device = Device(parse_env_file(Path(args.env)), args.share)
    test_dir = f"{device.root}/{TEST_DIR}"
    results = Results()
    threading.excepthook = _quiet_dropped_socket
    work = Path(tempfile.mkdtemp(prefix="tc-durable-"))
    device.sh(f"rm -rf {shlex.quote(test_dir)} && mkdir {shlex.quote(test_dir)} && chmod 777 {shlex.quote(test_dir)}")
    try:
        for mode in ("fin", "rst", "half-open", "half-open+previous"):
            drop_case(results, device, mode)
        if args.stall:
            mac_stall_case(results, device, work / "smb", args.stall)
    finally:
        device.sh(f"rm -rf {shlex.quote(test_dir)}", check=False)
        with contextlib.suppress(OSError):
            (work / "smb").rmdir()
        work.rmdir()
    print(f"RESULT pass={results.passed} fail={len(results.failed)}")
    return 1 if results.failed else 0


if __name__ == "__main__":
    sys.exit(main())
