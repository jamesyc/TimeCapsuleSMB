"""Native-symlink device suite (Samba patch 0045), run from a Mac against a deployed device.

    .venv/bin/python -m tests.samba.links_device --env .env [--afp] [--no-windows]

It works only inside a `__tc_links_test__` folder on the share, which it creates over SSH
and removes at the end. Credentials come from the env file and are never printed. The
Windows-style cases speak SMB2 directly with smbprotocol (a host tool, not a runtime
dependency), the way Windows and Linux clients create links; they are skipped without it.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shlex
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid

from timecapsulesmb.core.config import DEFAULTS, parse_env_file
from timecapsulesmb.repair_xattrs import RepairSummary, iter_scan_paths
from timecapsulesmb.transport.ssh import SshConnection, run_ssh, run_ssh_input

TEST_DIR = "__tc_links_test__"
# Characters macOS sends as private-use code points and catia maps back on disk.
MAPPED_NAMES = ("x:y", "p*q", "q?r", 'a"b', "l<t", "g>t", "b|c")
XSYM_SIZE = 1067
# smbd reports a refused FSCTL as INVALID_DEVICE_REQUEST (Linux: EOPNOTSUPP, Windows:
# "Incorrect function"); upstream refuses a reparse point on a directory as ACCESS_DENIED.
STATUS_INVALID_DEVICE_REQUEST = 0xC0000010
STATUS_ACCESS_DENIED = 0xC0000022
IO_REPARSE_TAG_SYMLINK = 0xA000000C
IO_REPARSE_TAG_NFS = 0x80000014
IO_REPARSE_TAG_LX_SYMLINK = 0xA000001D
IO_REPARSE_TAG_AF_UNIX = 0x80000023
NFS_SPECFILE_LNK = 0x00000000014B4E4C
NFS_SPECFILE_FIFO = 0x000000004F464946
FSCTL_GET_REPARSE_POINT = 0x000900A8
FSCTL_SET_REPARSE_POINT = 0x000900A4


def xsym(target: str) -> bytes:
    """The 1067-byte body macOS smbfs writes for a symlink."""
    raw = target.encode()
    body = b"XSym\n%04d\n%s\n%s\n" % (len(raw), hashlib.md5(raw).hexdigest().encode(), raw)
    return body + b" " * (XSYM_SIZE - len(body))


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def check(self, name: str, fn) -> None:
        try:
            ok = fn()
            detail = ""
        except Exception as error:  # a failing step is a failed check, not a crashed suite
            ok, detail = False, f"{type(error).__name__}: {error}"
        if ok:
            self.passed += 1
            print(f"PASS {name}", flush=True)
        else:
            self.failed.append(name)
            print(f"FAIL {name}{': ' + detail if detail else ''}", flush=True)


class Device:
    def __init__(self, env: dict[str, str], share: str | None = None) -> None:
        self.env = env
        self.connection = SshConnection(env["TC_HOST"], env["TC_PASSWORD"],
                                        env.get("TC_SSH_OPTS", DEFAULTS["TC_SSH_OPTS"]))
        self.host = env["TC_HOST"].split("@", 1)[-1]
        self.share, self.root = self._share(share or env.get("TC_SHARE_NAME"))
        self.dir = f"{self.root}/{TEST_DIR}"

    def sh(self, command: str, *, check: bool = True) -> str:
        return run_ssh(self.connection, command, check=check, timeout=180).stdout

    def put(self, path: str, data: bytes) -> None:
        run_ssh_input(self.connection, f"cat > {shlex.quote(path)}", input_bytes=data, timeout=180)

    def _share(self, wanted: str | None) -> tuple[str, str]:
        # The running smb.conf is the source of truth for the shares and their paths.
        conf = run_ssh(self.connection, "cat /mnt/Memory/samba4/etc/smb.conf", timeout=60).stdout
        shares: dict[str, str] = {}
        section = None
        for line in conf.splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
            elif section not in (None, "global") and line.startswith("path ="):
                shares[section] = line.split("=", 1)[1].strip()
        if wanted is None and len(shares) == 1:
            wanted = next(iter(shares))
        if wanted not in shares:
            raise RuntimeError(f"choose a share with --share (running smb.conf has {sorted(shares)})")
        return wanted, shares[wanted]

    def ls_l(self, name: str) -> str:
        return self.sh(f"cd {shlex.quote(self.dir)} && ls -ld {shlex.quote(name)} 2>/dev/null",
                       check=False).strip()

    def is_native_link(self, name: str, target: str) -> bool:
        line = self.ls_l(name)
        return line.startswith("lrwxr-xr-x") and line.endswith(" -> " + target)

    def is_dir(self, name: str) -> bool:
        return self.ls_l(name).startswith("d")

    def absent(self, name: str) -> bool:
        return self.ls_l(name) == ""


def mount(url_scheme: str, device: Device, mountpoint: Path) -> None:
    user = device.env.get("TC_SAMBA_USER") or "root"
    creds = urllib.parse.quote(user, safe="") + ":" + urllib.parse.quote(device.env["TC_PASSWORD"], safe="")
    url = f"//{creds}@{device.host}/{urllib.parse.quote(device.share)}"
    mountpoint.mkdir(parents=True, exist_ok=True)
    if url_scheme == "smb":
        cmd = ["mount_smbfs", "-o", "nobrowse,forcenewsession,nomdatacache,nodatacache", url, str(mountpoint)]
    else:
        cmd = ["mount_afp", "-o", "nobrowse", "afp:" + url, str(mountpoint)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        # The URL carries the password: report the exit status only.
        raise RuntimeError(f"{cmd[0]} failed with rc={proc.returncode}")


def unmount(mountpoint: Path) -> None:
    subprocess.run(["umount", str(mountpoint)], capture_output=True, timeout=120)


def fixtures(device: Device) -> None:
    d = shlex.quote(device.dir)
    links = [
        "ln -s t.txt link-file", "ln -s missing link-dangling", "ln -s sub/d link-dir",
        f"ln -s {d}/t.txt link-abs", "ln -s /etc/rc.conf link-outside", "ln -s ../t.txt rmdir-test/l",
    ] + [f"ln -s t.txt {shlex.quote('s' + name)}" for name in MAPPED_NAMES]
    # Issue #304's shapes: a walk over self, parent, dangling, directory and outside links, and one
    # link of each shape to rename.
    walk = ["echo a > walk/a.txt", "echo f > walk/sub/f", "ln -s . walk/.fcpcache", "ln -s .. walk/up",
            "ln -s missing walk/dangling", "ln -s sub walk/dirlink", "ln -s ../t.txt walk/out"]
    moves = ["echo t > moves/t.txt", "echo old > moves/existing", "ln -s t.txt moves/m-file",
             "ln -s nowhere moves/m-dangling", "ln -s into moves/m-dir", "ln -s . moves/m-self",
             "ln -s t.txt moves/m-over", "ln -s t.txt moves/m-across"]
    device.sh(f"rm -rf {d} && mkdir -p {d}/sub/d {d}/rmdir-test {d}/walk/sub {d}/moves/into && cd {d} && "
              "echo hello-file > t.txt && " + " && ".join(links + walk + moves))
    # A link an earlier release wrote over SMB: an XSym file on disk.
    device.put(f"{device.dir}/legacy", xsym("t.txt"))


def mac_checks(r: Results, device: Device, m: Path) -> None:
    def xattr(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["xattr", *args], capture_output=True, text=True, timeout=60)

    r.check("mac: native links list as links", lambda: all(
        (m / n).is_symlink() for n in ("link-file", "link-dangling", "link-dir", "link-abs", "link-outside")))
    r.check("mac: readlink relative, dangling, absolute and outside targets verbatim", lambda: (
        os.readlink(m / "link-file") == "t.txt" and os.readlink(m / "link-dangling") == "missing"
        and os.readlink(m / "link-abs") == f"{device.dir}/t.txt"
        and os.readlink(m / "link-outside") == "/etc/rc.conf"))
    r.check("mac: read through a file link", lambda: (m / "link-file").read_text() == "hello-file\n")
    r.check("mac: list through a directory link", lambda: os.listdir(m / "link-dir") == [])
    r.check("mac: legacy XSym file reads as a link", lambda: os.readlink(m / "legacy") == "t.txt")
    for name in MAPPED_NAMES:
        r.check(f"mac: SSH link named {name!r} is visible and readable",
                lambda n="s" + name: (m / n).is_symlink() and os.readlink(m / n) == "t.txt")

    os.symlink("t.txt", m / "new-link")
    r.check("mac: ln -s creates a native 0755 link", lambda: device.is_native_link("new-link", "t.txt"))
    r.check("mac: ln -sf replaces it", lambda: subprocess.run(
        ["ln", "-sf", "missing2", str(m / "new-link")], timeout=60).returncode == 0
        and device.is_native_link("new-link", "missing2"))
    for name in MAPPED_NAMES:
        path = m / ("n" + name)

        def created(path: Path = path, name: str = name) -> bool:
            os.symlink("t.txt", path)
            return device.is_native_link("n" + name, "t.txt") and os.readlink(path) == "t.txt"

        r.check(f"mac: ln -s {('n' + name)!r} is native and reads back", created)
        r.check(f"mac: xattr on {('n' + name)!r}", lambda p=path: xattr("-s", "-w", "user.tc", "v", str(p))
                .returncode == 0 and xattr("-s", "-p", "user.tc", str(p)).stdout.strip() == "v")
        r.check(f"mac: rm {('n' + name)!r}", lambda p=path, n="n" + name: (os.unlink(p), device.absent(n))[1])

    r.check("mac: xattr -s is stored on the link, not the target", lambda: (
        xattr("-s", "-w", "user.tc", "on-link", str(m / "link-file")).returncode == 0
        and xattr("-s", "-p", "user.tc", str(m / "link-file")).stdout.strip() == "on-link"
        and xattr("-p", "user.tc", str(m / "t.txt")).returncode != 0))
    before = os.stat(m / "t.txt").st_mtime
    r.check("mac: touch -h leaves the target alone", lambda: subprocess.run(
        ["touch", "-h", "-t", "200001010000", str(m / "link-file")], timeout=60).returncode == 0
        and os.stat(m / "t.txt").st_mtime == before)
    r.check("mac: mv renames the link, not the target", lambda: (
        os.rename(m / "link-file", m / "link-renamed"), device.is_native_link("link-renamed", "t.txt")
        and device.absent("link-file") and device.ls_l("t.txt").startswith("-"))[1])
    with tempfile.TemporaryDirectory() as local:
        tree = Path(local) / "tree"
        tree.mkdir()
        os.symlink("../t.txt", tree / "ll")
        r.check("mac: cp -pR copies a tree holding a link", lambda: subprocess.run(
            ["cp", "-pR", str(tree), str(m / "tree")], timeout=120).returncode == 0
            and device.is_native_link("tree/ll", "../t.txt"))
    r.check("mac: rewriting a legacy XSym link makes it native", lambda: (
        os.unlink(m / "legacy"), os.symlink("t.txt", m / "legacy"), device.is_native_link("legacy", "t.txt"))[2])
    r.check("mac: rm live, dangling and directory links keeps targets", lambda: (
        os.unlink(m / "link-renamed"), os.unlink(m / "link-dangling"), os.unlink(m / "link-dir"),
        device.absent("link-renamed") and device.absent("link-dangling") and device.absent("link-dir")
        and device.is_dir("sub/d") and device.ls_l("t.txt").startswith("-"))[3])
    r.check("mac: rm -rf a folder holding a link keeps the target", lambda: subprocess.run(
        ["rm", "-rf", str(m / "rmdir-test")], timeout=120).returncode == 0
        and device.absent("rmdir-test") and device.ls_l("t.txt").startswith("-"))
    for name in MAPPED_NAMES:
        r.check(f"mac: rm SSH link {('s' + name)!r}",
                lambda n="s" + name: (os.unlink(m / n), device.absent(n))[1])


class WindowsClient:
    """SMB2 without AAPL, creating links as Windows and Linux clients do."""

    def __init__(self, device: Device) -> None:
        from smbprotocol.connection import Connection
        from smbprotocol.session import Session
        from smbprotocol.tree import TreeConnect

        self.conn = Connection(uuid.uuid4(), device.host, 445)
        self.conn.connect()
        self.sess = Session(self.conn, device.env.get("TC_SAMBA_USER") or "root", device.env["TC_PASSWORD"])
        self.sess.connect()
        self.tree = TreeConnect(self.sess, rf"\\{device.host}\{device.share}")
        self.tree.connect()

    def close(self) -> None:
        self.tree.disconnect()
        self.sess.disconnect()
        self.conn.disconnect()

    def open(self, name: str, access: int, options: int = 0, disposition: int = 1, attrs: int = 0x80):
        from smbprotocol.open import ImpersonationLevel, Open

        h = Open(self.tree, TEST_DIR + ("\\" + name if name else ""))
        h.create(ImpersonationLevel.Impersonation, access, attrs, 0x7, disposition, options)
        return h

    def _send(self, req):
        return self.conn.receive(self.conn.send(req, self.sess.session_id, self.tree.tree_connect_id))

    def fsctl(self, h, code: int, data: bytes = b"", out: int = 0) -> bytes:
        from smbprotocol.ioctl import IOCTLFlags, SMB2IOCTLRequest, SMB2IOCTLResponse

        req = SMB2IOCTLRequest()
        req["ctl_code"] = code
        req["file_id"] = h.file_id
        req["max_output_response"] = out
        req["flags"] = IOCTLFlags.SMB2_0_IOCTL_IS_FSCTL
        req["buffer"] = data
        resp = SMB2IOCTLResponse()
        resp.unpack(self._send(req)["data"].get_value())
        return resp["buffer"].get_value()

    def query(self, h, info_type: int, info_class: int) -> bytes:
        from smbprotocol.open import SMB2QueryInfoRequest, SMB2QueryInfoResponse

        req = SMB2QueryInfoRequest()
        req["info_type"] = info_type
        req["file_info_class"] = info_class
        req["output_buffer_length"] = 4096
        req["file_id"] = h.file_id
        resp = SMB2QueryInfoResponse()
        resp.unpack(self._send(req)["data"].get_value())
        return resp["buffer"].get_value()

    def set_attributes(self, h, attrs: int) -> None:
        from smbprotocol.open import SMB2SetInfoRequest

        req = SMB2SetInfoRequest()
        req["info_type"] = 1  # SMB2_0_INFO_FILE
        req["file_info_class"] = 4  # FileBasicInformation
        req["file_id"] = h.file_id
        req["buffer"] = struct.pack("<qqqqII", 0, 0, 0, 0, attrs, 0)
        self._send(req)

    def listing(self) -> dict[str, tuple[int, int]]:
        from smbprotocol.file_info import FileInformationClass

        d = self.open("", 0x80000000, 0x1)  # GENERIC_READ, directory
        try:
            return {e["file_name"].get_value().decode("utf-16-le"):
                    (e["file_attributes"].get_value(), e["ea_size"].get_value())
                    for e in d.query_directory("*", FileInformationClass.FILE_ID_BOTH_DIRECTORY_INFORMATION)}
        finally:
            d.close()

    def create_reparse(self, name: str, payload: bytes, directory: bool = False) -> int:
        """CreateSymbolicLinkW / Linux smb2_create_reparse_inode: placeholder, SET, remove on failure."""
        from smbprotocol.exceptions import SMBResponseException

        options = 0x200000 | (0x1 if directory else 0x40)  # OPEN_REPARSE_POINT | (NON_)DIRECTORY_FILE
        h = self.open(name, 0x100 | 0x10000 | 0x100000, options, 2, 0x10 if directory else 0x80)
        try:
            self.fsctl(h, FSCTL_SET_REPARSE_POINT, payload)
            return 0
        except SMBResponseException as error:
            status = error.status
        finally:
            h.close()
        self.open(name, 0x10000, options | 0x1000).close()  # DELETE_ON_CLOSE
        return status


def symlink_payload(target: str, relative: bool = True) -> bytes:
    sub = target.encode("utf-16-le")
    body = struct.pack("<HHHHI", 0, len(sub), len(sub), len(sub), 1 if relative else 0) + sub + sub
    return struct.pack("<IHH", IO_REPARSE_TAG_SYMLINK, len(body), 0) + body


def nfs_payload(kind: int, target: str = "") -> bytes:
    body = struct.pack("<Q", kind) + target.encode("utf-16-le")
    return struct.pack("<IHH", IO_REPARSE_TAG_NFS, len(body), 0) + body


def lx_payload(target: str) -> bytes:
    body = struct.pack("<I", 2) + target.encode()
    return struct.pack("<IHH", IO_REPARSE_TAG_LX_SYMLINK, len(body), 0) + body


def windows_checks(r: Results, device: Device) -> None:
    from smbprotocol.exceptions import SMBResponseException

    w = WindowsClient(device)
    try:
        def fs_reparse() -> bool:
            h = w.open("", 0x80, 0x1)  # FILE_READ_ATTRIBUTES
            try:
                return bool(struct.unpack("<I", w.query(h, 2, 5)[:4])[0] & 0x80)
            finally:
                h.close()

        r.check("win: server advertises reparse point support", fs_reparse)
        listing = w.listing()
        r.check("win: file link lists as a symlink reparse point", lambda: (
            listing["link-file"][0] & 0x410 == 0x400 and listing["link-file"][1] == IO_REPARSE_TAG_SYMLINK))
        r.check("win: directory link also carries FILE_ATTRIBUTE_DIRECTORY",
                lambda: listing["link-dir"][0] & 0x410 == 0x410 and listing["link-dir"][1] == IO_REPARSE_TAG_SYMLINK)
        for name in MAPPED_NAMES:
            mapped = "s" + name.translate({ord(":"): "\uf022", ord("*"): "\uf021", ord("?"): "\uf025",
                                           ord('"'): "\uf020", ord("<"): "\uf023", ord(">"): "\uf024",
                                           ord("|"): "\uf027"})
            r.check(f"win: link {('s' + name)!r} lists with the symlink tag",
                    lambda n=mapped: listing.get(n, (0, 0))[1] == IO_REPARSE_TAG_SYMLINK)

        def reparse_target() -> bool:
            h = w.open("link-file", 0x80, 0x200000)
            try:
                data = w.fsctl(h, FSCTL_GET_REPARSE_POINT, out=4096)
            finally:
                h.close()
            sub_off, sub_len = struct.unpack("<HH", data[8:12])
            return struct.unpack("<I", data[:4])[0] == IO_REPARSE_TAG_SYMLINK and \
                data[20 + sub_off:20 + sub_off + sub_len].decode("utf-16-le") == "t.txt"

        r.check("win: FSCTL_GET_REPARSE_POINT returns the target", reparse_target)
        r.check("win: RemoveDirectory on a directory link removes only the link", lambda: (
            w.open("link-dir", 0x10000, 0x1 | 0x200000 | 0x1000).close(),
            device.absent("link-dir") and device.is_dir("sub/d"))[1])

        for name, payload, target in (
                ("win-rel", symlink_payload("t.txt"), "t.txt"),
                ("win-sub", symlink_payload("sub\\d"), "sub/d"),
                ("win-up", symlink_payload("..\\" + TEST_DIR + "\\t.txt"), "../" + TEST_DIR + "/t.txt"),
                ("nfs-lnk", nfs_payload(NFS_SPECFILE_LNK, "sub/d"), "sub/d"),
                ("wsl-lnk", lx_payload("t.txt"), "t.txt")):
            r.check(f"win: {name} becomes a native link",
                    lambda n=name, p=payload, t=target: w.create_reparse(n, p) == 0 and device.is_native_link(n, t))
        for name, payload, directory, status in (
                ("win-dir", symlink_payload("sub"), True, STATUS_ACCESS_DENIED),
                ("win-abs", symlink_payload("\\??\\C:\\x", relative=False), False, STATUS_INVALID_DEVICE_REQUEST),
                ("nfs-fifo", nfs_payload(NFS_SPECFILE_FIFO), False, STATUS_INVALID_DEVICE_REQUEST),
                ("af-unix", struct.pack("<IHH", IO_REPARSE_TAG_AF_UNIX, 0, 0), False,
                 STATUS_INVALID_DEVICE_REQUEST)):
            r.check(f"win: {name} is refused and leaves nothing",
                    lambda n=name, p=payload, d=directory, st=status: w.create_reparse(n, p, d) == st
                    and device.absent(n))

        def write_only_xsym() -> bool:
            # Linux mfsymlinks (smb3_create_mf_symlink) asks only for GENERIC_WRITE: smbd holds it O_WRONLY.
            h = w.open("mf-link", 0x40000000, 0x40, 2)  # GENERIC_WRITE, NON_DIRECTORY_FILE, FILE_CREATE
            h.write(xsym("sub/d"))
            h.close()
            return device.is_native_link("mf-link", "sub/d")

        r.check("win: an XSym file written without read access (Linux mfsymlinks) becomes a native link",
                write_only_xsym)

        def metadata() -> bool:
            # An XSym file with named streams and the HIDDEN attribute, all set before close. A stream
            # whose name contains AFP_AfpInfo is the client's own, not Finder info.
            h = w.open("meta", 0x12019F, 0x40, 2)  # read/write/attributes, FILE_CREATE
            h.write(xsym("t.txt"))
            for stream in ("tcstream", "backup.AFP_AfpInfo.notes"):
                s = w.open("meta:" + stream, 0x12019F, 0x40, 2)
                s.write(b"sv")
                s.close()
            w.set_attributes(h, 0x2)
            h.close()
            if not device.is_native_link("meta", "t.txt"):
                return False
            link = w.open("meta", 0x80 | 0x8, 0x200000)
            try:
                streams = w.query(link, 1, 22).decode("utf-16-le", errors="ignore")
            finally:
                link.close()
            return ":tcstream:$DATA" in streams and ":backup.AFP_AfpInfo.notes:$DATA" in streams and \
                w.listing()["meta"][0] & 0x2 == 0x2

        r.check("win: stream and DOS attributes set before close move to the link", metadata)
    except SMBResponseException as error:
        r.check(f"win: unexpected SMB error 0x{error.status:08x}", lambda: False)
    finally:
        w.close()


def mac_shape_checks(r: Results, device: Device, m: Path) -> None:
    """Issue #304: links to ., .., nothing, a directory and outside the tree, walked, renamed and removed."""
    walk, moves = m / "walk", m / "moves"
    shapes = {".fcpcache": ".", "up": "..", "dangling": "missing", "dirlink": "sub", "out": "../t.txt"}
    r.check("mac: self, parent, dangling, directory and outside links list as links",
            lambda: all((walk / n).is_symlink() for n in shapes))
    r.check("mac: readlink returns each of them verbatim",
            lambda: all(os.readlink(walk / n) == t for n, t in shapes.items()))

    def repair_walk() -> bool:
        # The issue saw 20 paths for 9 objects: the walk entered .fcpcache -> . as a directory.
        summary = RepairSummary()
        seen = sorted(str(p.relative_to(walk.resolve())) for p, _ in iter_scan_paths(
            walk, recursive=True, max_depth=None, include_hidden=True, include_time_machine=True,
            include_directories=True, summary=summary))
        return seen == ["a.txt", "sub", "sub/f"] and summary.skipped == len(shapes)

    r.check("mac: repair-xattrs visits each object once and skips the links", repair_walk)
    r.check("mac: find does not descend into a link to .", lambda: sorted(subprocess.run(
        ["find", str(walk)], capture_output=True, text=True, timeout=60).stdout.splitlines()) == sorted(
        [str(walk)] + [str(walk / n) for n in ("a.txt", "sub", "sub/f", *shapes)]))
    # macOS renames with a plain open, not FILE_OPEN_REPARSE_POINT, whatever the link points at.
    for src, dst, target in (("m-dangling", "m-dangling2", "nowhere"), ("m-dir", "m-dir2", "into"),
                             ("m-self", "m-self2", "."), ("m-file", "into/m-file", "t.txt")):
        r.check(f"mac: mv {src} to {dst} moves the link", lambda s=src, t=dst, g=target: (
            os.rename(moves / s, moves / t), device.is_native_link(f"moves/{t}", g)
            and device.absent(f"moves/{s}"))[1])
    r.check("mac: mv -f a link over a file replaces the file", lambda: subprocess.run(
        ["mv", "-f", str(moves / "m-over"), str(moves / "existing")], timeout=60).returncode == 0
        and device.is_native_link("moves/existing", "t.txt") and device.absent("moves/m-over"))
    r.check("mac: mv a link into another folder", lambda: (
        os.rename(moves / "m-across", walk / "sub" / "across"), device.is_native_link("walk/sub/across", "t.txt"))[1])
    os.symlink("t.txt", moves / "mac-made")
    r.check("mac: mv a link this Mac just made", lambda: (
        os.rename(moves / "mac-made", moves / "mac-moved"), device.is_native_link("moves/mac-moved", "t.txt"))[1])
    r.check("mac: every move leaves the targets alone",
            lambda: device.ls_l("moves/t.txt").startswith("-") and device.is_dir("moves/into"))
    r.check("mac: rm -rf folders holding self, parent and outside links keeps what they point at", lambda: (
        subprocess.run(["rm", "-rf", str(walk), str(moves)], timeout=120).returncode == 0
        and device.absent("walk") and device.absent("moves") and device.ls_l("t.txt").startswith("-")))


def afp_checks(r: Results, device: Device, m: Path, a: Path) -> None:
    os.symlink("t.txt", m / "smb-made")
    r.check("afp: a link made over SMB is a link over AFP", lambda: os.readlink(a / "smb-made") == "t.txt")
    os.symlink("t.txt", a / "afp-made")
    time.sleep(2)
    r.check("afp: a link made over AFP is a link over SMB", lambda: os.readlink(m / "afp-made") == "t.txt")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default=".env")
    parser.add_argument("--share", help="share name (default: TC_SHARE_NAME, else the only share)")
    parser.add_argument("--afp", action="store_true", help="also cross-check with an AFP mount")
    parser.add_argument("--no-windows", action="store_true", help="skip the SMB2 client cases")
    args = parser.parse_args()
    env = parse_env_file(Path(args.env))
    device = Device(env, args.share)
    results = Results()
    work = Path(tempfile.mkdtemp(prefix="tc-links-"))
    smb, afp = work / "smb", work / "afp"
    fixtures(device)
    try:
        mount("smb", device, smb)
        try:
            mac_checks(results, device, smb / TEST_DIR)
            mac_shape_checks(results, device, smb / TEST_DIR)
            if args.afp:
                mount("afp", device, afp)
                try:
                    afp_checks(results, device, smb / TEST_DIR, afp / TEST_DIR)
                finally:
                    unmount(afp)
        finally:
            unmount(smb)
        if args.no_windows:
            print("SKIP win: --no-windows")
        else:
            try:
                import smbprotocol  # noqa: F401
            except ImportError:
                print("SKIP win: smbprotocol is not installed on this host")
            else:
                fixtures(device)  # the Mac cases removed some of them
                windows_checks(results, device)
    finally:
        device.sh(f"rm -rf {shlex.quote(device.dir)}", check=False)
        for path in (smb, afp):
            if path.exists():
                path.rmdir()
        work.rmdir()
    print(f"RESULT pass={results.passed} fail={len(results.failed)}")
    return 1 if results.failed else 0


if __name__ == "__main__":
    sys.exit(main())
