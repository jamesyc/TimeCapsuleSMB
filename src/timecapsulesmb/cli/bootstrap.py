from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import confirm, load_optional_env_config
from timecapsulesmb.core.paths import AppPaths, is_source_distribution_root, resolve_app_paths
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.transport.local import find_command


REPO_ROOT = Path(__file__).resolve().parents[3]
VENVDIR = REPO_ROOT / ".venv"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"
HOMEBREW_INSTALL_COMMAND = '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
MACOS_SSHPASS_TAP = "hudochenkov/sshpass"
MACOS_SSHPASS_FORMULA = "sshpass"


class BootstrapError(Exception):
    pass


def run(cmd: list[str], *, cwd: Optional[Path] = None) -> None:
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def current_platform_label() -> str:
    if sys.platform == "darwin":
        return "macOS"
    if sys.platform.startswith("linux"):
        return "Linux"
    return sys.platform


def red(text: str) -> str:
    return f"{ANSI_RED}{text}{ANSI_RESET}"


def ensure_venv(python: str) -> Path:
    if not VENVDIR.exists():
        print(f"Creating virtualenv at {VENVDIR}", flush=True)
        run([python, "-m", "venv", str(VENVDIR)])
    else:
        print(f"Using existing virtualenv at {VENVDIR}", flush=True)
    return VENVDIR / "bin" / "python"


def venv_has_pip(venv_python: Path) -> bool:
    proc = subprocess.run(
        [str(venv_python), "-m", "pip", "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return proc.returncode == 0


def ensure_pip(venv_python: Path) -> None:
    if venv_has_pip(venv_python):
        return
    print("pip is missing from .venv; bootstrapping pip with ensurepip", flush=True)
    run([str(venv_python), "-m", "ensurepip", "--upgrade"])


def install_python_requirements(venv_python: Path) -> None:
    print("Installing Python dependencies into .venv", flush=True)
    ensure_pip(venv_python)
    run([str(venv_python), "-m", "pip", "install", "-U", "pip"])
    run([str(venv_python), "-m", "pip", "install", "-r", str(REQUIREMENTS)])
    run([str(venv_python), "-m", "pip", "install", "-e", str(REPO_ROOT)])


def resolve_bootstrap_app_paths() -> AppPaths | None:
    try:
        return resolve_app_paths()
    except Exception:
        return None


def is_packaged_install(app_paths: AppPaths | None) -> bool:
    return app_paths is not None and not is_source_distribution_root(app_paths.distribution_root)


def print_next_steps(command_prefix: str) -> None:
    print("Next steps:", flush=True)
    print(f"  1. {command_prefix} configure", flush=True)
    print(f"  2. {command_prefix} deploy", flush=True)
    print(f"  3. {command_prefix} doctor", flush=True)
    print(f"  4. NetBSD 4 only, after reboot if Samba did not auto-start: {command_prefix} activate", flush=True)


def run_packaged_bootstrap(command_context: CommandContext, app_paths: AppPaths) -> int:
    platform_label = current_platform_label()
    command_context.set_stage("packaged_install")
    command_context.update_fields(
        host_platform_label=platform_label,
        packaged_install=True,
        distribution_root=str(app_paths.distribution_root),
        config_path=str(app_paths.config_path),
        state_dir=str(app_paths.state_dir),
        smbclient_available_after=find_command("smbclient") is not None,
        sshpass_available_after=find_command("sshpass") is not None,
    )
    print(f"Detected host platform: {platform_label}", flush=True)
    print("Detected packaged TimeCapsuleSMB install.", flush=True)
    print("No repo-local virtualenv is needed; Python dependencies were installed by the package manager.", flush=True)
    print(f"Distribution root: {app_paths.distribution_root}", flush=True)
    print(f"Config path: {app_paths.config_path}", flush=True)
    print(f"State dir: {app_paths.state_dir}", flush=True)
    for tool in ("smbclient", "ssh", "sshpass"):
        status = "found" if find_command(tool) else "missing"
        print(f"{status} local tool {tool}", flush=True)
    print("\nHost setup complete.", flush=True)
    print_next_steps("tcapsule")
    command_context.set_stage("complete")
    command_context.succeed()
    return 0


def maybe_install_smbclient() -> None:
    if find_command("smbclient"):
        return

    print("smbclient is required for cross-platform SMB verification in 'tcapsule doctor'.", flush=True)

    if current_platform_label() == "macOS":
        brew = find_command("brew")
        if not brew:
            print("Homebrew not found, so bootstrap cannot install smbclient automatically.", flush=True)
            print("Install Homebrew from https://brew.sh and then run: brew install samba", flush=True)
            return
        print("On macOS, smbclient is provided by the Homebrew 'samba' formula.", flush=True)
        if not confirm("Install smbclient now via 'brew install samba'?", default=True, eof_default=False):
            print("Skipping smbclient install. Later, run 'brew install samba' before using 'tcapsule doctor'.", flush=True)
            return
        print("Installing smbclient via 'brew install samba'", flush=True)
        try:
            run([brew, "install", "samba"])
        except subprocess.CalledProcessError as exc:
            print("Warning: smbclient install failed. Host bootstrap will continue without it.", flush=True)
            print("Later, run 'brew install samba' and rerun './tcapsule bootstrap' or use '.venv/bin/tcapsule doctor' once smbclient is available.", flush=True)
            print(f"smbclient install command failed with exit code {exc.returncode}: {exc.cmd}", flush=True)
        return

    print("Automatic smbclient installation is not implemented for this platform.", flush=True)
    if find_command("apt-get"):
        print("Install it with: sudo apt-get update && sudo apt-get install -y smbclient", flush=True)
    elif find_command("dnf"):
        print("Install it with: sudo dnf install -y samba-client", flush=True)
    elif find_command("yum"):
        print("Install it with: sudo yum install -y samba-client", flush=True)
    elif find_command("zypper"):
        print("Install it with: sudo zypper install smbclient", flush=True)
    elif find_command("pacman"):
        print("Install it with: sudo pacman -S samba", flush=True)
    else:
        print("Install smbclient with your distro package manager before running 'tcapsule doctor'.", flush=True)
    print("After installing smbclient, rerun './tcapsule bootstrap' or use '.venv/bin/tcapsule doctor'.", flush=True)


def maybe_install_sshpass() -> None:
    if find_command("sshpass"):
        print("Found local sshpass.", flush=True)
        return

    print("sshpass is required for NetBSD4 devices that do not provide remote scp.", flush=True)
    platform_label = current_platform_label()
    if platform_label == "macOS":
        brew = find_command("brew")
        if not brew:
            print(red("Homebrew is missing, please install Homebrew:"), flush=True)
            print(HOMEBREW_INSTALL_COMMAND, flush=True)
            raise BootstrapError("Homebrew is required to install sshpass on macOS.")
        print(f"Installing sshpass via 'brew tap {MACOS_SSHPASS_TAP}' and 'brew install {MACOS_SSHPASS_FORMULA}'", flush=True)
        run([brew, "tap", MACOS_SSHPASS_TAP])
        run([brew, "install", MACOS_SSHPASS_FORMULA])
        return

    if platform_label == "Linux":
        if apt_get := find_command("apt-get"):
            run(["sudo", apt_get, "update"])
            run(["sudo", apt_get, "install", "-y", "sshpass"])
            return
        if dnf := find_command("dnf"):
            run(["sudo", dnf, "install", "-y", "sshpass"])
            return
        if yum := find_command("yum"):
            run(["sudo", yum, "install", "-y", "sshpass"])
            return
        if zypper := find_command("zypper"):
            run(["sudo", zypper, "install", "-y", "sshpass"])
            return
        if pacman := find_command("pacman"):
            run(["sudo", pacman, "-S", "--needed", "sshpass"])
            return
        raise BootstrapError("No supported Linux package manager found to install sshpass.")

    raise BootstrapError(f"Automatic sshpass installation is not implemented for {platform_label}.")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare the local host for TimeCapsuleSMB user workflows.")
    parser.add_argument("--python", default=sys.executable or "python3", help="Python interpreter to use for the repo .venv")
    args = parser.parse_args(argv)

    ensure_install_id()
    config = load_optional_env_config()
    telemetry = TelemetryClient.from_config(config)
    app_paths = resolve_bootstrap_app_paths()
    with CommandContext(
        telemetry,
        "bootstrap",
        "bootstrap_started",
        "bootstrap_finished",
        config=config,
        args=args,
        python_executable=args.python,
    ) as command_context:
        command_context.update_fields(
            requirements_path=str(REQUIREMENTS),
            venv_path=str(VENVDIR),
            requirements_present=REQUIREMENTS.exists(),
            venv_exists_before=VENVDIR.exists(),
            python_executable=args.python,
        )
        if app_paths is not None:
            command_context.update_fields(
                distribution_root=str(app_paths.distribution_root),
                config_path=str(app_paths.config_path),
                state_dir=str(app_paths.state_dir),
                packaged_install=is_packaged_install(app_paths),
            )
        if is_packaged_install(app_paths):
            return run_packaged_bootstrap(command_context, app_paths)

        command_context.set_stage("validate_requirements")
        if not REQUIREMENTS.exists():
            message = f"Missing {REQUIREMENTS}"
            print(message, file=sys.stderr)
            command_context.fail_with_error(message)
            return 1

        try:
            command_context.set_stage("detect_platform")
            platform_label = current_platform_label()
            command_context.update_fields(host_platform_label=platform_label)
            print(f"Detected host platform: {platform_label}", flush=True)
            command_context.set_stage("ensure_venv")
            venv_python = ensure_venv(args.python)
            command_context.update_fields(venv_python=str(venv_python))
            command_context.set_stage("install_python_requirements")
            install_python_requirements(venv_python)
            command_context.set_stage("install_smbclient")
            maybe_install_smbclient()
            command_context.set_stage("install_sshpass")
            maybe_install_sshpass()
        except subprocess.CalledProcessError as e:
            message = f"Command failed with exit code {e.returncode}: {e.cmd}"
            print(message, file=sys.stderr)
            command_context.fail_with_error(message)
            return e.returncode or 1
        except BootstrapError as e:
            print(str(e), file=sys.stderr)
            command_context.fail_with_error(str(e))
            return 1

        command_context.set_stage("complete")
        command_context.update_fields(
            smbclient_available_after=find_command("smbclient") is not None,
            sshpass_available_after=find_command("sshpass") is not None,
            venv_exists_after=VENVDIR.exists(),
        )
        print("\nHost setup complete.", flush=True)
        print_next_steps(str(VENVDIR / "bin" / "tcapsule"))
        command_context.succeed()
        return 0
    return 1
