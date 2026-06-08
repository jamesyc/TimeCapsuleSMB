from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from timecapsulesmb.core.config import AppConfig, validate_app_config
from timecapsulesmb.core.net import endpoint_host


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
REPAIR_ROOT_PARENT = Path("/Volumes")


@dataclass(frozen=True)
class XattrStatus:
    readable: bool
    stdout: str
    stderr: str


@dataclass(frozen=True)
class XattrProbe:
    status: XattrStatus
    name_status: XattrStatus | None = None
    names: tuple[str, ...] = ()
    failed_attribute: str | None = None
    failed_attribute_status: XattrStatus | None = None


@dataclass(frozen=True)
class FileDataStatus:
    readable: bool | None
    error: str | None = None


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
    xattr_names: tuple[str, ...] = ()
    xattr_failed_attribute: str | None = None
    file_data_error: str | None = None
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
    return endpoint_host(target).strip()


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


def validate_repair_root_under_volumes(path: Path) -> Path:
    try:
        resolved_path = path.expanduser().resolve()
        resolved_volumes = REPAIR_ROOT_PARENT.resolve()
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"Cannot access path: {path}: {exc}") from exc

    try:
        relative = resolved_path.relative_to(resolved_volumes)
    except ValueError as exc:
        raise RuntimeError(
            f"repair-xattrs can only scan mounted volumes under {REPAIR_ROOT_PARENT}. "
            f"Refusing to scan: {resolved_path}"
        ) from exc

    if not relative.parts:
        raise RuntimeError(
            f"repair-xattrs requires a mounted volume below {REPAIR_ROOT_PARENT}, not {REPAIR_ROOT_PARENT} itself. "
            f"Pass a mounted SMB share path such as {REPAIR_ROOT_PARENT / 'Data'}."
        )

    return resolved_path


def default_share_path_from_config(
    config: AppConfig,
    *,
    shares: list[MountedSmbShare] | None = None,
    path_exists_func: Callable[[Path], bool] = path_exists,
) -> Optional[Path]:
    errors = validate_app_config(config, profile="repair_xattrs")
    if errors:
        raise RuntimeError(errors[0].format_for_cli())
    target_host = ssh_target_host(config.get("TC_HOST"))
    if not target_host:
        return None

    available_shares = mounted_smb_shares() if shares is None else shares
    candidates = [
        share
        for share in available_shares
        if path_exists_func(share.mountpoint)
    ]
    target_matches = [share for share in candidates if share.server.lower() == target_host.lower()]
    if len(target_matches) == 1:
        return target_matches[0].mountpoint
    if len(target_matches) > 1:
        raise RuntimeError(f"Found multiple mounted SMB shares from {target_host}; pass --path explicitly.")
    if len(candidates) == 1:
        return candidates[0].mountpoint
    if len(candidates) > 1:
        raise RuntimeError(f"Found multiple mounted SMB shares; pass --path explicitly.")
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
) -> Iterator[tuple[Path, str]]:
    try:
        root = root.resolve()
        root_is_file = root.is_file()
        root_is_dir = root.is_dir()
    except OSError as exc:
        raise RuntimeError(f"Cannot access path: {root}: {exc}") from exc

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
        raise RuntimeError(f"Path does not exist or is not a regular file/directory: {root}")

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
            for entry in directory.iterdir():
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
        except OSError:
            summary.skipped += 1
            continue


def file_flags(path: Path) -> Optional[str]:
    proc = run_capture(["stat", "-f", "%Sf", str(path)])
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def xattr_status(path: Path) -> XattrStatus:
    proc = run_capture(["xattr", "-l", str(path)])
    return XattrStatus(readable=proc.returncode == 0, stdout=proc.stdout, stderr=proc.stderr)


def xattr_name_status(path: Path) -> XattrStatus:
    proc = run_capture(["xattr", str(path)])
    return XattrStatus(readable=proc.returncode == 0, stdout=proc.stdout, stderr=proc.stderr)


def xattr_value_status(path: Path, name: str) -> XattrStatus:
    proc = run_capture(["xattr", "-p", "-x", name, str(path)])
    return XattrStatus(readable=proc.returncode == 0, stdout=proc.stdout, stderr=proc.stderr)


def parse_xattr_names(stdout: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in stdout.splitlines() if line.strip())


def xattr_probe(path: Path) -> XattrProbe:
    status = xattr_status(path)
    if status.readable:
        return XattrProbe(status=status)

    name_status = xattr_name_status(path)
    if not name_status.readable:
        return XattrProbe(status=status, name_status=name_status)

    names = parse_xattr_names(name_status.stdout)
    for name in names:
        value_status = xattr_value_status(path, name)
        if not value_status.readable:
            return XattrProbe(
                status=status,
                name_status=name_status,
                names=names,
                failed_attribute=name,
                failed_attribute_status=value_status,
            )
    return XattrProbe(status=status, name_status=name_status, names=names)


def xattrs_readable(path: Path) -> bool:
    return xattr_status(path).readable


def xattr_error_text(status: XattrStatus) -> str:
    return (status.stderr or status.stdout or "xattr unreadable").strip()


def xattr_probe_error_text(probe: XattrProbe) -> str:
    if probe.failed_attribute and probe.failed_attribute_status:
        return f"{probe.failed_attribute}: {xattr_error_text(probe.failed_attribute_status)}"
    if probe.name_status is not None and not probe.name_status.readable:
        return xattr_error_text(probe.name_status)
    return xattr_error_text(probe.status)


def is_io_error_text(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return "errno 5" in lowered or "input/output error" in lowered or " eio" in lowered


def no_arch_xattr_failure_kind(probe: XattrProbe) -> str:
    error_kind = "io_error" if is_io_error_text(xattr_probe_error_text(probe)) else "failed"
    if probe.name_status is not None and not probe.name_status.readable:
        return f"xattr_list_{error_kind}_no_arch"
    if probe.failed_attribute_status is not None:
        return f"xattr_value_{error_kind}_no_arch"
    return f"xattr_display_{error_kind}_no_arch"


def file_data_status(path: Path, path_type: str) -> FileDataStatus:
    if path_type != "file":
        return FileDataStatus(readable=None)
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        return FileDataStatus(readable=False, error=str(exc))
    return FileDataStatus(readable=True)


def file_data_failure_kind(status: FileDataStatus) -> str:
    return "file_data_io_error" if is_io_error_text(status.error) else "file_data_read_failed"


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
    probe = xattr_probe(path)
    if probe.status.readable:
        if permission_actions:
            return RepairFinding(path=path, path_type=path_type, kind="permission_repair", actions=permission_actions)
        return RepairFinding(path=path, path_type=path_type, kind="ok")

    flags = file_flags(path)
    if not flags:
        data_status = file_data_status(path, path_type)
        return RepairFinding(
            path=path,
            path_type=path_type,
            kind=file_data_failure_kind(data_status) if data_status.readable is False else "xattr_failed_stat_failed",
            xattr_error=xattr_probe_error_text(probe),
            xattr_names=probe.names,
            xattr_failed_attribute=probe.failed_attribute,
            file_data_error=data_status.error,
            actions=permission_actions,
        )
    flag_set = {flag.strip() for flag in flags.split(",")}
    if "arch" not in flag_set:
        data_status = file_data_status(path, path_type)
        return RepairFinding(
            path=path,
            path_type=path_type,
            kind=(
                file_data_failure_kind(data_status)
                if data_status.readable is False
                else no_arch_xattr_failure_kind(probe)
            ),
            flags=flags,
            xattr_error=xattr_probe_error_text(probe),
            xattr_names=probe.names,
            xattr_failed_attribute=probe.failed_attribute,
            file_data_error=data_status.error,
            actions=permission_actions,
        )
    return RepairFinding(
        path=path,
        path_type=path_type,
        kind="repairable_arch_flag",
        flags=flags,
        xattr_error=xattr_probe_error_text(probe),
        xattr_names=probe.names,
        xattr_failed_attribute=probe.failed_attribute,
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
    if candidate.xattr_error and not xattrs_readable(candidate.path):
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
    if finding.xattr_failed_attribute:
        parts.append(f"xattr_failed_attribute={finding.xattr_failed_attribute}")
    if finding.xattr_names:
        parts.append(f"xattr_names={format_xattr_names(finding.xattr_names)}")
    if finding.xattr_error:
        parts.append(f"xattr_error={finding.xattr_error}")
    if finding.file_data_error:
        parts.append(f"file_data_error={finding.file_data_error}")
    return " ".join(parts)


def format_xattr_names(names: tuple[str, ...], *, limit: int = 6) -> str:
    selected = list(names[:limit])
    if len(names) > limit:
        selected.append(f"...+{len(names) - limit}")
    return ",".join(selected)


APPLE_METADATA_GUIDANCE_KINDS = {
    "xattr_list_io_error_no_arch",
    "xattr_value_io_error_no_arch",
    "xattr_display_io_error_no_arch",
}


def finding_suggests_apple_metadata_io(finding: RepairFinding) -> bool:
    return finding.kind in APPLE_METADATA_GUIDANCE_KINDS


def metadata_io_guidance_lines(findings: list[RepairFinding]) -> list[str]:
    if not any(finding_suggests_apple_metadata_io(finding) for finding in findings):
        return []
    return [
        (
            "Detected xattr I/O errors without the arch flag; this commonly matches Apple SMB/Fruit metadata "
            "stream state rather than the known arch-flag repair case."
        ),
        (
            "Enable Netatalk metadata for the device profile, redeploy, then test with newly copied "
            "iPhone/iPad Photos files."
        ),
        (
            "Existing damaged files may need to be re-copied or cleaned with an explicit metadata-removal "
            "workflow after validation; repair-xattrs will not clear all xattrs automatically."
        ),
    ]


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
    lines.extend(metadata_io_guidance_lines(findings))
    return "\n".join(lines)
