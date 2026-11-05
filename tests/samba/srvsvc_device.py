"""Share enumeration on a connection that serves files (Samba patch 0005), run
from a Mac against a deployed device.

    .venv/bin/python -m tests.samba.srvsvc_device --env .env [--case NAME ...]

smbd serves srvsvc in the worker that holds the client's trees, so what a share
enumeration does to the worker's share table reaches the open files. Modules set
share options in memory at tree connect (fruit's "posix locking = no" for Time
Machine shares, acl_xattr's "map" options); they are gone if the enumeration
reloads the table the way the separate rpcd_classic of a stock Samba does.

Each case keeps one worker and enumerates on its connection, as a Mac with a
share mounted does:

- listing: the enumeration names the share and IPC$.
- lock-enum-unlock: a lock taken before an enumeration is released after it.
  With the option lost, the unlock panics in decrement_lock_ref_count().
- enum-lock-attribute: a lock taken after an enumeration stays out of the
  kernel, so an attribute write on the file returns. With the option lost, the
  worker's own POSIX lock blocks its flock() for ever.
- publish: smb.conf changes and the smbd parent gets SIGHUP, as the manager does
  when a disk appears; an enumeration on the old connection then lists the new
  share and, after the change is undone, no longer does. The enumeration itself
  does not reread smb.conf, so this is what keeps the list current.
- mac: the same lock, enumeration and unlock from macOS's own SMB client.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import logging
import os
import re
import shlex
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from urllib.parse import unquote

from timecapsulesmb.core.config import parse_env_file
from tests.samba import device_processes
from tests.samba.durable_device import (DurableClient, _lock_first_byte, _plain_open, _poll,
                                        _quiet_dropped_socket, _QuietDroppedSocketLog,
                                        _smbd_children, _smbd_log, _smbd_log_since)
from tests.samba.links_device import Device, Results, mount, unmount

TEST_DIR = "__tc_srvsvc_test__"
CASES = ("listing", "lock-enum-unlock", "enum-lock-attribute", "publish", "mac")
SMB_CONF = "/mnt/Memory/samba4/etc/smb.conf"
SMB_CONF_NEW = SMB_CONF + ".tc-srvsvc"
SERVICE = "/mnt/Flash/service"
PUBLISHED_SHARE = "tc_srvsvc_publish"
# An attribute write takes milliseconds; the failure it tests never returns.
ATTRIBUTE_WRITE_SECONDS = 15
# SIGHUP reaches the workers as a message from the parent.
PUBLISH_SECONDS = 10
# The worker smbd forks for a new connection appears in ps at once; allow for a
# loaded device.
WORKER_SECONDS = 10

SRVSVC_UUID = uuid.UUID("4b324fc8-1670-01d3-1278-5a47bf6ee188")
SRVSVC_VERSION = 3
NDR_UUID = uuid.UUID("8a885d04-1ceb-11c9-9fe8-08002b104860")
NDR_VERSION = 2
RPC_REQUEST, RPC_RESPONSE, RPC_BIND, RPC_BIND_ACK = 0, 2, 11, 12
# First and last fragment.
RPC_FLAGS = 3
RPC_FRAGMENT = 4280
NET_SHARE_ENUM_ALL = 15


def _rpc_header(ptype: int, body: bytes, call_id: int) -> bytes:
    """A DCE/RPC PDU, little-endian."""
    return struct.pack("<BBBB4sHHI", 5, 0, ptype, RPC_FLAGS, b"\x10\0\0\0",
                       16 + len(body), 0, call_id) + body


def bind_pdu() -> bytes:
    syntaxes = (SRVSVC_UUID.bytes_le + struct.pack("<HH", SRVSVC_VERSION, 0)
                + NDR_UUID.bytes_le + struct.pack("<I", NDR_VERSION))
    return _rpc_header(RPC_BIND, struct.pack("<HHIBxxxHBx", RPC_FRAGMENT, RPC_FRAGMENT, 0, 1, 0, 1)
                       + syntaxes, 1)


def _ndr_string(text: str) -> bytes:
    """A conformant varying UTF-16 string with its NUL, padded to four bytes."""
    data = (text + "\0").encode("utf-16-le")
    count = len(data) // 2
    return struct.pack("<III", count, 0, count) + data + b"\0" * (-len(data) % 4)


def enum_all_pdu(server: str) -> bytes:
    """NetShareEnumAll at level 1: every share's name, type and comment."""
    stub = (struct.pack("<I", 0x20000) + _ndr_string(rf"\\{server}")
            + struct.pack("<IIIIII", 1, 1, 0x20004, 0, 0, 0xFFFFFFFF)
            + struct.pack("<II", 0x20008, 0))
    return _rpc_header(RPC_REQUEST, struct.pack("<IHH", len(stub), 0, NET_SHARE_ENUM_ALL) + stub, 2)


def _rpc_stub(pdu: bytes, expected: int) -> bytes:
    ptype = pdu[2]
    if ptype != expected:
        raise RuntimeError(f"RPC answered with PDU type {ptype}, not {expected}")
    if not pdu[3] & 2:
        raise RuntimeError("RPC answer continues in another fragment")
    length = struct.unpack_from("<H", pdu, 8)[0]
    return pdu[24:length]


def parse_enum_all(pdu: bytes) -> list[str]:
    """The share names in a NetShareEnumAll level 1 answer, or RuntimeError for
    any other answer."""
    stub = _rpc_stub(pdu, RPC_RESPONSE)
    level, _switch, _pointer, count = struct.unpack_from("<IIII", stub)
    if level != 1:
        raise RuntimeError(f"NetShareEnumAll answered level {level}")
    # The array's pointer and conformance, then each share's three fixed fields.
    offset = 24 + 12 * count
    names = []
    for index in range(2 * count):  # a name and a comment for each share
        chars = struct.unpack_from("<I", stub, offset + 8)[0]
        offset += 12
        text = stub[offset:offset + 2 * chars].decode("utf-16-le").rstrip("\0")
        offset += 2 * chars + (-2 * chars % 4)
        if index % 2 == 0:
            names.append(text)
    status = struct.unpack_from("<I", stub, len(stub) - 4)[0]
    if status != 0:
        raise RuntimeError(f"NetShareEnumAll failed with {status:#x}")
    return names


def enumerate_shares(device: Device, session) -> list[str]:
    """NetShareEnumAll over IPC$ on this session's connection."""
    from smbprotocol.open import (CreateDisposition, FilePipePrinterAccessMask, ImpersonationLevel,
                                  Open, ShareAccess)
    from smbprotocol.tree import TreeConnect

    ipc = TreeConnect(session, rf"\\{device.host}\IPC$")
    ipc.connect()
    try:
        pipe = Open(ipc, "srvsvc")
        pipe.create(ImpersonationLevel.Impersonation,
                    FilePipePrinterAccessMask.GENERIC_READ | FilePipePrinterAccessMask.GENERIC_WRITE,
                    0, ShareAccess.FILE_SHARE_READ | ShareAccess.FILE_SHARE_WRITE,
                    CreateDisposition.FILE_OPEN, 0)
        try:
            pipe.write(bind_pdu(), 0)
            _rpc_stub(pipe.read(0, RPC_FRAGMENT), RPC_BIND_ACK)
            pipe.write(enum_all_pdu(device.host), 0)
            return parse_enum_all(pipe.read(0, RPC_FRAGMENT))
        finally:
            pipe.close()
    finally:
        ipc.disconnect()


def _worker_on_port(device: Device, port: int) -> int | None:
    """The smbd worker with a connection from this client port."""
    wanted = re.compile(rf"[.:]{port}\b")
    for pid in _smbd_children(device):
        fstat = device.sh(f"/usr/bin/fstat -p {pid}", check=False)
        if any(" stream tcp " in line and wanted.search(line) for line in fstat.splitlines()):
            return pid
    return None


def _worker(device: Device, port: int) -> int:
    found: list[int | None] = [None]

    def appeared() -> bool:
        found[0] = _worker_on_port(device, port)
        return found[0] is not None

    if not _poll(appeared, WORKER_SECONDS, 0.5):
        raise RuntimeError(f"no smbd worker serves client port {port}")
    return found[0]


def _client_port(conn) -> int:
    return conn.transport._sock.getsockname()[1]


def _mac_ports(host: str) -> set[int]:
    """This Mac's ports with a connection to the device's SMB port. netstat
    prints addresses, and TC_HOST can be a name."""
    remote = f"{socket.gethostbyname(host)}.445"
    out = subprocess.run(["netstat", "-an", "-p", "tcp"], capture_output=True, text=True).stdout
    return {int(fields[3].rsplit(".", 1)[1]) for fields in map(str.split, out.splitlines())
            if len(fields) > 4 and fields[4] == remote and "ESTABLISHED" in fields}


def _alive(device: Device, pid: int) -> bool:
    return pid in _smbd_children(device)


def _finish(device: Device, conn, worker: int, answered: bool) -> None:
    """End the connection. Only a worker that stopped answering is killed: a
    kill is an unclean exit, which smbd logs and the scavenger then walks."""
    if not answered:
        device.sh(f"kill -9 {worker}", check=False)
    with contextlib.suppress(Exception):
        conn.disconnect()


def _kernel_locks(device: Device, path: str) -> list[str]:
    """The fcntl and flock locks the kernel has on path. A failed run raises:
    it must not read as a file without locks."""
    return device.sh(f"{SERVICE} --print-file-locks {shlex.quote(path)}").splitlines()


def _panics(device: Device, log: str, start: int) -> str:
    return _smbd_log_since(device, log, start, keep="sed -n '/PANIC/p'")


def _within(seconds: float, work) -> bool:
    """Whether work() returned within seconds. It runs in a thread of its own,
    because smbprotocol waits for ever for an answer that never comes."""
    done = threading.Event()
    failure: list[BaseException] = []

    def run() -> None:
        try:
            work()
        except BaseException as error:  # reported by the caller
            failure.append(error)
        done.set()

    threading.Thread(target=run, daemon=True).start()
    if not done.wait(seconds):
        return False
    if failure:
        raise failure[0]
    return True


def _answers(r: Results, name: str, seconds: float, work) -> bool:
    """The check that work() returns within seconds, and whether it passed."""
    failed = len(r.failed)
    r.check(name, lambda: _within(seconds, work))
    return len(r.failed) == failed


def listing_case(r: Results, device: Device) -> None:
    conn, sess, _tree = DurableClient(device).connect()
    try:
        names = enumerate_shares(device, sess)
        r.check("listing: names the share", lambda: device.share in names)
        r.check("listing: names IPC$", lambda: "IPC$" in names)
    finally:
        conn.disconnect()


def lock_enum_unlock_case(r: Results, device: Device) -> None:
    from smbprotocol.open import LockFlags, SMB2LockElement

    log, start = _smbd_log(device)
    conn, sess, tree = DurableClient(device).connect()
    worker = _worker(device, _client_port(conn))
    answered = False
    try:
        handle = _plain_open(tree, f"{TEST_DIR}\\unlock.bin")
        handle.write(b"payload", 0)
        _lock_first_byte(handle)
        enumerate_shares(device, sess)
        unlock = SMB2LockElement()
        unlock["offset"] = 0
        unlock["length"] = 1
        unlock["flags"] = LockFlags.SMB2_LOCKFLAG_UNLOCK
        answered = _answers(r, "lock-enum-unlock: unlock answers", ATTRIBUTE_WRITE_SECONDS,
                            lambda: handle.lock([unlock]))
        r.check("lock-enum-unlock: worker survives", lambda: _alive(device, worker))
        r.check("lock-enum-unlock: no panic logged", lambda: _panics(device, log, start) == "")
    finally:
        _finish(device, conn, worker, answered)


def enum_lock_attribute_case(r: Results, device: Device) -> None:
    conn, sess, tree = DurableClient(device).connect()
    worker = _worker(device, _client_port(conn))
    path = f"{TEST_DIR}\\attribute.bin"
    answered = False
    try:
        enumerate_shares(device, sess)
        handle = _plain_open(tree, path)
        handle.write(b"payload", 0)
        _lock_first_byte(handle)
        r.check("enum-lock-attribute: the lock stays out of the kernel",
                lambda: _kernel_locks(device, f"{device.root}/{TEST_DIR}/attribute.bin") == [])

        def write_attribute() -> None:
            stream = _plain_open(tree, f"{path}:com.example.tcsrvsvc")
            stream.write(b"value", 0)
            stream.close()

        answered = _answers(r, "enum-lock-attribute: the attribute write returns",
                            ATTRIBUTE_WRITE_SECONDS, write_attribute)
    finally:
        _finish(device, conn, worker, answered)


def _signal_parent(device: Device) -> None:
    rows = device_processes.parse(device.sh(device_processes.COMMAND))
    device.sh(f"kill -HUP {device_processes.samba_parent(rows).pid}")


def _replace_conf(device: Device, data: bytes) -> None:
    """Replace the running smb.conf in one step: any worker can load it at any
    moment (a session setup does), and must not find it half written."""
    device.put(SMB_CONF_NEW, data)
    device.sh(f"mv {SMB_CONF_NEW} {SMB_CONF}")


def publish_case(r: Results, device: Device) -> None:
    conn, sess, tree = DurableClient(device).connect()
    original = device.sh_bytes(f"cat {SMB_CONF}")
    share = f"\n[{PUBLISHED_SHARE}]\n    path = {device.root}/{TEST_DIR}\n".encode()

    def listed() -> bool:
        return PUBLISHED_SHARE in enumerate_shares(device, sess)

    try:
        _replace_conf(device, original + share)
        _signal_parent(device)
        r.check("publish: a new share is listed", lambda: _poll(listed, PUBLISH_SECONDS, 0.5))
    finally:
        _replace_conf(device, original)
        _signal_parent(device)
    try:
        r.check("publish: a removed share is not",
                lambda: _poll(lambda: not listed(), PUBLISH_SECONDS, 0.5))
        r.check("publish: the served share still answers",
                lambda: _plain_open(tree, f"{TEST_DIR}\\publish.bin") is not None)
    finally:
        conn.disconnect()


def _same_host(first: str, second: str) -> bool:
    if first == second:
        return True
    with contextlib.suppress(OSError):
        return socket.gethostbyname(first) == socket.gethostbyname(second)
    return False


def _existing_mount(device: Device, mounts: str | None = None) -> Path | None:
    """Where this Mac already has the device's share mounted, from mount(8)'s
    "//user@host/share on /path (smbfs, ...)" lines."""
    if mounts is None:
        mounts = subprocess.run(["mount"], capture_output=True, text=True).stdout
    for line in mounts.splitlines():
        found = re.fullmatch(r"//(?:[^@/]*@)?([^/]+)/(.+) on (.+) \(smbfs\b.*\)", line)
        if found and unicodedata.normalize("NFC", unquote(found[2])) == unicodedata.normalize("NFC", device.share) \
                and _same_host(found[1], device.host):
            return Path(found[3])
    return None


def mac_case(r: Results, device: Device, mountpoint: Path) -> None:
    log, start = _smbd_log(device)
    # macOS refuses a second mount of a share this Mac has mounted: use that one.
    existing = _existing_mount(device)
    if existing is not None:
        mountpoint = existing
    else:
        # No forcenewsession: smbutil must find this session, as Finder does.
        # It can be a session this Mac already had, so watch every connection.
        mount("smb", device, mountpoint, options="nobrowse")
    try:
        ports = _mac_ports(device.host)
        if not ports:
            raise RuntimeError(f"this Mac has no connection to {device.host}")
        workers = [_worker(device, port) for port in sorted(ports)]
        user = device.env.get("TC_SAMBA_USER") or "root"
        with open(mountpoint / TEST_DIR / "mac.bin", "wb+") as file:
            file.write(b"payload")
            file.flush()
            os.fsync(file.fileno())
            fcntl.flock(file, fcntl.LOCK_EX)
            listing = subprocess.run(["smbutil", "view", "-N", f"//{user}@{device.host}"],
                                     capture_output=True, text=True, timeout=60)
            r.check("mac: smbutil lists the shares on the mounted session",
                    lambda: listing.returncode == 0)
            r.check("mac: unlock", lambda: fcntl.flock(file, fcntl.LOCK_UN) is None)
        r.check("mac: worker survives", lambda: all(_alive(device, worker) for worker in workers))
        r.check("mac: no panic logged", lambda: _panics(device, log, start) == "")
    finally:
        if existing is None:
            unmount(mountpoint)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default=".env")
    parser.add_argument("--share", help="share name (default: TC_SHARE_NAME, else the only share)")
    parser.add_argument("--case", action="append", choices=CASES,
                        help="run only these cases (repeatable; default: all)")
    args = parser.parse_args()
    wanted = set(args.case or CASES)
    device = Device(parse_env_file(Path(args.env)), args.share)
    test_dir = shlex.quote(f"{device.root}/{TEST_DIR}")
    results = Results()
    threading.excepthook = _quiet_dropped_socket
    logging.getLogger("smbprotocol.connection").addFilter(_QuietDroppedSocketLog())
    work = Path(tempfile.mkdtemp(prefix="tc-srvsvc-"))
    device.sh(f"rm -rf {test_dir} && mkdir {test_dir} && chmod 777 {test_dir}")
    cases = {"listing": listing_case, "lock-enum-unlock": lock_enum_unlock_case,
             "enum-lock-attribute": enum_lock_attribute_case, "publish": publish_case}
    try:
        for name in CASES:
            if name not in wanted:
                continue
            started = time.monotonic()
            print(f"CASE {name}", flush=True)
            try:
                if name == "mac":
                    mac_case(results, device, work / "smb")
                else:
                    cases[name](results, device)
            except Exception as error:  # the next case gets its own connection
                results.failed.append(f"{name}: ran")
                print(f"FAIL {name}: ran: {type(error).__name__}: {error}", flush=True)
            print(f"TIME {name} {time.monotonic() - started:.1f} s", flush=True)
    finally:
        device.sh(f"rm -rf {test_dir}", check=False)
        # A mount that stayed keeps its folder: unmount() said so.
        with contextlib.suppress(OSError):
            (work / "smb").rmdir()
        with contextlib.suppress(OSError):
            work.rmdir()
    print(f"RESULT pass={results.passed} fail={len(results.failed)}")
    return 1 if results.failed else 0


if __name__ == "__main__":
    sys.exit(main())
