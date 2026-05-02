from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from timecapsulesmb.core.config import require_valid_config


DEFAULT_EXCLUDED_DIR_NAMES = {
    ".samba4",
    ".timemachine",
    "Backups.backupdb",
}
DEFAULT_EXCLUDED_SUFFIXES = (
    ".app",
    ".bundle",
    ".framework",
    ".photoslibrary",
    ".musiclibrary",
    ".sparsebundle",
)
DEFAULT_EXCLUDED_PREFIXES = (
    ".com.apple.TimeMachine.",
    "Backups of ",
)
DEFAULT_REPAIR_REPORT_LIMIT = 20
ACTION_CLEAR_ARCH_FLAG = "clear_arch_flag"
ACTION_FIX_PERMISSIONS = "fix_permissions"


@dataclass(frozen=True)
class XattrStatus:
    readable: bool
    stdout: str
    stderr: str


@dataclass(frozen=True)
class RepairCandidate:
    path: Path
    flags: str
    path_type: str = "file"
    xattr_error: str | None = None
    actions: tuple[str, ...] = (ACTION_CLEAR_ARCH_FLAG,)


@dataclass(frozen=True)
class RepairFinding:
    path: Path
    path_type: str
    kind: str
    flags: str | None = None
    xattr_error: str | None = None
    actions: tuple[str, ...] = ()

    @property
    def repairable(self) -> bool:
        return bool(self.actions)


@dataclass
class RepairSummary:
    scanned: int = 0
    scanned_files: int = 0
    scanned_dirs: int = 0
    skipped: int = 0
    unreadable: int = 0
    not_repairable: int = 0
    repairable: int = 0
    permission_repairable: int = 0
    repaired: int = 0
    failed: int = 0


@dataclass(frozen=True)
class MountedSmbShare:
    server: str
    share: str
    mountpoint: Path


def run_capture(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)


def ssh_target_host(target: str) -> str:
    return target.rsplit("@", 1)[-1].strip()


def parse_mounted_smb_shares(mount_output: str) -> list[MountedSmbShare]:
    shares: list[MountedSmbShare] = []
    for line in mount_output.splitlines():
        if " (smbfs," not in line and " (smbfs)" not in line:
            continue
        if not line.startswith("//") or " on " not in line:
            continue
        source, rest = line[2:].split(" on ", 1)
        mountpoint_text = rest.split(" (", 1)[0]
        if "/" not in source:
            continue
        user_and_server, share = source.rsplit("/", 1)
        server = user_and_server.rsplit("@", 1)[-1]
        shares.append(MountedSmbShare(server=unquote(server), share=unquote(share), mountpoint=Path(mountpoint_text)))
    return shares


def mounted_smb_shares() -> list[MountedSmbShare]:
    proc = run_capture(["mount"])
    if proc.returncode != 0:
        return []
    return parse_mounted_smb_shares(proc.stdout)


def path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def default_share_path_from_values(
    values: dict[str, str],
    *,
    shares: list[MountedSmbShare] | None = None,
    path_exists_func: Callable[[Path], bool] = path_exists,
) -> Optional[Path]:
    require_valid_config(values, profile="repair_xattrs")
    share_name = values.get("TC_SHARE_NAME")
    target_host = ssh_target_host(values.get("TC_HOST", ""))
    if not share_name or not target_host:
        return None

    available_shares = mounted_smb_shares() if shares is None else shares
    candidates = [
        share
        for share in available_shares
        if share.share == share_name and path_exists_func(share.mountpoint)
    ]
    for share in candidates:
        if share.server.lower() == target_host.lower():
            return share.mountpoint
    if len(candidates) == 1:
        return candidates[0].mountpoint
    if len(candidates) > 1:
        raise SystemExit(f"Found multiple mounted SMB shares named {share_name!r}; pass --path explicitly.")
    return None


def path_has_hidden_component(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    return any(part.startswith(".") for part in relative.parts)


def is_time_machine_path(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    for part in relative.parts:
        if part in DEFAULT_EXCLUDED_DIR_NAMES:
            return True
        if any(part.startswith(prefix) for prefix in DEFAULT_EXCLUDED_PREFIXES):
            return True
        if part.endswith(DEFAULT_EXCLUDED_SUFFIXES):
            return True
    return False


def should_skip_path(path: Path, root: Path, *, include_hidden: bool, include_time_machine: bool) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    if ".samba4" in relative.parts:
        return True
    if not include_hidden and path_has_hidden_component(path, root):
        return True
    if not include_time_machine and is_time_machine_path(path, root):
        return True
    return False


def iter_scan_paths(
    root: Path,
    *,
    recursive: bool,
    max_depth: Optional[int],
    include_hidden: bool,
    include_time_machine: bool,
    include_directories: bool = False,
    include_root_directory: bool = False,
    summary: RepairSummary,
):
    try:
        root = root.resolve()
        root_is_file = root.is_file()
        root_is_dir = root.is_dir()
    except OSError as exc:
        raise SystemExit(f"Cannot access path: {root}: {exc}") from exc

    if root_is_file:
        if not should_skip_path(
            root,
            root.parent,
            include_hidden=include_hidden,
            include_time_machine=include_time_machine,
        ):
            yield root, "file"
        else:
            summary.skipped += 1
        return

    if not root_is_dir:
        raise SystemExit(f"Path does not exist or is not a regular file/directory: {root}")

    if (
        include_directories
        and include_root_directory
        and not should_skip_path(root, root, include_hidden=include_hidden, include_time_machine=include_time_machine)
    ):
        yield root, "directory"

    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = list(directory.iterdir())
        except OSError:
            summary.skipped += 1
            continue

        for entry in entries:
            if should_skip_path(entry, root, include_hidden=include_hidden, include_time_machine=include_time_machine):
                summary.skipped += 1
                continue
            try:
                if entry.is_symlink():
                    summary.skipped += 1
                elif entry.is_file():
                    yield entry, "file"
                elif entry.is_dir():
                    if include_directories:
                        yield entry, "directory"
                    if recursive and (max_depth is None or depth < max_depth):
                        stack.append((entry, depth + 1))
                    else:
                        summary.skipped += 1
                else:
                    summary.skipped += 1
            except OSError:
                summary.skipped += 1


def file_flags(path: Path) -> Optional[str]:
    proc = run_capture(["stat", "-f", "%Sf", str(path)])
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def xattr_status(path: Path) -> XattrStatus:
    proc = run_capture(["xattr", "-l", str(path)])
    return XattrStatus(readable=proc.returncode == 0, stdout=proc.stdout, stderr=proc.stderr)


def xattrs_readable(path: Path) -> bool:
    return xattr_status(path).readable


def xattr_error_text(status: XattrStatus) -> str:
    return (status.stderr or status.stdout or "xattr unreadable").strip()


def desired_permission_action(path: Path, path_type: str, *, fix_permissions: bool) -> tuple[str, ...]:
    if not fix_permissions:
        return ()
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return ()
    desired_bits = 0o777 if path_type == "directory" else 0o666
    if mode & desired_bits == desired_bits:
        return ()
    return (ACTION_FIX_PERMISSIONS,)


def classify_path(path: Path, path_type: str, *, fix_permissions: bool = False) -> RepairFinding:
    permission_actions = desired_permission_action(path, path_type, fix_permissions=fix_permissions)
    status = xattr_status(path)
    if status.readable:
        if permission_actions:
            return RepairFinding(path=path, path_type=path_type, kind="permission_repair", actions=permission_actions)
        return RepairFinding(path=path, path_type=path_type, kind="ok")

    flags = file_flags(path)
    if not flags:
        return RepairFinding(
            path=path,
            path_type=path_type,
            kind="xattr_failed_stat_failed",
            xattr_error=xattr_error_text(status),
            actions=permission_actions,
        )
    flag_set = {flag.strip() for flag in flags.split(",")}
    if "arch" not in flag_set:
        return RepairFinding(
            path=path,
            path_type=path_type,
            kind="unreadable_no_arch_flag",
            flags=flags,
            xattr_error=xattr_error_text(status),
            actions=permission_actions,
        )
    return RepairFinding(
        path=path,
        path_type=path_type,
        kind="repairable_arch_flag",
        flags=flags,
        xattr_error=xattr_error_text(status),
        actions=(ACTION_CLEAR_ARCH_FLAG,) + permission_actions,
    )


def find_findings(
    root: Path,
    *,
    recursive: bool,
    max_depth: Optional[int],
    include_hidden: bool,
    include_time_machine: bool,
    include_directories: bool = False,
    include_root_directory: bool = False,
    fix_permissions: bool = False,
    summary: RepairSummary,
) -> list[RepairFinding]:
    findings: list[RepairFinding] = []
    for path, path_type in iter_scan_paths(
        root,
        recursive=recursive,
        max_depth=max_depth,
        include_hidden=include_hidden,
        include_time_machine=include_time_machine,
        include_directories=include_directories,
        include_root_directory=include_root_directory,
        summary=summary,
    ):
        summary.scanned += 1
        if path_type == "directory":
            summary.scanned_dirs += 1
        else:
            summary.scanned_files += 1
        finding = classify_path(path, path_type, fix_permissions=fix_permissions)
        if finding.kind == "ok":
            continue
        if finding.xattr_error:
            summary.unreadable += 1
        if finding.repairable:
            summary.repairable += 1
            if ACTION_FIX_PERMISSIONS in finding.actions:
                summary.permission_repairable += 1
        else:
            summary.not_repairable += 1
        findings.append(finding)
    return findings


def apply_permission_repair(path: Path, path_type: str) -> bool:
    mode = "ugo+rwx" if path_type == "directory" else "ugo+rw"
    proc = run_capture(["chmod", mode, str(path)])
    return proc.returncode == 0


def repair_candidate(candidate: RepairCandidate) -> bool:
    try:
        before_size = candidate.path.stat().st_size if candidate.path_type == "file" else None
    except OSError:
        return False
    if ACTION_CLEAR_ARCH_FLAG in candidate.actions:
        proc = run_capture(["chflags", "noarch", str(candidate.path)])
        if proc.returncode != 0:
            return False
        try:
            if before_size is not None and candidate.path.stat().st_size != before_size:
                return False
        except OSError:
            return False
        if not xattrs_readable(candidate.path):
            return False
    if ACTION_FIX_PERMISSIONS in candidate.actions and not apply_permission_repair(candidate.path, candidate.path_type):
        return False
    return True


def finding_to_candidate(finding: RepairFinding) -> RepairCandidate:
    return RepairCandidate(
        path=finding.path,
        flags=finding.flags or "",
        path_type=finding.path_type,
        xattr_error=finding.xattr_error,
        actions=finding.actions,
    )


def actionable_findings(findings: list[RepairFinding]) -> list[RepairFinding]:
    return [finding for finding in findings if finding.repairable]


def unresolved_findings_after_success(findings: list[RepairFinding]) -> list[RepairFinding]:
    return [finding for finding in findings if not finding.repairable]


def format_finding_line(finding: RepairFinding) -> str:
    actions = ",".join(finding.actions) if finding.actions else "none"
    parts = [f"{finding.kind}: {finding.path}", f"type={finding.path_type}", f"actions={actions}"]
    if finding.flags:
        parts.append(f"flags={finding.flags}")
    if finding.xattr_error:
        parts.append(f"xattr_error={finding.xattr_error}")
    return " ".join(parts)


def build_repair_report(
    findings: list[RepairFinding],
    *,
    failed: list[RepairFinding] | None = None,
    limit: int = DEFAULT_REPAIR_REPORT_LIMIT,
) -> str:
    failed = failed or []
    lines = [
        (
            "repair-xattrs detected issues: "
            f"total={len(findings)} repairable={len(actionable_findings(findings))} failed={len(failed)}"
        ),
    ]
    selected = failed or findings
    for finding in selected[:limit]:
        lines.append(format_finding_line(finding))
    remaining = len(selected) - limit
    if remaining > 0:
        lines.append(f"... and {remaining} more")
    return "\n".join(lines)
