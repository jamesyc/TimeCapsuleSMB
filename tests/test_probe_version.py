from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from timecapsulesmb.device import probe
from timecapsulesmb.device.probe import deployed_version_script, parse_deployed_version


def run_version_script(config_text: str | None) -> str:
    """The script's output under a real /bin/sh, reading a Flash config with this text (or none)."""
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "tcapsulesmb.conf"
        if config_text is not None:
            config.write_text(config_text)
        with mock.patch.object(probe, "FLASH_RUNTIME_CONFIG", str(config)):
            script = deployed_version_script()
        return subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, check=True).stdout


class DeployedVersionScriptTests(unittest.TestCase):
    def test_reads_both_keys_from_the_flash_config(self) -> None:
        output = run_version_script("TC_DEPLOY_RELEASE_TAG=v9.1.0\nTC_DEPLOY_CLI_VERSION_CODE=90100\nOTHER=1\n")

        result = parse_deployed_version(output)

        self.assertEqual((result.release_tag, result.cli_version_code, result.detail), ("v9.1.0", 90100, "ok"))

    def test_a_config_without_version_keys_prints_empty_values(self) -> None:
        output = run_version_script("SMBD_DEBUG_LOGGING=0\n")

        self.assertEqual(output, "release_tag=\ncli_version_code=\n")
        result = parse_deployed_version(output)
        self.assertEqual((result.release_tag, result.cli_version_code), (None, None))
        self.assertEqual(result.detail, "missing version metadata")

    def test_a_missing_config_prints_empty_values(self) -> None:
        self.assertEqual(run_version_script(None), "release_tag=\ncli_version_code=\n")


class ParseDeployedVersionTests(unittest.TestCase):
    def test_a_missing_key_leaves_that_value_out(self) -> None:
        result = parse_deployed_version("release_tag=v9.1.0\n")

        self.assertEqual((result.release_tag, result.cli_version_code), ("v9.1.0", None))
        self.assertEqual(result.detail, "missing version metadata")

    def test_a_code_that_is_not_a_number_is_unknown(self) -> None:
        result = parse_deployed_version("release_tag=v9.1.0\ncli_version_code=9x\n")

        self.assertEqual((result.release_tag, result.cli_version_code), ("v9.1.0", None))

    def test_login_noise_and_trailing_spaces_are_ignored(self) -> None:
        result = parse_deployed_version("Last login: today\nrelease_tag = v9.1.0  \ncli_version_code=90100 \nmotd line\n")

        self.assertEqual((result.release_tag, result.cli_version_code, result.detail), ("v9.1.0", 90100, "ok"))

    def test_empty_output_is_unknown(self) -> None:
        result = parse_deployed_version("")

        self.assertEqual((result.release_tag, result.cli_version_code), (None, None))


if __name__ == "__main__":
    unittest.main()
