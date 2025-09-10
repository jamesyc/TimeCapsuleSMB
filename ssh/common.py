from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from typing import Iterable, List, Optional, Tuple


def run(cmd: List[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
    )


def acp_available(py_exe: str) -> bool:
    try:
        run([py_exe, "-c", "import acp; print(1)"])
        return True
    except Exception:
        return False


def candidate_interpreters() -> List[str]:
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


def ensure_airpyrt_available(python_candidates: Optional[Iterable[str]] = None) -> tuple[Optional[str], Optional[str]]:
    acp_exec = find_acp_executable()
    py = find_airpyrt_python(python_candidates)
    if not acp_exec and not py:
        raise RuntimeError(
            "AirPyrt (acp) not found. Install per https://github.com/samuelthomas2774/airport/wiki/AirPyrt#installation\n"
            "Example: git clone https://github.com/x56/airpyrt-tools.git && cd airpyrt-tools && python2 setup.py install --user\n"
            "Then ensure 'acp' is on PATH or set AIRPYRT_PY to that interpreter."
        )
    return acp_exec, py


def set_dbug(host: str, password: str, value_hex: str, *, python_candidates: Optional[Iterable[str]] = None, verbose: bool = True) -> None:
    acp_exec, py = ensure_airpyrt_available(python_candidates)
    if acp_exec:
        cmd = [acp_exec, "-t", host, "-p", password, "--setprop", "dbug", value_hex]
    else:
        cmd = [py, "-B", "-m", "acp", "-t", host, "-p", password, "--setprop", "dbug", value_hex]
    if verbose:
        print("Running:", " ".join(shlex.quote(x) for x in cmd))
    try:
        run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        msg = e.stdout.strip() if e.stdout else str(e)
        raise RuntimeError(f"Failed to set dbug={value_hex} via AirPyrt. Output: {msg}")


def remove_property(host: str, password: str, name: str, *, python_candidates: Optional[Iterable[str]] = None, verbose: bool = True) -> None:
    """Remove an AirPort configuration property using AirPyrt.

    Some firmware treats removing the property as distinct from setting to zero.
    """
    acp_exec, py = ensure_airpyrt_available(python_candidates)
    if acp_exec:
        cmd = [acp_exec, "-t", host, "-p", password, "remove", name]
    else:
        cmd = [py, "-B", "-m", "acp", "-t", host, "-p", password, "remove", name]
    if verbose:
        print("Running:", " ".join(shlex.quote(x) for x in cmd))
    try:
        run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        msg = e.stdout.strip() if e.stdout else str(e)
        raise RuntimeError(f"Failed to remove property '{name}' via AirPyrt. Output: {msg}")


def reboot(host: str, password: str, *, python_candidates: Optional[Iterable[str]] = None, verbose: bool = True) -> None:
    acp_exec, py = ensure_airpyrt_available(python_candidates)
    if acp_exec:
        cmd = [acp_exec, "-t", host, "-p", password, "--reboot"]
    else:
        cmd = [py, "-B", "-m", "acp", "-t", host, "-p", password, "--reboot"]
    if verbose:
        print("Rebooting device:", " ".join(shlex.quote(x) for x in cmd))
    try:
        run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        msg = e.stdout.strip() if e.stdout else str(e)
        raise RuntimeError(f"Reboot command failed. Output: {msg}")


# --- SSH helpers (run commands on the device) ---

def ssh_run_command(host: str, password: str, command: str, *, timeout: int = 30, verbose: bool = True) -> Tuple[int, str]:
    """Run a shell command on the device via system ssh using pexpect.

    Uses password auth for user 'root', allows legacy DSA host keys, and disables
    strict host key checking. Returns (exit_code, combined_output).
    """
    try:
        import pexpect
    except Exception:
        raise RuntimeError("pexpect not available. Run 'make install' to install requirements.")

    ssh_cmd = [
        "ssh",
        "-o", "HostKeyAlgorithms=+ssh-dss",
        "-o", "PubkeyAuthentication=no",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        f"root@{host}",
        command,
    ]
    if verbose:
        print("SSH exec:", " ".join(shlex.quote(x) for x in ssh_cmd))

    import pexpect
    child = pexpect.spawn(ssh_cmd[0], ssh_cmd[1:], encoding="utf-8", timeout=timeout)
    out_chunks: List[str] = []
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


def disable_ssh_via_local_acp(host: str, password: str, *, verbose: bool = True) -> None:
    """Disable SSH by removing 'dbug' using the on-device 'acp' command via SSH.

    This aligns with reports that removing the property locally persists
    correctly, whereas remote ACP remove/set may not on some firmware.
    """
    cmds = [
        "acp remove dbug",
        "/usr/sbin/acp remove dbug",
        "/usr/bin/acp remove dbug",
    ]
    last_err = None
    for c in cmds:
        rc, out = ssh_run_command(host, password, c, verbose=verbose)
        if rc == 0:
            if verbose:
                print("Removed 'dbug' via:", c)
            break
        last_err = (rc, out)
    else:
        code, out = last_err or (1, "unknown error")
        raise RuntimeError(f"Failed to remove 'dbug' via on-device acp (rc={code}). Output: {out}")

    # Reboot the device to apply changes. Prefer acp --reboot; fall back to reboot(8).
    for c in ("acp --reboot", "/usr/sbin/acp --reboot", "/sbin/reboot", "reboot"):
        rc, out = ssh_run_command(host, password, c, verbose=verbose)
        if rc == 0:
            if verbose:
                print("Rebooted device via:", c)
            return
    raise RuntimeError("Failed to reboot device after removing 'dbug'.")
