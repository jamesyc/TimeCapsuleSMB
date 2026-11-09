"""What the manager reads from one kernel process table (service/procfacts.c)."""
import subprocess
from tests.native.build import ROOT, compile_modules


def test_proc_facts(tmp_path):
    binary = tmp_path / "procfacts"
    compile_modules(binary, ("native/service/procfacts.c", "native/service/proctable.c"),
                    flags=("-I", str(ROOT / "build/native")),
                    extra_sources=(ROOT / "tests/native/unit/test_procfacts.c",))
    subprocess.run([str(binary)], check=True, timeout=5)
