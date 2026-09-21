"""Exercise setup failures and event wakeups through their actual OS effects."""
import subprocess

import pytest
from tests.native.build import ROOT, compile_modules


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    directory = tmp_path_factory.mktemp("worker-native")
    for name, modules in {
        "worker": ["worker", "process", "parent", "acp"],
        "events": ["events"],
    }.items():
        compile_modules(directory / name, tuple(f"native/common/{module}.c" for module in modules),
                        flags=("-I", str(ROOT / "build/native")),
                        extra_sources=(ROOT / f"tests/native/unit/test_{name}.c",))
    return directory


@pytest.mark.parametrize("case", ["copy", "faults", "directories", "commands", "cancel"])
def test_setup_worker(tools, tmp_path, case):
    result = subprocess.run([str(tools / "worker"), case], cwd=tmp_path,
                            capture_output=True, timeout=20, start_new_session=True)
    assert result.returncode == 0, result.stderr


def test_signal_wakeup_and_optional_device_events(tools):
    result = subprocess.run([str(tools / "events")], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
