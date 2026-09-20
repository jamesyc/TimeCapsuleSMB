"""Fake-clock topology transitions use absolute, nonblocking confirmation deadlines."""
import subprocess
from tests.native.build import ROOT, instrumentation_flags


def test_storage_confirmation(tmp_path):
    binary = tmp_path / "settle"
    subprocess.run(["cc", "-Wall", "-Wextra", "-Werror", *instrumentation_flags(),
                    "-I", str(ROOT / "build/native"), str(ROOT / "build/native/storage/mast.c"),
                    str(ROOT / "build/native/storage/settle.c"),
                    str(ROOT / "tests/native/unit/test_storage_settle.c"), "-o", str(binary)],
                   check=True, capture_output=True)
    subprocess.run([str(binary)], check=True, timeout=5)
