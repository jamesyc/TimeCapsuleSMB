"""The production service keeps live diagnostics but omits test fixtures."""
import os
import subprocess
import time

from tests.native.build import compile_service


def test_production_service_rejects_fixtures_and_keeps_role_entrypoints(tmp_path):
    binary = compile_service(tmp_path / "service", flags=[
        "-UTC_NATIVE_TEST",
        # Exercise live collection's unavailable-ACP path without host tools.
        '-DTC_ACP_PATH="/nonexistent/tc-test-acp"',
    ])

    def run(*args):
        return subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=10)

    assert run("discovery", "--help").returncode == 0
    assert run("telemetry", "--version").returncode == 0
    assert run("--retain-policy").returncode == 3

    rejected = run("--print-link-plan", "--facts-file", str(tmp_path / "missing"))
    assert rejected.returncode == 3 and "Usage:" in rejected.stderr
    for removed in (("--version",), ("discovery", "--version"),
                    ("discovery", "--print-link-plan"), ("discovery", "--print-mast")):
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


def test_monotonic_helper_reads_the_kernel_monotonic_clock(tmp_path):
    # Doctor subtracts the manager title's started= from this reading, so it
    # must be the same clock: CLOCK_MONOTONIC, never the wall clock.
    binary = compile_service(tmp_path / "service", flags=[
        "-UTC_NATIVE_TEST",
        '-DTC_ACP_PATH="/nonexistent/tc-test-acp"',
    ])
    subprocess.run([str(binary), "--print-monotonic-ms"], capture_output=True, timeout=10)
    before = int(time.clock_gettime(time.CLOCK_MONOTONIC) * 1000)
    result = subprocess.run([str(binary), "--print-monotonic-ms"], capture_output=True, text=True, timeout=10)
    after = int(time.clock_gettime(time.CLOCK_MONOTONIC) * 1000)
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith("\n") and result.stdout.strip().isdigit()
    assert before - 1 <= int(result.stdout) <= after + 1

    extra = subprocess.run([str(binary), "--print-monotonic-ms", "extra"], capture_output=True, text=True, timeout=10)
    assert extra.returncode == 3 and extra.stdout == "" and "--print-monotonic-ms" in extra.stderr


def test_monotonic_helper_reports_a_closed_output(tmp_path):
    binary = compile_service(tmp_path / "service", flags=[
        "-UTC_NATIVE_TEST",
        '-DTC_ACP_PATH="/nonexistent/tc-test-acp"',
    ])
    reader, writer = os.pipe()
    os.close(reader)
    try:
        result = subprocess.run([str(binary), "--print-monotonic-ms"], stdout=writer, stderr=subprocess.PIPE,
                                timeout=10)
    finally:
        os.close(writer)
    assert result.returncode != 0
