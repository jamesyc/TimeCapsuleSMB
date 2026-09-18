"""Production helpers retain diagnostics but cannot load test facts files."""
import subprocess

import pytest

from tests.native.build import compile_native


@pytest.mark.parametrize("target,diagnostic,expected_status", [
    ("discovery", "--print-link-plan", 0),
    ("service", "--print-link-plan", 0),
])
def test_production_rejects_fixture_input_and_keeps_live_diagnostics(
    tmp_path, target, diagnostic, expected_status,
):
    binary = compile_native(target, tmp_path / target, flags=[
        "-UTC_NATIVE_TEST",
        # Exercise live collection's unavailable-ACP path without host tools.
        '-DTC_ACP_PATH="/nonexistent/tc-test-acp"',
    ])

    def run(*args):
        return subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=10)

    assert run("--version").returncode == 0
    rejected = run(diagnostic, "--facts-file", str(tmp_path / "missing"))
    assert rejected.returncode == (2 if target == "nbns" else 3) and "Usage:" in rejected.stderr
    live = run(diagnostic)
    assert live.returncode == expected_status, live.stderr
    if target != "nbns":
        assert live.stdout.startswith("plan: status=cold-start reason=")
        assert "mode=unknown" in live.stdout
    if target == "service":
        bind = run("--print-smb-bind-interfaces")
        assert bind.returncode == 0
        tokens, status = bind.stdout.splitlines()
        assert tokens == "127.0.0.1/8 ::1/128"
        assert status in {"status=incomplete reason=mode", "status=incomplete reason=iflist"}

    symbols = subprocess.run(["nm", str(binary)], capture_output=True, text=True, check=True).stdout
    assert "device_facts_parse_file" not in symbols
    assert "device_plan_collect_from_file" not in symbols
