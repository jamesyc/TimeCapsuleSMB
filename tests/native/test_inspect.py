"""Process ownership from the manager's process table, and listener checks using Apple's fstat layout."""
import subprocess
from tests.native.build import ROOT, compile_modules


def test_process_and_listener_observations(tmp_path):
    binary = tmp_path / "inspect"
    modules = ["native/service/inspect.c", "native/service/proctable.c", "native/common/worker.c", "native/common/process.c",
               "native/common/parent.c", "native/common/acp.c"]
    compile_modules(binary, modules, flags=("-I", str(ROOT / "build/native")),
                    extra_sources=(ROOT / "tests/native/unit/test_inspect.c",))
    subprocess.run([str(binary)], check=True, timeout=5)
