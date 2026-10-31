from __future__ import annotations

import contextlib
import fcntl
import functools
import ipaddress
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Union

from timecapsulesmb.checks import smb_macos
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.transport.local import find_command, run_local_capture

SMBCLIENT_DEBUG_TEXT_LIMIT = 1000


@dataclass(frozen=True)
class LocalSmbClient:
    """The SMB client doctor runs on this computer, the one place that says
    which program it is, where, and whether it works."""

    kind: str  # "macos" (Apple's client) or "smbclient"
    label: str
    tool: str
    path: str | None  # absolute; None when the client is missing
    # How it reports a connection macOS refused, which is what a missing
    # Local Network permission looks like.
    unreachable: str
    # The self-test behind problem(); it runs only when the client is there.
    self_test: Callable[[], Optional[str]] = field(compare=False, repr=False)

    def missing_message(self) -> str:
        if self.kind == "macos":
            return f"missing local tool {self.tool} ({smb_macos.SMBUTIL}), which comes with macOS"
        return f"missing local tool {self.tool}, please install {self.tool} on your computer"

    def problem(self) -> str | None:
        """Why the client is there but cannot work, or None (also when missing)."""
        return None if self.path is None else self.self_test()

    def unusable(self) -> str | None:
        """How the client keeps doctor's SMB checks from running, said after
        its label, or None when it can run them."""
        if self.path is None:
            return "is missing on this computer"
        if self.problem() is not None:
            return "on this computer does not work"
        return None


def local_smb_client() -> LocalSmbClient:
    """Apple's own client, the one Time Machine uses, on a Mac, and smbclient
    everywhere else."""
    if sys.platform == "darwin":
        return LocalSmbClient(
            kind="macos",
            label="Apple's SMB client",
            tool="smbutil",
            path=smb_macos.SMBUTIL if os.access(smb_macos.SMBUTIL, os.X_OK) else None,
            unreachable="No route to host",
            self_test=smb_macos.apple_smb_client_problem,
        )
    path = find_command("smbclient")
    return LocalSmbClient(
        kind="smbclient",
        label="smbclient",
        tool="smbclient",
        path=path,
        unreachable="NT_STATUS_HOST_UNREACHABLE",
        self_test=functools.partial(broken_smbclient_output, path or ""),
    )


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


# Share types `smbclient -g` prints in its share table; the workgroup listing
# that can follow adds Server and Workgroup rows.
SMBCLIENT_SHARE_TYPES = frozenset({"Disk", "IPC", "Printer", "Device"})


def parse_smbclient_disk_shares(stdout: str) -> list[str] | None:
    """The disk shares in `smbclient -g -L` output, or None when no share
    table came back.

    The table is the only proof the listing worked. `-L`'s exit status comes
    from the workgroup step after it (1 when the server name has no IPv4
    address), whether or not the table came back. Samba always lists IPC$, so
    a listed server has at least one row.
    """
    rows = [line.strip().split("|", 2) for line in stdout.splitlines()]
    rows = [row for row in rows if len(row) == 3 and row[0] in SMBCLIENT_SHARE_TYPES]
    if not rows:
        return None
    return list(dict.fromkeys(name.strip() for kind, name, _ in rows if kind == "Disk" and name.strip()))


def _normalize_smb_client_target(target: SmbClientTargetInput) -> SmbClientTarget:
    if isinstance(target, SmbClientTarget):
        return target
    try:
        ip = str(ipaddress.ip_address(target))
    except ValueError:
        ip = None
    return SmbClientTarget(target, ip)


def _smbclient_base_args(smbclient: str) -> list[str]:
    # The program the self-test checked, by its path, is the one that runs.
    return [smbclient, "-s", "/dev/null"]


def _smbclient_env() -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
        "HOME": os.environ.get("HOME", ""),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": os.environ.get("LANG", "C"),
        "LC_ALL": os.environ.get("LC_ALL", os.environ.get("LANG", "C")),
    }
    return {key: value for key, value in env.items() if value}


# Looks up a reserved name in lmhosts only, so no packet leaves this computer,
# yet smbclient sets up its connection machinery before the lookup fails.
SMBCLIENT_SELF_TEST_ARGS = ("-N", "--option=name resolve order=lmhosts", "-L", "//tcapsule-selftest.invalid")
SMBCLIENT_SELF_TEST_TIMEOUT_SECONDS = 10


@functools.cache
def broken_smbclient_output(smbclient: str) -> str | None:
    """What the smbclient at this path printed when it ran but did not work,
    or None when it works or did not finish in time.

    A working smbclient reports an NT_STATUS for the failed lookup; one that
    crashes or cannot start reports none. A timeout proves nothing, so it does
    not count against the tool."""
    try:
        proc = run_local_capture(
            _smbclient_base_args(smbclient) + list(SMBCLIENT_SELF_TEST_ARGS),
            timeout=SMBCLIENT_SELF_TEST_TIMEOUT_SECONDS,
            env=_smbclient_env(),
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError as exc:
        return str(exc)
    output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    if "NT_STATUS_" in output:
        return None
    return _smbclient_text_tail(output) or f"exited with rc={proc.returncode}"


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


def _smbclient_proc_failure_detail(proc: subprocess.CompletedProcess[str]) -> tuple[str, dict[str, object]]:
    # smbclient prints NT_STATUS failures on stdout while libsmb noise lands on
    # stderr (e.g. the misleading SMB1-fallback "smb1cli_req_writev_submit" line
    # emitted after the server already rejected the SMB2 call), so summarizing
    # from one stream's last line can bury the real error. Prefer the first
    # NT_STATUS line across both streams and keep full tails in details.
    stdout_tail = _smbclient_text_tail(proc.stdout)
    stderr_tail = _smbclient_text_tail(proc.stderr)
    lines: list[str] = []
    for text in (stdout_tail, stderr_tail):
        if text:
            lines.extend(line.strip() for line in text.splitlines() if line.strip())
    nt_status_lines = [line for line in lines if "NT_STATUS_" in line]
    if nt_status_lines:
        message = nt_status_lines[0]
    elif lines:
        message = lines[-1]
    else:
        message = f"failed with rc={proc.returncode}"
    details: dict[str, object] = {"returncode": proc.returncode}
    if stdout_tail:
        details["stdout_tail"] = stdout_tail
    if stderr_tail:
        details["stderr_tail"] = stderr_tail
    return message, details


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
        # The same failures in errno text, as Apple's client reports them.
        "Network is unreachable",
        "Connection refused",
        "Connection reset by peer",
        "Operation timed out",
    )
    for key in ("failure", "stderr_tail", "stdout_tail"):
        value = attempt.get(key)
        if isinstance(value, str) and any(fragment in value for fragment in retryable_fragments):
            return True
    return False


def authenticated_smb_listing_attempts(result: CheckResult) -> list[dict[str, object]]:
    attempts = result.details.get("attempts")
    if not isinstance(attempts, list):
        return []
    return [attempt for attempt in attempts if isinstance(attempt, dict)]


def authenticated_smb_listing_retryable(result: CheckResult) -> bool:
    if result.status == "PASS":
        return False
    attempts = authenticated_smb_listing_attempts(result)
    return bool(attempts) and all(_smbclient_attempt_retryable(attempt) for attempt in attempts)


def authenticated_smb_listing_with_attempts(result: CheckResult, attempts: list[dict[str, object]]) -> CheckResult:
    if not attempts:
        return result
    details = dict(result.details)
    details["attempts"] = attempts
    if result.status == "PASS":
        return CheckResult(result.status, result.message, details)
    failure_msg = _smbclient_attempts_failure_summary(attempts)
    return CheckResult(result.status, f"authenticated SMB listing failed after {len(attempts)} attempt(s): {failure_msg}", details)


def _smbclient_listing_args(
    smbclient: str,
    target: SmbClientTarget,
    username: str,
    password: str | None,
    *,
    port: Optional[int] = None,
) -> list[str]:
    """None writes *** for the password, to show the command."""
    args = _smbclient_base_args(smbclient) + ["-g"]
    if port is not None:
        args += ["-p", str(port)]
    if target.ip_address is not None:
        args += ["-I", target.ip_address]
    return args + ["-L", f"//{target.server}", "-U", f"{username}%{'***' if password is None else password}"]


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


def check_authenticated_smb_listing(
    username: str,
    password: str,
    server: SmbClientTargetInput | list[SmbClientTargetInput],
    *,
    port: Optional[int] = None,
    timeout: int = 20,
) -> CheckResult:
    """List the shares of the first server that answers, trying each in turn."""
    client = local_smb_client()
    if client.path is None:
        return CheckResult("FAIL", client.missing_message())
    smbclient = client.path
    if client.kind == "macos":
        # Apple's client lists through the address doctor connects to.
        def listing_args(target: SmbClientTarget, secret: str | None) -> list[str]:
            return smb_macos.view_args(target.ip_address or target.server, username, secret, port=port)

        def run(args: list[str]) -> subprocess.CompletedProcess[str]:
            return smb_macos.run_view(args, timeout=timeout)

        parse, failure_text = smb_macos.parse_smbutil_disk_shares, _apple_failure_text
    else:
        def listing_args(target: SmbClientTarget, secret: str | None) -> list[str]:
            return _smbclient_listing_args(smbclient, target, username, secret, port=port)

        def run(args: list[str]) -> subprocess.CompletedProcess[str]:
            return run_local_capture(args, timeout=timeout, env=_smbclient_env())

        parse, failure_text = parse_smbclient_disk_shares, lambda proc: _smbclient_proc_failure_detail(proc)[0]
    attempts: list[dict[str, object]] = []
    for target in map(_normalize_smb_client_target, server if isinstance(server, list) else [server]):
        command = shlex.join(listing_args(target, None))
        start = time.monotonic()
        try:
            proc = run(listing_args(target, password))
        except subprocess.TimeoutExpired as exc:
            attempt = _new_listing_attempt(target, timeout, start, command)
            attempt["outcome"] = "timeout"
            _record_listing_output(attempt, exc.stdout, exc.stderr)
            attempts.append(attempt)
            continue
        except OSError as exc:
            # The client went away, or cannot start, after the lookup found it.
            attempt = _new_listing_attempt(target, timeout, start, command)
            attempt["outcome"] = "error"
            attempt["failure"] = f"{smbclient} could not run: {exc}"
            attempts.append(attempt)
            continue
        attempt = _new_listing_attempt(target, timeout, start, command)
        attempt["returncode"] = proc.returncode
        _record_listing_output(attempt, proc.stdout, proc.stderr)
        attempts.append(attempt)
        disk_shares = parse(proc.stdout or "")
        if disk_shares is None:
            attempt["outcome"] = "error"
            attempt["failure"] = failure_text(proc)
            continue
        attempt["outcome"] = "pass"
        attempt["disk_shares"] = disk_shares
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
    failure_msg = _smbclient_attempts_failure_summary(attempts)
    return CheckResult("FAIL", f"authenticated SMB listing failed after {len(attempts)} attempt(s): {failure_msg}", {"attempts": attempts})


def _record_listing_output(attempt: dict[str, object], stdout: object, stderr: object) -> None:
    for key, value in (("stdout_tail", stdout), ("stderr_tail", stderr)):
        tail = _smbclient_text_tail(value)
        if tail is not None:
            attempt[key] = tail


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
    client = local_smb_client()
    if client.path is None:
        return [CheckResult("WARN", f"SMB file-ops verification skipped: {client.missing_message()}")]
    if client.kind == "macos":
        return _check_authenticated_smb_file_ops_macos(
            username, password, server, share_name, ip_address=ip_address, port=port, timeout=timeout,
        )
    smbclient = client.path

    test_dir_name = f".doctor-fileops-{uuid.uuid4().hex[:8]}"
    upload_name = ".sample.txt"
    renamed_name = ".sample-renamed.txt"
    copy_name = ".sample-copy.txt"

    def run_share_commands(remote: str, commands: list[str]) -> subprocess.CompletedProcess[str]:
        args = _smbclient_base_args(smbclient)
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
        msg, details = _smbclient_proc_failure_detail(proc)
        return results + [CheckResult("FAIL", f"{prefix}: {msg}", details)]

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
            except OSError as exc:
                return None, results + [CheckResult("FAIL", f"{timeout_prefix} failed: {smbclient} could not run: {exc}")]

        proc, timeout_results = run_step("SMB directory create", [f'mkdir "{test_dir_name}"'])
        if timeout_results is not None:
            return timeout_results
        assert proc is not None
        if proc.returncode != 0:
            msg, details = _smbclient_proc_failure_detail(proc)
            return [CheckResult("FAIL", f"SMB directory create failed: {msg}", details)]
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


def _apple_failure_text(proc: subprocess.CompletedProcess[str]) -> str:
    lines = [line.strip() for text in (proc.stderr or "", proc.stdout or "") for line in text.splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith("smbutil:")]
    if errors:
        return errors[0]
    return lines[-1] if lines else f"failed with rc={proc.returncode}"


def _read_uncached(path: Path) -> bytes:
    """Read the file from the device. After the file changed on the device, a
    plain read through the mount still returns this Mac's cached copy, while
    an F_NOCACHE read returns the device's. Bytes, so contents the device
    damaged fail the comparison instead of the decoding."""
    fd = os.open(path, os.O_RDONLY)
    try:
        if hasattr(fcntl, "F_NOCACHE"):  # macOS only; tests run it elsewhere too
            fcntl.fcntl(fd, fcntl.F_NOCACHE, 1)
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _smb_file_steps_on_mount(root: Path, target: str) -> list[CheckResult]:
    """smbclient's steps and messages, as file calls on a mounted share."""
    test_dir = root / f".doctor-fileops-{uuid.uuid4().hex[:8]}"
    upload = test_dir / ".sample.txt"
    renamed = test_dir / ".sample-renamed.txt"
    copy = test_dir / ".sample-copy.txt"
    upload_contents = b"line1\nline2\nline3\n"
    updated_contents = b"line1\nline2\nline3\nline4-updated\n"
    results: list[CheckResult] = []

    def failed(message: str) -> list[CheckResult]:
        # Leave nothing behind on the share; the failure is already known.
        shutil.rmtree(test_dir, ignore_errors=True)
        return results + [CheckResult("FAIL", message)]

    try:
        test_dir.mkdir()
    except OSError as exc:
        return [CheckResult("FAIL", f"SMB directory create failed: {exc}")]
    results.append(CheckResult("PASS", f"SMB directory create works for {target}"))

    for label in ("SMB file create", "SMB file overwrite/edit", "SMB file read", "SMB file rename", "SMB file copy"):
        try:
            if label == "SMB file create":
                upload.write_bytes(upload_contents)
            elif label == "SMB file overwrite/edit":
                upload.write_bytes(updated_contents)
            elif label == "SMB file read":
                if _read_uncached(upload) != updated_contents:
                    return failed("SMB file read failed: downloaded contents did not match overwritten contents")
            elif label == "SMB file rename":
                upload.rename(renamed)
            else:
                copy.write_bytes(_read_uncached(renamed))
                if _read_uncached(copy) != updated_contents:
                    return failed("SMB file copy failed: copied file contents did not match source")
        except OSError as exc:
            return failed(f"{label} failed: {exc}")
        results.append(CheckResult("PASS", f"{label} works for {target}"))

    try:
        copy.unlink()
        names = sorted(path.name for path in test_dir.iterdir())
    except OSError as exc:
        return failed(f"SMB file delete failed: {exc}")
    if copy.name in names:
        return failed(f"SMB file delete failed: ls output still contained {copy.name!r}")
    results.append(CheckResult("PASS", f"SMB file delete works for {target}"))
    if renamed.name not in names:
        return failed(f"SMB directory ls list failed: ls output did not contain {renamed.name!r}")
    results.append(CheckResult("PASS", f"SMB directory ls list works for {target}"))

    try:
        renamed.unlink()
        test_dir.rmdir()
    except OSError as exc:
        return failed(f"SMB directory delete failed: {exc}")
    results.append(CheckResult("PASS", f"SMB directory delete works for {target}"))

    try:
        leftover = any(path.name == test_dir.name for path in root.iterdir())
    except OSError as exc:
        return results + [CheckResult("FAIL", f"SMB final cleanup check failed: {exc}")]
    if leftover:
        return results + [CheckResult("FAIL", f"SMB final cleanup check failed: share still contained {test_dir.name!r}")]
    results.append(CheckResult("PASS", f"SMB final cleanup check passed for {target}"))
    return results


def _check_authenticated_smb_file_ops_macos(
    username: str,
    password: str,
    server: str,
    share_name: str,
    *,
    ip_address: str | None,
    port: Optional[int],
    timeout: int,
) -> list[CheckResult]:
    target_host = f"{server} via {ip_address}" if ip_address else server
    target = f"{username}@{target_host}/{share_name}"
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        smb_macos.unmount_stale_doctor_mounts()
    results: list[CheckResult] = []
    try:
        with smb_macos.mounted_share(
            ip_address or server, share_name, username, password, port=port, timeout=timeout,
        ) as root:
            results = _smb_file_steps_on_mount(root, target)
    except smb_macos.SmbMountTimeout:
        return [CheckResult("FAIL", f"SMB mount timed out for {target}")]
    except smb_macos.SmbMountError as exc:
        return [CheckResult("FAIL", f"SMB mount failed for {target}: {exc}", {"errno": exc.code})]
    except smb_macos.SmbUnmountError as exc:
        # The checks ran; the next doctor's sweep retries the unmount.
        results.append(CheckResult("WARN", f"SMB share stayed mounted after the checks for {target}: {exc}"))
    except (OSError, subprocess.SubprocessError) as exc:
        return results + [CheckResult("FAIL", f"SMB mount failed for {target}: {exc}")]
    return results
