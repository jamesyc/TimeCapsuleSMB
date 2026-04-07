from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parents[3]
VENVDIR = REPO_ROOT / ".venv"
REQUIREMENTS = REPO_ROOT / "requirements.txt"


def run(cmd: list[str], *, cwd: Optional[Path] = None) -> None:
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def confirm(prompt_text: str, *, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    reply = input(f"{prompt_text} {suffix}: ").strip().lower()
    if not reply:
        return default
    return reply in {"y", "yes"}


def ensure_venv(python: str) -> Path:
    if not VENVDIR.exists():
        print(f"Creating virtualenv at {VENVDIR}", flush=True)
        run([python, "-m", "venv", str(VENVDIR)])
    else:
        print(f"Using existing virtualenv at {VENVDIR}", flush=True)
    return VENVDIR / "bin" / "python"


def install_python_requirements(venv_python: Path) -> None:
    print("Installing Python dependencies into .venv", flush=True)
    run([str(venv_python), "-m", "pip", "install", "-U", "pip"])
    run([str(venv_python), "-m", "pip", "install", "-r", str(REQUIREMENTS)])
    run([str(venv_python), "-m", "pip", "install", "-e", str(REPO_ROOT)])


def maybe_install_airpyrt(skip_airpyrt: bool) -> None:
    if skip_airpyrt:
        print("Skipping AirPyrt setup.", flush=True)
        return

    make = shutil.which("make")
    if not make:
        print("Skipping AirPyrt setup because 'make' is not available.", flush=True)
        print("Later, install it manually or run 'make airpyrt'.", flush=True)
        return

    print("AirPyrt support is optional, but it is needed by 'prep-device' when SSH must be enabled on the Time Capsule.", flush=True)
    print("Installing it may trigger Homebrew package installs, pyenv installation, and a local Python 2.7.18 build.", flush=True)
    if not confirm("Continue with optional AirPyrt setup?", default=True):
        print("Skipping AirPyrt setup. You can install it later with 'make airpyrt' or rerun './tcapsule bootstrap'.", flush=True)
        return

    print("Provisioning AirPyrt via 'make airpyrt'", flush=True)
    print(
        "This optional step may take several minutes if Homebrew installs pyenv or pyenv builds Python 2.7.18.",
        flush=True,
    )
    print("If you do not need it right now, rerun bootstrap with '--skip-airpyrt'.", flush=True)
    try:
        run([make, "airpyrt"], cwd=REPO_ROOT)
    except subprocess.CalledProcessError as exc:
        print("Warning: AirPyrt setup failed. Host bootstrap will continue without it.", flush=True)
        print("Later, rerun './tcapsule bootstrap' or 'make airpyrt' after fixing the local prerequisites.", flush=True)
        print(f"AirPyrt setup command failed with exit code {exc.returncode}: {exc.cmd}", flush=True)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare the local host for TimeCapsuleSMB user workflows.")
    parser.add_argument("--python", default=sys.executable or "python3", help="Python interpreter to use for the repo .venv")
    parser.add_argument("--skip-airpyrt", action="store_true", help="Do not provision AirPyrt / .airpyrt-venv")
    args = parser.parse_args(argv)

    if not REQUIREMENTS.exists():
        print(f"Missing {REQUIREMENTS}", file=sys.stderr)
        return 1

    try:
        venv_python = ensure_venv(args.python)
        install_python_requirements(venv_python)
        maybe_install_airpyrt(args.skip_airpyrt)
    except subprocess.CalledProcessError as e:
        print(f"Command failed with exit code {e.returncode}: {e.cmd}", file=sys.stderr)
        return e.returncode or 1

    print("\nHost setup complete.", flush=True)
    print("Next steps:", flush=True)
    print(f"  1. {VENVDIR / 'bin' / 'tcapsule'} prep-device", flush=True)
    print(f"  2. {VENVDIR / 'bin' / 'tcapsule'} configure", flush=True)
    print(f"  3. {VENVDIR / 'bin' / 'tcapsule'} deploy", flush=True)
    print(f"  4. {VENVDIR / 'bin' / 'tcapsule'} doctor", flush=True)
    return 0
