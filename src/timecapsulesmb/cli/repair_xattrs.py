from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from timecapsulesmb.core.config import ENV_PATH, parse_env_values, require_valid_config


DEFAULT_EXCLUDED_DIR_NAMES = {
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


@dataclass(frozen=True)
class RepairCandidate:
    path: Path
    flags: str


@dataclass
class RepairSummary:
    scanned: int = 0
    skipped: int = 0
    repairable: int = 0
    repaired: int = 0
    failed: int = 0


def run_capture(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)


def default_share_path() -> Optional[Path]:
    values = parse_env_values(ENV_PATH)
    require_valid_config(values, profile="repair_xattrs")
    share_name = values.get("TC_SHARE_NAME")
    if not share_name:
        return None
    candidate = Path("/Volumes") / share_name
    if candidate.exists():
        return candidate
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
    if not include_hidden and path_has_hidden_component(path, root):
        return True
    if not include_time_machine and is_time_machine_path(path, root):
        return True
    return False


def iter_regular_files(
    root: Path,
    *,
    recursive: bool,
    max_depth: Optional[int],
    include_hidden: bool,
    include_time_machine: bool,
    summary: RepairSummary,
):
    root = root.resolve()
    if root.is_file():
        if not should_skip_path(root, root.parent, include_hidden=include_hidden, include_time_machine=include_time_machine):
            yield root
        else:
            summary.skipped += 1
        return

    if not root.is_dir():
        raise SystemExit(f"Path does not exist or is not a regular file/directory: {root}")

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
                    yield entry
                elif entry.is_dir() and recursive and (max_depth is None or depth < max_depth):
                    stack.append((entry, depth + 1))
                elif entry.is_dir():
                    summary.skipped += 1
            except OSError:
                summary.skipped += 1


def file_flags(path: Path) -> Optional[str]:
    proc = run_capture(["stat", "-f", "%Sf", str(path)])
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def xattrs_readable(path: Path) -> bool:
    return run_capture(["xattr", "-l", str(path)]).returncode == 0


def is_repairable(path: Path) -> tuple[bool, Optional[str]]:
    if xattrs_readable(path):
        return False, None
    flags = file_flags(path)
    if not flags:
        return False, None
    flag_set = {flag.strip() for flag in flags.split(",")}
    if "arch" not in flag_set:
        return False, flags
    return True, flags


def find_candidates(
    root: Path,
    *,
    recursive: bool,
    max_depth: Optional[int],
    include_hidden: bool,
    include_time_machine: bool,
    summary: RepairSummary,
) -> list[RepairCandidate]:
    candidates: list[RepairCandidate] = []
    for path in iter_regular_files(
        root,
        recursive=recursive,
        max_depth=max_depth,
        include_hidden=include_hidden,
        include_time_machine=include_time_machine,
        summary=summary,
    ):
        summary.scanned += 1
        repairable, flags = is_repairable(path)
        if repairable:
            candidates.append(RepairCandidate(path=path, flags=flags or "arch"))
    summary.repairable = len(candidates)
    return candidates


def repair_candidate(candidate: RepairCandidate) -> bool:
    before_size = candidate.path.stat().st_size
    proc = run_capture(["chflags", "noarch", str(candidate.path)])
    if proc.returncode != 0:
        return False
    if candidate.path.stat().st_size != before_size:
        return False
    return xattrs_readable(candidate.path)


def print_candidates(candidates: list[RepairCandidate], *, dry_run: bool) -> None:
    verb = "Would repair" if dry_run else "Repairable"
    for candidate in candidates:
        print(f"{verb}: {candidate.path} (flags: {candidate.flags})")


def print_summary(summary: RepairSummary, *, dry_run: bool) -> None:
    print("")
    print("Summary:")
    print(f"  scanned files: {summary.scanned}")
    print(f"  skipped: {summary.skipped}")
    print(f"  repairable: {summary.repairable}")
    if not dry_run:
        print(f"  repaired: {summary.repaired}")
        print(f"  failed: {summary.failed}")


def confirm(prompt: str) -> bool:
    return input(prompt).strip().lower() in {"y", "yes"}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Repair files whose SMB xattr metadata is broken by clearing the macOS arch flag.")
    parser.add_argument("--path", type=Path, default=None, help="Mounted SMB share path or subdirectory to scan. Defaults to /Volumes/<TC_SHARE_NAME>.")
    parser.add_argument("--dry-run", action="store_true", help="Only scan and report files; do not prompt or repair")
    parser.add_argument("--yes", action="store_true", help="Repair without prompting")
    parser.add_argument("--recursive", dest="recursive", action="store_true", default=True, help="Scan recursively (default)")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false", help="Only scan the top-level directory")
    parser.add_argument("--max-depth", type=int, default=None, help="Maximum directory depth to scan when recursive")
    parser.add_argument("--include-hidden", action="store_true", help="Include hidden dot paths")
    parser.add_argument("--include-time-machine", action="store_true", help="Include Time Machine and bundle-like paths normally skipped")
    args = parser.parse_args(argv)

    if args.dry_run and args.yes:
        parser.error("--dry-run and --yes are mutually exclusive")
    if args.max_depth is not None and args.max_depth < 0:
        parser.error("--max-depth must be non-negative")
    if sys.platform != "darwin":
        raise SystemExit("repair-xattrs must be run on macOS because it uses xattr/chflags on the mounted SMB share.")

    root = args.path or default_share_path()
    if root is None:
        raise SystemExit("Could not determine mounted share path. Pass --path explicitly.")
    root = root.expanduser()

    summary = RepairSummary()
    print(f"Scanning {root}")
    candidates = find_candidates(
        root,
        recursive=args.recursive,
        max_depth=args.max_depth,
        include_hidden=args.include_hidden,
        include_time_machine=args.include_time_machine,
        summary=summary,
    )

    if not candidates:
        print("No repairable files found.")
        print_summary(summary, dry_run=True)
        return 0

    print_candidates(candidates, dry_run=args.dry_run)
    if args.dry_run:
        print_summary(summary, dry_run=True)
        print("No changes made.")
        return 0

    if not args.yes and not confirm(f"Repair {len(candidates)} files by clearing the arch flag? [y/N]: "):
        print("No changes made.")
        print_summary(summary, dry_run=True)
        return 0

    for candidate in candidates:
        print(f"Repairing: {candidate.path}")
        if repair_candidate(candidate):
            summary.repaired += 1
            print(f"PASS xattr now readable: {candidate.path}")
        else:
            summary.failed += 1
            print(f"FAIL repair did not make xattr readable: {candidate.path}")

    print_summary(summary, dry_run=False)
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
