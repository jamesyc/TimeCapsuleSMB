"""The RAM image state machine on a fake clock, with the device's waits."""
import subprocess
from tests.native.build import ROOT, compile_modules


def test_image_transitions(tmp_path):
    binary = tmp_path / "image"
    compile_modules(binary, ("native/samba/image.c", "native/storage/settle.c", "native/storage/mast.c"),
                    flags=("-I", str(ROOT / "build/native")),
                    extra_sources=(ROOT / "tests/native/unit/test_image.c",))
    subprocess.run([str(binary)], check=True, timeout=5)
