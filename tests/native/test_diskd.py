"""Disk ownership transitions run against a simulated monotonic clock."""
import subprocess
from tests.native.build import ROOT, compile_modules


def test_diskd_handoff_transitions(tmp_path):
    binary = tmp_path / "diskd"
    compile_modules(binary, ("native/service/diskd.c",),
                    flags=("-I", str(ROOT / "build/native")),
                    extra_sources=(ROOT / "tests/native/unit/test_diskd.c",))
    subprocess.run([str(binary)], check=True, timeout=5)
