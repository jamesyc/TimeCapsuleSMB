from __future__ import annotations

import argparse
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from timecapsulesmb.app.contracts import repair_xattrs_payload
from timecapsulesmb.app.events import EventSink
from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import add_config_argument, confirm as confirm_prompt, load_optional_env_config
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.core.errors import system_exit_message
from timecapsulesmb.identity import ensure_install_id
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
from timecapsulesmb.services.app import jsonable
from timecapsulesmb.services.maintenance import LineLogCapture, RepairExecutionContext
from timecapsulesmb.telemetry import TelemetryClient


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


def print_candidates(candidates: list[RepairCandidate], *, dry_run: bool) -> None:
    for line in render_candidate_lines(candidates, dry_run=dry_run):
        print(line)


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


def print_diagnostics(findings: list[RepairFinding], *, verbose: bool) -> None:
    for line in render_diagnostic_lines(findings, verbose=verbose):
        print(line)


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


def print_summary(summary: RepairSummary, *, dry_run: bool) -> None:
    for line in render_summary_lines(summary, dry_run=dry_run):
        print(line)


def confirm(prompt_text: str) -> bool:
    return confirm_prompt(prompt_text, default=False, eof_default=False, interrupt_default=False)


def _emit_lines(emit: Callable[[str], None], lines: list[str]) -> None:
    for line in lines:
        emit(line)


def run_repair_structured(
    args: argparse.Namespace,
    command_context: CommandContext,
    config: AppConfig,
    *,
    emit_log: Callable[[str], None] | None = None,
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
    if not args.yes and not confirm(f"Repair {len(candidates)} paths with known-safe fixes?"):
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


def run_repair(args: argparse.Namespace, command_context: CommandContext, config: AppConfig) -> int:
    return run_repair_structured(args, command_context, config, emit_log=print).returncode


def _repair_result_payload(result: RepairRunResult, context: RepairExecutionContext | CommandContext) -> dict[str, object]:
    return repair_xattrs_payload({
        "returncode": result.returncode,
        "root": str(result.root),
        "finding_count": len(result.findings),
        "repairable_count": len(result.candidates),
        "stats": jsonable(result.summary),
        "report": result.report,
        "telemetry_result": context.result,
        "error": context.error if isinstance(context, RepairExecutionContext) else None,
    })


def run_repair_json(args: argparse.Namespace, config: AppConfig, sink: EventSink) -> int:
    operation = "repair-xattrs"
    context = RepairExecutionContext(lambda stage: sink.stage(operation, stage))
    stdout_capture = LineLogCapture(lambda message: sink.log(operation, message, level="info"))
    stderr_capture = LineLogCapture(lambda message: sink.log(operation, message, level="warning"))
    try:
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            result = run_repair_structured(
                args,
                context,
                config,
                emit_log=lambda message: sink.log(operation, message),
            )
    except SystemExit as exc:
        message = system_exit_message(exc) or "repair-xattrs failed"
        sink.error(operation, message, code="operation_failed")
        sink.result(operation, ok=False, payload={"error": message})
        return 1
    finally:
        stdout_capture.flush()
        stderr_capture.flush()
    payload = _repair_result_payload(result, context)
    sink.result(operation, ok=result.returncode == 0, payload=payload)
    return result.returncode


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Repair files whose SMB xattr metadata is broken by clearing the macOS arch flag.")
    add_config_argument(parser)
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
    parser.add_argument("--json", action="store_true", help="Emit app-event NDJSON instead of human-readable output")
    args = parser.parse_args(argv)

    if args.dry_run and args.yes:
        parser.error("--dry-run and --yes are mutually exclusive")
    if args.json and not args.dry_run and not args.yes:
        parser.error("--json repair requires --yes when not using --dry-run")
    if args.max_depth is not None and args.max_depth < 0:
        parser.error("--max-depth must be non-negative")

    ensure_install_id()
    config = load_optional_env_config(env_path=args.config)
    if args.json:
        sink = EventSink(lambda event: print(event.to_json_line(), end=""))
        operation = "repair-xattrs"
        sink.stage(operation, "platform_check")
        if sys.platform != "darwin":
            message = "repair-xattrs must be run on macOS because it uses xattr/chflags on the mounted SMB share."
            sink.error(operation, message, code="validation_failed")
            sink.result(operation, ok=False, payload={"error": message})
            return 1
        sink.stage(operation, "validate_params")
        return run_repair_json(args, config, sink)

    telemetry = TelemetryClient.from_config(config)
    with CommandContext(telemetry, "repair-xattrs", "repair_xattrs_started", "repair_xattrs_finished", config=config, args=args) as command_context:
        command_context.set_stage("platform_check")
        command_context.update_fields(host_platform=sys.platform)
        if sys.platform != "darwin":
            raise SystemExit("repair-xattrs must be run on macOS because it uses xattr/chflags on the mounted SMB share.")
        return run_repair(args, command_context, config)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
