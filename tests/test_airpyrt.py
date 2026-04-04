from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.integrations.airpyrt import (
    acp_run_check,
    candidate_interpreters,
    disable_ssh,
    enable_ssh,
    ensure_airpyrt_available,
)


class AirPyrtTests(unittest.TestCase):
    def test_candidate_interpreters_prefers_env_override(self) -> None:
        with mock.patch.dict(os.environ, {"AIRPYRT_PY": "/tmp/custom-python"}, clear=False):
            self.assertEqual(candidate_interpreters(), ["/tmp/custom-python"])

    def test_ensure_airpyrt_available_raises_when_missing(self) -> None:
        with mock.patch("timecapsulesmb.integrations.airpyrt.find_acp_executable", return_value=None):
            with mock.patch("timecapsulesmb.integrations.airpyrt.find_airpyrt_python", return_value=None):
                with self.assertRaises(RuntimeError):
                    ensure_airpyrt_available()

    def test_acp_run_check_raises_on_embedded_error_code(self) -> None:
        proc = mock.Mock(returncode=0, stdout="error code: -0x1234")
        with mock.patch("timecapsulesmb.integrations.airpyrt.run", return_value=proc):
            with self.assertRaises(RuntimeError):
                acp_run_check(["acp"])

    def test_enable_ssh_skips_reboot_when_requested(self) -> None:
        with mock.patch("timecapsulesmb.integrations.airpyrt.set_dbug") as set_dbug_mock:
            with mock.patch("timecapsulesmb.integrations.airpyrt.reboot") as reboot_mock:
                enable_ssh("10.0.0.2", "pw", reboot_device=False)
        set_dbug_mock.assert_called_once()
        reboot_mock.assert_not_called()

    def test_disable_ssh_retries_and_reboots_on_success(self) -> None:
        with mock.patch(
            "timecapsulesmb.integrations.airpyrt.ssh_run_command",
            side_effect=[(1, "nope"), (0, "ok")],
        ) as ssh_mock:
            with mock.patch("timecapsulesmb.integrations.airpyrt.reboot") as reboot_mock:
                disable_ssh("10.0.0.2", "pw", reboot_device=True, verbose=False)
        self.assertEqual(ssh_mock.call_count, 2)
        reboot_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
