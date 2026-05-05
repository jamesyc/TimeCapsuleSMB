from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import load_env_config
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.repair_xattrs import (
    ACTION_CLEAR_ARCH_FLAG,
    ACTION_FIX_PERMISSIONS,
    DEFAULT_REPAIR_REPORT_LIMIT,
    MountedSmbShare,
    RepairCandidate,
    RepairFinding,
    RepairSummary,
    XattrStatus,
    actionable_findings,
    build_repair_report,
    classify_path,
    default_share_path_from_config,
    file_flags,
    find_findings,
    finding_to_candidate,
    format_finding_line,
    is_time_machine_path,
    iter_scan_paths,
    mounted_smb_shares,
    parse_mounted_smb_shares,
    path_exists,
    path_has_hidden_component,
    repair_candidate,
    run_capture,
    should_skip_path,
    ssh_target_host,
    unresolved_findings_after_success,
    validate_repair_root_under_volumes,
    xattr_status,
    xattrs_readable,
)
from timecapsulesmb.telemetry import TelemetryClient


def default_share_path() -> Optional[Path]:
    config = load_env_config()
    try:
        return default_share_path_from_config(
            config,
            shares=mounted_smb_shares(),
            path_exists_func=path_exists,
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


def print_candidates(candidates: list[RepairCandidate], *, dry_run: bool) -> None:
    verb = "Would repair" if dry_run else "Repairable"
    for candidate in candidates:
        actions = ", ".join(candidate.actions) or "none"
        flags = f", flags: {candidate.flags}" if candidate.flags else ""
        print(f"{verb}: {candidate.path} ({candidate.path_type}, actions: {actions}{flags})")


def print_diagnostics(findings: list[RepairFinding], *, verbose: bool) -> None:
    for finding in findings:
        if finding.repairable:
            continue
        if finding.xattr_error or verbose:
            detail = f"{finding.kind}: {finding.path} ({finding.path_type})"
            if finding.flags:
                detail += f" flags={finding.flags}"
            if finding.xattr_error:
                detail += f" xattr_error={finding.xattr_error}"
            print(f"WARN {detail}")


def print_summary(summary: RepairSummary, *, dry_run: bool) -> None:
    print("")
    print("Summary:")
    print(f"  scanned paths: {summary.scanned}")
    print(f"  scanned files: {summary.scanned_files}")
    print(f"  scanned directories: {summary.scanned_dirs}")
    print(f"  skipped: {summary.skipped}")
    print(f"  unreadable xattrs: {summary.unreadable}")
    print(f"  not repairable: {summary.not_repairable}")
    print(f"  repairable: {summary.repairable}")
    print(f"  permission repairs: {summary.permission_repairable}")
    if not dry_run:
        print(f"  repaired: {summary.repaired}")
        print(f"  failed: {summary.failed}")


def confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in {"y", "yes"}
    except EOFError:
        return False
    except KeyboardInterrupt:
        print()
        return False


def load_telemetry_config(explicit_path: Path | None) -> AppConfig:
    try:
        config = load_env_config()
    except (OSError, SystemExit):
        return AppConfig.missing()
    if explicit_path is None:
        return config
    return config


def run_repair(args: argparse.Namespace, command_context: CommandContext) -> int:
    root = args.path or default_share_path()
    if root is None:
        raise SystemExit("Could not determine mounted share path. Pass --path explicitly.")
    try:
        root = validate_repair_root_under_volumes(root)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    summary = RepairSummary()
    print(f"Scanning {root}")
    try:
        findings = find_findings(
            root,
            recursive=args.recursive,
            max_depth=args.max_depth,
            include_hidden=args.include_hidden,
            include_time_machine=args.include_time_machine,
            include_directories=True,
            include_root_directory=True,
            fix_permissions=args.fix_permissions,
            summary=summary,
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    repairs = actionable_findings(findings)
    candidates = [finding_to_candidate(finding) for finding in repairs]

    if not findings:
        print("No repairable files found.")
        print_summary(summary, dry_run=True)
        command_context.succeed()
        return 0

    print_diagnostics(findings, verbose=args.verbose)
    if candidates:
        print_candidates(candidates, dry_run=args.dry_run)

    if args.dry_run:
        print_summary(summary, dry_run=True)
        print("No changes made.")
        command_context.fail_with_error(build_repair_report(findings))
        return 0

    if not candidates:
        print("No known-safe repairs are available for the detected issues.")
        print_summary(summary, dry_run=True)
        command_context.fail_with_error(build_repair_report(findings))
        return 1

    if not args.yes and not confirm(f"Repair {len(candidates)} paths with known-safe fixes? [y/N]: "):
        print("No changes made.")
        print_summary(summary, dry_run=True)
        command_context.fail_with_error(build_repair_report(findings))
        return 0

    failed_findings: list[RepairFinding] = []
    for finding, candidate in zip(repairs, candidates):
        print(f"Repairing: {candidate.path}")
        if repair_candidate(candidate):
            summary.repaired += 1
            if ACTION_CLEAR_ARCH_FLAG in candidate.actions:
                print(f"PASS xattr now readable: {candidate.path}")
            if ACTION_FIX_PERMISSIONS in candidate.actions:
                print(f"PASS permissions repaired: {candidate.path}")
        else:
            summary.failed += 1
            failed_findings.append(finding)
            if ACTION_CLEAR_ARCH_FLAG in candidate.actions:
                print(f"FAIL repair did not make xattr readable: {candidate.path}")
            else:
                print(f"FAIL repair did not fix detected issue: {candidate.path}")

    unresolved = unresolved_findings_after_success(findings) + failed_findings
    print_summary(summary, dry_run=False)
    if unresolved:
        command_context.fail_with_error(build_repair_report(findings, failed=unresolved))
        return 1
    command_context.succeed()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Repair files whose SMB xattr metadata is broken by clearing the macOS arch flag.")
    parser.add_argument("--path", type=Path, default=None, help="Mounted SMB share path or subdirectory to scan. Defaults to the mounted SMB share matching .env.")
    parser.add_argument("--dry-run", action="store_true", help="Only scan and report files; do not prompt or repair")
    parser.add_argument("--yes", action="store_true", help="Repair without prompting")
    parser.add_argument("--recursive", dest="recursive", action="store_true", default=True, help="Scan recursively (default)")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false", help="Only scan the top-level directory")
    parser.add_argument("--max-depth", type=int, default=None, help="Maximum directory depth to scan when recursive")
    parser.add_argument("--include-hidden", action="store_true", help="Include hidden dot paths")
    parser.add_argument("--include-time-machine", action="store_true", help="Include Time Machine and bundle-like paths normally skipped")
    parser.add_argument("--fix-permissions", action="store_true", help="Also repair missing write permissions on scanned files/directories")
    parser.add_argument("--verbose", action="store_true", help="Print detailed diagnostics for detected issues")
    args = parser.parse_args(argv)

    if args.dry_run and args.yes:
        parser.error("--dry-run and --yes are mutually exclusive")
    if args.max_depth is not None and args.max_depth < 0:
        parser.error("--max-depth must be non-negative")
    if sys.platform != "darwin":
        raise SystemExit("repair-xattrs must be run on macOS because it uses xattr/chflags on the mounted SMB share.")

    config = load_telemetry_config(args.path)
    telemetry = TelemetryClient.from_config(config)
    with CommandContext(telemetry, "repair-xattrs", "repair_xattrs_started", "repair_xattrs_finished", config=config, args=args) as command_context:
        return run_repair(args, command_context)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
