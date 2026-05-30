from __future__ import annotations

import ipaddress
import os
import shlex
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.transport.local import command_exists, run_local_capture

SMBCLIENT_DEBUG_TEXT_LIMIT = 1000


@dataclass(frozen=True)
class SmbClientTarget:
    server: str
    ip_address: str | None = None

    @property
    def display(self) -> str:
        if self.ip_address and self.ip_address != self.server:
            return f"{self.server} via {self.ip_address}"
        return self.server


SmbClientTargetInput = Union[str, SmbClientTarget]


def parse_smbclient_disk_shares(stdout: str) -> list[str]:
    shares: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "|" in line:
            parts = line.split("|", 2)
            if len(parts) >= 2 and parts[0] == "Disk":
                share_name = parts[1].strip()
            else:
                continue
        else:
            share_name = line
        if share_name and share_name != "IPC$" and share_name not in shares:
            shares.append(share_name)
    return shares


def _normalize_smb_client_target(target: SmbClientTargetInput) -> SmbClientTarget:
    if isinstance(target, SmbClientTarget):
        return target
    try:
        ip = str(ipaddress.ip_address(target))
    except ValueError:
        ip = None
    return SmbClientTarget(target, ip)


def _smbclient_base_args() -> list[str]:
    return ["smbclient", "-s", "/dev/null"]


def _smbclient_env() -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
        "HOME": os.environ.get("HOME", ""),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": os.environ.get("LANG", "C"),
        "LC_ALL": os.environ.get("LC_ALL", os.environ.get("LANG", "C")),
    }
    return {key: value for key, value in env.items() if value}


def _smbclient_text_tail(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    text = text.strip()
    if not text:
        return None
    if len(text) <= SMBCLIENT_DEBUG_TEXT_LIMIT:
        return text
    return text[-SMBCLIENT_DEBUG_TEXT_LIMIT:]


def _smbclient_failure_line(proc: subprocess.CompletedProcess[str]) -> str:
    text = _smbclient_text_tail(proc.stderr) or _smbclient_text_tail(proc.stdout) or ""
    detail = text.splitlines()
    return detail[-1] if detail else f"failed with rc={proc.returncode}"


def _smbclient_failure_target_display(target: SmbClientTarget) -> str:
    if target.ip_address:
        return f"{target.server} via {target.ip_address}"
    return target.server


def _smbclient_attempt_display(attempt: dict[str, object]) -> str:
    server = str(attempt.get("server") or "unknown")
    ip_address = attempt.get("ip_address")
    if isinstance(ip_address, str) and ip_address and ip_address != server:
        server = f"{server} via {ip_address}"
    return server


def _smbclient_attempt_failure_summary(index: int, attempt: dict[str, object]) -> str:
    display = _smbclient_attempt_display(attempt)
    outcome = attempt.get("outcome")
    if outcome == "timeout":
        failure = "timed out"
    elif outcome == "missing_expected_share":
        failure = f"expected share {attempt.get('expected_share')!r} not found"
    else:
        failure = str(attempt.get("failure") or f"failed with outcome={outcome or 'unknown'}")
    command = attempt.get("command")
    if isinstance(command, str) and command:
        return f"attempt {index} {display} using {command}: {failure}"
    return f"attempt {index} {display}: {failure}"


def _smbclient_attempts_failure_summary(attempts: list[dict[str, object]]) -> str:
    if not attempts:
        return "not attempted"
    return "; ".join(_smbclient_attempt_failure_summary(index, attempt) for index, attempt in enumerate(attempts, start=1))


def _smbclient_attempt_retryable(attempt: dict[str, object]) -> bool:
    if attempt.get("outcome") == "timeout":
        return True
    if attempt.get("outcome") != "error":
        return False

    retryable_fragments = (
        "NT_STATUS_IO_TIMEOUT",
        "NT_STATUS_CONNECTION_REFUSED",
        "NT_STATUS_CONNECTION_DISCONNECTED",
        "NT_STATUS_NETWORK_UNREACHABLE",
        "Connection refused",
        "Connection reset by peer",
        "Operation timed out",
    )
    for key in ("failure", "stderr_tail", "stdout_tail"):
        value = attempt.get(key)
        if isinstance(value, str) and any(fragment in value for fragment in retryable_fragments):
            return True
    return False


def _redacted_smbclient_command(args: list[str]) -> str:
    redacted: list[str] = []
    redact_next = False
    for arg in args:
        if redact_next:
            username = arg.split("%", 1)[0]
            redacted.append(f"{username}%***" if username else "***")
            redact_next = False
            continue
        redacted.append(arg)
        if arg in {"-U", "--user"}:
            redact_next = True
    return shlex.join(redacted)


def _smbclient_listing_args(
    target: SmbClientTarget,
    username: str,
    password: str,
    *,
    port: Optional[int] = None,
) -> list[str]:
    args = _smbclient_base_args() + ["-g"]
    if port is not None:
        args += ["-p", str(port)]
    if target.ip_address is not None:
        args += ["-I", target.ip_address]
    return args + ["-L", f"//{target.server}", "-U", f"{username}%{password}"]


def _new_listing_attempt(target: SmbClientTarget, timeout: int, start: float, command: str | None = None) -> dict[str, object]:
    attempt = {
        "server": target.server,
        "timeout_sec": timeout,
        "elapsed_sec": round(time.monotonic() - start, 3),
    }
    if target.ip_address:
        attempt["ip_address"] = target.ip_address
    if command is not None:
        attempt["command"] = command
    return attempt


def _run_smbclient_listing(
    target: SmbClientTarget,
    username: str,
    password: str,
    *,
    port: Optional[int] = None,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    args = _smbclient_listing_args(target, username, password, port=port)
    return run_local_capture(
        args,
        timeout=timeout,
        env=_smbclient_env(),
    )


def check_authenticated_smb_listing(
    username: str,
    password: str,
    server: SmbClientTargetInput | list[SmbClientTargetInput],
    *,
    expected_share_name: Optional[str] = None,
    port: Optional[int] = None,
    timeout: int = 20,
    retry_delays: tuple[int, ...] = (),
) -> CheckResult:
    if not command_exists("smbclient"):
        return CheckResult("FAIL", "missing local tool smbclient")
    if isinstance(server, list) or retry_delays:
        servers = server if isinstance(server, list) else [server]
        return try_authenticated_smb_listing(
            username,
            password,
            servers,
            expected_share_name=expected_share_name,
            port=port,
            timeout=timeout,
            retry_delays=retry_delays,
        )

    target = _normalize_smb_client_target(server)
    command = _redacted_smbclient_command(_smbclient_listing_args(target, username, password, port=port))
    try:
        start = time.monotonic()
        proc = _run_smbclient_listing(target, username, password, port=port, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        attempt = _new_listing_attempt(target, timeout, start, command)
        attempt["outcome"] = "timeout"
        stdout_tail = _smbclient_text_tail(exc.stdout)
        stderr_tail = _smbclient_text_tail(exc.stderr)
        if stdout_tail is not None:
            attempt["stdout_tail"] = stdout_tail
        if stderr_tail is not None:
            attempt["stderr_tail"] = stderr_tail
        return CheckResult(
            "FAIL",
            f"authenticated SMB listing failed: timed out via {_smbclient_failure_target_display(target)}",
            {"attempts": [attempt]},
        )
    attempt = _new_listing_attempt(target, timeout, start, command)
    attempt["returncode"] = proc.returncode
    stdout_tail = _smbclient_text_tail(proc.stdout)
    stderr_tail = _smbclient_text_tail(proc.stderr)
    if stdout_tail is not None:
        attempt["stdout_tail"] = stdout_tail
    if stderr_tail is not None:
        attempt["stderr_tail"] = stderr_tail
    if proc.returncode == 0:
        disk_shares = parse_smbclient_disk_shares(proc.stdout)
        attempt["disk_shares"] = disk_shares
        if expected_share_name is not None and expected_share_name not in proc.stdout:
            attempt["outcome"] = "missing_expected_share"
            attempt["expected_share"] = expected_share_name
            return CheckResult(
                "FAIL",
                f"authenticated SMB listing did not include expected share {expected_share_name!r} on {target.display}",
                {"attempts": [attempt], "disk_shares": disk_shares},
            )
        attempt["outcome"] = "pass"
        if expected_share_name is not None:
            attempt["expected_share"] = expected_share_name
            attempt["expected_share_found"] = True
        return CheckResult(
            "PASS",
            f"authenticated SMB listing works for {username}@{target.display}",
            {
                "server": target.server,
                "ip_address": target.ip_address,
                "disk_shares": disk_shares,
                "attempts": [attempt],
            },
        )
    attempt["outcome"] = "error"
    msg = _smbclient_failure_line(proc)
    attempt["failure"] = msg
    return CheckResult(
        "FAIL",
        f"authenticated SMB listing failed via {_smbclient_failure_target_display(target)} using {command}: {msg}",
        {"attempts": [attempt]},
    )


def try_authenticated_smb_listing(
    username: str,
    password: str,
    servers: list[SmbClientTargetInput],
    *,
    expected_share_name: Optional[str] = None,
    port: Optional[int] = None,
    timeout: int = 30,
    retry_delays: tuple[int, ...] = (),
) -> CheckResult:
    if not command_exists("smbclient"):
        return CheckResult("WARN", "SMB listing verification skipped: smbclient not found")

    attempts: list[dict[str, object]] = []
    targets = [_normalize_smb_client_target(server_input) for server_input in servers]
    for round_index in range(len(retry_delays) + 1):
        round_attempts: list[dict[str, object]] = []
        for target in targets:
            command = _redacted_smbclient_command(_smbclient_listing_args(target, username, password, port=port))
            try:
                start = time.monotonic()
                proc = _run_smbclient_listing(target, username, password, port=port, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                attempt = _new_listing_attempt(target, timeout, start, command)
                attempt["outcome"] = "timeout"
                stdout_tail = _smbclient_text_tail(exc.stdout)
                stderr_tail = _smbclient_text_tail(exc.stderr)
                if stdout_tail is not None:
                    attempt["stdout_tail"] = stdout_tail
                if stderr_tail is not None:
                    attempt["stderr_tail"] = stderr_tail
                attempts.append(attempt)
                round_attempts.append(attempt)
                continue
            attempt = _new_listing_attempt(target, timeout, start, command)
            attempt["returncode"] = proc.returncode
            stdout_tail = _smbclient_text_tail(proc.stdout)
            stderr_tail = _smbclient_text_tail(proc.stderr)
            if stdout_tail is not None:
                attempt["stdout_tail"] = stdout_tail
            if stderr_tail is not None:
                attempt["stderr_tail"] = stderr_tail
            if proc.returncode == 0:
                disk_shares = parse_smbclient_disk_shares(proc.stdout)
                attempt["disk_shares"] = disk_shares
                if expected_share_name is not None and expected_share_name not in proc.stdout:
                    attempt["outcome"] = "missing_expected_share"
                    attempt["expected_share"] = expected_share_name
                    attempts.append(attempt)
                    round_attempts.append(attempt)
                    continue
                attempt["outcome"] = "pass"
                if expected_share_name is not None:
                    attempt["expected_share"] = expected_share_name
                    attempt["expected_share_found"] = True
                attempts.append(attempt)
                return CheckResult(
                    "PASS",
                    f"authenticated SMB listing works for {username}@{target.display}",
                    {
                        "server": target.server,
                        "ip_address": target.ip_address,
                        "disk_shares": disk_shares,
                        "attempts": attempts,
                    },
                )
            attempt["outcome"] = "error"
            raw_failure = _smbclient_failure_line(proc)
            attempt["failure"] = raw_failure
            attempts.append(attempt)
            round_attempts.append(attempt)

        if round_index >= len(retry_delays):
            break
        if not round_attempts or not all(_smbclient_attempt_retryable(attempt) for attempt in round_attempts):
            break
        retry_delay = retry_delays[round_index]
        for attempt in round_attempts:
            attempt["next_retry_delay_sec"] = retry_delay
        time.sleep(retry_delay)

    failure_msg = _smbclient_attempts_failure_summary(attempts)
    return CheckResult("FAIL", f"authenticated SMB listing failed after {len(attempts)} attempt(s): {failure_msg}", {"attempts": attempts})


def check_authenticated_smb_file_ops_detailed(
    username: str,
    password: str,
    server: str,
    share_name: str,
    *,
    ip_address: str | None = None,
    port: Optional[int] = None,
    timeout: int = 20,
) -> list[CheckResult]:
    if not command_exists("smbclient"):
        return [CheckResult("WARN", "SMB file-ops verification skipped: smbclient not found")]

    test_dir_name = f".doctor-fileops-{uuid.uuid4().hex[:8]}"
    upload_name = ".sample.txt"
    renamed_name = ".sample-renamed.txt"
    copy_name = ".sample-copy.txt"

    def run_share_commands(remote: str, commands: list[str]) -> subprocess.CompletedProcess[str]:
        args = _smbclient_base_args()
        if port is not None:
            args += ["-p", str(port)]
        if ip_address is not None:
            args += ["-I", ip_address]
        return run_local_capture(
            args + [remote, "-U", f"{username}%{password}", "-c", "; ".join(commands)],
            timeout=timeout,
            env=_smbclient_env(),
        )

    def fail_result(prefix: str, proc: subprocess.CompletedProcess[str]) -> list[CheckResult]:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        msg = detail[-1] if detail else f"failed with rc={proc.returncode}"
        return results + [CheckResult("FAIL", f"{prefix}: {msg}")]

    with tempfile.TemporaryDirectory(prefix="tcapsule-doctor-") as tmpdir:
        tmp_root = Path(tmpdir)
        upload_path = tmp_root / upload_name
        update_path = tmp_root / ".sample-update.txt"
        readback_path = tmp_root / ".sample-readback.txt"
        copy_source_path = tmp_root / ".sample-copy-source.txt"
        copy_readback_path = tmp_root / ".sample-copy-readback.txt"
        upload_contents = "line1\nline2\nline3\n"
        updated_contents = "line1\nline2\nline3\nline4-updated\n"
        upload_path.write_text(upload_contents, encoding="utf-8")
        update_path.write_text(updated_contents, encoding="utf-8")

        remote = f"//{server}/{share_name}"
        results: list[CheckResult] = []
        target_host = f"{server} via {ip_address}" if ip_address else server
        target = f"{username}@{target_host}/{share_name}"

        def run_step(timeout_prefix: str, commands: list[str]) -> tuple[subprocess.CompletedProcess[str] | None, list[CheckResult] | None]:
            try:
                return run_share_commands(remote, commands), None
            except subprocess.TimeoutExpired:
                return None, results + [CheckResult("FAIL", f"{timeout_prefix} timed out for {target}")]

        proc, timeout_results = run_step("SMB directory create", [f'mkdir "{test_dir_name}"'])
        if timeout_results is not None:
            return timeout_results
        assert proc is not None
        if proc.returncode != 0:
            return [CheckResult("FAIL", f"SMB directory create failed: {((proc.stderr or proc.stdout).strip().splitlines() or [f'failed with rc={proc.returncode}'])[-1]}")]
        results.append(CheckResult("PASS", f"SMB directory create works for {target}"))

        proc, timeout_results = run_step("SMB file create", [f'cd "{test_dir_name}"', f'put "{upload_path}" "{upload_name}"'])
        if timeout_results is not None:
            return timeout_results
        assert proc is not None
        if proc.returncode != 0:
            return fail_result("SMB file create failed", proc)
        results.append(CheckResult("PASS", f"SMB file create works for {target}"))

        proc, timeout_results = run_step("SMB file overwrite/edit", [f'cd "{test_dir_name}"', f'put "{update_path}" "{upload_name}"'])
        if timeout_results is not None:
            return timeout_results
        assert proc is not None
        if proc.returncode != 0:
            return fail_result("SMB file overwrite/edit failed", proc)
        results.append(CheckResult("PASS", f"SMB file overwrite/edit works for {target}"))

        proc, timeout_results = run_step(
            "SMB file read",
            [f'cd "{test_dir_name}"', f'get "{upload_name}" "{readback_path}"'],
        )
        if timeout_results is not None:
            return timeout_results
        assert proc is not None
        if proc.returncode != 0:
            return fail_result("SMB file read failed", proc)
        if not readback_path.exists():
            return results + [CheckResult("FAIL", "SMB file read failed: downloaded file missing after get")]
        if readback_path.read_text(encoding="utf-8") != updated_contents:
            return results + [CheckResult("FAIL", "SMB file read failed: downloaded contents did not match overwritten contents")]
        results.append(CheckResult("PASS", f"SMB file read works for {target}"))

        proc, timeout_results = run_step(
            "SMB file rename",
            [f'cd "{test_dir_name}"', f'rename "{upload_name}" "{renamed_name}"'],
        )
        if timeout_results is not None:
            return timeout_results
        assert proc is not None
        if proc.returncode != 0:
            return fail_result("SMB file rename failed", proc)
        results.append(CheckResult("PASS", f"SMB file rename works for {target}"))

        proc, timeout_results = run_step(
            "SMB file copy",
            [
                f'cd "{test_dir_name}"',
                f'get "{renamed_name}" "{copy_source_path}"',
                f'put "{copy_source_path}" "{copy_name}"',
                f'get "{copy_name}" "{copy_readback_path}"',
            ],
        )
        if timeout_results is not None:
            return timeout_results
        assert proc is not None
        if proc.returncode != 0:
            return fail_result("SMB file copy failed", proc)
        if not copy_readback_path.exists():
            return results + [CheckResult("FAIL", "SMB file copy failed: copied file missing after get")]
        if copy_readback_path.read_text(encoding="utf-8") != updated_contents:
            return results + [CheckResult("FAIL", "SMB file copy failed: copied file contents did not match source")]
        results.append(CheckResult("PASS", f"SMB file copy works for {target}"))

        proc, timeout_results = run_step("SMB file delete", [f'cd "{test_dir_name}"', f'del "{copy_name}"', "ls"])
        if timeout_results is not None:
            return timeout_results
        assert proc is not None
        if proc.returncode != 0:
            return fail_result("SMB file delete failed", proc)
        ls_after_delete = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if any(copy_name in line for line in ls_after_delete):
            return results + [CheckResult("FAIL", f"SMB file delete failed: ls output still contained {copy_name!r}")]
        results.append(CheckResult("PASS", f"SMB file delete works for {target}"))

        if not any(renamed_name in line for line in ls_after_delete):
            return results + [CheckResult("FAIL", f"SMB directory ls list failed: ls output did not contain {renamed_name!r}")]
        results.append(CheckResult("PASS", f"SMB directory ls list works for {target}"))

        proc, timeout_results = run_step(
            "SMB directory delete",
            [f'cd "{test_dir_name}"', f'del "{renamed_name}"', 'cd ".."', f'rmdir "{test_dir_name}"'],
        )
        if timeout_results is not None:
            return timeout_results
        assert proc is not None
        if proc.returncode != 0:
            return fail_result("SMB directory delete failed", proc)
        results.append(CheckResult("PASS", f"SMB directory delete works for {target}"))

        proc, timeout_results = run_step("SMB final cleanup check", ["ls"])
        if timeout_results is not None:
            return timeout_results
        assert proc is not None
        if proc.returncode != 0:
            return fail_result("SMB final cleanup check failed", proc)
        final_ls = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if any(test_dir_name in line for line in final_ls):
            return results + [CheckResult("FAIL", f"SMB final cleanup check failed: share still contained {test_dir_name!r}")]
        results.append(CheckResult("PASS", f"SMB final cleanup check passed for {target}"))
        return results
