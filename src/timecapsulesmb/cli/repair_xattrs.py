from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from timecapsulesmb.app.contracts import repair_xattrs_payload
from timecapsulesmb.app.events import EventSink
from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import (
    add_config_argument,
    add_no_input_argument,
    confirm as confirm_prompt,
    no_input_enabled,
)
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.services.app import jsonable
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.repair_xattrs import (
    RepairXattrsRequest,
    RepairXattrsServiceError,
    RepairRunResult,
    run_repair as run_repair_service,
)
from timecapsulesmb.services.runtime import load_optional_env_config
from timecapsulesmb.telemetry import TelemetryClient


def confirm(prompt_text: str) -> bool:
    return confirm_prompt(prompt_text, default=False, eof_default=False, interrupt_default=False)


def repair_request_from_args(args: argparse.Namespace) -> RepairXattrsRequest:
    return RepairXattrsRequest(
        path=args.path,
        dry_run=args.dry_run,
        approve_repairs=args.yes,
        recursive=args.recursive,
        max_depth=args.max_depth,
        include_hidden=args.include_hidden,
        include_time_machine=args.include_time_machine,
        fix_permissions=args.fix_permissions,
        verbose=args.verbose,
    )


def run_repair(args: argparse.Namespace, command_context: CommandContext, config: AppConfig) -> int:
    try:
        result = run_repair_service(
            repair_request_from_args(args),
            config,
            callbacks=OperationCallbacks(
                set_stage=command_context.set_stage,
                update_fields=command_context.update_fields,
                log=print,
            ),
            confirm=None if no_input_enabled(args) else confirm,
        )
    except RepairXattrsServiceError as exc:
        raise SystemExit(str(exc)) from exc
    apply_result_to_command_context(result, command_context)
    return result.returncode


def apply_result_to_command_context(result: RepairRunResult, command_context: CommandContext) -> None:
    if result.telemetry_result == "success":
        command_context.succeed()
    elif result.error:
        command_context.fail_with_error(result.error)
    else:
        command_context.fail()


def _repair_result_payload(result: RepairRunResult) -> dict[str, object]:
    fields = result.to_payload_fields()
    fields["stats"] = jsonable(result.summary)
    return repair_xattrs_payload(fields)


def run_repair_json(args: argparse.Namespace, config: AppConfig, sink: EventSink) -> int:
    operation = "repair-xattrs"
    try:
        result = run_repair_service(
            repair_request_from_args(args),
            config,
            callbacks=OperationCallbacks(
                set_stage=lambda stage: sink.stage(operation, stage),
                log=lambda message: sink.log(operation, message),
            ),
        )
    except RepairXattrsServiceError as exc:
        message = str(exc) or "repair-xattrs failed"
        sink.error(operation, message, code="operation_failed")
        sink.result(operation, ok=False, payload={"error": message})
        return 1
    payload = _repair_result_payload(result)
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
