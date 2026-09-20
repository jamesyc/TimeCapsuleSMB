"""Process ownership and listener checks using Apple's actual ps/fstat layouts."""
import subprocess
from tests.native.build import ROOT, instrumentation_flags


def test_process_and_listener_observations(tmp_path):
    binary = tmp_path / "inspect"
    modules = ["service/inspect.c", "common/worker.c", "common/process.c", "common/parent.c", "common/acp.c"]
    subprocess.run(["cc", "-Wall", "-Wextra", "-Werror", *instrumentation_flags(),
                    "-I", str(ROOT / "build/native"),
                    *(str(ROOT / "build/native" / module) for module in modules),
                    str(ROOT / "tests/native/unit/test_inspect.c"), "-o", str(binary)],
                   check=True, capture_output=True)
    subprocess.run([str(binary)], check=True, timeout=5)
