"""Fake-clock topology transitions use absolute, nonblocking confirmation deadlines."""
import subprocess
from tests.native.build import ROOT, compile_modules


def test_storage_confirmation(tmp_path):
    binary = tmp_path / "settle"
    compile_modules(binary, ("native/storage/mast.c", "native/storage/settle.c"),
                    flags=("-I", str(ROOT / "build/native")),
                    extra_sources=(ROOT / "tests/native/unit/test_storage_settle.c",))
    subprocess.run([str(binary)], check=True, timeout=5)
