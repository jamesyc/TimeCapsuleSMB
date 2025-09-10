from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from typing import Iterable, List, Optional, Tuple
import re


def run(cmd: List[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
    )


def acp_run_check(cmd: List[str]) -> str:
    """Run an AirPyrt command, detect in-output error codes, and raise.

    Some AirPyrt (acp) invocations print "error code: -0x.." but still exit 0
    when authentication fails. This parses stdout to catch that case.

    Returns captured stdout on success.
    """
    proc = run(cmd, check=False, capture=True)
    out = proc.stdout or ""
    if proc.returncode != 0:
        raise RuntimeError(out.strip() or f"Command failed with rc={proc.returncode}")
    low = out.lower()
    m = re.search(r"error[_ ]code\s*:?[\t ]*(-?0x[0-9a-f]+)", low)
    if m and m.group(1).startswith("-0x"):
        raise RuntimeError(f"AirPyrt reported error_code {m.group(1)} (likely wrong admin password). Output: {out.strip()}")
    return out


def acp_available(py_exe: str) -> bool:
    try:
        # Capture output to avoid printing to stdout during interpreter probes
        run([py_exe, "-c", "import acp; print(1)"], capture=True)
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
        acp_run_check(cmd)
    except RuntimeError as e:
        raise RuntimeError(f"Failed to set dbug={value_hex} via AirPyrt. Output: {e}")


def reboot(host: str, password: str, *, python_candidates: Optional[Iterable[str]] = None, verbose: bool = True) -> None:
    acp_exec, py = ensure_airpyrt_available(python_candidates)
    if acp_exec:
        cmd = [acp_exec, "-t", host, "-p", password, "--reboot"]
    else:
        cmd = [py, "-B", "-m", "acp", "-t", host, "-p", password, "--reboot"]
    if verbose:
        print("Rebooting device:", " ".join(shlex.quote(x) for x in cmd))
    try:
        acp_run_check(cmd)
    except RuntimeError as e:
        raise RuntimeError(f"Reboot command failed. Output: {e}")


# --- SSH helper (run commands on the device) ---

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


    
