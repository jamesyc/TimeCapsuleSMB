from __future__ import annotations

import argparse
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Callable, Optional

from timecapsulesmb.app.contracts import repair_xattrs_payload
from timecapsulesmb.app.events import EventSink
from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import (
    add_config_argument,
    add_no_input_argument,
    confirm as confirm_prompt,
    load_optional_env_config,
    no_input_enabled,
)
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
    build_repair_report,
    classify_path,
    default_share_path_from_config,
    file_flags,
    find_findings,
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
    validate_repair_root_under_volumes,
    xattr_status,
    xattrs_readable,
)
from timecapsulesmb.services.app import jsonable
from timecapsulesmb.services.maintenance import LineLogCapture, RepairExecutionContext
from timecapsulesmb.services.repair_xattrs import (
    RepairRunResult,
    render_candidate_lines,
    render_diagnostic_lines,
    render_summary_lines,
    run_repair_structured as _run_repair_structured,
)
from timecapsulesmb.telemetry import TelemetryClient


def confirm(prompt_text: str) -> bool:
    return confirm_prompt(prompt_text, default=False, eof_default=False, interrupt_default=False)


def run_repair_structured(
    args: argparse.Namespace,
    command_context: CommandContext,
    config: AppConfig,
    *,
    emit_log: Callable[[str], None] | None = None,
) -> RepairRunResult:
    return _run_repair_structured(
        args,
        command_context,
        config,
        emit_log=emit_log,
        confirm=confirm,
        noninteractive=no_input_enabled(args),
    )


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
    add_no_input_argument(parser)
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
