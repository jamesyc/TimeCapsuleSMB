"""Stuck-process decisions (service/stuck.c) without a kernel."""
import subprocess
from tests.native.build import ROOT, compile_modules


def test_stuck_decisions_and_fixture(tmp_path):
    binary = tmp_path / "stuck"
    compile_modules(binary, ["native/service/stuck.c", "native/service/proctable.c"], flags=("-I", str(ROOT / "build/native")),
                    extra_sources=(ROOT / "tests/native/unit/test_stuck.c",))
    run = subprocess.run([str(binary), str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
