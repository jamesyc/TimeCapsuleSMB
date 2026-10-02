"""Buffer-cache stall decisions (kern/60584 recovery) without a kernel."""
import subprocess
from tests.native.build import ROOT, compile_modules


def test_bufstall_decisions_and_fixture(tmp_path):
    binary = tmp_path / "bufstall"
    compile_modules(binary, ["native/service/bufstall.c"], flags=("-I", str(ROOT / "build/native")),
                    extra_sources=(ROOT / "tests/native/unit/test_bufstall.c",))
    run = subprocess.run([str(binary), str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
