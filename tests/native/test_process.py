"""Real fork/exec/pipe/signal behavior for the appliance's C supervisor."""
import subprocess

import pytest

from tests.native.build import ROOT, compile_modules


@pytest.fixture(scope="module")
def driver(tmp_path_factory):
    binary = tmp_path_factory.mktemp("process-native") / "process"
    compile_modules(binary, ("native/common/process.c", "native/common/parent.c"),
                    flags=("-I", str(ROOT / "build/native"), "-Dsetpgid=tc_test_setpgid"),
                    extra_sources=(ROOT / "tests/native/unit/test_process.c",))
    return binary


@pytest.mark.parametrize("case", ["lifetime", "group", "capture", "overflow", "exec_failure", "orphan", "stop", "drain",
                                  "term_before_reset", "wait_until"])
def test_owned_process_lifecycle(driver, case):
    # Isolate the regression driver as well as its owned children: a failing
    # process-group test must never signal pytest or the user's terminal.
    result = subprocess.run([str(driver), case], capture_output=True, text=True,
                            timeout=20, start_new_session=True)
    assert result.returncode == 0, result.stderr
