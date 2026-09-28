"""Durable-handle, shutdown-close and handle-time device suite (Samba patches
0019, 0024, 0029, 0062, NetBSD 4's futimens in 0002, and the parent scavenger
in 0008), run from a Mac against a deployed device.

    .venv/bin/python -m tests.samba.durable_device --env .env [--stall SECONDS]
        [--case NAME ...]

A durable handle survives a lost connection while the smbd that owns it is
alive; it does not survive that smbd being killed (as on Windows, that needs a
persistent handle). The SMB2 cases open a file with a lease and a durable v2
request through smbprotocol (a host tool), drop the connection, and reconnect
it from a new connection with the same client GUID:

- fin/rst: the client closes or resets TCP; the old smbd notices and marks the
  open disconnected.
- half-open: the old connection stays up. Without PreviousSessionId the open is
  still live, so after 0024's retry window the answer is OBJECT_NAME_NOT_FOUND
  (MS-SMB2 3.3.5.9.12). An immediate refusal has the same status, so the case
  also requires the wait to have lasted about the retry window;
  naming the old session in the new session setup makes the old smbd close it.
- rst+ipc-tdis: before the reset, IPC$ is connected and disconnected, as a Mac
  listing shares does. That tree disconnect leaves smbd's working directory at
  "/", and the logoff that follows the reset closes the durable open from
  there; 0029 stats the closed file by name, so without 0062 the cookie is
  refused and the reconnect fails.
- rst+second-session: the durable open belongs to a second session on the
  connection. The first session's trees close first only when the session
  table is walked in that order, so this case can pass without 0062; the
  ipc-tdis cases are the deterministic ones.

The delete-on-close cases open a file with FILE_DELETE_ON_CLOSE and end the
session without a CLOSE: by a reset connection (drop) or a bare SMB2 LOGOFF,
which smbd serves without changing into any share. With +ipc-tdis the IPC$
tree disconnect first leaves the working directory at "/", so without 0062
the delete finds no parent directory and the file survives.

The settime cases set a file's last-write time with SET_INFO and read it back
through a new handle. settime:data sets it on a handle opened for reading and
writing, as Windows CopyFile does on the file it writes; smbd then calls
futimens() on that handle's descriptor, which NetBSD 4 libc lacks, and 0002's
replacement once always failed (NOT_SUPPORTED). settime:attributes uses a
handle opened only for attributes, as macOS does, which smbd serves by name.

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
STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
# 0024 retries a live durable open 34 times, 150 ms apart.
LIVE_RETRY_SECONDS = 34 * 0.150
# Slack for the device's reply after the retries: an answer much later than the
# window is not 0024's retry running out.
LIVE_RETRY_SLACK_SECONDS = 3
DROP_MODES = ("fin", "rst", "half-open", "half-open+previous", "rst+ipc-tdis", "rst+second-session")
DELETE_ON_CLOSE_MODES = ("drop", "drop+ipc-tdis", "logoff", "logoff+ipc-tdis")
SETTIME_MODES = ("data", "attributes")
CASES = (DROP_MODES + tuple(f"doc:{mode}" for mode in DELETE_ON_CLOSE_MODES)
         + tuple(f"settime:{mode}" for mode in SETTIME_MODES))
# 2001-02-03 04:05:06 UTC as an SMB FILETIME (100 ns units since 1601).
SETTIME_FILETIME = (981173106 + 11644473600) * 10_000_000


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


def _ipc_connect_disconnect(device: Device, session) -> None:
    """Connect and disconnect IPC$, as a Mac listing shares does. smbd's
    close_cnum() for that tree leaves its working directory at "/"."""
    from smbprotocol.tree import TreeConnect

    ipc = TreeConnect(session, rf"\\{device.host}\IPC$")
    ipc.connect()
    ipc.disconnect()


def _reset(conn) -> None:
    """Drop the connection with a TCP reset."""
    sock = conn.transport._sock
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


def drop_case(r: Results, device: Device, mode: str) -> None:
    from smbprotocol.exceptions import SMBResponseException
    from smbprotocol.session import Session
    from smbprotocol.tree import TreeConnect

    client = DurableClient(device)
    path = f"{TEST_DIR}\\{mode}.bin"
    payload = f"durable payload {mode}\n".encode()
    conn1, sess1, tree1 = client.connect()
    if mode == "rst+second-session":
        # The durable open belongs to a second session on this connection.
        sess1 = Session(conn1, device.env.get("TC_SAMBA_USER") or "root", device.env["TC_PASSWORD"])
        sess1.connect()
        tree1 = TreeConnect(sess1, rf"\\{device.host}\{device.share}")
        tree1.connect()
    handle, granted = client.open_durable(tree1, path)
    r.check(f"{mode}: durable handle granted", lambda: granted)
    handle.write(payload, 0)
    if mode == "rst+ipc-tdis":
        _ipc_connect_disconnect(device, sess1)
    sock = conn1.transport._sock
    if mode.startswith("rst"):
        _reset(conn1)
    elif mode == "fin":
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
                    # The status alone cannot tell a retried refusal from an
                    # immediate one, so the wait must match 0024's window.
                    return (error.status == STATUS_OBJECT_NAME_NOT_FOUND and
                            LIVE_RETRY_SECONDS - 1 <= waited <= LIVE_RETRY_SECONDS + LIVE_RETRY_SLACK_SECONDS)
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


def delete_on_close_case(r: Results, device: Device, mode: str) -> None:
    """A FILE_DELETE_ON_CLOSE file closed by smbd at session teardown, not by
    a client CLOSE, must still be deleted."""
    from smbprotocol.open import (CreateDisposition, CreateOptions, FilePipePrinterAccessMask,
                                  ImpersonationLevel, Open, ShareAccess)

    name = f"doc-{mode}.bin"
    on_disk = f"{device.root}/{TEST_DIR}/{name}"
    conn, sess, tree = DurableClient(device).connect()
    try:
        handle = Open(tree, f"{TEST_DIR}\\{name}")
        handle.create(ImpersonationLevel.Impersonation,
                      FilePipePrinterAccessMask.GENERIC_READ | FilePipePrinterAccessMask.GENERIC_WRITE
                      | FilePipePrinterAccessMask.DELETE,
                      0x80, ShareAccess.FILE_SHARE_READ | ShareAccess.FILE_SHARE_DELETE,
                      CreateDisposition.FILE_OVERWRITE_IF,
                      CreateOptions.FILE_NON_DIRECTORY_FILE | CreateOptions.FILE_DELETE_ON_CLOSE)
        handle.write(b"delete me\n", 0)
        r.check(f"doc:{mode}: the file exists while open",
                lambda: device.sh(f"ls {shlex.quote(on_disk)}", check=False).strip() != "")
        if mode.endswith("+ipc-tdis"):
            _ipc_connect_disconnect(device, sess)
        if mode.startswith("logoff"):
            # A bare SMB2 LOGOFF: smbprotocol's default first closes every open
            # and tree, which would delete the file through a normal CLOSE.
            sess.disconnect(close=False)
            time.sleep(2)
        else:
            _reset(conn)
            time.sleep(3)
        r.check(f"doc:{mode}: smbd deleted the file at session teardown",
                lambda: device.sh(f"ls {shlex.quote(on_disk)} 2>/dev/null", check=False).strip() == "")
    finally:
        with contextlib.suppress(Exception):
            conn.disconnect()


def _set_basic_info(handle, info) -> None:
    """SMB2 SET_INFO on an open handle (smbprotocol's Open has no set_info)."""
    from smbprotocol.open import SMB2SetInfoRequest, SMB2SetInfoResponse

    request = SMB2SetInfoRequest()
    request["info_type"] = info.INFO_TYPE
    request["file_info_class"] = info.INFO_CLASS
    request["file_id"] = handle.file_id
    request["buffer"] = info
    sent = handle.connection.send(request, handle.tree_connect.session.session_id,
                                  handle.tree_connect.tree_connect_id)
    SMB2SetInfoResponse().unpack(handle.connection.receive(sent)["data"].get_value())


def _query_basic_info(handle):
    """SMB2 QUERY_INFO FileBasicInformation on an open handle."""
    from smbprotocol.file_info import FileBasicInformation
    from smbprotocol.open import SMB2QueryInfoRequest, SMB2QueryInfoResponse

    info = FileBasicInformation()
    request = SMB2QueryInfoRequest()
    request["info_type"] = info.INFO_TYPE
    request["file_info_class"] = info.INFO_CLASS
    request["file_id"] = handle.file_id
    request["output_buffer_length"] = len(info)
    sent = handle.connection.send(request, handle.tree_connect.session.session_id,
                                  handle.tree_connect.tree_connect_id)
    response = SMB2QueryInfoResponse()
    response.unpack(handle.connection.receive(sent)["data"].get_value())
    return response.parse_buffer(FileBasicInformation)


def settime_case(r: Results, device: Device, mode: str) -> None:
    """Set a file's last-write time on an open handle; a new handle must read it back."""
    from smbprotocol.exceptions import SMBResponseException
    from smbprotocol.file_info import FileBasicInformation
    from smbprotocol.open import (CreateDisposition, CreateOptions, FilePipePrinterAccessMask,
                                  ImpersonationLevel, Open, ShareAccess)

    path = f"{TEST_DIR}\\settime-{mode}.bin"
    share = ShareAccess.FILE_SHARE_READ | ShareAccess.FILE_SHARE_WRITE
    conn, _, tree = DurableClient(device).connect()
    try:
        created = Open(tree, path)
        created.create(ImpersonationLevel.Impersonation,
                       FilePipePrinterAccessMask.GENERIC_READ | FilePipePrinterAccessMask.GENERIC_WRITE,
                       0x80, share, CreateDisposition.FILE_OVERWRITE_IF, CreateOptions.FILE_NON_DIRECTORY_FILE)
        created.write(b"settime\n", 0)
        created.close()
        if mode == "data":
            access = FilePipePrinterAccessMask.GENERIC_READ | FilePipePrinterAccessMask.GENERIC_WRITE
        else:
            access = (FilePipePrinterAccessMask.FILE_READ_ATTRIBUTES
                      | FilePipePrinterAccessMask.FILE_WRITE_ATTRIBUTES)
        handle = Open(tree, path)
        handle.create(ImpersonationLevel.Impersonation, access, 0x80, share,
                      CreateDisposition.FILE_OPEN, CreateOptions.FILE_NON_DIRECTORY_FILE)
        info = FileBasicInformation()
        for field in ("creation_time", "last_access_time", "change_time", "file_attributes"):
            info[field] = 0  # 0 leaves the value unchanged
        info["last_write_time"] = SETTIME_FILETIME

        def accepted() -> bool:
            try:
                _set_basic_info(handle, info)
                return True
            except SMBResponseException as error:
                print(f"    settime:{mode}: SET_INFO failed 0x{error.status:08x}", flush=True)
                return False

        r.check(f"settime:{mode}: SET_INFO accepts the last-write time", accepted)
        handle.close()
        check = Open(tree, path)
        check.create(ImpersonationLevel.Impersonation, FilePipePrinterAccessMask.FILE_READ_ATTRIBUTES,
                     0x80, share, CreateDisposition.FILE_OPEN, CreateOptions.FILE_NON_DIRECTORY_FILE)
        try:
            r.check(f"settime:{mode}: a new handle reads the time back",
                    lambda: _query_basic_info(check)["last_write_time"].get_value() == SETTIME_FILETIME)
        finally:
            check.close()
    finally:
        with contextlib.suppress(Exception):
            conn.disconnect()


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
    parser.add_argument("--case", action="append", choices=CASES + ("mac",),
                        help="run only these cases (repeatable; default: all)")
    args = parser.parse_args()
    wanted = set(args.case or CASES + ("mac",))
    device = Device(parse_env_file(Path(args.env)), args.share)
    test_dir = f"{device.root}/{TEST_DIR}"
    results = Results()
    threading.excepthook = _quiet_dropped_socket
    work = Path(tempfile.mkdtemp(prefix="tc-durable-"))
    device.sh(f"rm -rf {shlex.quote(test_dir)} && mkdir {shlex.quote(test_dir)} && chmod 777 {shlex.quote(test_dir)}")
    try:
        for mode in DROP_MODES:
            if mode in wanted:
                drop_case(results, device, mode)
        for mode in DELETE_ON_CLOSE_MODES:
            if f"doc:{mode}" in wanted:
                delete_on_close_case(results, device, mode)
        for mode in SETTIME_MODES:
            if f"settime:{mode}" in wanted:
                settime_case(results, device, mode)
        if args.stall and "mac" in wanted:
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
