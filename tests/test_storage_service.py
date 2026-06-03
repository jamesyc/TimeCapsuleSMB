from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.device.storage import MaStDiscoveryResult, MaStVolume
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.storage import (
    mount_mast_volumes_with_diagnostics,
    read_mast_volumes_with_diagnostics,
    wait_for_mast_volumes_with_diagnostics,
)
from timecapsulesmb.transport.ssh import SshConnection


class RecordingMaStCallbacks:
    def __init__(self) -> None:
        self.stages: list[str] = []
        self.debug_fields: dict[str, object] = {}

    def set_stage(self, stage: str) -> None:
        self.stages.append(stage)

    def add_debug_fields(self, **fields: object) -> None:
        self.debug_fields.update(fields)

    def callbacks(self) -> OperationCallbacks:
        return OperationCallbacks(
            set_stage=self.set_stage,
            add_debug_fields=self.add_debug_fields,
        )


class MaStStorageServiceTests(unittest.TestCase):
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

    def test_read_mast_volumes_records_stage_and_candidates(self) -> None:
        recorder = RecordingMaStCallbacks()
        connection = self.make_connection()
        volume = self.make_volume()

        result = read_mast_volumes_with_diagnostics(
            connection,
            callbacks=recorder.callbacks(),
            read_mast_volumes=mock.Mock(return_value=(volume,)),
        )

        self.assertEqual(result, (volume,))
        self.assertEqual(recorder.stages, ["read_mast"])
        self.assertEqual(recorder.debug_fields["mast_volume_count"], 1)
        self.assertEqual(recorder.debug_fields["mast_candidates"][0]["part"], "dk2")

    def test_mount_mast_volumes_reads_mounts_and_records_mounted_candidates(self) -> None:
        recorder = RecordingMaStCallbacks()
        connection = self.make_connection()
        read_volume = self.make_volume("dk2")
        mounted_volume = self.make_volume("dk3")
        read_mock = mock.Mock(return_value=(read_volume,))
        mount_mock = mock.Mock(return_value=(mounted_volume,))

        result = mount_mast_volumes_with_diagnostics(
            connection,
            callbacks=recorder.callbacks(),
            wait_seconds=12,
            mount_stage="mount_hfs_volumes",
            read_mast_volumes=read_mock,
            mounted_mast_volumes=mount_mock,
        )

        self.assertEqual(result, (mounted_volume,))
        read_mock.assert_called_once_with(connection)
        mount_mock.assert_called_once_with(connection, (read_volume,), wait_seconds=12)
        self.assertEqual(recorder.stages, ["read_mast", "mount_hfs_volumes"])
        self.assertEqual(recorder.debug_fields["mast_volume_count"], 1)
        self.assertEqual(recorder.debug_fields["mast_mounted_volume_count"], 1)
        self.assertEqual(recorder.debug_fields["mast_mounted_candidates"][0]["part"], "dk3")

    def test_wait_for_mast_volumes_records_raw_output_only_when_empty(self) -> None:
        recorder = RecordingMaStCallbacks()
        connection = self.make_connection()
        discovery = MaStDiscoveryResult((), 10, "MaSt=[]")
        wait_mock = mock.Mock(return_value=discovery)

        result = wait_for_mast_volumes_with_diagnostics(
            connection,
            callbacks=recorder.callbacks(),
            attempts=10,
            delay_seconds=3,
            wait_for_mast_volumes=wait_mock,
        )

        self.assertEqual(result, discovery)
        wait_mock.assert_called_once_with(connection, attempts=10, delay_seconds=3)
        self.assertEqual(recorder.stages, ["read_mast"])
        self.assertEqual(recorder.debug_fields["mast_read_attempts"], 10)
        self.assertEqual(recorder.debug_fields["mast_volume_count"], 0)
        self.assertEqual(recorder.debug_fields["mast_acp_output_chars"], len("MaSt=[]"))
        self.assertEqual(recorder.debug_fields["mast_acp_output"], "MaSt=[]")

    def test_bad_mounted_candidate_shape_does_not_block_operation(self) -> None:
        recorder = RecordingMaStCallbacks()
        connection = self.make_connection()
        volume = self.make_volume()
        mounted = (SimpleNamespace(volume_root="/Volumes/dk2"),)

        result = mount_mast_volumes_with_diagnostics(
            connection,
            callbacks=recorder.callbacks(),
            wait_seconds=12,
            read_mast_volumes=mock.Mock(return_value=(volume,)),
            mounted_mast_volumes=mock.Mock(return_value=mounted),
        )

        self.assertEqual(result, mounted)
        self.assertEqual(recorder.debug_fields["mast_mounted_volume_count"], 1)
        self.assertIsNone(recorder.debug_fields["mast_mounted_candidates"])


if __name__ == "__main__":
    unittest.main()
