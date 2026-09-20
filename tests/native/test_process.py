"""Real fork/exec/pipe/signal behavior for the appliance's C supervisor."""
import subprocess

import pytest

from tests.native.build import ROOT, instrumentation_flags


@pytest.fixture(scope="module")
def driver(tmp_path_factory):
    binary = tmp_path_factory.mktemp("process-native") / "process"
    subprocess.run(["cc", "-D_GNU_SOURCE", "-Wall", "-Wextra", "-Werror",
                    *instrumentation_flags(), "-I", str(ROOT / "build/native"),
                    str(ROOT / "build/native/common/process.c"),
                    str(ROOT / "build/native/common/parent.c"),
                    str(ROOT / "tests/native/unit/test_process.c"), "-o", str(binary)],
                   capture_output=True, check=True)
    return binary


@pytest.mark.parametrize("case", ["lifetime", "group", "capture", "overflow", "exec_failure", "orphan", "stop", "drain"])
def test_owned_process_lifecycle(driver, case):
    # Isolate the regression driver as well as its owned children: a failing
    # process-group test must never signal pytest or the user's terminal.
    result = subprocess.run([str(driver), case], capture_output=True, text=True,
                            timeout=20, start_new_session=True)
    assert result.returncode == 0, result.stderr
