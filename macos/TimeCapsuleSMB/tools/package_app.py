#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[1]
APP_NAME = "TimeCapsuleSMB"
PRODUCT_NAME = "TimeCapsuleSMB"


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        input=input_text,
        text=True,
        check=True,
        stdout=subprocess.PIPE if input_text is not None else None,
        stderr=subprocess.PIPE if input_text is not None else None,
    )


def build_swift(configuration: str) -> Path:
    run(["swift", "build", "-c", configuration, "--product", PRODUCT_NAME], cwd=PACKAGE_ROOT)
    executable = PACKAGE_ROOT / ".build" / configuration / PRODUCT_NAME
    if not executable.is_file():
        raise RuntimeError(f"Swift build did not produce {executable}")
    return executable


def copy_resources(configuration: str, resources_dir: Path) -> None:
    build_dir = PACKAGE_ROOT / ".build" / configuration
    for resource_bundle in build_dir.glob("*.bundle"):
        destination = resources_dir / resource_bundle.name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(resource_bundle, destination)


def write_info_plist(contents_dir: Path) -> None:
    info = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": PRODUCT_NAME,
        "CFBundleIdentifier": "com.timecapsulesmb.TimeCapsuleSMB",
        "CFBundleName": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
    }
    with (contents_dir / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle)
    (contents_dir / "PkgInfo").write_text("APPL????", encoding="utf-8")


def write_helper_wrapper(helper_path: Path) -> None:
    helper_path.write_text(
        """#!/bin/sh
set -eu

CONTENTS_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
PYTHON="$RESOURCES_DIR/Python/bin/python"

if [ -z "${TCAPSULE_STATE_DIR:-}" ]; then
    export TCAPSULE_STATE_DIR="$HOME/Library/Application Support/TimeCapsuleSMB"
fi
if [ -z "${TCAPSULE_CONFIG:-}" ]; then
    export TCAPSULE_CONFIG="$TCAPSULE_STATE_DIR/.env"
fi
if [ -z "${TCAPSULE_DISTRIBUTION_ROOT:-}" ]; then
    export TCAPSULE_DISTRIBUTION_ROOT="$RESOURCES_DIR/Distribution"
fi

mkdir -p "$TCAPSULE_STATE_DIR"
export PATH="$RESOURCES_DIR/Tools/bin:${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"
export PYTHONNOUSERSITE=1

exec "$PYTHON" -m timecapsulesmb.cli.main "$@"
""",
        encoding="utf-8",
    )
    helper_path.chmod(0o755)


def create_python_runtime(python: str, resources_dir: Path) -> None:
    runtime = resources_dir / "Python"
    if runtime.exists():
        shutil.rmtree(runtime)
    run([python, "-m", "venv", str(runtime)])
    runtime_python = runtime / "bin" / "python"
    run([str(runtime_python), "-m", "pip", "install", "-U", "pip"])
    generated_build_lib = REPO_ROOT / "build" / "lib"
    build_lib_existed = generated_build_lib.exists()
    try:
        run([str(runtime_python), "-m", "pip", "install", str(REPO_ROOT)])
    finally:
        if not build_lib_existed and generated_build_lib.exists():
            shutil.rmtree(generated_build_lib)


def copy_distribution(resources_dir: Path) -> None:
    distribution = resources_dir / "Distribution"
    if distribution.exists():
        shutil.rmtree(distribution)
    distribution.mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "bin", distribution / "bin")


def copy_tool(name: str, tools_bin: Path) -> bool:
    source = shutil.which(name)
    if not source:
        return False
    destination = tools_bin / name
    shutil.copy2(source, destination)
    destination.chmod(0o755)
    return True


def copy_tools(resources_dir: Path, require_tools: bool) -> None:
    tools_bin = resources_dir / "Tools" / "bin"
    tools_bin.mkdir(parents=True, exist_ok=True)
    missing = [tool for tool in ("sshpass", "smbclient") if not copy_tool(tool, tools_bin)]
    if missing and require_tools:
        joined = ", ".join(missing)
        raise RuntimeError(f"Missing required host tool(s) for bundling: {joined}")
    if missing:
        print(f"warning: missing optional bundled tool(s): {', '.join(missing)}", file=sys.stderr)


def smoke_request(helper: Path, operation: str, state_dir: Path) -> None:
    env = os.environ.copy()
    env["TCAPSULE_STATE_DIR"] = str(state_dir)
    env["TCAPSULE_CONFIG"] = str(state_dir / ".env")
    request = json.dumps({"operation": operation, "params": {}})
    completed = run([str(helper), "api"], input_text=request, env=env)
    if '"type":"result"' not in completed.stdout and '"type": "result"' not in completed.stdout:
        raise RuntimeError(f"{operation} smoke test did not emit a result event:\n{completed.stdout}\n{completed.stderr}")
    if '"ok":false' in completed.stdout or '"ok": false' in completed.stdout:
        raise RuntimeError(f"{operation} smoke test failed:\n{completed.stdout}\n{completed.stderr}")


def smoke_test(app: Path) -> None:
    helper = app / "Contents" / "Helpers" / "tcapsule"
    with tempfile.TemporaryDirectory(prefix="timecapsulesmb-package-smoke-") as tmp:
        state_dir = Path(tmp)
        smoke_request(helper, "capabilities", state_dir)
        smoke_request(helper, "validate-install", state_dir)


def package_app(args: argparse.Namespace) -> Path:
    executable = build_swift(args.configuration)
    output_dir = args.output.resolve()
    app = output_dir / f"{APP_NAME}.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    helpers = contents / "Helpers"
    resources = contents / "Resources"

    if app.exists():
        shutil.rmtree(app)
    macos.mkdir(parents=True)
    helpers.mkdir()
    resources.mkdir()

    write_info_plist(contents)
    shutil.copy2(executable, macos / PRODUCT_NAME)
    copy_resources(args.configuration, resources)
    write_helper_wrapper(helpers / "tcapsule")
    create_python_runtime(args.python, resources)
    copy_distribution(resources)
    copy_tools(resources, args.require_tools)

    if not args.skip_smoke:
        smoke_test(app)
    return app


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a self-contained TimeCapsuleSMB.app bundle.")
    parser.add_argument("--output", type=Path, default=PACKAGE_ROOT / "dist", help="Directory that will receive TimeCapsuleSMB.app.")
    parser.add_argument("--configuration", choices=("debug", "release"), default="release", help="Swift build configuration.")
    parser.add_argument("--python", default=sys.executable, help="Python interpreter used to create the bundled runtime.")
    parser.add_argument("--require-tools", action="store_true", help="Fail if sshpass or smbclient cannot be copied into the app bundle.")
    parser.add_argument("--skip-smoke", action="store_true", help="Skip bundled helper capabilities and validate-install smoke tests.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        app = package_app(parse_args(argv or sys.argv[1:]))
    except subprocess.CalledProcessError as exc:
        print(f"command failed with exit code {exc.returncode}: {exc.cmd}", file=sys.stderr)
        if exc.stdout:
            print(exc.stdout, file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return exc.returncode or 1
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(app)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
