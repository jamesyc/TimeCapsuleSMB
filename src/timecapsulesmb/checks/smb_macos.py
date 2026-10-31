"""Doctor's SMB checks on macOS, through Apple's own SMB client, the one Time
Machine and Finder use (checks/smb.py picks it on macOS and smbclient
elsewhere).

`smbutil view` lists the shares, and NetFS mounts one inside this process for
the file steps, which then run as file calls on the mount.
"""
from __future__ import annotations

import contextlib
import ctypes
import errno
import ipaddress
import os
import re
import subprocess
import tempfile
import threading
import urllib.parse
from collections.abc import Iterator
from pathlib import Path

from timecapsulesmb.core.process import run_process
from timecapsulesmb.transport.local import run_local_capture

SMBUTIL = "/usr/bin/smbutil"
UMOUNT = "/sbin/umount"
DISKUTIL = "/usr/sbin/diskutil"
MOUNT = "/sbin/mount"
# Mount folders are named for the doctor that made them, PID included.
DOCTOR_MOUNT_PREFIX = "tcapsule-doctor-smb-"
_DOCTOR_MOUNT_NAME = re.compile(rf"{DOCTOR_MOUNT_PREFIX}(?P<pid>\d+)-")
# A `mount -t smbfs` line:
# "//admin@10.0.0.2/Data on /private/var/folders/.../tcapsule-doctor-smb-123-abc (smbfs, ...)"
_SMBFS_MOUNT_LINE = re.compile(r" on (?P<path>/.*) \(smbfs[,)]")
NETFS_FRAMEWORK = "/System/Library/Frameworks/NetFS.framework/NetFS"
COREFOUNDATION_FRAMEWORK = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
DEFAULT_SMB_PORT = 445
# MNT_DONTBROWSE: hidden from Finder and the Desktop.
MNT_DONTBROWSE = 0x00100000
_CF_STRING_ENCODING_UTF8 = 0x08000100
_CF_NUMBER_INT_TYPE = 9
# smbutil prints each share as "%-48s%-8s%s": name, type, comment.
SMBUTIL_NAME_WIDTH = 48
SMBUTIL_TYPE_WIDTH = 8
SMBUTIL_SHARE_TYPES = frozenset({"Disk", "Pipe", "Printer", "Device"})


class SmbMountError(Exception):
    """NetFS refused the mount; `code` is its errno."""

    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"{os.strerror(code)} (errno {code})")


class SmbMountTimeout(Exception):
    pass


class SmbUnmountError(Exception):
    """A share stayed mounted after both umount and diskutil."""


def apple_smb_client_problem() -> str | None:
    """Why Apple's SMB client, with smbutil there, cannot run here, or None
    when it can (checks/smb.py's LocalSmbClient says whether it is there)."""
    try:
        _NetFS.load()
    except OSError as exc:
        return f"NetFS.framework could not be loaded: {exc}"
    return None


def smb_url_host(host: str, port: int | None) -> str:
    """The host part of an smb:// URL. Apple's share listing fails ("Broken
    pipe") for an IP address with a port other than 445, while the same
    loopback tunnel named `localhost` works, so a loopback address with a
    custom port is written as localhost."""
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        address = None
    if address is not None and address.is_loopback and port not in (None, DEFAULT_SMB_PORT):
        host = "localhost"
    elif address is not None and address.version == 6:
        # A scope zone's "%" is itself escaped inside a URL.
        host = "[" + host.replace("%", "%25") + "]"
    return host if port in (None, DEFAULT_SMB_PORT) else f"{host}:{port}"


def smb_url(host: str, share: str, port: int | None) -> str:
    return f"smb://{smb_url_host(host, port)}/{urllib.parse.quote(share, safe='')}"


class _NetFS:
    """NetFSMountURLSync and the CoreFoundation calls its arguments need."""

    _lock = threading.Lock()
    _loaded: tuple[ctypes.CDLL, ctypes.CDLL] | None = None

    @classmethod
    def load(cls) -> tuple[ctypes.CDLL, ctypes.CDLL]:
        with cls._lock:
            if cls._loaded is None:
                cf = ctypes.CDLL(COREFOUNDATION_FRAMEWORK)
                netfs = ctypes.CDLL(NETFS_FRAMEWORK)
                vp = ctypes.c_void_p
                cf.CFStringCreateWithCString.restype = vp
                cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
                cf.CFURLCreateWithString.restype = vp
                cf.CFURLCreateWithString.argtypes = [vp, vp, vp]
                cf.CFURLCreateFromFileSystemRepresentation.restype = vp
                cf.CFURLCreateFromFileSystemRepresentation.argtypes = [vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool]
                cf.CFDictionaryCreateMutable.restype = vp
                cf.CFDictionaryCreateMutable.argtypes = [vp, ctypes.c_long, vp, vp]
                cf.CFDictionarySetValue.argtypes = [vp, vp, vp]
                cf.CFNumberCreate.restype = vp
                cf.CFNumberCreate.argtypes = [vp, ctypes.c_int, vp]
                cf.CFArrayGetCount.restype = ctypes.c_long
                cf.CFArrayGetCount.argtypes = [vp]
                cf.CFArrayGetValueAtIndex.restype = vp
                cf.CFArrayGetValueAtIndex.argtypes = [vp, ctypes.c_long]
                cf.CFStringGetCString.restype = ctypes.c_bool
                cf.CFStringGetCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
                cf.CFRelease.argtypes = [vp]
                netfs.NetFSMountURLSync.restype = ctypes.c_int
                netfs.NetFSMountURLSync.argtypes = [vp, vp, vp, vp, vp, vp, ctypes.POINTER(vp)]
                cls._loaded = (cf, netfs)
            return cls._loaded


def _netfs_mount(url: str, mountpoint: Path, username: str, password: str) -> Path:
    """Mount `url` on the existing directory `mountpoint`, without any prompt."""
    cf, netfs = _NetFS.load()
    created: list[int] = []

    def own(ref: int | None, failure: int = errno.ENOMEM) -> int:
        if not ref:
            raise SmbMountError(failure)
        created.append(ref)
        return ref

    def cfstr(text: str) -> int:
        return own(cf.CFStringCreateWithCString(None, text.encode("utf-8"), _CF_STRING_ENCODING_UTF8))

    try:
        cf_true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
        key_callbacks = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
        value_callbacks = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))
        # CoreFoundation returns NULL for a string it does not accept as a URL.
        cf_url = own(cf.CFURLCreateWithString(None, cfstr(url), None), errno.EINVAL)
        raw_path = os.fsencode(mountpoint)
        cf_path = own(cf.CFURLCreateFromFileSystemRepresentation(None, raw_path, len(raw_path), True))
        open_options = own(cf.CFDictionaryCreateMutable(None, 0, key_callbacks, value_callbacks))
        cf.CFDictionarySetValue(open_options, cfstr("UIOption"), cfstr("NoUI"))
        # Time Machine may already hold a session to this share; a new one
        # authenticates with this password instead of reusing it.
        cf.CFDictionarySetValue(open_options, cfstr("ForceNewSession"), cf_true)
        mount_options = own(cf.CFDictionaryCreateMutable(None, 0, key_callbacks, value_callbacks))
        flags = ctypes.c_int(MNT_DONTBROWSE)
        cf.CFDictionarySetValue(
            mount_options, cfstr("MountFlags"), own(cf.CFNumberCreate(None, _CF_NUMBER_INT_TYPE, ctypes.byref(flags)))
        )
        cf.CFDictionarySetValue(mount_options, cfstr("MountAtMountDir"), cf_true)
        # File calls on a soft mount fail after a while instead of hanging
        # when the device stops answering.
        cf.CFDictionarySetValue(mount_options, cfstr("SoftMount"), cf_true)
        mountpoints = ctypes.c_void_p()
        code = netfs.NetFSMountURLSync(
            cf_url, cf_path, cfstr(username), cfstr(password), open_options, mount_options, ctypes.byref(mountpoints)
        )
        if code != 0:
            raise SmbMountError(code)
        mounted = mountpoint
        if mountpoints.value:
            created.append(mountpoints.value)
            if cf.CFArrayGetCount(mountpoints) > 0:
                buffer = ctypes.create_string_buffer(4096)
                if cf.CFStringGetCString(cf.CFArrayGetValueAtIndex(mountpoints, 0), buffer, len(buffer), _CF_STRING_ENCODING_UTF8):
                    mounted = Path(buffer.value.decode("utf-8"))
        return mounted
    finally:
        for ref in reversed(created):
            cf.CFRelease(ref)


def smbfs_mount_points() -> list[Path]:
    """Every SMB mount point in the kernel's mount table. `mount` reads the
    table without touching the shares, so one that stopped answering cannot
    hang it."""
    proc = run_local_capture([MOUNT, "-t", "smbfs"], timeout=10)
    lines = (proc.stdout or "").splitlines()
    return [Path(match.group("path")) for match in map(_SMBFS_MOUNT_LINE.search, lines) if match]


def unmount(path: Path, *, timeout: int = 20) -> None:
    """Force the unmount, so a file left open cannot keep the share mounted.
    diskutil is the fallback when umount fails, does not finish or cannot
    run; SmbUnmountError says the mount table still lists the share."""
    failures: list[str] = []
    for cmd in ([UMOUNT, "-f", str(path)], [DISKUTIL, "unmount", "force", str(path)]):
        try:
            proc = run_local_capture(cmd, timeout=timeout)
        except subprocess.TimeoutExpired:
            failures.append(f"{cmd[0]} did not finish within {timeout} s")
            continue
        except OSError as exc:
            failures.append(f"{cmd[0]} could not run: {exc}")
            continue
        if proc.returncode == 0:
            return
        failures.append((proc.stderr or proc.stdout or f"{cmd[0]} exited {proc.returncode}").strip())
    # A command that did not finish in time may still have unmounted it.
    try:
        still_mounted = path in smbfs_mount_points()
    except (OSError, subprocess.SubprocessError):
        still_mounted = True
    if still_mounted:
        raise SmbUnmountError(f"{path} is still mounted: {'; '.join(failures)}")


def _doctor_pid(path: Path) -> int | None:
    """The PID in a doctor mount folder's name, or None for any other path."""
    match = _DOCTOR_MOUNT_NAME.match(path.name)
    return int(match.group("pid")) if match else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def unmount_stale_doctor_mounts() -> list[Path]:
    """Unmount the mounts an earlier doctor left behind: one that exited while
    NetFS was still mounting, or whose unmount failed. Then remove the empty
    mount folders of doctors that exited, such as one stopped mid-mount. Each
    folder names its doctor's PID, so a doctor running now keeps its own."""
    removed: list[Path] = []
    for path in smbfs_mount_points():
        pid = _doctor_pid(path)
        if pid is None or _pid_alive(pid):
            continue
        with contextlib.suppress(OSError, subprocess.SubprocessError, SmbUnmountError):
            unmount(path)
            removed.append(path)
    for folder in Path(tempfile.gettempdir()).resolve().glob(f"{DOCTOR_MOUNT_PREFIX}*"):
        pid = _doctor_pid(folder)
        if pid is not None and not _pid_alive(pid):
            # rmdir removes only an empty folder; one still mounted is busy.
            with contextlib.suppress(OSError):
                folder.rmdir()
    return removed


def _mount_within(url: str, mountpoint: Path, username: str, password: str, timeout: float) -> Path:
    """NetFS blocks 60 s on an address that does not answer, so the mount runs
    in a thread. A mount that finishes after the deadline is unmounted by that
    thread, and its folder removed, so nothing stays behind doctor's back."""
    lock = threading.Lock()
    state: dict[str, object] = {"abandoned": False}

    def worker() -> None:
        try:
            outcome: object = _netfs_mount(url, mountpoint, username, password)
        except BaseException as exc:  # handed to the waiting thread
            outcome = exc
        with lock:
            if not state["abandoned"]:
                state["outcome"] = outcome
                return
        if isinstance(outcome, Path):
            # A failure here is left to the next doctor's sweep.
            with contextlib.suppress(OSError, subprocess.SubprocessError, SmbUnmountError):
                unmount(outcome)
                mountpoint.rmdir()

    thread = threading.Thread(target=worker, name="tcapsule-smb-mount", daemon=True)
    thread.start()
    thread.join(timeout)
    with lock:
        if "outcome" not in state:
            state["abandoned"] = True
            raise SmbMountTimeout(f"mount did not finish within {timeout:g} s")
        outcome = state["outcome"]
    if isinstance(outcome, BaseException):
        raise outcome
    assert isinstance(outcome, Path)
    return outcome


@contextlib.contextmanager
def mounted_share(
    host: str,
    share: str,
    username: str,
    password: str,
    *,
    port: int | None = None,
    timeout: float = 20,
) -> Iterator[Path]:
    """Mount `share` on a fresh temporary directory, and always unmount it.
    SmbUnmountError after the body means the share stayed mounted."""
    # NetFS refuses a mount point reached through a symlink with ELOOP, and
    # the temporary directory is under /var, a symlink to /private/var.
    mountpoint = Path(tempfile.mkdtemp(prefix=f"{DOCTOR_MOUNT_PREFIX}{os.getpid()}-")).resolve()
    try:
        mounted = _mount_within(smb_url(host, share, port), mountpoint, username, password, timeout)
        try:
            yield mounted
        finally:
            unmount(mounted)
    finally:
        with contextlib.suppress(OSError):
            mountpoint.rmdir()


def _smbutil_share_row(line: str) -> tuple[str, str] | None:
    """(type, name) of one table row, or None for any other line.

    smbutil prints "%-48s%-8s%s", and printf pads by bytes: a name with
    non-ASCII characters is fewer than 48 characters wide, and a name of 48
    bytes or more is followed by the type with no space between them."""
    raw = line.encode("utf-8")
    kind = raw[SMBUTIL_NAME_WIDTH:SMBUTIL_NAME_WIDTH + SMBUTIL_TYPE_WIDTH].strip().decode("utf-8", "replace")
    if kind in SMBUTIL_SHARE_TYPES:
        return kind, raw[:SMBUTIL_NAME_WIDTH].rstrip().decode("utf-8", "replace")
    # A long name: the type starts where the name ends, padded to 8 bytes.
    for start in range(SMBUTIL_NAME_WIDTH, len(raw)):
        for known in SMBUTIL_SHARE_TYPES:
            token = known.encode()
            padding = raw[start + len(token):start + SMBUTIL_TYPE_WIDTH]
            if raw.startswith(token, start) and padding == b" " * (SMBUTIL_TYPE_WIDTH - len(token)):
                return known, raw[:start].decode("utf-8", "replace")
    return None


def parse_smbutil_disk_shares(stdout: str) -> list[str] | None:
    """The disk shares in `smbutil view` output, or None when no share table
    came back. Samba always lists IPC$, so a listed server has a row."""
    rows: list[tuple[str, str]] = []
    in_table = False
    for line in stdout.splitlines():
        if line.startswith("-----"):
            in_table = True
            continue
        if not in_table or not line.strip():
            continue
        row = _smbutil_share_row(line)
        if row is not None:
            rows.append(row)
    if not rows:
        return None
    return list(dict.fromkeys(name for kind, name in rows if kind == "Disk" and name))


def view_args(host: str, username: str, password: str | None, *, port: int | None = None) -> list[str]:
    """`smbutil view` as `username`, in a session of its own (-f): smbutil
    otherwise reuses any session to the same host name, whatever its user or
    port, so two `localhost` tunnels would list each other's shares. The
    password goes in the URL (None writes *** there, to show the command);
    -N keeps smbutil from asking for another."""
    secret = "***" if password is None else urllib.parse.quote(password, safe="")
    user = urllib.parse.quote(username, safe="")
    return [SMBUTIL, "view", "-N", "-f", f"//{user}:{secret}@{smb_url_host(host, port)}"]


def run_view(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    # Share names are UTF-8 whatever this process's locale says.
    return run_process(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
