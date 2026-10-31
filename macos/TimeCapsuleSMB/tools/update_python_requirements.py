#!/usr/bin/env python3
"""Write the requirements files packaging installs into the app, pinned with hashes.

bundled-requirements.txt holds pyproject.toml's dependencies, their own
dependencies and certifi; build-requirements.txt holds what builds the
project, pyproject.toml's [build-system].requires. Both resolve for the
bundled Python's version on macOS, whichever Python runs uv. Run this after
changing a dependency, the build system or PYTHON_RUNTIME_VERSION; it keeps
the versions the files already pin where they still fit, and --upgrade moves
everything to the newest releases. Like packaging, it ignores uv's
configuration files and takes only releases at least a day old.
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):
    raise SystemExit("update_python_requirements.py needs Python 3.11 or newer (for tomllib).")

import argparse
import os
import subprocess
import tomllib
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))

from package_app import (  # noqa: E402
    BUNDLED_EXTRA_PYTHON_REQUIREMENTS,
    BUNDLED_PYTHON_REQUIREMENTS,
    PYTHON_BUILD_REQUIREMENTS,
    PYTHON_RUNTIME_VERSION,
    REPO_ROOT,
    UV_PACKAGING_OPTIONS,
    uv_executable,
)

PYPROJECT = REPO_ROOT / "pyproject.toml"
# The command the files' headers name, in place of the uv command line with
# this checkout's paths.
COMPILE_COMMAND = "python3 macos/TimeCapsuleSMB/tools/update_python_requirements.py"


def compile_requirements(uv: str, sources: list[str], stdin: str, output: Path, *, upgrade: bool) -> None:
    python_version = ".".join(PYTHON_RUNTIME_VERSION.split(".")[:2])
    subprocess.run(
        [
            uv, "pip", "compile", *UV_PACKAGING_OPTIONS, "--quiet", "--generate-hashes",
            "--python-version", python_version, "--python-platform", "macos",
            *(["--upgrade"] if upgrade else []),
            "--output-file", str(output), *sources, "-",
        ],
        input=stdin,
        text=True,
        check=True,
        cwd=PYPROJECT.parent,
        env={**os.environ, "UV_CUSTOM_COMPILE_COMMAND": COMPILE_COMMAND},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the app's hash-pinned Python requirements files.")
    parser.add_argument("--upgrade", action="store_true", help="Pin the newest versions instead of keeping the current pins.")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        uv = uv_executable()
        build_requires = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["build-system"]["requires"]
        compile_requirements(
            uv, [PYPROJECT.name], "".join(f"{name}\n" for name in BUNDLED_EXTRA_PYTHON_REQUIREMENTS),
            BUNDLED_PYTHON_REQUIREMENTS, upgrade=args.upgrade,
        )
        compile_requirements(
            uv, [], "".join(f"{requirement}\n" for requirement in build_requires),
            PYTHON_BUILD_REQUIREMENTS, upgrade=args.upgrade,
        )
    except RuntimeError as exc:
        print(f"update_python_requirements: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        # uv has printed why.
        return exc.returncode
    print(f"Wrote {BUNDLED_PYTHON_REQUIREMENTS.name} and {PYTHON_BUILD_REQUIREMENTS.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
