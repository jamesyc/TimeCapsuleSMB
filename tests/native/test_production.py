"""The production service keeps live diagnostics but omits test fixtures."""
import subprocess

from tests.native.build import compile_service


def test_production_service_rejects_fixtures_and_keeps_role_entrypoints(tmp_path):
    binary = compile_service(tmp_path / "service", flags=[
        "-UTC_NATIVE_TEST",
        # Exercise live collection's unavailable-ACP path without host tools.
        '-DTC_ACP_PATH="/nonexistent/tc-test-acp"',
    ])

    def run(*args):
        return subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=10)

    assert run("--version").returncode == 0
    assert run("discovery", "--version").returncode == 0
    assert run("telemetry", "--version").returncode == 0
    assert run("--retain-policy").returncode == 3

    rejected = run("--print-link-plan", "--facts-file", str(tmp_path / "missing"))
    assert rejected.returncode == 3 and "Usage:" in rejected.stderr
    for removed in (("discovery", "--print-link-plan"), ("discovery", "--print-mast")):
        rejected = run(*removed)
        assert rejected.returncode == 3 and "Usage:" in rejected.stderr

    live = run("--print-link-plan")
    assert live.returncode == 0, live.stderr
    assert live.stdout.startswith("plan: status=cold-start reason=")
    assert "mode=unknown" in live.stdout
    assert run("--print-smb-bind-interfaces").returncode == 3

    symbols = subprocess.run(["nm", str(binary)], capture_output=True, text=True, check=True).stdout
    assert "device_facts_parse_file" not in symbols
    assert "device_plan_collect_from_file" not in symbols
