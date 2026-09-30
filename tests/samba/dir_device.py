"""Directory-operation device suite for the appliance's *at emulation (Samba patch
0002) and directory opens (patch 0063), run from a Mac against a deployed device.

    .venv/bin/python -m tests.samba.dir_device --env .env [--case NAME ...]
        [--record FILE] [--compare FILE] [--no-mac]

Every file-system call smbd makes relative to a directory goes through
lib/replace/tc_at_emulation.c on the appliance, and NetBSD 4 also lists
directories through its fdopendir(). The SMB2 cases (smbprotocol, a host tool)
check the directory paths a client can reach:

- open-matrix: every create disposition, with and without FILE_DIRECTORY_FILE
  and FILE_NON_DIRECTORY_FILE, for listing, attribute and read/write access,
  on an existing directory. Whatever opens must be a directory handle: SMB2
  READ on it is INVALID_DEVICE_REQUEST (smbd's is_directory check), it lists,
  and after close an exclusive open succeeds and the directory is intact. A
  file handle on a directory (what patch 0004 papered over) fails the READ.
- listing: a 600-entry directory read in small responses, with RESTART_SCANS,
  REOPEN, RETURN_SINGLE_ENTRY, a wildcard and exact names.
- mapped-names: directories whose names on disk hold the characters catia
  maps (: * ? " < > |), listed and opened by the names a client sees.
- renamed-open: a directory handle keeps listing its own directory after the
  directory is renamed on disk and another takes its name (fdopendir works on
  the descriptor, not the name).
- deep: 40 nested directories (1,363 bytes): create, list and rename within
  the deepest one; move a file from it up a level, into a sibling, to the top
  of the test folder and back; hard-link it into the sibling and the top (or
  see a refusal create nothing); and delete. The depth reached, each move's
  and link's status, and the levels smbd refuses to delete (past NetBSD's
  PATH_MAX from the share root) are recorded.
- delete: delete-on-close of empty and non-empty directories, directory
  renames onto existing names.
- times: last-write time set through attribute-only handles on a file and a
  directory, read back.
- listing-changes: 1,200 files listed on one handle in 2 KiB responses while
  each page is deleted as it arrives (all listed once, none left), then while
  files that sort first are created (no name repeated), then a query past the
  end after more such files (nothing repeated), then a restarted listing that
  shows the new file and not a deleted one. NetBSD 4's HFS lost a listing's place
  whenever a descriptor on the directory was closed; both kernels' HFS repeat
  the last entries when a listing is read again after its end.
- read-only: MAXIMUM_ALLOWED on a read-only file opens it with read but not
  write access (smbd works as root; patch 0066), a write open is still
  refused, the maximal-access context still ignores the attribute, and a
  read-only directory opens.

The Mac cases (skipped with --no-mac) mount the share: a 40-file tree with
Unicode, mapped and case-only renames copied with ditto and compared, a
150-entry directory listed twice, and a sparsebundle (Time Machine's band
directory) created, written and detached. Everything happens inside a
`__tc_dir_test__` folder created over SSH and removed at the end.

--record writes every open-matrix and error status to a JSON file; --compare
reports statuses that differ from such a file (for example the previous smbd),
and lists values only one of the two recorded (checks added or renamed since)
as NEW or GONE rather than as changes.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import shlex
import struct
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid

from timecapsulesmb.core.config import parse_env_file
from tests.samba.links_device import MAPPED_NAMES, Device, Results, mount, unmount

TEST_DIR = "__tc_dir_test__"

STATUS_SUCCESS = 0
STATUS_NO_MORE_FILES = 0x80000006
STATUS_NO_SUCH_FILE = 0xC000000F
STATUS_INVALID_DEVICE_REQUEST = 0xC0000010
STATUS_OBJECT_NAME_COLLISION = 0xC0000035
STATUS_FILE_IS_A_DIRECTORY = 0xC00000BA
STATUS_DIRECTORY_NOT_EMPTY = 0xC0000101
STATUS_NOT_A_DIRECTORY = 0xC0000103
STATUS_ACCESS_DENIED = 0xC0000022

FILE_READ_DATA = 0x1
FILE_LIST_DIRECTORY = 0x1
FILE_WRITE_DATA = 0x2
FILE_APPEND_DATA = 0x4
MAXIMUM_ALLOWED = 0x02000000
FILE_ATTRIBUTE_READONLY = 0x1
FILE_READ_ATTRIBUTES = 0x80
FILE_WRITE_ATTRIBUTES = 0x100
DELETE = 0x10000
SYNCHRONIZE = 0x100000
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_DIRECTORY_FILE = 0x1
FILE_NON_DIRECTORY_FILE = 0x40
FILE_DELETE_ON_CLOSE = 0x1000
SHARE_ALL = 0x7

DISPOSITIONS = {"supersede": 0, "open": 1, "create": 2, "open_if": 3, "overwrite": 4, "overwrite_if": 5}
OPTIONS = {"none": 0, "directory": FILE_DIRECTORY_FILE, "non_directory": FILE_NON_DIRECTORY_FILE}
ACCESS = {
    "list": FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
    "attributes": FILE_READ_ATTRIBUTES | FILE_WRITE_ATTRIBUTES | SYNCHRONIZE,
    "read_write": GENERIC_READ | GENERIC_WRITE,
}
# 2001-02-03 04:05:06 UTC as an SMB FILETIME (100 ns units since 1601).
SETTIME_FILETIME = (981173106 + 11644473600) * 10_000_000
# What the devices keep of a time (lib/replace/tc_at_emulation.c clamps to
# it): HFS keeps 1904-01-01 to 2040-02-06 06:28:15, and NetBSD 4's 32-bit
# time_t ends at 2038-01-19 03:14:07, which Samba reads back as "never", so
# its last is one second earlier. Unix times.
HFS_FIRST, HFS_LAST, NETBSD4_LAST = -2082844800, 2212122495, 2147483646
TIME_RANGE = (("2038-01-18", 2147385600), ("2040-02-05", 2212012800), ("2106-02-07", 4294967295),
              ("1903-12-31", -2082844801), ("1968-01-01", -63158400))


def filetime(unix: int) -> int:
    return (unix + 11644473600) * 10_000_000


def kept_time(unix: int, netbsd4: bool) -> int:
    """The time a device keeps when unix is set (read back while the kernel
    still caches it; from disk a time before 1970 reads as 1970)."""
    return min(max(unix, HFS_FIRST), NETBSD4_LAST if netbsd4 else HFS_LAST)


def status_of(fn):
    """Run fn; return (NTSTATUS, result), with the status of a refused request."""
    from smbprotocol.exceptions import SMBResponseException

    try:
        return STATUS_SUCCESS, fn()
    except SMBResponseException as error:
        return error.status, None


class Client:
    """One SMB2 connection to the test share (no AAPL: Windows semantics)."""

    def __init__(self, device: Device) -> None:
        from smbprotocol.connection import Connection
        from smbprotocol.session import Session
        from smbprotocol.tree import TreeConnect

        self.device = device
        self.conn = Connection(uuid.uuid4(), device.host, 445)
        self.conn.connect()
        self.sess = Session(self.conn, device.env.get("TC_SAMBA_USER") or "root", device.env["TC_PASSWORD"])
        self.sess.connect()
        self.tree = TreeConnect(self.sess, rf"\\{device.host}\{device.share}")
        self.tree.connect()

    def close(self) -> None:
        for step in (self.tree.disconnect, self.sess.disconnect, self.conn.disconnect):
            try:
                step()
            except Exception:
                pass

    def open(self, path: str, access: int, disposition: int = 1, options: int = 0,
             share: int = SHARE_ALL, attrs: int = 0):
        from smbprotocol.open import ImpersonationLevel, Open

        handle = Open(self.tree, path)
        handle.create(ImpersonationLevel.Impersonation, access, attrs, share, disposition, options)
        return handle

    def _send(self, req):
        return self.conn.receive(self.conn.send(req, self.sess.session_id, self.tree.tree_connect_id))

    def read(self, handle, length: int = 16) -> bytes:
        return handle.read(0, length)

    def names(self, handle, pattern: str = "*", flags: int = 0, max_output: int = 65536) -> list[str]:
        """Every name a QUERY_DIRECTORY sequence returns, from the first call's flags on."""
        from smbprotocol.exceptions import SMBResponseException
        from smbprotocol.file_info import FileInformationClass

        out: list[str] = []
        first = True
        while True:
            try:
                entries = handle.query_directory(pattern, FileInformationClass.FILE_ID_BOTH_DIRECTORY_INFORMATION,
                                                 flags=flags if first else 0, max_output=max_output)
            except SMBResponseException as error:
                if error.status == STATUS_NO_MORE_FILES or (not first and error.status == STATUS_NO_SUCH_FILE):
                    return out
                raise
            first = False
            out.extend(e["file_name"].get_value().decode("utf-16-le") for e in entries)

    def names_single(self, handle) -> list[str]:
        """Read one entry per call, as SMB2_RETURN_SINGLE_ENTRY asks."""
        from smbprotocol.exceptions import SMBResponseException
        from smbprotocol.file_info import FileInformationClass
        from smbprotocol.open import QueryDirectoryFlags

        out: list[str] = []
        while True:
            try:
                entries = handle.query_directory("*", FileInformationClass.FILE_NAMES_INFORMATION,
                                                 flags=QueryDirectoryFlags.SMB2_RETURN_SINGLE_ENTRY)
            except SMBResponseException as error:
                if error.status == STATUS_NO_MORE_FILES:
                    return out
                raise
            if len(entries) != 1:
                raise AssertionError(f"RETURN_SINGLE_ENTRY returned {len(entries)} entries")
            out.append(entries[0]["file_name"].get_value().decode("utf-16-le"))

    def set_info(self, handle, info_class: int, buffer: bytes) -> None:
        from smbprotocol.open import SMB2SetInfoRequest

        req = SMB2SetInfoRequest()
        req["info_type"] = 1  # SMB2_0_INFO_FILE
        req["file_info_class"] = info_class
        req["file_id"] = handle.file_id
        req["buffer"] = buffer
        self._send(req)

    def query_info(self, handle, info_class: int) -> bytes:
        from smbprotocol.open import SMB2QueryInfoRequest, SMB2QueryInfoResponse

        req = SMB2QueryInfoRequest()
        req["info_type"] = 1
        req["file_info_class"] = info_class
        req["output_buffer_length"] = 4096
        req["file_id"] = handle.file_id
        resp = SMB2QueryInfoResponse()
        resp.unpack(self._send(req)["data"].get_value())
        return resp["buffer"].get_value()

    def rename(self, handle, new_path: str, replace: bool = False) -> None:
        import struct

        name = new_path.encode("utf-16-le")
        self.set_info(handle, 10, struct.pack("<B7xQI", 1 if replace else 0, 0, len(name)) + name)

    def link(self, handle, new_path: str, replace: bool = False) -> None:
        """A hard link to the handle's file (FileLinkInformation, laid out as the rename)."""
        import struct

        name = new_path.encode("utf-16-le")
        self.set_info(handle, 11, struct.pack("<B7xQI", 1 if replace else 0, 0, len(name)) + name)

    def delete_on_close(self, handle) -> None:
        self.set_info(handle, 13, b"\x01")  # FileDispositionInformation

    def set_write_time(self, handle, filetime: int) -> None:
        import struct

        self.set_info(handle, 4, struct.pack("<qqqqII", 0, 0, filetime, 0, 0, 0))

    def write_time(self, handle) -> int:
        import struct

        return struct.unpack_from("<q", self.query_info(handle, 4), 16)[0]


def compare_records(before: dict, after: dict) -> list[str]:
    """What differs between two --record files. A value recorded only in one of
    them (a check added, renamed or skipped) is NEW or GONE, not a change."""
    lines = []
    for case in sorted(set(before) | set(after)):
        old, new = before.get(case) or {}, after.get(case) or {}
        for key in sorted(set(old) | set(new)):
            if key not in old:
                lines.append(f"NEW {case} {key}: {new[key]}")
            elif key not in new:
                lines.append(f"GONE {case} {key}: {old[key]}")
            elif old[key] != new[key]:
                lines.append(f"CHANGED {case} {key}: {old[key]} -> {new[key]}")
    return lines


def on_disk(device: Device, path: str) -> bool:
    return "present" in device.sh(f"test -e {shlex.quote(path)} && echo present || echo absent")


def rel(*parts: str) -> str:
    return "\\".join((TEST_DIR,) + parts)


def fill(device: Device, directory: str, count: int, prefix: str = "e") -> None:
    """count empty files in directory, made on the device (the device has no seq)."""
    device.sh(f"cd {shlex.quote(directory)} && i=0; while [ $i -lt {count} ]; do "
              f": > {prefix}$(printf %05d $i); i=$((i+1)); done")


def open_matrix(r: Results, device: Device, record: dict) -> None:
    from smbprotocol.exceptions import SMBResponseException

    base = f"{device.dir}/matrix"
    device.sh(f"mkdir -p {shlex.quote(base)}/dir && : > {shlex.quote(base)}/dir/inside")
    client = Client(device)
    statuses = record.setdefault("open-matrix", {})
    try:
        for dname, disposition in DISPOSITIONS.items():
            for oname, options in OPTIONS.items():
                for aname, access in ACCESS.items():
                    key = f"{dname}/{oname}/{aname}"
                    status, handle = status_of(lambda: client.open(rel("matrix", "dir"), access, disposition,
                                                                   options))
                    statuses[key] = f"0x{status:08x}"
                    if oname == "non_directory":
                        r.check(f"open-matrix {key}: FILE_NON_DIRECTORY_FILE never opens a directory",
                                lambda: status in (STATUS_FILE_IS_A_DIRECTORY, STATUS_OBJECT_NAME_COLLISION))
                    if handle is None:
                        continue

                    def is_directory_handle() -> bool:
                        try:
                            client.read(handle)
                        except SMBResponseException as error:
                            return error.status == STATUS_INVALID_DEVICE_REQUEST
                        return False

                    r.check(f"open-matrix {key}: a directory handle (READ is INVALID_DEVICE_REQUEST)",
                            is_directory_handle)
                    if access & FILE_LIST_DIRECTORY:
                        r.check(f"open-matrix {key}: lists the directory",
                                lambda: sorted(client.names(handle)) == [".", "..", "inside"])
                    handle.close()

                    def nothing_left() -> bool:
                        again = client.open(rel("matrix", "dir"), DELETE | FILE_READ_ATTRIBUTES, 1,
                                            FILE_DIRECTORY_FILE, share=0)
                        again.close()
                        return device.sh(f"ls {shlex.quote(base)}/dir").split() == ["inside"]

                    r.check(f"open-matrix {key}: closed cleanly, directory intact", nothing_left)
    finally:
        client.close()


def listing(r: Results, device: Device) -> None:
    from smbprotocol.open import QueryDirectoryFlags

    base = f"{device.dir}/listing"
    device.sh(f"mkdir -p {shlex.quote(base)}")
    fill(device, base, 600)
    expected = sorted(["."] + [".."] + [f"e{i:05d}" for i in range(600)])
    client = Client(device)
    try:
        handle = client.open(rel("listing"), ACCESS["list"], 1, FILE_DIRECTORY_FILE)
        r.check("listing: 600 entries in 2 KiB responses, each once",
                lambda: sorted(client.names(handle, max_output=2048)) == expected)
        r.check("listing: RESTART_SCANS lists everything again",
                lambda: sorted(client.names(handle, flags=QueryDirectoryFlags.SMB2_RESTART_SCANS,
                                            max_output=4096)) == expected)
        r.check("listing: REOPEN lists everything again",
                lambda: sorted(client.names(handle, flags=QueryDirectoryFlags.SMB2_REOPEN)) == expected)
        handle.close()
        handle = client.open(rel("listing"), ACCESS["list"], 1, FILE_DIRECTORY_FILE)
        r.check("listing: RETURN_SINGLE_ENTRY returns every entry once",
                lambda: sorted(client.names_single(handle)) == expected)
        handle.close()
        handle = client.open(rel("listing"), ACCESS["list"], 1, FILE_DIRECTORY_FILE)
        r.check("listing: a wildcard selects its entries",
                lambda: sorted(client.names(handle, "e0001*")) == [f"e{i:05d}" for i in range(10, 20)])
        handle.close()
        handle = client.open(rel("listing"), ACCESS["list"], 1, FILE_DIRECTORY_FILE)
        r.check("listing: an exact name finds that entry", lambda: client.names(handle, "e00042") == ["e00042"])
        handle.close()
        handle = client.open(rel("listing"), ACCESS["list"], 1, FILE_DIRECTORY_FILE)
        r.check("listing: a missing exact name is NO_SUCH_FILE",
                lambda: status_of(lambda: client.names(handle, "absent"))[0] == STATUS_NO_SUCH_FILE)
        handle.close()
    finally:
        client.close()


def mapped_names(r: Results, device: Device) -> None:
    base = f"{device.dir}/mapped"
    device.sh(f"mkdir -p {shlex.quote(base)} && cd {shlex.quote(base)} && " + " && ".join(
        f"mkdir {shlex.quote(name)} && : > {shlex.quote(name)}/inside" for name in MAPPED_NAMES))
    client = Client(device)
    try:
        top = client.open(rel("mapped"), ACCESS["list"], 1, FILE_DIRECTORY_FILE)
        seen = [n for n in client.names(top) if n not in (".", "..")]
        top.close()
        r.check("mapped-names: every mapped directory is listed", lambda: len(seen) == len(MAPPED_NAMES))
        for name in seen:
            def lists_inside(name: str = name) -> bool:
                handle = client.open(rel("mapped", name), ACCESS["list"], 1, FILE_DIRECTORY_FILE)
                try:
                    return "inside" in client.names(handle)
                finally:
                    handle.close()

            r.check(f"mapped-names: {name!r} opens and lists by the client's name", lists_inside)
        # One made over SMB lands on disk with the real character.
        made = client.open(rel("mapped", "made\uf022here"), ACCESS["list"], 2, FILE_DIRECTORY_FILE)
        made.close()
        r.check("mapped-names: a directory made over SMB has the real ':' on disk",
                lambda: on_disk(device, f"{base}/made:here"))
    finally:
        client.close()


def renamed_open(r: Results, device: Device) -> None:
    base = f"{device.dir}/renamed"
    device.sh(f"mkdir -p {shlex.quote(base)}/a && : > {shlex.quote(base)}/a/orig-file")
    client = Client(device)
    try:
        handle = client.open(rel("renamed", "a"), ACCESS["list"], 1, FILE_DIRECTORY_FILE)
        # Before the first listing, which is when smbd opens the directory stream.
        device.sh(f"cd {shlex.quote(base)} && mv a moved && mkdir a && : > a/new-file")
        names = client.names(handle)
        handle.close()
        r.check("renamed-open: the handle lists its own directory, not the new one under its old name",
                lambda: "orig-file" in names and "new-file" not in names)
    finally:
        client.close()


STATUS_OBJECT_NAME_INVALID = 0xC0000033


def deep(r: Results, device: Device) -> dict:
    """Nest 40 directories (1,300+ bytes); work at the deepest one.

    The *at emulation resolves every name relative to a directory descriptor,
    so any depth can be created, listed and renamed. smbd itself still looks
    up a few names by their full path from the share root (the parent of a
    file closed with delete-on-close, for one), and NetBSD refuses a path of
    PATH_MAX (1024) bytes or more with ENAMETOOLONG, which smbd reports as
    OBJECT_NAME_INVALID. Upstream Samba has the same limit at Linux's 4096.
    The old vfs_default fallbacks renamed through absolute names, so they
    refused to create a directory within about 1,024 absolute bytes instead.
    Record how deep creation and deletion go; losing or half-deleting
    anything is the failure."""
    client = Client(device)
    record: dict = {}
    try:
        path = [TEST_DIR, "deep"]
        client.open("\\".join(path), ACCESS["list"], 2, FILE_DIRECTORY_FILE).close()
        refused = STATUS_SUCCESS
        for i in range(40):
            candidate = "\\".join(path + [f"level{i:02d}-" + "x" * 24])
            refused, _ = status_of(lambda: client.open(candidate, ACCESS["list"], 2, FILE_DIRECTORY_FILE).close())
            if refused != STATUS_SUCCESS:
                break
            path.append(candidate.rsplit("\\", 1)[1])
        bottom = "\\".join(path)
        record["levels"] = len(path) - 2
        record["create_refused"] = f"0x{refused:08x}"
        record["bottom_bytes"] = len((device.root + "/" + bottom.replace("\\", "/")).encode())
        r.check(f"deep: nesting goes {record['levels']} levels ({record['bottom_bytes']} bytes) and any "
                f"refusal is a clean name-too-long (0x{refused:08x})",
                lambda: refused in (STATUS_SUCCESS, STATUS_OBJECT_NAME_INVALID) and record["levels"] > 10)
        parent = "\\".join(path[:-1])

        def create_and_list() -> bool:
            f = client.open(bottom + "\\f", GENERIC_READ | GENERIC_WRITE, 2, FILE_NON_DIRECTORY_FILE)
            f.write(b"deep", 0)
            f.close()
            d = client.open(bottom, ACCESS["list"], 1, FILE_DIRECTORY_FILE)
            try:
                return "f" in client.names(d)
            finally:
                d.close()

        r.check("deep: create and list at the deepest level", create_and_list)

        def rename_within() -> bool:
            f = client.open(bottom + "\\f", DELETE | FILE_READ_ATTRIBUTES, 1, FILE_NON_DIRECTORY_FILE)
            client.rename(f, bottom + "\\g")
            f.close()
            g = client.open(bottom + "\\g", GENERIC_READ, 1, FILE_NON_DIRECTORY_FILE)
            try:
                return g.read(0, 4) == b"deep"
            finally:
                g.close()

        r.check("deep: rename within the deepest directory", rename_within)

        def exists(name: str) -> bool:
            return status_of(lambda: client.open(name, FILE_READ_ATTRIBUTES, 1, FILE_NON_DIRECTORY_FILE)
                             .close())[0] == STATUS_SUCCESS

        # Between directories past PATH_MAX the emulated renameat() names the
        # other directory relative to one of them ("../" and down), from
        # whichever side is shorter, so these moves all work.
        def move(src: str, dst: str) -> int:
            def go() -> None:
                f = client.open(src, DELETE | FILE_READ_ATTRIBUTES, 1, FILE_NON_DIRECTORY_FILE)
                try:
                    client.rename(f, dst)
                finally:
                    f.close()
            return status_of(go)[0]

        status = move(bottom + "\\g", parent + "\\up")
        record["rename_up"] = f"0x{status:08x}"
        r.check(f"deep: rename up a level moves the file (0x{status:08x})",
                lambda: status == STATUS_SUCCESS and exists(parent + "\\up") and not exists(bottom + "\\g"))
        sibling = parent + "\\sibling"
        client.open(sibling, ACCESS["list"], 2, FILE_DIRECTORY_FILE).close()
        status = move(parent + "\\up", sibling + "\\g")
        record["rename_sibling"] = f"0x{status:08x}"
        r.check(f"deep: rename into a sibling of the deepest directory (0x{status:08x})",
                lambda: status == STATUS_SUCCESS and exists(sibling + "\\g") and not exists(parent + "\\up"))
        # To the test folder at the top of the share: only "../" repeated fits.
        status = move(sibling + "\\g", TEST_DIR + "\\from_deep")
        record["rename_top"] = f"0x{status:08x}"
        r.check(f"deep: rename from past the path limit to the top (0x{status:08x})",
                lambda: status == STATUS_SUCCESS and exists(TEST_DIR + "\\from_deep") and not exists(sibling + "\\g"))
        status = move(TEST_DIR + "\\from_deep", bottom + "\\g")
        record["rename_down"] = f"0x{status:08x}"
        r.check(f"deep: rename from the top to the deepest directory (0x{status:08x})",
                lambda: status == STATUS_SUCCESS and exists(bottom + "\\g") and not exists(TEST_DIR + "\\from_deep"))
        # Hard links between the same directories go through the emulated
        # linkat(), which names the directories the same way. smbd may refuse
        # SMB hard links on a share; then nothing may be created.
        def file_id(name: str) -> bytes:
            f = client.open(name, FILE_READ_ATTRIBUTES, 1, FILE_NON_DIRECTORY_FILE)
            try:
                return client.query_info(f, 6)[:8]  # FileInternalInformation
            finally:
                f.close()

        def link(src: str, dst: str) -> int:
            def go() -> None:
                f = client.open(src, FILE_READ_ATTRIBUTES, 1, FILE_NON_DIRECTORY_FILE)
                try:
                    client.link(f, dst)
                finally:
                    f.close()
            return status_of(go)[0]

        for key, dst, where in (("link_sibling", sibling + "\\glink", "a sibling"),
                                ("link_top", TEST_DIR + "\\glink", "the top of the test folder")):
            status = link(bottom + "\\g", dst)
            record[key] = f"0x{status:08x}"
            if status == STATUS_SUCCESS:
                r.check(f"deep: hard link from the deepest directory to {where} names the same file",
                        lambda dst=dst: file_id(dst) == file_id(bottom + "\\g"))
                status_of(lambda dst=dst: client.open(dst, DELETE, 1,
                                                      FILE_NON_DIRECTORY_FILE | FILE_DELETE_ON_CLOSE).close())
                r.check(f"deep: deleting the link to {where} keeps the original",
                        lambda dst=dst: exists(bottom + "\\g") and not exists(dst))
            else:
                r.check(f"deep: a refused hard link to {where} creates nothing (0x{status:08x})",
                        lambda dst=dst: exists(bottom + "\\g") and not exists(dst))
        status_of(lambda: client.open(sibling, DELETE, 1, FILE_DIRECTORY_FILE | FILE_DELETE_ON_CLOSE).close())
        # Delete bottom up; record where smbd first refuses (the full-path limit).
        for name in (bottom + "\\g", parent + "\\up"):
            status_of(lambda name=name: client.open(name, DELETE, 1,
                                                    FILE_NON_DIRECTORY_FILE | FILE_DELETE_ON_CLOSE).close())
        refusals = []
        while len(path) > 1:
            full = "\\".join(path)
            status, _ = status_of(lambda: client.open(full, DELETE, 1,
                                                      FILE_DIRECTORY_FILE | FILE_DELETE_ON_CLOSE).close())
            if status != STATUS_SUCCESS:
                refusals.append((len(path) - 2, status))
            path.pop()
        record["delete_refused_levels"] = [level for level, _ in refusals]
        r.check(f"deep: deletion refuses only past the full-path limit, cleanly "
                f"(refused levels {record['delete_refused_levels']})",
                lambda: all(status == STATUS_OBJECT_NAME_INVALID for _, status in refusals) and
                all(level > 25 for level, _ in refusals))
        return record
    finally:
        client.close()
        # Whatever smbd could not delete, remove on the device.
        device.sh(f"rm -rf {shlex.quote(device.dir)}/deep", check=False)


def deletes(r: Results, device: Device, record: dict) -> None:
    from smbprotocol.exceptions import SMBResponseException

    base = f"{device.dir}/delete"
    device.sh(f"mkdir -p {shlex.quote(base)}/empty {shlex.quote(base)}/full {shlex.quote(base)}/target "
              f"{shlex.quote(base)}/src {shlex.quote(base)}/free && : > {shlex.quote(base)}/full/f && "
              f": > {shlex.quote(base)}/src/marker")
    client = Client(device)
    statuses = record.setdefault("delete", {})
    try:
        def delete_empty() -> bool:
            h = client.open(rel("delete", "empty"), DELETE | FILE_READ_ATTRIBUTES, 1, FILE_DIRECTORY_FILE)
            client.delete_on_close(h)
            h.close()
            return not on_disk(device, f"{base}/empty")

        r.check("delete: an empty directory is deleted on close", delete_empty)

        def refuse_full() -> bool:
            h = client.open(rel("delete", "full"), DELETE | FILE_READ_ATTRIBUTES, 1, FILE_DIRECTORY_FILE)
            try:
                client.delete_on_close(h)
            except SMBResponseException as error:
                statuses["non_empty"] = f"0x{error.status:08x}"
                return error.status == STATUS_DIRECTORY_NOT_EMPTY
            finally:
                h.close()
            return False

        r.check("delete: a non-empty directory is DIRECTORY_NOT_EMPTY", refuse_full)
        r.check("delete: the non-empty directory and its file remain",
                lambda: on_disk(device, f"{base}/full/f"))

        # Renaming a directory onto an existing empty one: refused without
        # replace; with it, Samba may refuse or replace (POSIX rename() does).
        # Either way nothing may be lost or left half done.
        for replace in (False, True):
            h = client.open(rel("delete", "src"), DELETE | FILE_READ_ATTRIBUTES, 1, FILE_DIRECTORY_FILE)
            try:
                status, _ = status_of(lambda: client.rename(h, rel("delete", "target"), replace))
            finally:
                h.close()
            statuses[f"rename_dir_over_dir_replace={replace}"] = f"0x{status:08x}"
            moved = on_disk(device, f"{base}/target/marker") and not on_disk(device, f"{base}/src")
            kept = on_disk(device, f"{base}/src/marker") and on_disk(device, f"{base}/target")
            if replace:
                r.check(f"delete: a directory renamed onto an empty one with replace moves or stays whole "
                        f"(0x{status:08x})",
                        lambda status=status, moved=moved, kept=kept:
                        (status == STATUS_SUCCESS and moved) or (status != STATUS_SUCCESS and kept))
            else:
                r.check(f"delete: a directory renamed onto an existing one without replace is refused "
                        f"(0x{status:08x})",
                        lambda status=status, kept=kept: status == STATUS_OBJECT_NAME_COLLISION and kept)

        def rename_dir() -> bool:
            h = client.open(rel("delete", "free"), DELETE | FILE_READ_ATTRIBUTES, 1, FILE_DIRECTORY_FILE)
            client.rename(h, rel("delete", "renamed-dir"))
            h.close()
            return on_disk(device, f"{base}/renamed-dir") and not on_disk(device, f"{base}/free")

        r.check("delete: a directory renames to a free name", rename_dir)
    finally:
        client.close()


def times(r: Results, device: Device) -> None:
    base = f"{device.dir}/times"
    device.sh(f"mkdir -p {shlex.quote(base)}/dir && : > {shlex.quote(base)}/file")
    client = Client(device)
    try:
        for name, options in (("file", FILE_NON_DIRECTORY_FILE), ("dir", FILE_DIRECTORY_FILE)):
            def settime(name: str = name, options: int = options) -> bool:
                h = client.open(rel("times", name), FILE_READ_ATTRIBUTES | FILE_WRITE_ATTRIBUTES, 1, options)
                client.set_write_time(h, SETTIME_FILETIME)
                h.close()
                h = client.open(rel("times", name), FILE_READ_ATTRIBUTES, 1, options)
                try:
                    return client.write_time(h) == SETTIME_FILETIME
                finally:
                    h.close()

            r.check(f"times: last-write time set through an attribute-only handle on a {name}", settime)
        time_range(r, client, device.sh("uname -r").strip().startswith("4."))
    finally:
        client.close()


def time_range(r: Results, client, netbsd4: bool) -> None:
    """Write times at and past what the device keeps come back clamped to it,
    not as 30828 ("never") or wrapped decades away."""
    for label, unix in TIME_RANGE:
        want = kept_time(unix, netbsd4)

        def settime(unix: int = unix, want: int = want) -> bool:
            h = client.open(rel("times", "file"), FILE_READ_ATTRIBUTES | FILE_WRITE_ATTRIBUTES, 1,
                            FILE_NON_DIRECTORY_FILE)
            client.set_write_time(h, filetime(unix))
            h.close()
            h = client.open(rel("times", "file"), FILE_READ_ATTRIBUTES, 1, FILE_NON_DIRECTORY_FILE)
            try:
                return client.write_time(h) == filetime(want)
            finally:
                h.close()

        shown = (datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=want)).strftime("%Y-%m-%d %H:%M:%S")
        r.check(f"times: last-write time {label} reads back as {shown}", settime)


def tree_contents(root: Path) -> dict[str, bytes | None]:
    """Relative path -> file bytes (None for a directory). HFS+ stores names
    decomposed (NFD), so a name written composed comes back decomposed; compare
    names in NFD."""
    out: dict[str, bytes | None] = {}
    for path in root.rglob("*"):
        key = unicodedata.normalize("NFD", str(path.relative_to(root)))
        out[key] = None if path.is_dir() else path.read_bytes()
    return out


def mac_cases(r: Results, device: Device) -> None:
    mountpoint = Path(tempfile.mkdtemp(prefix="tc-dir-mnt-"))
    local = Path(tempfile.mkdtemp(prefix="tc-dir-src-"))
    try:
        mount("smb", device, mountpoint)
        share_dir = mountpoint / TEST_DIR

        # A tree with Unicode (composed and decomposed), spaces, and the characters
        # catia maps: 40 files in 8 directories two levels deep.
        tree = local / "tree"
        for i in range(2):
            top = tree / f"Top {i:02d}"
            for j in range(4):
                sub = top / (unicodedata.normalize("NFD", f"café {j}") if j % 2 else f"x:y {j}")
                sub.mkdir(parents=True)
                for k in range(5):
                    (sub / f"f{k:02d} é.txt").write_text(f"{i}/{j}/{k}\n")
        started = time.monotonic()
        copy = subprocess.run(["ditto", str(tree), str(share_dir / "tree")], capture_output=True, text=True,
                              timeout=3600)
        r.check(f"mac: ditto copies a 40-file tree ({time.monotonic() - started:.0f} s)",
                lambda: copy.returncode == 0)
        r.check("mac: the copy has every name and byte (names compared as HFS+ stores them, NFD)",
                lambda: tree_contents(tree) == tree_contents(share_dir / "tree"))
        r.check("mac: find sees every file",
                lambda: sum(len(f) for _, _, f in os.walk(share_dir / "tree")) == 40)

        def renames() -> bool:
            (share_dir / "tree" / "Top 00").rename(share_dir / "tree" / "Renamed Top")
            (share_dir / "tree" / "Top 01").rename(share_dir / "tree" / "top 01 tmp")
            (share_dir / "tree" / "top 01 tmp").rename(share_dir / "tree" / "top 01")
            names = set(os.listdir(share_dir / "tree"))
            return {"Renamed Top", "top 01"} <= names and "Top 00" not in names and \
                (share_dir / "tree" / "Renamed Top" / "x:y 0" / "f00 é.txt").read_text() == "0/0/0\n"

        r.check("mac: directory renames, including a case-only rename", renames)
        started = time.monotonic()
        shutil.rmtree(share_dir / "tree")
        r.check(f"mac: rm -rf of the tree ({time.monotonic() - started:.0f} s)",
                lambda: not on_disk(device, f"{device.dir}/tree"))

        big = share_dir / "big"
        big.mkdir()
        for i in range(150):
            (big / f"e{i:05d}").write_bytes(b"")
        first = sorted(os.listdir(big))
        second = sorted(os.listdir(big))
        r.check("mac: a 150-entry directory lists completely, twice",
                lambda: first == second == [f"e{i:05d}" for i in range(150)])
        shutil.rmtree(big)

        bundle = share_dir / "test.sparsebundle"
        create = subprocess.run(["hdiutil", "create", "-size", "100m", "-type", "SPARSEBUNDLE", "-fs", "HFS+J",
                                 "-volname", "tcdirtest", str(bundle)], capture_output=True, text=True, timeout=600)
        r.check("mac: hdiutil creates a sparsebundle on the share", lambda: create.returncode == 0)
        if create.returncode == 0:
            attach = subprocess.run(["hdiutil", "attach", "-nobrowse", "-plist", str(bundle)],
                                    capture_output=True, text=True, timeout=600)
            ok = attach.returncode == 0
            if ok:
                import plistlib

                entities = plistlib.loads(attach.stdout.encode())["system-entities"]
                volume = next(e["mount-point"] for e in entities if "mount-point" in e)
                with open(Path(volume) / "data.bin", "wb") as out:
                    for _ in range(20):
                        out.write(os.urandom(1 << 20))
                detach = subprocess.run(["hdiutil", "detach", volume], capture_output=True, text=True, timeout=600)
                ok = detach.returncode == 0
            r.check("mac: the sparsebundle attaches, takes 20 MiB and detaches", lambda: ok)
            bands_mount = sorted(os.listdir(bundle / "bands"))
            bands_device = sorted(device.sh(f"ls {shlex.quote(device.dir)}/test.sparsebundle/bands").split())
            r.check(f"mac: the band directory lists the same over SMB and on disk ({len(bands_mount)} bands)",
                    lambda: bands_mount == bands_device and len(bands_mount) > 0)
            shutil.rmtree(bundle)
    finally:
        unmount(mountpoint)
        shutil.rmtree(local, ignore_errors=True)
        try:
            mountpoint.rmdir()
        except OSError:
            pass


def listing_changes(r: Results, device: Device, record: dict, count: int = 1200) -> None:
    """A directory that changes while one handle lists it in small responses.
    On NetBSD 4, HFS loses a listing's place whenever a descriptor on the
    directory is closed, and smbd closes one for every create and delete, so
    this left files behind and repeated names until the emulated fdopendir()
    read the whole directory up front."""
    base = f"{device.dir}/changing"

    def make(n: int) -> None:
        device.sh(f"rm -rf {shlex.quote(base)} && mkdir {shlex.quote(base)} && cd {shlex.quote(base)} && "
                  f"i=0; while [ $i -lt {n} ]; do : > t$(printf %04d $i).txt; i=$((i+1)); done")

    def pages(handle, after_page, restart: bool = True) -> list[str]:
        from smbprotocol.exceptions import SMBResponseException
        from smbprotocol.file_info import FileInformationClass

        names: list[str] = []
        page = 0
        while True:
            try:
                entries = handle.query_directory("*", FileInformationClass.FILE_NAMES_INFORMATION,
                                                 flags=1 if page == 0 and restart else 0, max_output=2048)
            except SMBResponseException as error:
                if error.status in (STATUS_NO_MORE_FILES, STATUS_NO_SUCH_FILE):
                    return names
                raise
            got = [e["file_name"].get_value().decode("utf-16-le") for e in entries]
            got = [n for n in got if n not in (".", "..")]
            names.extend(got)
            after_page(page, got)
            page += 1
            if page > 10 * count:  # a listing that never ends
                return names

    client = Client(device)
    try:
        # Delete every page's files before asking for the next page.
        make(count)
        h = client.open(rel("changing"), ACCESS["list"], 1, FILE_DIRECTORY_FILE)

        def delete_page(page: int, got: list[str]) -> None:
            for name in got:
                client.open(rel("changing", name), DELETE, 1,
                            FILE_NON_DIRECTORY_FILE | FILE_DELETE_ON_CLOSE).close()

        seen = pages(h, delete_page)
        h.close()
        left = device.sh(f"ls {shlex.quote(base)}").split()
        record["delete_left"] = len(left)
        r.check(f"listing-changes: deleting each page as it arrives lists all {count} files once "
                f"and leaves none ({len(seen)} listed, {len(left)} left)",
                lambda: sorted(seen) == [f"t{i:04d}.txt" for i in range(count)] and not left)

        # Create files that sort before the listing's position while it runs.
        make(count)
        h = client.open(rel("changing"), ACCESS["list"], 1, FILE_DIRECTORY_FILE)
        created: list[str] = []

        def create_during(page: int, got: list[str]) -> None:
            if page < 5:
                for i in range(10):
                    name = f"a{page:02d}{i:02d}.txt"
                    client.open(rel("changing", name), GENERIC_WRITE, 2, FILE_NON_DIRECTORY_FILE).close()
                    created.append(name)

        seen = pages(h, create_during)
        originals = [n for n in seen if n.startswith("t")]
        record["create_listed"] = len(seen)
        r.check(f"listing-changes: creating files during the listing repeats nothing "
                f"({len(seen)} listed for {count} + {len(created)})",
                lambda: sorted(originals) == [f"t{i:04d}.txt" for i in range(count)]
                and len(set(seen)) == len(seen) and set(seen) <= set(originals) | set(created))

        # Querying again after the end, with files created before the
        # listing's position, repeats nothing (HFS returned the last entries
        # again there; NetBSD 6's readdir() now stays at the end).
        for i in range(5):
            client.open(rel("changing", f"e{i:02d}.txt"), GENERIC_WRITE, 2, FILE_NON_DIRECTORY_FILE).close()
        after_end = pages(h, lambda page, got: None, restart=False)
        record["after_end"] = len(after_end)
        r.check(f"listing-changes: querying again after the end repeats nothing ({len(after_end)} more)",
                lambda: all(n.startswith("e") for n in after_end) and len(set(after_end)) == len(after_end))

        # A restarted listing on the same handle shows what changed since.
        client.open(rel("changing", "z_new.txt"), GENERIC_WRITE, 2, FILE_NON_DIRECTORY_FILE).close()
        client.open(rel("changing", "t0000.txt"), DELETE, 1,
                    FILE_NON_DIRECTORY_FILE | FILE_DELETE_ON_CLOSE).close()
        again = [n for n in client.names(h, flags=1) if n not in (".", "..")]
        h.close()
        r.check("listing-changes: a restarted listing shows a new file and not a deleted one",
                lambda: "z_new.txt" in again and "t0000.txt" not in again and "t0001.txt" in again)
    finally:
        client.close()
        device.sh(f"rm -rf {shlex.quote(base)}", check=False)


def read_only(r: Results, device: Device, record: dict) -> None:
    """MAXIMUM_ALLOWED on a read-only file. smbd works as root on the appliance
    ("force user = root"), and root used to get full access including write,
    which the read-only attribute then refused (patch 0066)."""
    from smbprotocol.create_contexts import (CreateContextName, SMB2CreateContextRequest,
                                             SMB2CreateQueryMaximalAccessRequest)
    from smbprotocol.open import ImpersonationLevel, Open

    client = Client(device)
    name = rel("readonly.txt")
    try:
        client.open(name, GENERIC_WRITE, 2, FILE_NON_DIRECTORY_FILE, attrs=FILE_ATTRIBUTE_READONLY).close()

        def granted() -> int:
            h = client.open(name, MAXIMUM_ALLOWED, 1, FILE_NON_DIRECTORY_FILE)
            try:
                return struct.unpack_from("<I", client.query_info(h, 8))[0]  # FileAccessInformation
            finally:
                h.close()

        status, mask = status_of(granted)
        record["maximum_allowed"] = f"0x{status:08x}"
        r.check(f"read-only: MAXIMUM_ALLOWED opens a read-only file (0x{status:08x})",
                lambda: status == STATUS_SUCCESS)
        if status == STATUS_SUCCESS:
            record["granted"] = f"0x{mask:08x}"
            r.check(f"read-only: it grants reading but not writing (0x{mask:08x})",
                    lambda: mask & FILE_READ_DATA and not mask & (FILE_WRITE_DATA | FILE_APPEND_DATA))
        status, _ = status_of(lambda: client.open(name, FILE_WRITE_DATA, 1, FILE_NON_DIRECTORY_FILE).close())
        r.check(f"read-only: opening it for writing is still refused (0x{status:08x})",
                lambda: status == STATUS_ACCESS_DENIED)

        def maximal_access() -> int:
            ctx = SMB2CreateContextRequest()
            ctx["buffer_name"] = CreateContextName.SMB2_CREATE_QUERY_MAXIMAL_ACCESS_REQUEST
            ctx["buffer_data"] = SMB2CreateQueryMaximalAccessRequest()
            h = Open(client.tree, name)
            replies = h.create(ImpersonationLevel.Impersonation, FILE_READ_ATTRIBUTES, 0, SHARE_ALL, 1,
                               FILE_NON_DIRECTORY_FILE, create_contexts=[ctx])
            try:
                return next(c["maximal_access"].get_value() for c in replies or []
                            if "maximal_access" in getattr(c, "fields", {}))
            finally:
                h.close()

        status, mxac = status_of(maximal_access)
        r.check(f"read-only: the maximal-access context still ignores the attribute (0x{mxac or 0:08x})",
                lambda: status == STATUS_SUCCESS and mxac & FILE_WRITE_DATA)
        # A directory with the read-only attribute is not read-only to Windows.
        client.open(rel("readonly-dir"), GENERIC_READ, 2, FILE_DIRECTORY_FILE, attrs=FILE_ATTRIBUTE_READONLY).close()
        status, _ = status_of(lambda: client.open(rel("readonly-dir"), MAXIMUM_ALLOWED, 1,
                                                  FILE_DIRECTORY_FILE).close())
        r.check(f"read-only: MAXIMUM_ALLOWED opens a read-only directory (0x{status:08x})",
                lambda: status == STATUS_SUCCESS)
    finally:
        client.close()
        device.sh(f"rm -rf {shlex.quote(device.dir)}/readonly.txt {shlex.quote(device.dir)}/readonly-dir",
                  check=False)


SMB_CASES = ("open-matrix", "listing", "mapped-names", "renamed-open", "deep", "delete", "times",
             "listing-changes", "read-only")


# --quick (AGENTS.md "Test tiers"): no Mac case, and 300 files for
# listing-changes, still more than one 4 KiB getdents() of NetBSD 4 entries.
QUICK_LISTING_FILES = 300


def run_case(case: str, r: Results, device: Device, record: dict, quick: bool = False) -> None:
    if case == "open-matrix":
        open_matrix(r, device, record)
    elif case == "listing":
        listing(r, device)
    elif case == "mapped-names":
        mapped_names(r, device)
    elif case == "renamed-open":
        renamed_open(r, device)
    elif case == "deep":
        record["deep"] = deep(r, device)
    elif case == "delete":
        deletes(r, device, record)
    elif case == "times":
        times(r, device)
    elif case == "listing-changes":
        listing_changes(r, device, record.setdefault("listing-changes", {}),
                        QUICK_LISTING_FILES if quick else 1200)
    elif case == "read-only":
        read_only(r, device, record.setdefault("read-only", {}))
    elif case == "mac":
        mac_cases(r, device)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", default=".env")
    parser.add_argument("--share")
    parser.add_argument("--case", action="append", choices=SMB_CASES + ("mac",))
    parser.add_argument("--no-mac", action="store_true")
    parser.add_argument("--quick", action="store_true",
                        help="the quick tier: no Mac case, a smaller listing-changes directory")
    parser.add_argument("--record")
    parser.add_argument("--compare")
    args = parser.parse_args()
    env = parse_env_file(Path(args.env))
    device = Device(env, args.share)
    device.dir = f"{device.root}/{TEST_DIR}"
    cases = args.case or list(SMB_CASES) + ([] if args.no_mac or args.quick else ["mac"])
    r = Results()
    record: dict = {}
    device.sh(f"rm -rf {shlex.quote(device.dir)} && mkdir -p {shlex.quote(device.dir)}")
    try:
        for case in cases:
            print(f"== {case}", flush=True)
            start = time.monotonic()
            try:
                run_case(case, r, device, record, args.quick)
            except Exception as error:  # one broken case must not hide the others
                r.check(f"{case}: ran without an unexpected error",
                        lambda error=error: (_ for _ in ()).throw(error))
            print(f"== {case} took {time.monotonic() - start:.0f} s", flush=True)
    finally:
        device.sh(f"rm -rf {shlex.quote(device.dir)}", check=False)
    if args.record:
        Path(args.record).write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    if args.compare:
        before = json.loads(Path(args.compare).read_text())
        for line in compare_records(before, record):
            print(line, flush=True)
    print(f"{r.passed} passed, {len(r.failed)} failed", flush=True)
    for name in r.failed:
        print(f"  FAILED {name}", flush=True)
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
