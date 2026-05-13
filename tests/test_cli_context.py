from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import NonInteractivePromptError
from timecapsulesmb.device.storage import (
    MaStDiscoveryResult,
    MaStVolume,
    PayloadCandidateCheck,
    PayloadHome,
    PayloadHomeSelection,
)
from timecapsulesmb.transport.ssh import SshConnection


class CommandContextHelperTests(unittest.TestCase):
    def make_context(self) -> CommandContext:
        return CommandContext(mock.Mock(), "test", "test_started", "test_finished")

    def make_connection(self) -> SshConnection:
        return SshConnection("root@10.0.0.2", "pw", "-o foo")

    def make_volume(self, partition_device: str = "dk2") -> MaStVolume:
        return MaStVolume(
            "wd0",
            partition_device,
            f"/Volumes/{partition_device}",
            "Data",
            "12345678-1234-1234-1234-123456789012",
            True,
            "hfs",
        )

    def test_confirm_or_fail_returns_prompt_result(self) -> None:
        context = self.make_context()
        with mock.patch("timecapsulesmb.cli.context.runtime.confirm", return_value=True) as confirm_mock:
            result = context.confirm_or_fail("Continue?", default=False, noninteractive_message="no stdin")

        self.assertTrue(result)
        confirm_mock.assert_called_once_with(
            "Continue?",
            default=False,
            eof_default=None,
            interrupt_default=None,
            noninteractive_message="no stdin",
        )
        self.assertEqual(context.result, "failure")
        self.assertEqual(context.error_lines, [])

    def test_confirm_or_fail_records_noninteractive_failure(self) -> None:
        context = self.make_context()
        output = io.StringIO()
        with mock.patch(
            "timecapsulesmb.cli.context.runtime.confirm",
            side_effect=NonInteractivePromptError("no stdin"),
        ):
            with redirect_stdout(output):
                result = context.confirm_or_fail("Continue?", default=False, noninteractive_message="no stdin")

        self.assertIsNone(result)
        self.assertEqual(context.result, "failure")
        self.assertEqual(context.error_lines, ["no stdin"])
        self.assertIn("no stdin", output.getvalue())

    def test_mount_mast_volumes_reads_then_mounts_and_records_debug(self) -> None:
        context = self.make_context()
        connection = self.make_connection()
        read_volume = self.make_volume("dk2")
        mounted_volume = self.make_volume("dk3")

        with mock.patch("timecapsulesmb.cli.context.read_mast_volumes_conn", return_value=(read_volume,)) as read_mock:
            with mock.patch(
                "timecapsulesmb.cli.context.mounted_mast_volumes_conn",
                return_value=(mounted_volume,),
            ) as mount_mock:
                result = context.mount_mast_volumes(connection, wait_seconds=12, mount_stage="mount_hfs_volumes")

        self.assertEqual(result, (mounted_volume,))
        read_mock.assert_called_once_with(connection)
        mount_mock.assert_called_once_with(connection, (read_volume,), wait_seconds=12)
        self.assertEqual(context.debug_stage, "mount_hfs_volumes")
        self.assertEqual(context.debug_fields["mast_volume_count"], 1)
        self.assertEqual(context.debug_fields["mast_mounted_volume_count"], 1)

    def test_wait_and_select_payload_home_record_storage_diagnostics(self) -> None:
        context = self.make_context()
        connection = self.make_connection()
        volume = self.make_volume("dk2")
        discovery = MaStDiscoveryResult((volume,), 3, "MaSt=valid")
        selection = PayloadHomeSelection(
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            (PayloadCandidateCheck(volume, True, True),),
        )

        with mock.patch("timecapsulesmb.cli.context.wait_for_mast_volumes_conn", return_value=discovery) as wait_mock:
            result = context.wait_for_mast_volumes(connection, attempts=10, delay_seconds=3)
        with mock.patch(
            "timecapsulesmb.cli.context.select_payload_home_with_diagnostics_conn",
            return_value=selection,
        ) as select_mock:
            selected = context.select_payload_home(connection, result.volumes, ".samba4", wait_seconds=7)

        wait_mock.assert_called_once_with(connection, attempts=10, delay_seconds=3)
        select_mock.assert_called_once_with(connection, (volume,), ".samba4", wait_seconds=7)
        self.assertEqual(selected.payload_home, PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"))
        self.assertEqual(context.debug_fields["mast_read_attempts"], 3)
        self.assertIn("mast_candidate_checks", context.debug_fields)
        self.assertNotIn("mast_acp_output", context.debug_fields)

    def test_wait_for_mast_volumes_records_raw_acp_output_when_empty(self) -> None:
        context = self.make_context()
        connection = self.make_connection()
        raw_output = "MaSt=[]"
        discovery = MaStDiscoveryResult((), 10, raw_output)

        with mock.patch("timecapsulesmb.cli.context.wait_for_mast_volumes_conn", return_value=discovery):
            result = context.wait_for_mast_volumes(connection, attempts=10, delay_seconds=3)

        self.assertEqual(result.volumes, ())
        self.assertEqual(context.debug_fields["mast_acp_output_chars"], len(raw_output))
        self.assertEqual(context.debug_fields["mast_acp_output"], raw_output)


if __name__ == "__main__":
    unittest.main()
