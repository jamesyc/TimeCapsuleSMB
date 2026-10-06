"""The process table's host fixture (service/proctable.c)."""
import subprocess
from tests.native.build import ROOT, compile_modules


def test_proctable_fixture(tmp_path):
    binary = tmp_path / "proctable"
    compile_modules(binary, ["native/service/proctable.c"], flags=("-I", str(ROOT / "build/native")),
                    extra_sources=(ROOT / "tests/native/unit/test_proctable.c",))
    run = subprocess.run([str(binary), str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
