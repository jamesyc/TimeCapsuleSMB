from __future__ import annotations

import argparse
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import add_config_argument, load_optional_env_config, print_json
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.install_validation import paths_to_jsonable
from timecapsulesmb.telemetry import TelemetryClient


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Show TimeCapsuleSMB local path resolution.")
    add_config_argument(parser)
    parser.add_argument("--json", action="store_true", help="Output paths as JSON")
    args = parser.parse_args(argv)

    ensure_install_id()
    config = load_optional_env_config(env_path=args.config)
    telemetry = TelemetryClient.from_config(config)
    with CommandContext(
        telemetry,
        "paths",
        "paths_started",
        "paths_finished",
        config=config,
        args=args,
        json_output=args.json,
    ) as command_context:
        command_context.set_stage("resolve_paths")
        app_paths = resolve_app_paths(config_path=args.config)
        command_context.update_fields(
            config_exists=app_paths.config_path.exists(),
            state_dir_exists=app_paths.state_dir.exists(),
            distribution_root_exists=app_paths.distribution_root.exists(),
        )
        command_context.set_stage("summarize_artifacts")
        data = paths_to_jsonable(app_paths)
        artifact_count = len(data["artifacts"])
        missing_artifact_count = sum(1 for artifact in data["artifacts"] if not artifact["ok"])
        command_context.update_fields(
            artifact_count=artifact_count,
            missing_artifact_count=missing_artifact_count,
        )
        if args.json:
            print_json(data)
            command_context.succeed()
            return 0

        command_context.set_stage("render_paths")
        print(f"distribution root: {data['distribution_root']}")
        print(f"config path: {data['config_path']}")
        print(f"state dir: {data['state_dir']}")
        print(f"package root: {data['package_root']}")
        print(f"artifact manifest: {data['artifact_manifest']}")
        print("artifacts:")
        for artifact in data["artifacts"]:
            status = "ok" if artifact["ok"] else "missing/invalid"
            print(f"  {status} {artifact['name']}: {artifact['absolute_path']}")
        command_context.succeed()
        return 0
    return 1
