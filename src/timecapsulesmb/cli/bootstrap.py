from __future__ import annotations

import argparse
import os
import platform
import shlex
import subprocess
import sys
from pathlib import Path
from typing import NoReturn, Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.util import color_red
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.services.runtime import load_optional_env_config
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.core.process import popen_process, run_process
from timecapsulesmb.transport.local import find_command
from timecapsulesmb.transport.ssh_client import require_local_ssh
from timecapsulesmb.transport.errors import SshError


REPO_ROOT = Path(__file__).resolve().parents[3]
VENVDIR = REPO_ROOT / ".venv"
# Present only in a source checkout, which bootstrap installs from.
PYPROJECT = REPO_ROOT / "pyproject.toml"
HOMEBREW_INSTALL_COMMAND = '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
# The Mac app uses macOS's own ssh and SMB client.
MAC_APP_HINT = (
    "Or use the TimeCapsuleSMB Mac app, which needs no Homebrew: "
    "https://github.com/jamesyc/TimeCapsuleSMB/releases"
)
MIN_BOOTSTRAP_PYTHON = (3, 9)
FIXED_ZEROCONF_PYTHON = (3, 10)
OLD_ZEROCONF_NOTE = (
    "Note: .venv uses Python 3.9, so Bonjour discovery runs zeroconf 0.148, which has known bugs "
    "that a crafted mDNS packet on your network can trigger. For the fixed zeroconf, delete .venv "
    "and rerun './tcapsule bootstrap --python /path/to/python3.10' with Python 3.10 or newer."
)
MIN_MACOS_AUTO_HOST_TOOL_INSTALL = (14, 0)
PYTHON_VERSION_PROBE = "import sys; print('%d.%d.%d' % sys.version_info[:3])"
MACOS_HOST_TOOL_PACKAGES = {"ssh": "openssh"}
LINUX_HOST_TOOL_PACKAGES = {
    "apt-get": {"ssh": "openssh-client", "smbclient": "smbclient"},
    "dnf": {"ssh": "openssh-clients", "smbclient": "samba-client"},
    "zypper": {"ssh": "openssh-clients", "smbclient": "samba-client"},
    "pacman": {"ssh": "openssh", "smbclient": "smbclient"},
}
# Telemetry keeps the start and the end of a failed command's output: brew and
# pip print their error last, but some brew errors come first.
COMMAND_OUTPUT_HEAD_LIMIT = 2048
COMMAND_OUTPUT_TAIL_LIMIT = 6144
# After Ctrl-C the child got the same SIGINT; give it this long to exit.
INTERRUPTED_CHILD_GRACE_SECONDS = 5.0


class BootstrapError(Exception):
    pass


class BootstrapPreflightError(BootstrapError):
    def __init__(self, message: str, fields: dict[str, object]) -> None:
        self.fields = fields
        super().__init__(message)


class BootstrapCommandError(Exception):
    def __init__(self, cmd: list[str], returncode: int, output: str) -> None:
        self.cmd = cmd
        self.returncode = returncode
        self.output = output
        super().__init__(f"Command failed with exit code {returncode}: {cmd}")


def _echo(chunk: bytes) -> None:
    sys.stdout.flush()
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(chunk.decode("utf-8", errors="replace"))
        sys.stdout.flush()
        return
    buffer.write(chunk)
    buffer.flush()


def _clean_command_output(text: str) -> str:
    """Keep each line as a terminal shows it: the text after its last carriage return."""
    return "\n".join(line.rstrip("\r").rsplit("\r", 1)[-1] for line in text.split("\n"))


def _command_output(chunks: list[bytes]) -> str:
    return _clean_command_output(b"".join(chunks).decode("utf-8", errors="replace"))


def run(cmd: list[str]) -> None:
    """Run cmd with its output shown as it arrives, keeping a copy for errors.

    Installing host tools can take minutes; output held until the command
    exits left the terminal silent, and users stopped bootstrap with Ctrl-C.
    """
    proc = popen_process(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert proc.stdout is not None
    chunks: list[bytes] = []
    try:
        while True:
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                break
            chunks.append(chunk)
            _echo(chunk)
        returncode = proc.wait()
    except KeyboardInterrupt as exc:
        exc.command_output = _command_output(chunks)  # type: ignore[attr-defined]
        try:
            proc.wait(timeout=INTERRUPTED_CHILD_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            proc.terminate()
        raise
    finally:
        proc.stdout.close()
        proc.wait()
    if returncode != 0:
        raise BootstrapCommandError(cmd, returncode, _command_output(chunks))


def _truncate_command_output(
    text: str,
    head: int = COMMAND_OUTPUT_HEAD_LIMIT,
    tail: int = COMMAND_OUTPUT_TAIL_LIMIT,
) -> str:
    text = text.rstrip()
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head].rstrip()}\n...<truncated {omitted} chars>...\n{text[-tail:]}"


def _format_command_output(label: str, text: str) -> str | None:
    formatted = _truncate_command_output(text)
    if not formatted:
        return None
    return f"{label}:\n{formatted}"


def _format_command_error(exc: BootstrapCommandError) -> str:
    message = f"Command failed with exit code {exc.returncode}: {exc.cmd}"
    output = _format_command_output("output", exc.output)
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
        proc = run_process(
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


def venv_python_version(venv_dir: Path) -> str | None:
    """The Python a venv was made with, from its pyvenv.cfg: `version` from
    `python -m venv`, `version_info` from uv. None when it cannot be read."""
    try:
        lines = (venv_dir / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip() in ("version", "version_info") and value.strip():
            return value.strip()
    return None


def old_zeroconf_note(platform_label: str, python_version: str, *, dns_sd_installed: bool) -> str | None:
    """Linux on Python 3.9 gets zeroconf 0.148, whose mDNS fixes need 3.10
    (pyproject.toml). Bonjour uses dns-sd whenever it is installed
    (discovery/bonjour.py), as it always is on macOS, and then never runs
    zeroconf, so only Linux without dns-sd gets the note."""
    if platform_label != "Linux" or dns_sd_installed:
        return None
    parsed = _parse_version_prefix(python_version)
    if parsed is None or _version_at_least(parsed, FIXED_ZEROCONF_PYTHON):
        return None
    return OLD_ZEROCONF_NOTE


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
        proc = run_process(
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


def required_host_tools(platform_label: str) -> tuple[str, ...]:
    """ssh logs in to the device; every Mac has it, minimal Linux installs may
    not. smbclient runs doctor's SMB checks, except on macOS, where doctor uses
    Apple's own SMB client (checks/smb_macos.py)."""
    if platform_label == "macOS":
        return ("ssh",)
    return ("ssh", "smbclient")


def _required_host_tool_paths(platform_label: str) -> dict[str, str | None]:
    return {tool: find_command(tool) for tool in required_host_tools(platform_label)}


def _missing_tools_from_paths(paths: dict[str, str | None]) -> list[str]:
    return [tool for tool, path in paths.items() if path is None]


def _print_host_tool_status(paths: dict[str, str | None]) -> None:
    for tool, path in paths.items():
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
    paths = _required_host_tool_paths(platform_label)
    missing_tools = _missing_tools_from_paths(paths)
    fields: dict[str, object] = {
        "host_os_version": macos_version or "unknown",
        "macos_auto_host_tool_install_supported": auto_install_supported,
        "missing_host_tools": _format_tools(missing_tools),
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
    print(color_red(f"Required host tools: {_format_tools(list(paths))}"), flush=True)
    raise BootstrapPreflightError(message, fields)


def ensure_venv(python: str) -> Path:
    if not VENVDIR.exists():
        print(f"Creating virtualenv at {VENVDIR}", flush=True)
        run([python, "-m", "venv", str(VENVDIR)])
    else:
        print(f"Using existing virtualenv at {VENVDIR}", flush=True)
    return VENVDIR / "bin" / "python"


def venv_has_pip(venv_python: Path) -> bool:
    proc = run_process(
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
    # Eager: rerunning bootstrap, say after `git pull`, also moves the
    # dependencies already in .venv to their newest releases.
    run([str(venv_python), "-m", "pip", "install", "-U", "--upgrade-strategy", "eager", "-e", str(REPO_ROOT)])


def _missing_required_host_tools(platform_label: str) -> list[str]:
    return _missing_tools_from_paths(_required_host_tool_paths(platform_label))


def _format_tools(tools: list[str]) -> str:
    return ", ".join(tools)


def _macos_manual_install_command(missing_tools: list[str]) -> str:
    packages = [MACOS_HOST_TOOL_PACKAGES[tool] for tool in missing_tools]
    return f"brew install {' '.join(packages)}"


def _python_venv_package(python: str, python_version: str) -> str | None:
    """The apt package that gives `python` the ensurepip its venv needs.

    Debian and Ubuntu split ensurepip into pythonX.Y-venv, so `python -m venv`
    fails there until it is installed. Other distributions ship it in Python.
    None without apt, when `python` imports ensurepip, or when .venv already
    has pip: bootstrap needs ensurepip only to create .venv or to repair it.
    """
    venv_python = VENVDIR / "bin" / "python"
    if find_command("apt-get") is None or (venv_python.exists() and venv_has_pip(venv_python)):
        return None
    probe = run_process([python, "-c", "import ensurepip"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if probe.returncode == 0:
        return None
    major, minor = python_version.split(".")[:2]
    return f"python{major}.{minor}-venv"


def _linux_package_manager() -> str | None:
    return next((manager for manager in LINUX_HOST_TOOL_PACKAGES if find_command(manager) is not None), None)


def _linux_install_commands(manager: str, packages: list[str], *, sudo: bool) -> list[list[str]]:
    """The `manager` commands that install `packages`, by name, after sudo if `sudo`."""
    prefix = ["sudo"] if sudo else []
    if manager == "apt-get":
        return [[*prefix, manager, "update"], [*prefix, manager, "install", "-y", *packages]]
    if manager == "pacman":
        return [[*prefix, manager, "-S", "--needed", *packages]]
    return [[*prefix, manager, "install", "-y", *packages]]


def _raise_host_tool_install_error(message: str, manual_command: str | None = None, detail: str | None = None) -> NoReturn:
    print(color_red(message), flush=True)
    if manual_command:
        print(color_red("Install them manually, then rerun './tcapsule bootstrap':"), flush=True)
        print(manual_command, flush=True)
    raise BootstrapError(f"{message}\n\n{detail}" if detail else message)


def _run_install_commands(names: str, commands: list[list[str]], manual_command: str) -> None:
    """Run the package manager `commands` that install `names`, or say how to by hand."""
    try:
        for command in commands:
            run(command)
    except BootstrapCommandError as exc:
        _raise_host_tool_install_error(
            f"Failed to install {names} automatically (exit code {exc.returncode})",
            manual_command,
            _format_command_error(exc),
        )
    except BrokenPipeError:
        # The terminal showing the output went away; the install did not fail.
        raise
    except OSError as exc:
        # A program that is missing or cannot run, such as the package manager.
        _raise_host_tool_install_error(f"Failed to install {names} automatically ({exc})", manual_command)


def _install_with_linux_package_manager(names: str, manager: str, packages: list[str]) -> str:
    """Install `packages` with `manager` as root; return the command to do it by hand.

    Root, usually in a container that has no sudo, runs the commands directly.
    """
    as_user = os.geteuid() != 0
    has_sudo = find_command("sudo") is not None
    commands = _linux_install_commands(manager, packages, sudo=as_user and has_sudo)
    manual_command = " && ".join(shlex.join(command) for command in commands)
    if as_user and not has_sudo:
        _raise_host_tool_install_error(
            f"Installing {names} needs root, and sudo is not installed. Run the command below as root.",
            manual_command,
        )
    print(f"Installing {names} with {manager}", flush=True)
    _run_install_commands(names, commands, manual_command)
    return manual_command


def install_python_venv_package(package: str) -> None:
    """Install the apt package `_python_venv_package` named."""
    print(f"Missing {package}, which Python needs to create or repair .venv", flush=True)
    _install_with_linux_package_manager(package, "apt-get", [package])
    print(f"Installed {package}", flush=True)


def _install_macos_host_tools(missing_tools: list[str], manual_command: str) -> None:
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
        # main prints this error last, so the app hint appears once, at the end.
        raise BootstrapError(
            "Install Homebrew or manually install the missing tools on macOS: "
            f"{_format_tools(missing_tools)}. {MAC_APP_HINT}"
        )

    packages = [MACOS_HOST_TOOL_PACKAGES[tool] for tool in missing_tools]
    print(f"Installing missing host tools via Homebrew: {_format_tools(missing_tools)}", flush=True)
    _run_install_commands(_format_tools(missing_tools), [[brew, "install", *packages]], manual_command)


def _require_supported_ssh() -> None:
    try:
        require_local_ssh()
    except SshError as exc:
        raise BootstrapError(str(exc)) from exc


def install_required_host_tools(platform_label: str) -> None:
    missing_tools = _missing_required_host_tools(platform_label)
    if "ssh" not in missing_tools:
        _require_supported_ssh()
    if not missing_tools:
        print(f"Found required host tools: {_format_tools(list(required_host_tools(platform_label)))}", flush=True)
        return

    print(f"Missing required host tools: {_format_tools(missing_tools)}", flush=True)
    tools = _format_tools(missing_tools)
    if platform_label == "macOS":
        manual_command = _macos_manual_install_command(missing_tools)
        _install_macos_host_tools(missing_tools, manual_command)
    elif platform_label == "Linux":
        manager = _linux_package_manager()
        if manager is None:
            _raise_host_tool_install_error(
                f"No supported Linux package manager found to install missing host tools: {tools}",
                f"Install {tools} with your distro package manager.",
            )
        packages = [LINUX_HOST_TOOL_PACKAGES[manager][tool] for tool in missing_tools]
        manual_command = _install_with_linux_package_manager(tools, manager, packages)
    else:
        _raise_host_tool_install_error(
            f"Automatic host tool installation is not implemented for {platform_label}.",
            f"Install {tools} with your OS package manager.",
        )

    still_missing = _missing_required_host_tools(platform_label)
    if still_missing:
        _raise_host_tool_install_error(
            f"Required host tools are still missing after install attempt: {_format_tools(still_missing)}",
            manual_command,
        )
    _require_supported_ssh()
    print(f"Installed required host tools: {tools}", flush=True)


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
            pyproject_path=str(PYPROJECT),
            venv_path=str(VENVDIR),
            pyproject_present=PYPROJECT.exists(),
            venv_exists_before=VENVDIR.exists(),
            python_executable=args.python,
        )
        command_context.set_stage("validate_project")
        if not PYPROJECT.exists():
            message = f"Missing {PYPROJECT}"
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
            command_context.set_stage("check_python_venv")
            venv_package = _python_venv_package(args.python, selected_python_version) if platform_label == "Linux" else None
            if venv_package:
                # .venv cannot be created or repaired without it, so it comes
                # first. The host tools still come last: a failure there (no
                # Homebrew, no sudo, a package apt refuses) leaves a .venv
                # that can deploy.
                command_context.update_fields(python_venv_package=venv_package)
                command_context.set_stage("install_python_venv_package")
                install_python_venv_package(venv_package)
            command_context.set_stage("ensure_venv")
            venv_python = ensure_venv(args.python)
            command_context.update_fields(venv_python=str(venv_python))
            command_context.set_stage("install_python_requirements")
            install_python_requirements(venv_python)
            command_context.set_stage("install_host_tools")
            install_required_host_tools(platform_label)
        except BootstrapCommandError as e:
            message = _format_command_error(e)
            print(message, file=sys.stderr)
            command_context.fail_with_error(message)
            return e.returncode or 1
        except KeyboardInterrupt as e:
            output = _format_command_output("output", getattr(e, "command_output", ""))
            if output is not None:
                command_context.set_error(f"Cancelled by user\n\n{output}")
            raise
        except BootstrapError as e:
            if isinstance(e, BootstrapPreflightError):
                command_context.update_fields(**e.fields)
            print(str(e), file=sys.stderr)
            command_context.fail_with_error(str(e))
            return 1
        except OSError as e:
            # A program bootstrap starts is missing or cannot run.
            message = f"{type(e).__name__}: {e}"
            print(message, file=sys.stderr)
            command_context.fail_with_error(message)
            return 1

        command_context.set_stage("complete")
        # An existing .venv keeps the Python it was made with, whatever --python says.
        venv_version = venv_python_version(venv_python.parent.parent) or selected_python_version
        command_context.update_fields(
            venv_exists_after=VENVDIR.exists(),
            venv_python_version=venv_version,
        )
        if platform_label != "macOS":
            # Doctor uses Apple's own SMB client on a Mac.
            command_context.update_fields(smbclient_available_after=find_command("smbclient") is not None)
        zeroconf_note = old_zeroconf_note(
            platform_label, venv_version, dns_sd_installed=find_command("dns-sd") is not None,
        )
        if zeroconf_note:
            print(f"\n{zeroconf_note}", flush=True)
        print("\nHost setup complete.", flush=True)
        print("Next steps:", flush=True)
        print(f"  1. {VENVDIR / 'bin' / 'tcapsule'} configure", flush=True)
        print(f"  2. {VENVDIR / 'bin' / 'tcapsule'} deploy", flush=True)
        print(f"  3. {VENVDIR / 'bin' / 'tcapsule'} doctor", flush=True)
        print(f"  4. NetBSD 4 only, after reboot if Samba did not auto-start: {VENVDIR / 'bin' / 'tcapsule'} activate", flush=True)
        command_context.succeed()
        return 0
    return 1
