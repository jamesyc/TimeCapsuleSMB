from __future__ import annotations

import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.transport.local import command_exists, run_local_capture


def _smbclient_base_args() -> list[str]:
    return ["smbclient", "-s", "/dev/null"]


def _run_smbclient_listing(
    server: str,
    username: str,
    password: str,
    *,
    port: Optional[int] = None,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    args = _smbclient_base_args() + ["-g"]
    if port is not None:
        args += ["-p", str(port)]
    return run_local_capture(
        args + ["-L", f"//{server}", "-U", f"{username}%{password}"],
        timeout=timeout,
    )


def check_authenticated_smb_listing(
    username: str,
    password: str,
    server: str | list[str],
    *,
    expected_share_name: Optional[str] = None,
    port: Optional[int] = None,
    timeout: int = 20,
) -> CheckResult:
    if not command_exists("smbclient"):
        return CheckResult("FAIL", "missing local tool smbclient")
    if isinstance(server, list):
        return try_authenticated_smb_listing(
            username,
            password,
            server,
            expected_share_name=expected_share_name,
            port=port,
            timeout=timeout,
        )

    try:
        proc = _run_smbclient_listing(server, username, password, port=port, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CheckResult("FAIL", f"authenticated SMB listing failed: timed out via {server}")
    if proc.returncode == 0:
        if expected_share_name is not None and expected_share_name not in proc.stdout:
            return CheckResult(
                "FAIL",
                f"authenticated SMB listing did not include expected share {expected_share_name!r} on {server}",
            )
        return CheckResult("PASS", f"authenticated SMB listing works for {username}@{server}")
    detail = (proc.stderr or proc.stdout).strip().splitlines()
    msg = detail[-1] if detail else f"failed with rc={proc.returncode}"
    return CheckResult("FAIL", f"authenticated SMB listing failed: {msg}")


def try_authenticated_smb_listing(
    username: str,
    password: str,
    servers: list[str],
    *,
    expected_share_name: Optional[str] = None,
    port: Optional[int] = None,
    timeout: int = 12,
) -> CheckResult:
    if not command_exists("smbclient"):
        return CheckResult("WARN", "SMB listing verification skipped: smbclient not found")

    failure_msg = "not attempted"
    for server in servers:
        try:
            proc = _run_smbclient_listing(server, username, password, port=port, timeout=timeout)
        except subprocess.TimeoutExpired:
            failure_msg = f"timed out via {server}"
            continue
        if proc.returncode == 0:
            if expected_share_name is not None and expected_share_name not in proc.stdout:
                failure_msg = f"expected share {expected_share_name!r} not found via {server}"
                continue
            return CheckResult("PASS", f"authenticated SMB listing works for {username}@{server}")
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        failure_msg = detail[-1] if detail else f"failed with rc={proc.returncode} via {server}"
    return CheckResult("FAIL", f"authenticated SMB listing failed: {failure_msg}")


def check_authenticated_smb_file_ops_detailed(
    username: str,
    password: str,
    server: str,
    share_name: str,
    *,
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
        return run_local_capture(
            _smbclient_base_args()
            + ([ "-p", str(port) ] if port is not None else [])
            + [remote, "-U", f"{username}%{password}", "-c", "; ".join(commands)],
            timeout=timeout,
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
        target = f"{username}@{server}/{share_name}"

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
