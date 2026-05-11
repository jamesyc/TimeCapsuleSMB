from __future__ import annotations

import argparse
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import add_config_argument, load_optional_env_config, print_json
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.install_validation import install_checks_to_jsonable, install_ok, validate_install
from timecapsulesmb.telemetry import TelemetryClient


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the local TimeCapsuleSMB repo-only install.")
    add_config_argument(parser)
    parser.add_argument("--json", action="store_true", help="Output validation results as JSON")
    args = parser.parse_args(argv)

    ensure_install_id()
    config = load_optional_env_config(env_path=args.config)
    telemetry = TelemetryClient.from_config(config)
    with CommandContext(
        telemetry,
        "validate-install",
        "validate_install_started",
        "validate_install_finished",
        config=config,
        args=args,
        json_output=args.json,
    ) as command_context:
        command_context.set_stage("resolve_paths")
        app_paths = resolve_app_paths(config_path=args.config)
        command_context.update_fields(
            config_exists=app_paths.config_path.exists(),
            state_dir_exists=app_paths.state_dir.exists(),
        )
        command_context.set_stage("validate_install")
        checks = validate_install(app_paths)
        ok = install_ok(checks)
        failed_checks = [check for check in checks if not check.ok]
        command_context.update_fields(
            install_ok=ok,
            check_count=len(checks),
            failed_check_count=len(failed_checks),
            failed_check_ids=[check.id for check in failed_checks],
        )
        if args.json:
            print_json({
                "ok": ok,
                "checks": install_checks_to_jsonable(checks),
            })
            if ok:
                command_context.succeed()
                return 0
            command_context.fail_with_error(_validation_failure_message(failed_checks))
            return 1

        command_context.set_stage("render_validation")
        for check in checks:
            status = "PASS" if check.ok else "FAIL"
            print(f"{status} {check.message}")
        print("Summary: install validation passed." if ok else "Summary: install validation failed.")
        if ok:
            command_context.succeed()
            return 0
        command_context.fail_with_error(_validation_failure_message(failed_checks))
        return 1
    return 1


def _validation_failure_message(failed_checks: list[object]) -> str:
    messages = [getattr(check, "message", "") for check in failed_checks]
    detail = "; ".join(message for message in messages if message)
    return f"install validation failed: {detail}" if detail else "install validation failed"
