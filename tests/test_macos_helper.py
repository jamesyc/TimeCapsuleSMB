"""The macOS app's helper launcher (Sources/TimeCapsuleSMBHelper/main.c)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_SOURCE = REPO_ROOT / "macos" / "TimeCapsuleSMB" / "Sources" / "TimeCapsuleSMBHelper" / "main.c"
CC = shutil.which("cc")
FRAMEWORK_LIBRARY = Path("Resources/Python/Runtime/Python.framework/Versions/Current/Python")

# Stands in for the Python framework: prints what the helper handed
# Py_BytesMain and exits with FAKE_PYTHON_EXIT (0 when unset).
FAKE_PYTHON = r"""
#include <stdio.h>
#include <stdlib.h>
int Py_BytesMain(int argc, char **argv) {
    const char *exit_code = getenv("FAKE_PYTHON_EXIT");
    for (int i = 0; i < argc; i++) printf("arg=%s\n", argv[i]);
    printf("home=%s\n", getenv("PYTHONHOME"));
    return exit_code ? atoi(exit_code) : 0;
}
"""


@unittest.skipUnless(sys.platform == "darwin" and CC is not None, "needs macOS and a C compiler")
class MacosHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.contents = self.root / "Fake.app" / "Contents"
        self.helper = self.contents / "Helpers" / "tcapsule"
        self.helper.parent.mkdir(parents=True)
        subprocess.run([CC, "-o", str(self.helper), str(HELPER_SOURCE)], check=True)

    def install_framework(self, source: str) -> None:
        library = self.contents / FRAMEWORK_LIBRARY
        library.parent.mkdir(parents=True)
        source_path = self.root / "python.c"
        source_path.write_text(source)
        subprocess.run([CC, "-dynamiclib", "-o", str(library), str(source_path)], check=True)

    def run_helper(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.root),
            "TCAPSULE_STATE_DIR": str(self.root / "state"),
            **env,
        }
        return subprocess.run(
            [str(self.helper), *args],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_runs_the_cli_in_the_bundled_framework_inside_the_helper(self) -> None:
        self.install_framework(FAKE_PYTHON)

        completed = self.run_helper("api", "two words", FAKE_PYTHON_EXIT="3")

        self.assertEqual(completed.returncode, 3, completed.stderr)
        self.assertEqual(
            completed.stdout.splitlines(),
            [
                f"arg={self.helper}",
                "arg=-m",
                "arg=timecapsulesmb.cli.main",
                "arg=api",
                "arg=two words",
                f"home={self.contents / FRAMEWORK_LIBRARY.parent}",
            ],
        )

    def test_exits_70_naming_the_framework_when_it_cannot_load(self) -> None:
        completed = self.run_helper("api")

        self.assertEqual(completed.returncode, 70)
        self.assertIn(f"load {self.contents / FRAMEWORK_LIBRARY} failed", completed.stderr)
        self.assertEqual(completed.stdout, "")

    def test_exits_70_when_the_framework_has_no_entry_point(self) -> None:
        self.install_framework("int not_python(void) { return 0; }\n")

        completed = self.run_helper("api")

        self.assertEqual(completed.returncode, 70)
        self.assertIn("Py_BytesMain", completed.stderr)


if __name__ == "__main__":
    unittest.main()
