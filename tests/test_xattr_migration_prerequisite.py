from __future__ import annotations

from unittest import mock

import pytest

from timecapsulesmb.services.xattr_migration import (
    InstallationEvidence,
    InvalidMigrationReceipt,
    classify_migration_prerequisite,
    build_xattr_migration_plan,
    follow_xattr_migration,
    parse_installation_evidence_output,
    parse_migration_receipt,
    parse_xattr_migration_status,
    probe_migration_prerequisite,
)
from timecapsulesmb.device.storage import MaStVolume
from timecapsulesmb.transport.ssh import SshConnection


COMPLETE_RECEIPT = """\
format=1
migration=1
state=complete
source_release=v2.2.9
source_version_code=20215
source_backend=netatalk
device_identity=syAP:119
payload_identity=uuid:payload
volume=uuid:internal
volume=uuid:usb
"""


@pytest.mark.parametrize(
    ("evidence", "state", "allowed"),
    [
        (InstallationEvidence(), "clean", True),
        (
            InstallationEvidence(
                config_text="TC_DEPLOY_RELEASE_TAG=v2.2.9\nTC_DEPLOY_CLI_VERSION_CODE=20215\n",
                rc_local=True,
                boot=True,
                manager=True,
            ),
            "legacy",
            False,
        ),
        (
            InstallationEvidence(
                config_text="TC_DEPLOY_RELEASE_TAG=v3.1.0\nTC_DEPLOY_CLI_VERSION_CODE=30100\n",
                rc_local=True,
                boot=True,
                manager=True,
            ),
            "installed",
            True,
        ),
        (
            InstallationEvidence(
                config_text="TC_DEPLOY_RELEASE_TAG=v3.1.0\nTC_DEPLOY_CLI_VERSION_CODE=30100\n",
            ),
            "ambiguous",
            False,
        ),
        (InstallationEvidence(manager=True), "ambiguous", False),
        (InstallationEvidence(receipt_text=COMPLETE_RECEIPT), "ready", True),
        (
            InstallationEvidence(receipt_text=COMPLETE_RECEIPT.replace("state=complete", "state=incomplete")),
            "incomplete",
            False,
        ),
    ],
)
def test_classifies_installation_evidence(evidence, state, allowed):
    result = classify_migration_prerequisite(evidence)
    assert result.state == state
    assert result.allowed is allowed


def test_receipt_parser_rejects_missing_scope_and_duplicates():
    with pytest.raises(InvalidMigrationReceipt):
        parse_migration_receipt(COMPLETE_RECEIPT.replace("volume=uuid:internal\nvolume=uuid:usb\n", ""))
    with pytest.raises(InvalidMigrationReceipt):
        parse_migration_receipt(COMPLETE_RECEIPT + "state=complete\n")


def test_malformed_receipt_blocks_otherwise_healthy_installation():
    result = classify_migration_prerequisite(
        InstallationEvidence(
            config_text="TC_DEPLOY_RELEASE_TAG=v3.1.0\nTC_DEPLOY_CLI_VERSION_CODE=30100\n",
            receipt_text="state=complete\n",
            rc_local=True,
            boot=True,
            service=True,
        )
    )
    assert result.state == "ambiguous"
    assert not result.allowed


def test_parses_framed_remote_probe_without_evaluating_config():
    output = """\
evidence:rc_local=1
evidence:boot=1
evidence:manager=1
evidence:service=0
evidence:legacy_start=0
evidence:payload_marker=1
__TC_CONFIG_BEGIN__
TC_DEPLOY_RELEASE_TAG='v2.2.9'
TC_DEPLOY_CLI_VERSION_CODE=20215
EVIL=$(reboot)
__TC_CONFIG_END__
__TC_RECEIPT_BEGIN__
__TC_RECEIPT_END__
"""
    evidence = parse_installation_evidence_output(output)
    result = classify_migration_prerequisite(evidence)
    assert result.state == "legacy"
    assert result.release_tag == "v2.2.9"


def test_probe_uses_bounded_literal_reads_and_classifies():
    connection = SshConnection(host="root@device", password="pw", ssh_opts="")
    output = """\
evidence:rc_local=0
evidence:boot=0
evidence:manager=0
evidence:service=0
evidence:legacy_start=0
evidence:payload_marker=0
__TC_CONFIG_BEGIN__
__TC_CONFIG_END__
__TC_RECEIPT_BEGIN__
__TC_RECEIPT_END__
"""
    with mock.patch(
        "timecapsulesmb.services.xattr_migration.run_ssh",
        return_value=mock.Mock(stdout=output),
    ) as run:
        result = probe_migration_prerequisite(connection)
    assert result.state == "clean"
    command = run.call_args.args[1]
    assert "sed -n" in command
    assert "xattr-upgrade.state" in command
    assert ". " not in command


def _volume(device: str, uuid: str, *, builtin: bool) -> MaStVolume:
    return MaStVolume(
        disk_device="wd0" if builtin else "sd0",
        partition_device=device,
        volume_root=f"/Volumes/{device}",
        name=device,
        adisk_uuid=uuid,
        builtin=builtin,
        format="hfs",
    )


def test_explicit_plan_records_selected_scope_and_legacy_source(tmp_path):
    prerequisite = classify_migration_prerequisite(
        InstallationEvidence(
            config_text="TC_DEPLOY_RELEASE_TAG=v2.2.9\nTC_DEPLOY_CLI_VERSION_CODE=20215\n",
            rc_local=True,
            boot=True,
            manager=True,
        )
    )
    internal = _volume("dk2", "internal", builtin=True)
    external = _volume("dk5", "external", builtin=False)
    plan = build_xattr_migration_plan(
        prerequisite,
        migrator_path=tmp_path / "xattr-hfs-migrate",
        volumes=(internal, external),
        source_backend="netatalk",
        device_identity="syAP:119",
        payload_volume=internal,
        payload_dir_name=".samba4",
    )
    assert plan.tdb_path == "/Volumes/dk2/.samba4/private/xattr.tdb"
    assert plan.payload_identity == "uuid:internal"
    assert tuple(volume.adisk_uuid for volume in plan.volumes) == ("internal", "external")


def test_status_parser_is_bounded_and_treats_negative_counts_as_zero():
    status = parse_xattr_migration_status(
        "state=running\noperation_id=42\nphase=scan\nentries=12\nconversions=-3\nwarnings=bad\n"
    )
    assert status.state == "running"
    assert status.entries == 12
    assert status.conversions == 0
    assert status.warnings == 0
    assert not status.terminal


def test_follow_has_no_scan_wide_timeout_and_returns_terminal_status():
    connection = SshConnection(host="root@device", password="pw", ssh_opts="")
    running = mock.Mock(returncode=0, stdout="state=running\noperation_id=42\nentries=10\n")
    complete = mock.Mock(returncode=0, stdout="state=complete\noperation_id=42\nentries=11\n")
    observed = []
    with mock.patch(
        "timecapsulesmb.services.xattr_migration.run_ssh",
        side_effect=[running, running, complete],
    ), mock.patch("timecapsulesmb.services.xattr_migration.time.sleep") as sleep:
        status = follow_xattr_migration(connection, on_status=observed.append)
    assert status.state == "complete"
    assert [item.state for item in observed] == ["running", "running", "complete"]
    assert sleep.call_count == 2
