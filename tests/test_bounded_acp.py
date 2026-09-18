"""Shell MaSt consumers use the real shared native collector, including deadlines."""
import os
from pathlib import Path
import shlex
import subprocess
import time

from tests.native.test_acp_capture import capture_tools, raw_env  # noqa: F401
from timecapsulesmb.deploy.boot_assets import load_boot_asset_text


def run_native_mast(tmp_path, capture_tools, script, env):
    library = tmp_path / "common.sh"
    library.write_text(load_boot_asset_text("common.sh"))
    return subprocess.run(["/bin/sh", "-c", f"""
set -eu
. {shlex.quote(str(library))}
TC_DISCOVERY_BIN={shlex.quote(str(capture_tools[2]))}
TC_ACP_QUERY_SECONDS=5
{script}
"""], env=env, text=True, capture_output=True, timeout=10)


def test_fast_acp_answer_is_returned_intact_with_its_exit_status(tmp_path, capture_tools):
    data = b"MaSt = (\n  item\n);"
    result = run_native_mast(tmp_path, capture_tools, 'out=$(tc_read_mast); printf "[%s]\\n" "$out"; tc_acp_mast_available', raw_env(tmp_path, data))
    assert result.returncode == 0 and result.stdout == "[" + data.decode() + "]\n"
    result = run_native_mast(tmp_path, capture_tools, 'if out=$(tc_read_mast); then exit 9; else echo "rc=$? out=[$out]"; fi', raw_env(tmp_path, b"untrusted", exit_code=3))
    assert result.returncode == 0 and result.stdout == "rc=3 out=[]\n"
    assert not list(tmp_path.glob("tc-bounded*"))


def test_hanging_acp_is_killed_at_the_bound_and_reported_unavailable(tmp_path, capture_tools):
    calls = tmp_path / "calls"
    env = {**os.environ, "TC_TEST_ACP_MODE": "ignore_term", "TC_TEST_ACP_KEY": "MaSt", "TC_TEST_ACP_CALLS": str(calls)}
    started = time.monotonic()
    result = run_native_mast(tmp_path, capture_tools, 'TC_ACP_QUERY_SECONDS=1\nif out=$(tc_read_mast); then exit 9; else echo "rc=$? out=[$out]"; fi\nif tc_acp_mast_available; then exit 9; else echo unavailable; fi', env)
    assert result.returncode == 0 and result.stdout == "rc=1 out=[]\nunavailable\n"
    assert time.monotonic() - started < 8
    assert "timed out" in result.stderr
    for line in calls.read_text().splitlines():
        pid = int(line.split()[1])
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError(f"collector {pid} survived cleanup")
    assert not list(tmp_path.glob("tc-bounded*"))
