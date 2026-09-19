from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from timecapsulesmb.device.storage import MaStVolume
from timecapsulesmb.services.migrate_xattr import MigrateXattrError, run_xattr_migration
from timecapsulesmb.services.xattr_migration import (
    MigrationPrerequisite,
    XattrMigrationStatus,
    XattrMigrationPlan,
    start_xattr_migration,
    stage_xattr_migrator,
)
from timecapsulesmb.transport.ssh import SshConnection


def _volume(device: str = "dk2", uuid: str = "internal") -> MaStVolume:
    return MaStVolume("wd0", device, f"/Volumes/{device}", "Data", uuid, True, "hfs")


def _target():
    connection = SshConnection("root@device", "pw", "")
    compatibility = SimpleNamespace(payload_family="netbsd6_samba4", supported=True)
    probe = SimpleNamespace(airport_syap="119")
    return SimpleNamespace(
        connection=connection,
        probe_state=SimpleNamespace(compatibility=compatibility, probe_result=probe),
    )


def _prerequisite(state: str = "legacy", allowed: bool = False) -> MigrationPrerequisite:
    return MigrationPrerequisite(
        state=state,
        allowed=allowed,
        release_tag="v2.2.9",
        version_code=20215,
        receipt=None,
        reason="legacy",
        source_backend="netatalk",
    )


def test_clean_install_finishes_without_staging_or_scanning(tmp_path):
    target = _target()
    with mock.patch(
        "timecapsulesmb.services.migrate_xattr.probe_migration_prerequisite",
        return_value=_prerequisite("clean", True),
    ), mock.patch("timecapsulesmb.services.migrate_xattr._verified_migrator") as artifact, mock.patch(
        "timecapsulesmb.services.migrate_xattr.storage_service.mount_mast_volumes_with_diagnostics"
    ) as mount:
        result = run_xattr_migration(target, tmp_path)
    assert result.status.state == "complete"
    artifact.assert_not_called()
    mount.assert_not_called()


def test_explicit_operation_selects_attached_scope_and_follows_device_owner(tmp_path):
    target = _target()
    internal = _volume()
    external = MaStVolume("sd0", "dk5", "/Volumes/dk5", "USB", "external", False, "hfs")
    started = XattrMigrationStatus("running", operation_id="42", phase="offline_transition")
    complete = XattrMigrationStatus("complete", operation_id="42", phase="finished", entries=25)
    with mock.patch(
        "timecapsulesmb.services.migrate_xattr.probe_migration_prerequisite",
        return_value=_prerequisite(),
    ), mock.patch(
        "timecapsulesmb.services.migrate_xattr._verified_migrator",
        return_value=Path("/artifacts/xattr-hfs-migrate"),
    ), mock.patch(
        "timecapsulesmb.services.migrate_xattr.storage_service.mount_mast_volumes_with_diagnostics",
        return_value=(internal, external),
    ), mock.patch(
        "timecapsulesmb.services.migrate_xattr.run_ssh",
        return_value=mock.Mock(returncode=0),
    ), mock.patch(
        "timecapsulesmb.services.migrate_xattr.start_xattr_migration",
        return_value=started,
    ) as start, mock.patch(
        "timecapsulesmb.services.migrate_xattr.follow_xattr_migration",
        return_value=complete,
    ) as follow:
        result = run_xattr_migration(target, tmp_path)
    plan = start.call_args.args[1]
    assert [volume.adisk_uuid for volume in plan.volumes] == ["internal", "external"]
    assert plan.tdb_path == "/Volumes/dk2/.samba4/private/xattr.tdb"
    follow.assert_called_once()
    assert result.status == complete
    assert result.selected_volumes == ("/Volumes/dk2", "/Volumes/dk5")


def test_ambiguous_installation_fails_before_artifact_or_disk_work(tmp_path):
    target = _target()
    prerequisite = _prerequisite("ambiguous", False)
    with mock.patch(
        "timecapsulesmb.services.migrate_xattr.probe_migration_prerequisite",
        return_value=prerequisite,
    ), mock.patch("timecapsulesmb.services.migrate_xattr._verified_migrator") as artifact:
        with pytest.raises(MigrateXattrError) as raised:
            run_xattr_migration(target, tmp_path)
    assert raised.value.code == "migration_state_ambiguous"
    artifact.assert_not_called()


def test_start_stages_verified_ram_binary_and_passes_every_volume_identity(tmp_path):
    connection = SshConnection("root@device", "pw", "")
    binary = tmp_path / "xattr-hfs-migrate"
    binary.write_bytes(b"binary")
    plan = XattrMigrationPlan(
        prerequisite=_prerequisite(),
        migrator_path=binary,
        source_backend="netatalk",
        device_identity="syAP:119",
        payload_identity="uuid:internal",
        tdb_path="/Volumes/dk2/.samba4/private/xattr.tdb",
        volumes=(_volume(), MaStVolume("sd0", "dk5", "/Volumes/dk5", "USB", "external", False, "hfs")),
    )
    with mock.patch("timecapsulesmb.services.xattr_migration.run_scp"), mock.patch(
        "timecapsulesmb.services.xattr_migration.run_ssh",
        side_effect=[
            mock.Mock(returncode=0, stdout="Filesystem 1K-blocks Used Avail Capacity Mounted on\nmem 15000 1 14000 1% /mnt/Memory\n"),
            mock.Mock(returncode=0, stdout=""),
            mock.Mock(returncode=0, stdout="state=running\noperation_id=42\n"),
        ],
    ) as ssh:
        status = start_xattr_migration(connection, plan)
    command = ssh.call_args_list[2].args[1]
    assert "maintain" in command
    assert "uuid:internal /Volumes/dk2" in command
    assert "uuid:external /Volumes/dk5" in command
    assert status.operation_id == "42"


def test_stage_rejects_insufficient_ram_before_upload(tmp_path):
    connection = SshConnection("root@device", "pw", "")
    binary = tmp_path / "xattr-hfs-migrate"
    binary.write_bytes(b"x" * 100_000)
    with mock.patch(
        "timecapsulesmb.services.xattr_migration.run_ssh",
        return_value=mock.Mock(stdout="Filesystem 1K-blocks Used Avail Capacity Mounted on\nmem 100 99 1 99% /mnt/Memory\n"),
    ), mock.patch("timecapsulesmb.services.xattr_migration.run_scp") as upload:
        with pytest.raises(RuntimeError, match="not enough free migration RAM"):
            stage_xattr_migrator(connection, binary)
    upload.assert_not_called()
