from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.util import color_red
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.services.runtime import load_optional_env_config
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.transport.local import find_command


REPO_ROOT = Path(__file__).resolve().parents[3]
VENVDIR = REPO_ROOT / ".venv"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
HOMEBREW_INSTALL_COMMAND = '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
MACOS_SSHPASS_FORMULA = "sshpass"
REQUIRED_HOST_TOOLS = ("sshpass", "smbclient")
MIN_BOOTSTRAP_PYTHON = (3, 9)
MIN_MACOS_AUTO_HOST_TOOL_INSTALL = (14, 0)
PYTHON_VERSION_PROBE = "import sys; print('%d.%d.%d' % sys.version_info[:3])"
MACOS_HOST_TOOL_PACKAGES = {
    "sshpass": MACOS_SSHPASS_FORMULA,
    "smbclient": "samba",
}
LINUX_HOST_TOOL_PACKAGES = {
    "apt-get": {"sshpass": "sshpass", "smbclient": "smbclient"},
    "dnf": {"sshpass": "sshpass", "smbclient": "samba-client"},
    "yum": {"sshpass": "sshpass", "smbclient": "samba-client"},
    "zypper": {"sshpass": "sshpass", "smbclient": "samba-client"},
    "pacman": {"sshpass": "sshpass", "smbclient": "smbclient"},
}
COMMAND_OUTPUT_ERROR_LIMIT = 8192


class BootstrapError(Exception):
    pass


class BootstrapPreflightError(BootstrapError):
    def __init__(self, message: str, fields: dict[str, object]) -> None:
        self.fields = fields
        super().__init__(message)


class BootstrapCommandError(Exception):
    def __init__(self, cmd: list[str], returncode: int, stdout: str, stderr: str) -> None:
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(f"Command failed with exit code {returncode}: {cmd}")


def run(cmd: list[str], *, cwd: Optional[Path] = None) -> None:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if proc.stdout:
        sys.stdout.write(proc.stdout)
        sys.stdout.flush()
    if proc.stderr:
        sys.stderr.write(proc.stderr)
        sys.stderr.flush()
    if proc.returncode != 0:
        raise BootstrapCommandError(cmd, proc.returncode, proc.stdout or "", proc.stderr or "")


def _truncate_command_output(text: str, limit: int = COMMAND_OUTPUT_ERROR_LIMIT) -> str:
    if len(text) <= limit:
        return text.rstrip()
    omitted = len(text) - limit
    return f"{text[:limit].rstrip()}\n...<truncated {omitted} chars>"


def _format_command_output(label: str, text: str) -> str | None:
    formatted = _truncate_command_output(text)
    if not formatted:
        return None
    return f"{label}:\n{formatted}"


def _format_command_error(exc: BootstrapCommandError) -> str:
    message = f"Command failed with exit code {exc.returncode}: {exc.cmd}"
    output = _format_command_output("stderr", exc.stderr)
    if output is None:
        output = _format_command_output("stdout", exc.stdout)
    if output is not None:
        message = f"{message}\n\n{output}"
    return message


def current_platform_label() -> str:
    if sys.platform == "darwin":
        return "macOS"
    if sys.platform.startswith("linux"):
        return "Linux"
    return sys.platform


def _format_version_tuple(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def _parse_version_prefix(value: str) -> tuple[int, ...] | None:
    parts: list[int] = []
    for raw_part in value.strip().split("."):
        digits = ""
        for char in raw_part:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else None


def _version_at_least(version: tuple[int, ...], minimum: tuple[int, ...]) -> bool:
    width = max(len(version), len(minimum))
    padded_version = version + (0,) * (width - len(version))
    padded_minimum = minimum + (0,) * (width - len(minimum))
    return padded_version >= padded_minimum


def detect_selected_python_version(python: str) -> str:
    try:
        proc = subprocess.run(
            [python, "-c", PYTHON_VERSION_PROBE],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise BootstrapError(f"Selected Python could not be run: {python}: {exc}") from exc

    if proc.returncode != 0:
        message = f"Selected Python could not report its version: {python} (exit code {proc.returncode})"
        output = _format_command_output("stderr", proc.stderr or "")
        if output is None:
            output = _format_command_output("stdout", proc.stdout or "")
        if output is not None:
            message = f"{message}\n\n{output}"
        raise BootstrapError(message)

    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise BootstrapError(f"Selected Python did not print a version: {python}")
    return lines[-1]


def validate_selected_python(python: str) -> str:
    version = detect_selected_python_version(python)
    parsed = _parse_version_prefix(version)
    minimum = _format_version_tuple(MIN_BOOTSTRAP_PYTHON)
    if parsed is None:
        raise BootstrapError(f"Selected Python printed an unreadable version: {python}: {version}")
    if not _version_at_least(parsed, MIN_BOOTSTRAP_PYTHON):
        raise BootstrapError(
            f"TimeCapsuleSMB bootstrap requires Python {minimum} or newer. "
            f"Selected Python {python} is {version}. "
            f"Rerun './tcapsule bootstrap --python /path/to/python{minimum}' with a newer Python."
        )
    print(f"Selected Python: {python} ({version})", flush=True)
    return version


def detect_macos_product_version() -> str | None:
    try:
        proc = subprocess.run(
            ["sw_vers", "-productVersion"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
    except OSError:
        proc = None
    if proc is not None and proc.returncode == 0:
        version = proc.stdout.strip()
        if version:
            return version
    return platform.mac_ver()[0] or None


def _required_host_tool_paths() -> dict[str, str | None]:
    return {tool: find_command(tool) for tool in REQUIRED_HOST_TOOLS}


def _missing_tools_from_paths(paths: dict[str, str | None]) -> list[str]:
    return [tool for tool in REQUIRED_HOST_TOOLS if paths.get(tool) is None]


def _print_host_tool_status(paths: dict[str, str | None]) -> None:
    for tool in REQUIRED_HOST_TOOLS:
        path = paths.get(tool)
        if path:
            print(f"Found {tool}: {path}", flush=True)
        else:
            print(f"Missing {tool}", flush=True)


def check_macos_host_tool_install_support(platform_label: str) -> dict[str, object]:
    if platform_label != "macOS":
        return {}

    macos_version = detect_macos_product_version()
    parsed = _parse_version_prefix(macos_version) if macos_version else None
    auto_install_supported = parsed is not None and _version_at_least(parsed, MIN_MACOS_AUTO_HOST_TOOL_INSTALL)
    paths = _required_host_tool_paths()
    missing_tools = _missing_tools_from_paths(paths)
    fields: dict[str, object] = {
        "host_os_version": macos_version or "unknown",
        "macos_auto_host_tool_install_supported": auto_install_supported,
        "missing_host_tools": _format_tools(missing_tools),
        "smbclient_path": paths.get("smbclient"),
        "sshpass_path": paths.get("sshpass"),
    }
    if auto_install_supported:
        return fields

    print(f"Detected macOS version: {macos_version or 'unknown'}", flush=True)
    _print_host_tool_status(paths)
    if not missing_tools:
        return fields

    minimum = _format_version_tuple(MIN_MACOS_AUTO_HOST_TOOL_INSTALL)
    message = (
        f"Automatic TimeCapsuleSMB host-tool install requires macOS {minimum} or newer. "
        f"Manually install the missing host tools ({_format_tools(missing_tools)}), "
        "then rerun './tcapsule bootstrap'."
    )
    print(color_red(message), flush=True)
    print(color_red("Required host tools: smbclient and sshpass"), flush=True)
    raise BootstrapPreflightError(message, fields)


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


def _missing_required_host_tools() -> list[str]:
    return [tool for tool in REQUIRED_HOST_TOOLS if find_command(tool) is None]


def _format_tools(tools: list[str]) -> str:
    return ", ".join(tools)


def _macos_manual_install_command(missing_tools: list[str]) -> str:
    packages = [MACOS_HOST_TOOL_PACKAGES[tool] for tool in missing_tools]
    return f"brew install {' '.join(packages)}"


def _linux_install_plan(missing_tools: list[str]) -> tuple[list[list[str]], str] | None:
    for manager, packages_by_tool in LINUX_HOST_TOOL_PACKAGES.items():
        executable = find_command(manager)
        if executable is None:
            continue
        packages = [packages_by_tool[tool] for tool in missing_tools]
        if manager == "apt-get":
            return (
                [
                    ["sudo", executable, "update"],
                    ["sudo", executable, "install", "-y", *packages],
                ],
                f"sudo apt-get update && sudo apt-get install -y {' '.join(packages)}",
            )
        if manager == "pacman":
            return (
                [["sudo", executable, "-S", "--needed", *packages]],
                f"sudo pacman -S --needed {' '.join(packages)}",
            )
        return (
            [["sudo", executable, "install", "-y", *packages]],
            f"sudo {manager} install -y {' '.join(packages)}",
        )
    return None


def _raise_host_tool_install_error(message: str, manual_command: str | None = None) -> None:
    print(color_red(message), flush=True)
    if manual_command:
        print(color_red("Install the missing tools manually, then rerun './tcapsule bootstrap':"), flush=True)
        print(manual_command, flush=True)
    raise BootstrapError(message)


def _install_macos_host_tools(missing_tools: list[str]) -> None:
    brew = find_command("brew")
    if brew is None:
        print(
            color_red(
                "Install Homebrew so bootstrap can install missing host tools automatically, "
                f"or manually install the missing tools on macOS: {_format_tools(missing_tools)}. "
                "Then rerun './tcapsule bootstrap'."
            ),
            flush=True,
        )
        print(color_red(f"Missing host tools: {_format_tools(missing_tools)}"), flush=True)
        print(color_red("Homebrew install command:"), flush=True)
        print(HOMEBREW_INSTALL_COMMAND, flush=True)
        raise BootstrapError(
            "Install Homebrew or manually install the missing tools on macOS: "
            f"{_format_tools(missing_tools)}"
        )

    packages = [MACOS_HOST_TOOL_PACKAGES[tool] for tool in missing_tools]
    print(f"Installing missing host tools via Homebrew: {_format_tools(missing_tools)}", flush=True)
    run([brew, "install", *packages])


def install_required_host_tools() -> None:
    missing_tools = _missing_required_host_tools()
    if not missing_tools:
        print(f"Found required host tools: {_format_tools(list(REQUIRED_HOST_TOOLS))}", flush=True)
        return

    print(f"Missing required host tools: {_format_tools(missing_tools)}", flush=True)
    platform_label = current_platform_label()
    manual_command: str | None = None
    try:
        if platform_label == "macOS":
            manual_command = _macos_manual_install_command(missing_tools)
            _install_macos_host_tools(missing_tools)
        elif platform_label == "Linux":
            plan = _linux_install_plan(missing_tools)
            if plan is None:
                _raise_host_tool_install_error(
                    f"No supported Linux package manager found to install missing host tools: {_format_tools(missing_tools)}",
                    f"Install {_format_tools(missing_tools)} with your distro package manager.",
                )
            commands, manual_command = plan
            print(f"Installing missing host tools via Linux package manager: {_format_tools(missing_tools)}", flush=True)
            for command in commands:
                run(command)
        else:
            _raise_host_tool_install_error(
                f"Automatic host tool installation is not implemented for {platform_label}.",
                f"Install {_format_tools(missing_tools)} with your OS package manager.",
            )
    except BootstrapCommandError as exc:
        message = f"Failed to install missing host tools automatically: {_format_tools(missing_tools)} (exit code {exc.returncode})"
        print(color_red(message), flush=True)
        if manual_command:
            print(color_red("Install the missing tools manually, then rerun './tcapsule bootstrap':"), flush=True)
            print(manual_command, flush=True)
        raise BootstrapError(f"{message}\n\n{_format_command_error(exc)}") from exc
    except subprocess.CalledProcessError as exc:
        message = f"Failed to install missing host tools automatically: {_format_tools(missing_tools)} (exit code {exc.returncode})"
        print(color_red(message), flush=True)
        if manual_command:
            print(color_red("Install the missing tools manually, then rerun './tcapsule bootstrap':"), flush=True)
            print(manual_command, flush=True)
        raise BootstrapError(message) from exc

    still_missing = _missing_required_host_tools()
    if still_missing:
        _raise_host_tool_install_error(
            f"Required host tools are still missing after install attempt: {_format_tools(still_missing)}",
            manual_command,
        )
    print(f"Installed required host tools: {_format_tools(missing_tools)}", flush=True)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare the local host for TimeCapsuleSMB user workflows.")
    parser.add_argument("--python", default=sys.executable or "python3", help="Python interpreter to use for the repo .venv")
    args = parser.parse_args(argv)

    ensure_install_id()
    config = load_optional_env_config()
    telemetry = TelemetryClient.from_config(config)
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
            command_context.set_stage("check_python")
            selected_python_version = validate_selected_python(args.python)
            command_context.update_fields(
                selected_python=args.python,
                selected_python_version=selected_python_version,
            )
            command_context.set_stage("check_host_support")
            command_context.update_fields(**check_macos_host_tool_install_support(platform_label))
            command_context.set_stage("ensure_venv")
            venv_python = ensure_venv(args.python)
            command_context.update_fields(venv_python=str(venv_python))
            command_context.set_stage("install_python_requirements")
            install_python_requirements(venv_python)
            command_context.set_stage("install_host_tools")
            install_required_host_tools()
        except BootstrapCommandError as e:
            message = _format_command_error(e)
            print(message, file=sys.stderr)
            command_context.fail_with_error(message)
            return e.returncode or 1
        except subprocess.CalledProcessError as e:
            message = f"Command failed with exit code {e.returncode}: {e.cmd}"
            print(message, file=sys.stderr)
            command_context.fail_with_error(message)
            return e.returncode or 1
        except BootstrapError as e:
            if isinstance(e, BootstrapPreflightError):
                command_context.update_fields(**e.fields)
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
        print("Next steps:", flush=True)
        print(f"  1. {VENVDIR / 'bin' / 'tcapsule'} configure", flush=True)
        print(f"  2. {VENVDIR / 'bin' / 'tcapsule'} deploy", flush=True)
        print(f"  3. {VENVDIR / 'bin' / 'tcapsule'} doctor", flush=True)
        print(f"  4. NetBSD 4 only, after reboot if Samba did not auto-start: {VENVDIR / 'bin' / 'tcapsule'} activate", flush=True)
        command_context.succeed()
        return 0
    return 1
