"""The foreign-process stopper (service/stopper.c), on a simulated clock."""
import subprocess
from tests.native.build import ROOT, compile_modules


def test_stopper(tmp_path):
    binary = tmp_path / "stopper"
    compile_modules(binary, ("native/service/stopper.c",),
                    flags=("-I", str(ROOT / "build/native")),
                    extra_sources=(ROOT / "tests/native/unit/test_stopper.c",))
    subprocess.run([str(binary)], check=True, timeout=5)
