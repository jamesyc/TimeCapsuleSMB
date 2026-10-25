"""Native Windows must exit before CLI imports and side effects."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
MESSAGE = "TimeCapsuleSMB supports only macOS and Linux. Run this command on a Mac or Linux computer."
ENTRIES = {
    "source": "runpy.run_path(sys.argv[0], run_name='__main__')",
    "installed": "from timecapsulesmb.cli.main import main; raise SystemExit(main())",
    "module": "runpy.run_module('timecapsulesmb.cli.main', run_name='__main__')",
}


def invoke(entry, platform, args, cwd, *, block_cli=False):
    script = """
import runpy, sys
sys.dont_write_bytecode = True
sys.platform = sys.argv.pop(1)
sys.argv = sys.argv[1:]
"""
    if block_cli:
        script += """
class BlockCLI:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('timecapsulesmb.') and fullname not in ('timecapsulesmb.cli', 'timecapsulesmb.cli.main'):
            raise AssertionError('Windows must exit before importing ' + fullname)
sys.meta_path.insert(0, BlockCLI())
def block_side_effects(event, args):
    if event.startswith(('socket.', 'subprocess.')) or event in ('os.exec', 'os.posix_spawn', 'os.system'):
        raise AssertionError('Windows must exit before ' + event)
sys.addaudithook(block_side_effects)
"""
    return subprocess.run(
        [sys.executable, "-c", script + ENTRIES[entry], platform, str(ROOT / "tcapsule"), *args],
        cwd=cwd, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True, text=True, timeout=20,
    )


def assert_rejected(result, cwd):
    assert (result.returncode, result.stdout, result.stderr) == (1, "", MESSAGE + "\n")
    assert list(cwd.iterdir()) == []


@pytest.mark.parametrize("entry", ENTRIES)
def test_windows_exits_before_cli_imports_or_side_effects(entry, tmp_path):
    assert_rejected(invoke(entry, "win32", ["deploy"], tmp_path, block_cli=True), tmp_path)


# The source launcher keeps help and bootstrap in this interpreter and hands
# every other command to the venv's launcher; neither path may run first.
@pytest.mark.parametrize("args", [[], ["--help"], ["bootstrap"]])
def test_source_launcher_exits_before_its_handoff_choice(args, tmp_path):
    assert_rejected(invoke("source", "win32", args, tmp_path, block_cli=True), tmp_path)


# Only native Windows is turned away; other POSIX platforms are left to try.
# The host's own platform stands in for macOS and Linux: claiming the other
# one breaks the standard library (urllib loads macOS's _scproxy for darwin).
@pytest.mark.parametrize("platform", [sys.platform, "freebsd14"])
@pytest.mark.parametrize("entry", ENTRIES)
def test_other_platforms_still_reach_cli_help(entry, platform, tmp_path):
    result = invoke(entry, platform, ["--help"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert "TimeCapsuleSMB command line interface" in result.stdout
    assert result.stderr == ""
