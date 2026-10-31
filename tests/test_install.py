"""How tcapsule was installed, and the commands its messages give for it."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from timecapsulesmb import basebinary
from timecapsulesmb.basebinary import BasebinaryError
from timecapsulesmb.core.errors import missing_dependency_message
from timecapsulesmb.core.install import installed_with_homebrew, reinstall_command, tcapsule_command
from timecapsulesmb.flash import FlashAnalysisError, require_zopfli_gzip_available


def client(value: str | None):
    """The environment with TCAPSULE_CLIENT set to `value`, or unset."""
    env = {name: setting for name, setting in os.environ.items() if name != "TCAPSULE_CLIENT"}
    if value is not None:
        env["TCAPSULE_CLIENT"] = value
    return mock.patch.dict(os.environ, env, clear=True)


def homebrew():
    return client("homebrew")


def checkout():
    return client(None)


class InstallKindTests(unittest.TestCase):
    def test_only_the_formulas_client_name_is_homebrew(self) -> None:
        cases = {"homebrew": True, " homebrew\n": True, "macos_gui": False, "": False, "Homebrew": False}
        for value, expected in cases.items():
            with self.subTest(value=value), client(value):
                self.assertIs(installed_with_homebrew(), expected)
        with checkout():
            self.assertFalse(installed_with_homebrew())

    def test_commands_for_a_checkout(self) -> None:
        with checkout():
            self.assertEqual(tcapsule_command("deploy"), ".venv/bin/tcapsule deploy")
            self.assertEqual(tcapsule_command(), ".venv/bin/tcapsule <command>")
            self.assertEqual(reinstall_command(), "./tcapsule bootstrap")

    def test_commands_for_homebrew(self) -> None:
        with homebrew():
            self.assertEqual(tcapsule_command("deploy"), "tcapsule deploy")
            self.assertEqual(tcapsule_command(), "tcapsule <command>")
            self.assertEqual(reinstall_command(), "brew reinstall tcapsule")

    def test_arguments_are_quoted_for_the_shell(self) -> None:
        with homebrew():
            self.assertEqual(
                tcapsule_command("configure", "--name", "James's Capsule", "--pattern", "$(reboot)"),
                "tcapsule configure --name 'James'\"'\"'s Capsule' --pattern '$(reboot)'",
            )


class MissingPackageMessageTests(unittest.TestCase):
    def test_homebrew_install_is_told_to_reinstall_the_formula(self) -> None:
        # A Homebrew install's package root is in its Cellar, never a checkout.
        with tempfile.TemporaryDirectory() as tmp, homebrew():
            with mock.patch("timecapsulesmb.core.errors.package_project_root", return_value=Path(tmp)):
                message = missing_dependency_message("zeroconf", ModuleNotFoundError("No module named 'zeroconf'"))

        self.assertIn("Failed to load zeroconf", message)
        self.assertIn("Run `brew reinstall tcapsule` first", message)
        self.assertNotIn("bootstrap", message)

    def test_explicit_install_command_still_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, homebrew():
            with mock.patch("timecapsulesmb.core.errors.package_project_root", return_value=Path(tmp)):
                message = missing_dependency_message("zeroconf", install_command="pip install zeroconf")

        self.assertIn("Run `pip install zeroconf` first", message)

    # Outside a source checkout, so the messages give the generic recovery.
    CASES = {
        "checkout": (checkout, "Run `./tcapsule bootstrap` first", "Then rerun `.venv/bin/tcapsule flash`."),
        "homebrew": (homebrew, "Run `brew reinstall tcapsule` first", "Then rerun `tcapsule flash`."),
    }

    def test_missing_zopfli_names_the_installs_own_commands(self) -> None:
        for name, (env, install, rerun) in self.CASES.items():
            with self.subTest(name=name), env(), tempfile.TemporaryDirectory() as tmp:
                with mock.patch("timecapsulesmb.core.errors.package_project_root", return_value=Path(tmp)):
                    with mock.patch("timecapsulesmb.core.errors.importlib.import_module", side_effect=ModuleNotFoundError("zopfli")):
                        with self.assertRaises(FlashAnalysisError) as raised:
                            require_zopfli_gzip_available()
                self.assertIn("Failed to load zopfli", str(raised.exception))
                self.assertIn(install, str(raised.exception))
                self.assertIn(rerun, str(raised.exception))

    def test_missing_pycryptodome_names_the_installs_own_commands(self) -> None:
        for name, (env, install, rerun) in self.CASES.items():
            basebinary._load_aes_module.cache_clear()
            try:
                with self.subTest(name=name), env(), tempfile.TemporaryDirectory() as tmp, mock.patch(
                    "timecapsulesmb.core.errors.package_project_root", return_value=Path(tmp)
                ):
                    with mock.patch(
                        "timecapsulesmb.basebinary.importlib.import_module",
                        side_effect=ModuleNotFoundError("Crypto"),
                    ):
                        with self.assertRaises(BasebinaryError) as raised:
                            basebinary._load_aes_module()
                    self.assertIn("Failed to load pycryptodome", str(raised.exception))
                    self.assertIn(install, str(raised.exception))
                    self.assertIn(rerun, str(raised.exception))
            finally:
                basebinary._load_aes_module.cache_clear()


if __name__ == "__main__":
    unittest.main()
