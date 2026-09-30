"""HFS file-growth device suite (Samba patch 0065), run from a Mac against a
device whose running smbd carries the patch.

    .venv/bin/python -m tests.samba.growth_device --env .env.backup6 [--share NAME] [--aio]

HFS has no sparse files: growing a file allocates every block up to its new
end. Without 0065, smbtorture's smb2.rw.invalid (one byte written at
MAXFILESIZE - 1, 16 TiB) kept a NetBSD 6 device's kernel busy for over 45
minutes and rebooted a NetBSD 4 one (2026-09-28). So before sending anything,
the suite looks for 0065's refusal message in the smbd binary the device runs
(/mnt/Memory/samba4/sbin/smbd, read with the device's sed) and stops if it is
missing. Run it only against a build of this checkout.

The refused requests grow a file past the volume's total size, which no amount
of freed space can make fit, and must fail with STATUS_DISK_FULL within a few
seconds without changing the file:

- write:maxfilesize: smb2.rw.invalid's sequence. 64 KiB at offset 0, then one
  byte at MAXFILESIZE - 1 (DISK_FULL, as Windows answers), then a zero-length
  write at MAXFILESIZE (success, nothing written).
- write:past-volume: one byte just past the volume's size.
- eof:past-volume: SET_INFO FileEndOfFileInformation past the volume's size.
- copychunk:past-volume: a server-side copy of one byte to there.
- stream:resource: one byte written there through a file's AFP_Resource
  stream, which patch 0056 serves from the native resource fork; the fork
  keeps its 10 bytes, and can still grow by 100 MiB.

--aio runs the suite with the production aio_fork settings (the manager's
VFS_AIO_FORK_ENABLED: aio_fork in vfs objects, aio sizes 1, two helpers),
written to the running smb.conf in RAM only and restored at the end. SMB2
never sends stream writes or zero-length writes asynchronously; the other
writes then go through aio_fork, and with debug logging the suite checks that
smbd logged the refused one completing there.

The allowed requests must keep working: a write 32 MiB past the end (inside
the growth 0065 never checks), an end of file set to 96 MiB (checked, and
fits) and back to 0, and 96 MiB of sequential 4 MiB writes (smbd's cached
size falls behind them). Every file is opened delete-on-close in a
`__tc_growth_test__` folder, which the suite removes at the end.

mac:sparsebundle mounts the share and does what Time Machine does to its
sparse bundle, with the band size the devices' backups use (487,854,080
bytes): creates an HFS+ image that ends 400 MiB into a band, fills 600 MiB
across two bands, grows the image so that it ends more than 64 MiB into a
later band, and reads the data back. Each image end starts its band that far
past the band's beginning, growth beyond the 64 MiB 0065 leaves unchecked, so
both must pass the free-space check.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import shlex
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from timecapsulesmb.core.config import parse_env_file
from tests.samba.links_device import Device, Results, mount, unmount

TEST_DIR = "__tc_growth_test__"
RUNNING_SMBD = "/mnt/Memory/samba4/sbin/smbd"
CONF = "/mnt/Memory/samba4/etc/smb.conf"
# The manager's aio_fork settings (build/native/samba/config.c) for this run.
AIO_SED = """s/^    aio read size = 0$/    aio read size = 1/
s/^    aio write size = 0$/    aio write size = 1/
/^    vfs objects = .*xattr_tdb$/{
s/$/ aio_fork/
a\\
    aio_fork:max_children = 2
}
"""
# aio_pwrite_smb2_done() at log level 10, for a write refused in pwrite_fsync_send().
AIO_REFUSAL_LOG = "pwrite_recv returned -1, err = No space left on device"
# Part of the refusal's log format in source3/smbd/tc_file_growth.c.
PATCH_MARKER = "bytes needs more than the"
STATUS_DISK_FULL = 0xC000007F
# [MS-FSA] MAXFILESIZE, as in smb2.rw.invalid.
MAXFILESIZE = 0xFFFFFFF0000
MiB = 1024 * 1024
GiB = 1024 * MiB
# A refusal happens before any allocation; allow for a slow link and device.
REFUSAL_SECONDS = 10
FSCTL_SRV_REQUEST_RESUME_KEY = 0x00140078
FSCTL_SRV_COPYCHUNK_WRITE = 0x001480F2
# Time Machine's band size on the devices' backup sparse bundles.
TM_BAND_BYTES = 487854080
# Growth 0065 allows without looking at the free space (TC_GROWTH_UNCHECKED).
UNCHECKED_GROWTH = 64 * MiB
CASES = ("write:maxfilesize", "write:past-volume", "eof:past-volume", "copychunk:past-volume", "stream:resource",
         "allowed:hole", "allowed:eof", "allowed:sequential", "mac:sparsebundle")
# The quick tier (AGENTS.md "Test tiers"): both refusals of a write and a
# hole that fits; the stream, copy, end-of-file and Mac cases are full only.
QUICK_CASES = ("write:maxfilesize", "write:past-volume", "allowed:hole")


def running_smbd_has_patch(device: Device) -> bool:
    """Whether the smbd binary the device runs contains 0065's refusal message.

    The device has no grep or hash tools; its sed reads the binary and prints
    only the marker, so nothing large crosses the SSH connection."""
    out = device.sh(f"sed -n 's/.*\\({PATCH_MARKER}\\).*/\\1/p' {RUNNING_SMBD} | sed -n 1p", check=False)
    return out.strip() == PATCH_MARKER


def volume_bytes(device: Device) -> tuple[int, int]:
    """(total, available) bytes of the share's volume, from df -P -k as dfree.sh reads it."""
    line = device.sh(f"df -P -k {shlex.quote(device.root)} | sed -n 2p").split()
    return int(line[1]) * 1024, int(line[3]) * 1024


def on_disk_size(device: Device, name: str) -> int:
    """The file's size as the device's ls sees it (the device has no stat)."""
    fields = device.sh(f"ls -ln {shlex.quote(f'{device.root}/{TEST_DIR}/{name}')}").split()
    return int(fields[4])


class Client:
    def __init__(self, device: Device) -> None:
        from smbprotocol.connection import Connection
        from smbprotocol.session import Session
        from smbprotocol.tree import TreeConnect

        self.conn = Connection(uuid.uuid4(), device.host, 445)
        self.conn.connect()
        session = Session(self.conn, device.env.get("TC_SAMBA_USER") or "root", device.env["TC_PASSWORD"])
        session.connect()
        self.tree = TreeConnect(session, rf"\\{device.host}\{device.share}")
        self.tree.connect()

    def create(self, name: str):
        """Create name for reading and writing, deleted when its last handle closes."""
        from smbprotocol.open import (CreateDisposition, CreateOptions, FilePipePrinterAccessMask,
                                      ImpersonationLevel, Open, ShareAccess)

        handle = Open(self.tree, f"{TEST_DIR}\\{name}")
        handle.create(ImpersonationLevel.Impersonation,
                      FilePipePrinterAccessMask.GENERIC_READ | FilePipePrinterAccessMask.GENERIC_WRITE
                      | FilePipePrinterAccessMask.DELETE,
                      0x80, ShareAccess.FILE_SHARE_READ | ShareAccess.FILE_SHARE_DELETE,
                      CreateDisposition.FILE_OVERWRITE_IF,
                      CreateOptions.FILE_NON_DIRECTORY_FILE | CreateOptions.FILE_DELETE_ON_CLOSE)
        return handle

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.conn.disconnect()


def _send(handle, request):
    sent = handle.connection.send(request, handle.tree_connect.session.session_id,
                                  handle.tree_connect.tree_connect_id)
    return handle.connection.receive(sent)


def set_end_of_file(handle, size: int) -> None:
    """SMB2 SET_INFO FileEndOfFileInformation (smbprotocol's Open has no set_info)."""
    from smbprotocol.file_info import FileEndOfFileInformation
    from smbprotocol.open import SMB2SetInfoRequest

    info = FileEndOfFileInformation()
    info["end_of_file"] = size
    request = SMB2SetInfoRequest()
    request["info_type"] = info.INFO_TYPE
    request["file_info_class"] = info.INFO_CLASS
    request["file_id"] = handle.file_id
    request["buffer"] = info
    _send(handle, request)


def _ioctl(handle, ctl_code: int, payload: bytes, max_output: int) -> bytes:
    from smbprotocol.ioctl import IOCTLFlags, SMB2IOCTLRequest, SMB2IOCTLResponse

    request = SMB2IOCTLRequest()
    request["ctl_code"] = ctl_code
    request["file_id"] = handle.file_id
    request["max_input_response"] = 0
    request["max_output_response"] = max_output
    request["flags"] = IOCTLFlags.SMB2_0_IOCTL_IS_FSCTL
    request["buffer"] = payload
    response = SMB2IOCTLResponse()
    response.unpack(_send(handle, request)["data"].get_value())
    return response["buffer"].get_value()


def copy_chunk(source, dest, source_offset: int, target_offset: int, length: int) -> None:
    """FSCTL_SRV_COPYCHUNK_WRITE of one chunk (MS-SMB2 2.2.31.1)."""
    resume_key = _ioctl(source, FSCTL_SRV_REQUEST_RESUME_KEY, b"", 32)[:24]
    chunk = struct.pack("<QQII", source_offset, target_offset, length, 0)
    _ioctl(dest, FSCTL_SRV_COPYCHUNK_WRITE, resume_key + struct.pack("<II", 1, 0) + chunk, 12)


def refused(action) -> tuple[bool, str]:
    """Whether action fails with DISK_FULL within REFUSAL_SECONDS, and what happened."""
    from smbprotocol.exceptions import SMBResponseException

    start = time.monotonic()
    try:
        action()
    except SMBResponseException as error:
        elapsed = time.monotonic() - start
        ok = error.status == STATUS_DISK_FULL and elapsed < REFUSAL_SECONDS
        return ok, f"0x{error.status:08x} after {elapsed:.1f} s"
    return False, f"succeeded after {time.monotonic() - start:.1f} s"


def refusal_check(r: Results, device: Device, name: str, file_name: str, size: int, action) -> None:
    ok, detail = refused(action)
    print(f"    {name}: {detail}", flush=True)
    r.check(f"{name}: DISK_FULL before anything is allocated", lambda: ok)
    r.check(f"{name}: the file keeps its {size} bytes", lambda: on_disk_size(device, file_name) == size)


def write_maxfilesize(r: Results, device: Device, client: Client, total: int) -> None:
    handle = client.create("rw-invalid.bin")
    try:
        handle.write(b"\0" * 65536, 0)
        refusal_check(r, device, "write:maxfilesize", "rw-invalid.bin", 65536,
                      lambda: handle.write(b"\0", MAXFILESIZE - 1))
        r.check("write:maxfilesize: a zero-length write at MAXFILESIZE writes nothing",
                lambda: handle.write(b"", MAXFILESIZE) == 0)
    finally:
        handle.close()


def write_past_volume(r: Results, device: Device, client: Client, total: int) -> None:
    handle = client.create("write-past.bin")
    try:
        refusal_check(r, device, "write:past-volume", "write-past.bin", 0,
                      lambda: handle.write(b"x", total + GiB))
    finally:
        handle.close()


def eof_past_volume(r: Results, device: Device, client: Client, total: int) -> None:
    handle = client.create("eof-past.bin")
    try:
        handle.write(b"eof", 0)
        refusal_check(r, device, "eof:past-volume", "eof-past.bin", 3,
                      lambda: set_end_of_file(handle, total + GiB))
    finally:
        handle.close()


def copychunk_past_volume(r: Results, device: Device, client: Client, total: int) -> None:
    source = client.create("chunk-source.bin")
    dest = client.create("chunk-dest.bin")
    try:
        source.write(b"c", 0)
        refusal_check(r, device, "copychunk:past-volume", "chunk-dest.bin", 0,
                      lambda: copy_chunk(source, dest, 0, total + GiB, 1))
    finally:
        dest.close()
        source.close()


def stream_resource(r: Results, device: Device, client: Client, total: int) -> None:
    from smbprotocol.open import (CreateDisposition, CreateOptions, FilePipePrinterAccessMask,
                                  ImpersonationLevel, Open, ShareAccess)

    base = client.create("rsrc.bin")
    base.write(b"d", 0)
    fork = Open(client.tree, f"{TEST_DIR}\\rsrc.bin:AFP_Resource")
    fork.create(ImpersonationLevel.Impersonation,
                FilePipePrinterAccessMask.GENERIC_READ | FilePipePrinterAccessMask.GENERIC_WRITE,
                0x80, ShareAccess.FILE_SHARE_READ | ShareAccess.FILE_SHARE_WRITE | ShareAccess.FILE_SHARE_DELETE,
                CreateDisposition.FILE_OPEN_IF, CreateOptions.FILE_NON_DIRECTORY_FILE)
    try:
        fork.write(b"resource!!", 0)
        refusal_check(r, device, "stream:resource", "rsrc.bin/..namedfork/rsrc", 10,
                      lambda: fork.write(b"r", total + GiB))
        r.check("stream:resource: the fork can still grow by 100 MiB",
                lambda: fork.write(b"r", 100 * MiB) == 1
                and on_disk_size(device, "rsrc.bin/..namedfork/rsrc") == 100 * MiB + 1)
    finally:
        fork.close()
        base.close()


def smbd_parent_hup(device: Device) -> None:
    """Make the running smbd reread smb.conf."""
    device.sh('M=$(ps axo pid,command | sed -n "s/^ *\\([0-9]*\\) service: role=manager.*/\\1/p"); '
              'P=$(ps axo pid,ppid,command | sed -n "s/^ *\\([0-9]*\\) *$M \\/mnt\\/Memory\\/samba4\\/sbin\\/smbd -F.*/\\1/p"); '
              'kill -HUP $P')
    time.sleep(3)


@contextlib.contextmanager
def aio_fork_enabled(device: Device):
    """The production aio_fork settings in the running smb.conf, restored afterwards.

    Edited on the device (the SSH output is not byte-exact); the original is
    kept beside it until the run ends."""
    backup, script = CONF + ".tc-growth", "/mnt/Memory/tc-growth-aio.sed"
    device.put(script, AIO_SED.encode())
    device.sh(f"cp {CONF} {backup} && sed -f {script} {backup} > {CONF}.new && mv {CONF}.new {CONF} "
              f"&& chmod 600 {CONF}; rm -f {script}")
    try:
        conf = device.sh(f"cat {CONF}")
        if "aio write size = 1" not in conf or "aio_fork:max_children = 2" not in conf:
            raise RuntimeError("could not enable aio_fork in the running smb.conf")
        smbd_parent_hup(device)
        yield
    finally:
        device.sh(f"mv {backup} {CONF} && chmod 600 {CONF}")
        smbd_parent_hup(device)


def aio_refusal_logged(r: Results, device: Device) -> None:
    """The refused write completed through pwrite_fsync_send(), per smbd's log tail."""
    conf = device.sh(f"cat {CONF}")
    if "log level = 10" not in conf:
        print("    aio: smbd is not logging at level 10; not checking which path refused", flush=True)
        return
    log = device.sh(f"sed -n 's/^ *log file = //p' {CONF}").strip()
    size = int(device.sh(f"ls -ln {shlex.quote(log)}").split()[4])
    # The last 4 MiB, read with dd: the log can be many GB and the device slow.
    tail = device.sh(f"dd if={shlex.quote(log)} bs=65536 skip={max(0, size // 65536 - 64)} 2>/dev/null "
                     f"| sed -n '/{AIO_REFUSAL_LOG}/p'")
    r.check("aio: smbd logged the refused write completing through aio_fork", lambda: AIO_REFUSAL_LOG in tail)


def allowed_hole(r: Results, device: Device, client: Client, total: int) -> None:
    handle = client.create("hole.bin")
    try:
        r.check("allowed:hole: a write 32 MiB past the end succeeds",
                lambda: handle.write(b"h", 32 * MiB) == 1)
        r.check("allowed:hole: the file ends after it", lambda: on_disk_size(device, "hole.bin") == 32 * MiB + 1)
    finally:
        handle.close()


def allowed_eof(r: Results, device: Device, client: Client, total: int) -> None:
    handle = client.create("eof.bin")
    try:
        set_end_of_file(handle, 96 * MiB)
        r.check("allowed:eof: end of file set to 96 MiB", lambda: on_disk_size(device, "eof.bin") == 96 * MiB)
        set_end_of_file(handle, 0)
        r.check("allowed:eof: and back to 0", lambda: on_disk_size(device, "eof.bin") == 0)
    finally:
        handle.close()


def allowed_sequential(r: Results, device: Device, client: Client, total: int) -> None:
    handle = client.create("sequential.bin")
    # 4 MiB is the most smbprotocol's 64 credits allow in one write.
    block = bytes(range(256)) * (4 * MiB // 256)
    try:
        written = sum(handle.write(block, i * len(block)) for i in range(24))
        r.check("allowed:sequential: 96 MiB of 4 MiB writes succeed", lambda: written == 96 * MiB)
        r.check("allowed:sequential: the file holds them",
                lambda: on_disk_size(device, "sequential.bin") == 96 * MiB)
    finally:
        handle.close()


def _hdiutil(*args: str) -> str:
    return subprocess.run(["hdiutil", *args], check=True, capture_output=True, text=True, timeout=600).stdout


def mac_sparsebundle(r: Results, device: Device, client: Client, total: int) -> None:
    # The image ends 400 MiB into band 40: writing its last sector starts that
    # band far past its beginning, growth beyond 0065's unchecked 64 MiB that
    # must be checked and allowed. Growing the image does the same to a later
    # band (hdiutil picks where the grown image ends).
    work = Path(tempfile.mkdtemp(prefix="tc-growth-"))
    share, volume = work / "share", work / "volume"
    image = share / TEST_DIR / "tm.sparsebundle"
    bands_dir = shlex.quote(f"{device.root}/{TEST_DIR}/tm.sparsebundle/bands")

    def last_band() -> tuple[int, int]:
        """(index, bytes) of the highest-numbered band file, from the device."""
        rows = [line.split() for line in device.sh(f"ls -ln {bands_dir}").splitlines()]
        return max((int(row[8], 16), int(row[4])) for row in rows if len(row) == 9 and row[0][0] == "-")

    mount("smb", device, share)
    attached = False
    try:
        _hdiutil("create", "-sectors", str((40 * TM_BAND_BYTES + 400 * MiB) // 512),
                 "-type", "SPARSEBUNDLE", "-fs", "HFS+J",
                 "-imagekey", f"sparse-band-size={TM_BAND_BYTES // 512}", "-volname", "tcgrowth", str(image))
        first = last_band()
        print(f"    mac:sparsebundle: band {first[0]:x} holds {first[1]} bytes", flush=True)
        r.check("mac:sparsebundle: the image's last band starts 400 MiB in", lambda: first == (40, 400 * MiB))
        _hdiutil("attach", "-nobrowse", "-mountpoint", str(volume), str(image))
        attached = True
        digests = {}
        for i in range(3):
            data = os.urandom(200 * MiB)
            (volume / f"f{i}").write_bytes(data)
            digests[f"f{i}"] = hashlib.sha256(data).hexdigest()
        _hdiutil("detach", str(volume))
        attached = False
        bands = device.sh(f"ls {bands_dir}").split()
        # 600 MiB of files need two bands; the image's last sector is in a third.
        r.check("mac:sparsebundle: the files and the image's end span three bands",
                lambda: len(bands) >= 3)
        _hdiutil("resize", "-sectors", str((80 * TM_BAND_BYTES + 450 * MiB) // 512), str(image))
        grown = last_band()
        print(f"    mac:sparsebundle: band {grown[0]:x} holds {grown[1]} bytes", flush=True)
        r.check("mac:sparsebundle: the grown image's last band starts past the unchecked growth",
                lambda: grown[0] > 40 and grown[1] > UNCHECKED_GROWTH)
        _hdiutil("attach", "-nobrowse", "-mountpoint", str(volume), str(image))
        attached = True
        r.check("mac:sparsebundle: the grown image reads its data back",
                lambda: all(hashlib.sha256((volume / name).read_bytes()).hexdigest() == digest
                            for name, digest in digests.items()))
    finally:
        if attached:
            subprocess.run(["hdiutil", "detach", "-force", str(volume)], capture_output=True, timeout=120)
        unmount(share)
        with contextlib.suppress(OSError):
            share.rmdir()
            volume.rmdir()
            work.rmdir()


RUNNERS = {
    "write:maxfilesize": write_maxfilesize,
    "write:past-volume": write_past_volume,
    "eof:past-volume": eof_past_volume,
    "copychunk:past-volume": copychunk_past_volume,
    "stream:resource": stream_resource,
    "allowed:hole": allowed_hole,
    "allowed:eof": allowed_eof,
    "allowed:sequential": allowed_sequential,
    "mac:sparsebundle": mac_sparsebundle,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default=".env")
    parser.add_argument("--share", help="share name (default: TC_SHARE_NAME, else the only share)")
    parser.add_argument("--case", action="append", choices=CASES,
                        help="run only these cases (repeatable; default: all)")
    parser.add_argument("--quick", action="store_true",
                        help="the quick tier (AGENTS.md): " + ", ".join(QUICK_CASES))
    parser.add_argument("--aio", action="store_true",
                        help="run with aio_fork enabled in the running smb.conf (restored afterwards)")
    args = parser.parse_args()
    wanted = args.case or list(QUICK_CASES if args.quick else CASES)
    device = Device(parse_env_file(Path(args.env)), args.share)
    if not running_smbd_has_patch(device):
        print(f"ABORT {RUNNING_SMBD} lacks patch 0065; these requests would make HFS allocate "
              "the whole growth", flush=True)
        return 2
    total, available = volume_bytes(device)
    print(f"volume {device.root}: {total} bytes, {available} available", flush=True)
    test_dir = shlex.quote(f"{device.root}/{TEST_DIR}")
    results = Results()
    device.sh(f"rm -rf {test_dir} && mkdir {test_dir} && chmod 777 {test_dir}")
    with aio_fork_enabled(device) if args.aio else contextlib.nullcontext():
        client = Client(device)
        try:
            for case in wanted:
                start = time.monotonic()
                RUNNERS[case](results, device, client, total)
                print(f"== {case} took {time.monotonic() - start:.0f} s", flush=True)
                if args.aio and case == "write:past-volume":
                    aio_refusal_logged(results, device)
        finally:
            client.close()
            device.sh(f"rm -rf {test_dir}", check=False)
    left = device.sh(f"ls -d {test_dir} 2>/dev/null", check=False).strip()
    results.check("cleanup: the test folder is gone", lambda: left == "")
    _, after = volume_bytes(device)
    print(f"available space changed by {after - available} bytes during the run", flush=True)
    print(f"RESULT pass={results.passed} fail={len(results.failed)}")
    return 1 if results.failed else 0


if __name__ == "__main__":
    sys.exit(main())
