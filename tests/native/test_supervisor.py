import os
import signal
import subprocess
import time
from pathlib import Path

from tests.native.build import compile_native


def _build_supervisor(tmp_path: Path, *, receipt: Path | None = None):
    state = tmp_path / "state"
    socket_path = Path("/tmp") / f"tc-service-{abs(hash(str(tmp_path))) & 0xffffffff:x}.sock"
    config = tmp_path / "tcapsulesmb.conf"
    config.write_text("NBNS_ENABLED=0\nTELEMETRY=false\n")
    receipt = receipt or tmp_path / "missing-receipt"
    binary = compile_native(
        "service",
        tmp_path / "service",
        flags=[
            f'-DTC_SERVICE_STATE_DIR="{state}"',
            f'-DTC_SERVICE_LOCK_PATH="{state / "service.lock"}"',
            f'-DTC_SERVICE_SOCKET_PATH="{socket_path}"',
            f'-DTC_FLASH_CONFIG_PATH="{config}"',
            f'-DTC_XATTR_RECEIPT_PATH="{receipt}"',
            '-DTC_ACP_PATH="/nonexistent/tc-test-acp"',
        ],
    )
    return binary, socket_path


def _status(binary: Path):
    result = subprocess.run([str(binary), "status"], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    roles = {}
    for line in result.stdout.splitlines():
        fields = dict(item.split("=", 1) for item in line.split() if "=" in item)
        if "role" in fields:
            roles[fields["role"]] = fields
    return roles


def _wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition did not become true")


def _ready_roles(binary: Path):
    current = _status(binary)
    return current if current.get("mdns", {}).get("state") == "ready" else None


def test_supervisor_is_singleton_and_restarts_only_failed_role(tmp_path):
    binary, socket_path = _build_supervisor(tmp_path)
    supervisor = subprocess.Popen([str(binary), "run"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _wait_until(socket_path.exists)
        roles = _wait_until(lambda: _ready_roles(binary))
        assert roles["netbios"]["state"] == "disabled"
        assert roles["telemetry"]["state"] == "disabled"
        old_pid = int(roles["mdns"]["pid"])

        duplicate = subprocess.run([str(binary), "run"], capture_output=True, text=True, timeout=5)
        assert duplicate.returncode != 0

        os.kill(old_pid, signal.SIGKILL)
        restarted = _wait_until(
            lambda: (current := _status(binary)).get("mdns", {}).get("state") == "ready"
            and int(current["mdns"]["pid"]) != old_pid
            and current,
        )
        assert restarted["netbios"]["state"] == "disabled"
        assert restarted["telemetry"]["state"] == "disabled"

        stopped = subprocess.run([str(binary), "stop"], capture_output=True, text=True, timeout=5)
        assert stopped.returncode == 0
        assert supervisor.wait(timeout=10) == 0
    finally:
        if supervisor.poll() is None:
            supervisor.terminate()
            supervisor.wait(timeout=10)


def test_supervisor_rejects_incomplete_migration_receipt(tmp_path):
    receipt = tmp_path / "receipt"
    receipt.write_text("format=1\nmigration=1\nstate=incomplete\n")
    binary, socket_path = _build_supervisor(tmp_path, receipt=receipt)
    result = subprocess.run([str(binary), "run"], capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert "incomplete or invalid" in (tmp_path / "state/service.log").read_text()
    assert not socket_path.exists()


def test_worker_exits_when_supervisor_control_channel_closes(tmp_path):
    binary, socket_path = _build_supervisor(tmp_path)
    supervisor = subprocess.Popen([str(binary), "run"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _wait_until(socket_path.exists)
    roles = _wait_until(lambda: _ready_roles(binary))
    worker_pid = int(roles["mdns"]["pid"])
    supervisor.kill()
    supervisor.wait(timeout=5)

    def worker_gone():
        try:
            os.kill(worker_pid, 0)
        except ProcessLookupError:
            return True
        return False

    _wait_until(worker_gone)
