from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.repair_xattrs import (
    ACTION_CLEAR_ARCH_FLAG,
    ACTION_FIX_PERMISSIONS,
    RepairCandidate,
    RepairFinding,
    RepairSummary,
    actionable_findings,
    build_repair_report,
    default_share_path_from_config,
    find_findings,
    finding_to_candidate,
    mounted_smb_shares,
    path_exists,
    repair_candidate,
    unresolved_findings_after_success,
    validate_repair_root_under_volumes,
)


@dataclass(frozen=True)
class RepairRunResult:
    returncode: int
    root: Path
    findings: list[RepairFinding]
    candidates: list[RepairCandidate]
    summary: RepairSummary
    report: str | None = None


def render_candidate_lines(candidates: list[RepairCandidate], *, dry_run: bool) -> list[str]:
    verb = "Would repair" if dry_run else "Repairable"
    lines: list[str] = []
    for candidate in candidates:
        actions = ", ".join(candidate.actions) or "none"
        flags = f", flags: {candidate.flags}" if candidate.flags else ""
        lines.append(f"{verb}: {candidate.path} ({candidate.path_type}, actions: {actions}{flags})")
    return lines


def render_diagnostic_lines(findings: list[RepairFinding], *, verbose: bool) -> list[str]:
    lines: list[str] = []
    for finding in findings:
        if finding.repairable:
            continue
        if finding.xattr_error or verbose:
            detail = f"{finding.kind}: {finding.path} ({finding.path_type})"
            if finding.flags:
                detail += f" flags={finding.flags}"
            if finding.xattr_error:
                detail += f" xattr_error={finding.xattr_error}"
            lines.append(f"WARN {detail}")
    return lines


def render_summary_lines(summary: RepairSummary, *, dry_run: bool) -> list[str]:
    lines = [
        "",
        "Summary:",
        f"  scanned paths: {summary.scanned}",
        f"  scanned files: {summary.scanned_files}",
        f"  scanned directories: {summary.scanned_dirs}",
        f"  skipped: {summary.skipped}",
        f"  unreadable xattrs: {summary.unreadable}",
        f"  not repairable: {summary.not_repairable}",
        f"  repairable: {summary.repairable}",
        f"  permission repairs: {summary.permission_repairable}",
    ]
    if not dry_run:
        lines.extend([
            f"  repaired: {summary.repaired}",
            f"  failed: {summary.failed}",
        ])
    return lines


def _emit_lines(emit: Callable[[str], None], lines: list[str]) -> None:
    for line in lines:
        emit(line)


def run_repair_structured(
    args: argparse.Namespace,
    command_context,
    config: AppConfig,
    *,
    emit_log: Callable[[str], None] | None = None,
    confirm: Callable[[str], bool] | None = None,
) -> RepairRunResult:
    def emit(message: str) -> None:
        if emit_log is not None:
            emit_log(message)

    command_context.set_stage("resolve_scan_root")
    command_context.update_fields(
        dry_run=args.dry_run,
        recursive=args.recursive,
        max_depth=args.max_depth,
        include_hidden=args.include_hidden,
        include_time_machine=args.include_time_machine,
        fix_permissions=args.fix_permissions,
        explicit_path=args.path is not None,
    )
    if args.path is None:
        try:
            root = default_share_path_from_config(
                config,
                shares=mounted_smb_shares(),
                path_exists_func=path_exists,
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
    else:
        root = args.path
    if root is None:
        raise SystemExit("Could not determine mounted share path. Pass --path explicitly.")
    try:
        root = validate_repair_root_under_volumes(root)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    summary = RepairSummary()
    command_context.update_fields(repair_root=str(root))
    command_context.set_stage("scan_findings")
    emit(f"Scanning {root}")
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
    command_context.update_fields(
        scanned_paths=summary.scanned,
        scanned_files=summary.scanned_files,
        scanned_dirs=summary.scanned_dirs,
        skipped_paths=summary.skipped,
        unreadable_xattrs=summary.unreadable,
        finding_count=len(findings),
        repairable_count=len(candidates),
        permission_repairable=summary.permission_repairable,
    )

    if not findings:
        emit("No repairable files found.")
        _emit_lines(emit, render_summary_lines(summary, dry_run=True))
        command_context.succeed()
        return RepairRunResult(0, root, findings, candidates, summary)

    command_context.set_stage("report_findings")
    _emit_lines(emit, render_diagnostic_lines(findings, verbose=args.verbose))
    if candidates:
        _emit_lines(emit, render_candidate_lines(candidates, dry_run=args.dry_run))

    if args.dry_run:
        _emit_lines(emit, render_summary_lines(summary, dry_run=True))
        emit("No changes made.")
        report = build_repair_report(findings)
        command_context.fail_with_error(report)
        return RepairRunResult(0, root, findings, candidates, summary, report=report)

    if not candidates:
        emit("No known-safe repairs are available for the detected issues.")
        _emit_lines(emit, render_summary_lines(summary, dry_run=True))
        report = build_repair_report(findings)
        command_context.fail_with_error(report)
        return RepairRunResult(1, root, findings, candidates, summary, report=report)

    command_context.set_stage("confirm_repair")
    if not args.yes and not (confirm is not None and confirm(f"Repair {len(candidates)} paths with known-safe fixes?")):
        emit("No changes made.")
        _emit_lines(emit, render_summary_lines(summary, dry_run=True))
        report = build_repair_report(findings)
        command_context.fail_with_error(report)
        return RepairRunResult(0, root, findings, candidates, summary, report=report)

    command_context.set_stage("repair_findings")
    failed_findings: list[RepairFinding] = []
    for finding, candidate in zip(repairs, candidates):
        emit(f"Repairing: {candidate.path}")
        if repair_candidate(candidate):
            summary.repaired += 1
            if ACTION_CLEAR_ARCH_FLAG in candidate.actions:
                emit(f"PASS xattr now readable: {candidate.path}")
            if ACTION_FIX_PERMISSIONS in candidate.actions:
                emit(f"PASS permissions repaired: {candidate.path}")
        else:
            summary.failed += 1
            failed_findings.append(finding)
            if ACTION_CLEAR_ARCH_FLAG in candidate.actions:
                emit(f"FAIL repair did not make xattr readable: {candidate.path}")
            else:
                emit(f"FAIL repair did not fix detected issue: {candidate.path}")

    unresolved = unresolved_findings_after_success(findings) + failed_findings
    command_context.update_fields(repaired_count=summary.repaired, repair_failed_count=summary.failed)
    _emit_lines(emit, render_summary_lines(summary, dry_run=False))
    if unresolved:
        report = build_repair_report(findings, failed=unresolved)
        command_context.fail_with_error(report)
        return RepairRunResult(1, root, findings, candidates, summary, report=report)
    command_context.succeed()
    return RepairRunResult(0, root, findings, candidates, summary)
