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


def ensure_venv(python: str) -> Path:
    if not VENVDIR.exists():
        print(f"Creating virtualenv at {VENVDIR}")
        run([python, "-m", "venv", str(VENVDIR)])
    else:
        print(f"Using existing virtualenv at {VENVDIR}")
    return VENVDIR / "bin" / "python"


def install_python_requirements(venv_python: Path) -> None:
    print("Installing Python dependencies into .venv")
    run([str(venv_python), "-m", "pip", "install", "-U", "pip"])
    run([str(venv_python), "-m", "pip", "install", "-r", str(REQUIREMENTS)])
    run([str(venv_python), "-m", "pip", "install", "-e", str(REPO_ROOT)])


def maybe_install_airpyrt(skip_airpyrt: bool) -> None:
    if skip_airpyrt:
        print("Skipping AirPyrt setup.")
        return

    make = shutil.which("make")
    if not make:
        print("Skipping AirPyrt setup because 'make' is not available.")
        print("Later, install it manually or run 'make airpyrt'.")
        return

    print("Provisioning AirPyrt via 'make airpyrt'")
    run([make, "airpyrt"], cwd=REPO_ROOT)


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

    print("\nHost setup complete.")
    print("Next steps:")
    print(f"  1. {VENVDIR / 'bin' / 'tcapsule'} prep-device")
    print(f"  2. {VENVDIR / 'bin' / 'tcapsule'} configure")
    print(f"  3. {VENVDIR / 'bin' / 'tcapsule'} deploy")
    print(f"  4. {VENVDIR / 'bin' / 'tcapsule'} doctor")
    return 0
