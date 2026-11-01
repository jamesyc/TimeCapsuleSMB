"""validate-install's check of the SSH password helper, which every device login runs."""
from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from timecapsulesmb import install_validation
from timecapsulesmb.install_validation import InstallCheckResult, install_ok, validate_install


class SshAskpassCheckTests(unittest.TestCase):
    def test_validate_install_fails_on_the_helper_alone(self) -> None:
        # Every other check passes, so the helper alone decides the result.
        other_checks = (
            "validate_python_modules",
            "validate_boot_assets",
            "validate_distribution",
            "validate_artifact_hashes",
            "validate_boot_script_tokens",
        )
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "ssh-askpass"
            helper.write_text("#!/bin/sh\n")
            for mode, ok in ((0o644, False), (0o755, True)):
                with self.subTest(mode=oct(mode)), contextlib.ExitStack() as stack:
                    for name in other_checks:
                        stack.enter_context(mock.patch.object(
                            install_validation, name, return_value=InstallCheckResult(name, True, "ok")
                        ))
                    stack.enter_context(mock.patch.object(install_validation, "SSH_ASKPASS_PATH", helper))
                    helper.chmod(mode)
                    checks = validate_install(mock.sentinel.app_paths)
                    self.assertIs(install_ok(checks), ok)
                    self.assertEqual(
                        [check.id for check in checks if not check.ok], [] if ok else ["ssh_askpass"]
                    )


if __name__ == "__main__":
    unittest.main()
