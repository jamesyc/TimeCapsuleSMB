from __future__ import annotations

import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.transport.local import command_exists, run_local_capture


def check_authenticated_smb_listing(username: str, password: str, server: str, *, timeout: int = 20) -> CheckResult:
    if not command_exists("smbutil"):
        return CheckResult("FAIL", "missing local tool smbutil")

    proc = run_local_capture(["smbutil", "view", f"//{username}:{password}@{server}"], timeout=timeout)
    if proc.returncode == 0:
        return CheckResult("PASS", f"authenticated SMB listing works for {username}@{server}")
    detail = (proc.stderr or proc.stdout).strip().splitlines()
    msg = detail[-1] if detail else f"failed with rc={proc.returncode}"
    return CheckResult("FAIL", f"authenticated SMB listing failed: {msg}")


def try_authenticated_smb_listing(username: str, password: str, servers: list[str], *, timeout: int = 12) -> CheckResult:
    if not command_exists("smbutil"):
        return CheckResult("WARN", "SMB listing verification skipped: smbutil not found")

    failure_msg = "not attempted"
    for server in servers:
        try:
            proc = run_local_capture(["smbutil", "view", f"//{username}:{password}@{server}"], timeout=timeout)
        except subprocess.TimeoutExpired:
            failure_msg = f"timed out via {server}"
            continue
        if proc.returncode == 0:
            return CheckResult("PASS", f"authenticated SMB listing works for {username}@{server}")
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        failure_msg = detail[-1] if detail else f"failed with rc={proc.returncode} via {server}"
    return CheckResult("FAIL", f"authenticated SMB listing failed: {failure_msg}")


def exercise_mounted_share_file_ops(root: Path, *, prefix: str = "doctor-fileops") -> None:
    test_dir = root / f"{prefix}-{uuid.uuid4().hex[:8]}"
    test_file = test_dir / "sample.txt"
    renamed_file = test_dir / "sample-renamed.txt"

    test_dir.mkdir()
    test_file.write_text("line1\n")
    with test_file.open("a", encoding="utf-8") as handle:
        handle.write("line2\n")

    content = test_file.read_text()
    if content != "line1\nline2\n":
        raise RuntimeError(f"unexpected file contents after append: {content!r}")

    test_file.rename(renamed_file)
    if not renamed_file.exists():
        raise RuntimeError("renamed file is missing")

    copied = test_dir / "sample-copy.txt"
    copied.write_text(renamed_file.read_text())
    copied.unlink()
    renamed_file.unlink()
    test_dir.rmdir()


def _mount_smb_share(username: str, password: str, server: str, share_name: str, mountpoint: Path, *, timeout: int) -> subprocess.CompletedProcess[str]:
    remote = f"//{quote(username, safe='')}:{quote(password, safe='')}@{server}/{quote(share_name, safe='')}"
    return run_local_capture(["/sbin/mount_smbfs", remote, str(mountpoint)], timeout=timeout)


def _find_existing_smb_mount(username: str, share_name: str, *, timeout: int) -> Path | None:
    proc = run_local_capture(["/sbin/mount"], timeout=timeout)
    if proc.returncode != 0:
        return None
    pattern = re.compile(rf"^//{re.escape(username)}@[^/]+/{re.escape(share_name)} on (.+?) \(smbfs[,)]")
    for line in proc.stdout.splitlines():
        match = pattern.search(line.strip())
        if match:
            return Path(match.group(1))
    return None


def _unmount_smb_share(mountpoint: Path, *, timeout: int) -> None:
    proc = run_local_capture(["/sbin/umount", str(mountpoint)], timeout=timeout)
    if proc.returncode == 0:
        return
    if command_exists("diskutil"):
        run_local_capture(["/usr/sbin/diskutil", "unmount", "force", str(mountpoint)], timeout=timeout)


def check_authenticated_smb_file_ops(
    username: str,
    password: str,
    server: str,
    share_name: str,
    *,
    timeout: int = 20,
) -> CheckResult:
    if not command_exists("mount_smbfs"):
        return CheckResult("WARN", "SMB file-ops verification skipped: mount_smbfs not found")

    with tempfile.TemporaryDirectory(prefix="tcapsule-doctor-") as tmpdir:
        mountpoint = Path(tmpdir) / "share"
        mountpoint.mkdir()
        mounted = False
        try:
            proc = _mount_smb_share(username, password, server, share_name, mountpoint, timeout=timeout)
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout).strip().splitlines()
                msg = detail[-1] if detail else f"failed with rc={proc.returncode}"
                if "File exists" in msg:
                    existing_mount = _find_existing_smb_mount(username, share_name, timeout=timeout)
                    if existing_mount is not None:
                        exercise_mounted_share_file_ops(existing_mount, prefix="doctor-fileops-reuse")
                        return CheckResult(
                            "PASS",
                            f"authenticated SMB file ops work for {username}@{server}/{share_name} via existing mount {existing_mount}",
                        )
                return CheckResult("FAIL", f"authenticated SMB file ops failed to mount share: {msg}")
            mounted = True
            exercise_mounted_share_file_ops(mountpoint)
            return CheckResult("PASS", f"authenticated SMB file ops work for {username}@{server}/{share_name}")
        except Exception as exc:
            return CheckResult("FAIL", f"authenticated SMB file ops failed: {exc}")
        finally:
            if mounted:
                try:
                    _unmount_smb_share(mountpoint, timeout=timeout)
                except Exception:
                    pass
