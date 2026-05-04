from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Iterable
from typing import Optional, Tuple


AIRPYRT_NOT_FOUND_ERROR = "AirPyrt (acp) not found."
AIRPYRT_SSH_OPTIONS = [
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "KexAlgorithms=+diffie-hellman-group14-sha1",
    "-o", "PubkeyAuthentication=no",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
]

LogCallback = Callable[[str], None]


def _resolve_log(log: LogCallback | None, verbose: bool) -> LogCallback | None:
    if log is not None:
        return log
    if verbose:
        return print
    return None


def _emit(log: LogCallback | None, message: str) -> None:
    if log is not None:
        log(message)


def run(cmd: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
    )


def acp_run_check(cmd: list[str]) -> str:
    proc = run(cmd, check=False, capture=True)
    out = proc.stdout or ""
    if proc.returncode != 0:
        raise RuntimeError(out.strip() or f"Command failed with rc={proc.returncode}")
    lowered = out.lower()
    match = re.search(r"error[_ ]code\s*:?[\t ]*(-?0x[0-9a-f]+)", lowered)
    if match and match.group(1).startswith("-0x"):
        raise RuntimeError(
            f"AirPyrt reported error_code {match.group(1)} (likely wrong admin password). Output: {out.strip()}"
        )
    return out


def acp_available(py_exe: str) -> bool:
    try:
        run([py_exe, "-c", "import acp; print(1)"], capture=True)
        return True
    except Exception:
        return False


def candidate_interpreters() -> list[str]:
    env_py = os.environ.get("AIRPYRT_PY")
    if env_py:
        return [env_py]
    local_env = os.path.join(os.getcwd(), ".airpyrt-venv", "bin", "python")
    return [local_env, "python2", "python2.7", "python"]


def find_airpyrt_python(candidates: Optional[Iterable[str]] = None) -> Optional[str]:
    for py in (candidates or candidate_interpreters()):
        try:
            if acp_available(py):
                return py
        except Exception:
            continue
    return None


def find_acp_executable() -> Optional[str]:
    return shutil.which("acp")


def ensure_airpyrt_available(python_candidates: Optional[Iterable[str]] = None) -> Tuple[Optional[str], Optional[str]]:
    acp_exec = find_acp_executable()
    py = find_airpyrt_python(python_candidates)
    if not acp_exec and not py:
        raise RuntimeError(AIRPYRT_NOT_FOUND_ERROR)
    return acp_exec, py


def _acp_command(host: str, password: str, *args: str, python_candidates: Optional[Iterable[str]] = None) -> list[str]:
    acp_exec, py = ensure_airpyrt_available(python_candidates)
    if acp_exec:
        return [acp_exec, "-t", host, "-p", password, *args]
    return [py, "-B", "-m", "acp", "-t", host, "-p", password, *args]


def set_dbug(
    host: str,
    password: str,
    value_hex: str,
    *,
    python_candidates: Optional[Iterable[str]] = None,
    log: LogCallback | None = None,
    verbose: bool = False,
) -> None:
    logger = _resolve_log(log, verbose)
    cmd = _acp_command(host, password, "--setprop", "dbug", value_hex, python_candidates=python_candidates)
    _emit(logger, f"Running: {' '.join(shlex.quote(x) for x in cmd)}")
    try:
        acp_run_check(cmd)
    except RuntimeError as e:
        raise RuntimeError(f"Failed to set dbug={value_hex} via AirPyrt. Output: {e}")


def reboot(
    host: str,
    password: str,
    *,
    python_candidates: Optional[Iterable[str]] = None,
    log: LogCallback | None = None,
    verbose: bool = False,
) -> None:
    logger = _resolve_log(log, verbose)
    cmd = _acp_command(host, password, "--reboot", python_candidates=python_candidates)
    _emit(logger, f"Rebooting device: {' '.join(shlex.quote(x) for x in cmd)}")
    try:
        acp_run_check(cmd)
    except RuntimeError as e:
        raise RuntimeError(f"Reboot command failed. Output: {e}")


def build_airpyrt_ssh_command(host: str, command: str) -> list[str]:
    return [
        "ssh",
        *AIRPYRT_SSH_OPTIONS,
        f"root@{host}",
        command,
    ]


def ssh_run_command(
    host: str,
    password: str,
    command: str,
    *,
    timeout: int = 30,
    log: LogCallback | None = None,
    verbose: bool = False,
) -> tuple[int, str]:
    try:
        import pexpect
    except Exception:
        raise RuntimeError(
            "pexpect not available. Run './tcapsule bootstrap' first, or use 'make install'."
        )

    ssh_cmd = build_airpyrt_ssh_command(host, command)
    _emit(_resolve_log(log, verbose), f"SSH exec: {' '.join(shlex.quote(x) for x in ssh_cmd)}")

    child = pexpect.spawn(ssh_cmd[0], ssh_cmd[1:], encoding="utf-8", timeout=timeout)
    out_chunks: list[str] = []
    try:
        i = child.expect(["[Pp]assword:", pexpect.EOF, pexpect.TIMEOUT], timeout=timeout)
        if i == 0:
            child.sendline(password)
            child.expect(pexpect.EOF, timeout=timeout)
        out_chunks.append(child.before or "")
    finally:
        try:
            child.close()
        except Exception:
            pass
    rc = child.exitstatus if child.exitstatus is not None else (child.signalstatus or 1)
    return rc, "".join(out_chunks)


def enable_ssh(
    host: str,
    password: str,
    *,
    reboot_device: bool = True,
    python_candidates: Optional[Iterable[str]] = None,
    log: LogCallback | None = None,
    verbose: bool = False,
) -> None:
    logger = _resolve_log(log, verbose)
    set_dbug(host, password, "0x3000", python_candidates=python_candidates, log=logger)
    if reboot_device:
        reboot(host, password, python_candidates=python_candidates, log=logger)


def _dbug_property_already_absent(output: str) -> bool:
    return "remove property error: -10" in output


def disable_ssh(
    host: str,
    password: str,
    *,
    reboot_device: bool = True,
    python_candidates: Optional[Iterable[str]] = None,
    log: LogCallback | None = None,
    verbose: bool = False,
) -> None:
    logger = _resolve_log(log, verbose)
    cmds = [
        "acp remove dbug",
        "/usr/sbin/acp remove dbug",
        "/usr/bin/acp remove dbug",
    ]
    last_err: Optional[Tuple[int, str]] = None
    for command in cmds:
        rc, out = ssh_run_command(host, password, command, log=logger)
        if rc == 0:
            _emit(logger, f"Removed 'dbug' via: {command}")
            break
        if _dbug_property_already_absent(out):
            _emit(logger, f"'dbug' already absent via: {command}")
            break
        last_err = (rc, out)
    else:
        code, out = last_err or (1, "unknown error")
        raise RuntimeError(f"Failed to remove 'dbug' via on-device acp (rc={code}). Output: {out}")

    if reboot_device:
        reboot(host, password, python_candidates=python_candidates, log=logger)
