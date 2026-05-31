from __future__ import annotations

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


class RepairXattrsServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class RepairXattrsRequest:
    path: Path | None
    dry_run: bool
    approve_repairs: bool
    recursive: bool = True
    max_depth: int | None = None
    include_hidden: bool = False
    include_time_machine: bool = False
    fix_permissions: bool = False
    verbose: bool = False


@dataclass(frozen=True)
class RepairXattrsCallbacks:
    set_stage: Callable[[str], None] | None = None
    update_fields: Callable[..., None] | None = None
    log: Callable[[str], None] | None = None

    def stage(self, stage: str) -> None:
        if self.set_stage is not None:
            self.set_stage(stage)

    def update(self, **fields: object) -> None:
        if self.update_fields is not None:
            self.update_fields(**fields)

    def message(self, message: str) -> None:
        if self.log is not None:
            self.log(message)


@dataclass(frozen=True)
class RepairRunResult:
    returncode: int
    root: Path
    findings: list[RepairFinding]
    candidates: list[RepairCandidate]
    summary: RepairSummary
    report: str | None = None
    telemetry_result: str = "success"
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_payload_fields(self) -> dict[str, object]:
        return {
            "returncode": self.returncode,
            "root": str(self.root),
            "finding_count": len(self.findings),
            "repairable_count": len(self.candidates),
            "stats": self.summary,
            "report": self.report,
            "telemetry_result": self.telemetry_result,
            "error": self.error,
        }


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


def run_repair(
    request: RepairXattrsRequest,
    config: AppConfig,
    *,
    callbacks: RepairXattrsCallbacks | None = None,
    confirm: Callable[[str], bool] | None = None,
) -> RepairRunResult:
    callbacks = callbacks or RepairXattrsCallbacks()

    def emit(message: str) -> None:
        callbacks.message(message)

    callbacks.stage("resolve_scan_root")
    callbacks.update(
        dry_run=request.dry_run,
        recursive=request.recursive,
        max_depth=request.max_depth,
        include_hidden=request.include_hidden,
        include_time_machine=request.include_time_machine,
        fix_permissions=request.fix_permissions,
        explicit_path=request.path is not None,
    )
    if request.path is None:
        try:
            root = default_share_path_from_config(
                config,
                shares=mounted_smb_shares(),
                path_exists_func=path_exists,
            )
        except RuntimeError as exc:
            raise RepairXattrsServiceError(str(exc)) from exc
    else:
        root = request.path
    if root is None:
        raise RepairXattrsServiceError("Could not determine mounted share path. Pass --path explicitly.")
    try:
        root = validate_repair_root_under_volumes(root)
    except RuntimeError as exc:
        raise RepairXattrsServiceError(str(exc)) from exc

    summary = RepairSummary()
    callbacks.update(repair_root=str(root))
    callbacks.stage("scan_findings")
    emit(f"Scanning {root}")
    try:
        findings = find_findings(
            root,
            recursive=request.recursive,
            max_depth=request.max_depth,
            include_hidden=request.include_hidden,
            include_time_machine=request.include_time_machine,
            include_directories=True,
            include_root_directory=True,
            fix_permissions=request.fix_permissions,
            summary=summary,
        )
    except RuntimeError as exc:
        raise RepairXattrsServiceError(str(exc)) from exc
    repairs = actionable_findings(findings)
    candidates = [finding_to_candidate(finding) for finding in repairs]
    callbacks.update(
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
        return RepairRunResult(0, root, findings, candidates, summary)

    callbacks.stage("report_findings")
    _emit_lines(emit, render_diagnostic_lines(findings, verbose=request.verbose))
    if candidates:
        _emit_lines(emit, render_candidate_lines(candidates, dry_run=request.dry_run))

    if request.dry_run:
        _emit_lines(emit, render_summary_lines(summary, dry_run=True))
        emit("No changes made.")
        report = build_repair_report(findings)
        return RepairRunResult(0, root, findings, candidates, summary, report=report, telemetry_result="failure", error=report)

    if not candidates:
        emit("No known-safe repairs are available for the detected issues.")
        _emit_lines(emit, render_summary_lines(summary, dry_run=True))
        report = build_repair_report(findings)
        return RepairRunResult(1, root, findings, candidates, summary, report=report, telemetry_result="failure", error=report)

    callbacks.stage("confirm_repair")
    if not request.approve_repairs and confirm is None:
        message = "Running `repair-xattrs` in non-interactive mode requires `--yes` to apply repairs."
        emit(message)
        return RepairRunResult(1, root, findings, candidates, summary, report=message, telemetry_result="failure", error=message)
    if not request.approve_repairs and not confirm(f"Repair {len(candidates)} paths with known-safe fixes?"):
        emit("No changes made.")
        _emit_lines(emit, render_summary_lines(summary, dry_run=True))
        report = build_repair_report(findings)
        return RepairRunResult(0, root, findings, candidates, summary, report=report, telemetry_result="failure", error=report)

    callbacks.stage("repair_findings")
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
    callbacks.update(repaired_count=summary.repaired, repair_failed_count=summary.failed)
    _emit_lines(emit, render_summary_lines(summary, dry_run=False))
    if unresolved:
        report = build_repair_report(findings, failed=unresolved)
        return RepairRunResult(1, root, findings, candidates, summary, report=report, telemetry_result="failure", error=report)
    return RepairRunResult(0, root, findings, candidates, summary)
