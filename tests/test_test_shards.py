"""TC_TEST_SHARD in tests/conftest.py: CI jobs that split one suite run every
test exactly once between them."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def run(tmp_path):
    ran = tmp_path / "ran"
    ran.mkdir()
    probe = tmp_path / "test_probe.py"
    probe.write_text(f"""import os, pathlib, pytest
@pytest.mark.parametrize("n", range(20))
def test_case(n):
    pathlib.Path({str(ran)!r}, f"{{n}}-{{os.getpid()}}").touch()
""")

    def run(shard, *options):
        env = {k: v for k, v in os.environ.items() if k != "TC_TEST_SHARD"}
        env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
        if shard is not None:
            env["TC_TEST_SHARD"] = shard
        for path in ran.iterdir():
            path.unlink()
        result = subprocess.run([sys.executable, "-m", "pytest", "-p", "tests.conftest", "-p", "no:cacheprovider",
                                 "-q", "--rootdir", str(tmp_path), *options, str(probe)],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        return result, sorted(int(path.name.split("-")[0]) for path in ran.iterdir())

    return run


def test_shards_run_every_test_exactly_once_between_them(run):
    _, everything = run(None)
    assert everything == list(range(20))
    first, one = run("1/2")
    second, two = run("2/2")
    assert first.returncode == second.returncode == 0
    assert one and two and sorted(one + two) == everything
    assert f"{20 - len(one)} deselected" in first.stdout


def test_xdist_workers_agree_on_the_shard(run):
    _, alone = run("1/3")
    result, workers = run("1/3", "-n", "2")
    assert result.returncode == 0, result.stdout + result.stderr
    assert workers == alone


def test_one_shard_of_one_is_the_whole_suite(run):
    assert run("1/1")[1] == list(range(20))


@pytest.mark.parametrize("value", ["0/2", "3/2", "2", "a/b", "1/0"])
def test_a_bad_shard_is_refused_before_anything_runs(run, value):
    result, ran = run(value)
    assert result.returncode == pytest.ExitCode.USAGE_ERROR and not ran
    assert "TC_TEST_SHARD must be N/COUNT" in result.stderr
