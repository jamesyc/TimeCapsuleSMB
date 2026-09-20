from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.deploy.planner import FileTransfer
from timecapsulesmb.deploy.commands import RemovePathAction
from timecapsulesmb.device.storage import PayloadHome, PayloadVerificationResult
from timecapsulesmb.services.deploy import (
    DeployDeviceError,
    DeployRuntimeConfig,
    _allocated_flash_bytes,
    _required_flash_upload_peak,
    upload_and_verify_deployment_payload,
)


def _flash_transfer(source: str, destination: str) -> FileTransfer:
    return FileTransfer(source, destination, "flash_atomic", 120, source)


def test_flash_peak_accounts_for_atomic_temp_and_sequential_replacements(tmp_path: Path) -> None:
    discovery = tmp_path / "discoveryd"
    config = tmp_path / "tcapsulesmb.conf"
    discovery.write_bytes(b"d" * 2300)
    config.write_bytes(b"c" * 1100)
    transfers = [
        _flash_transfer("discovery", "/mnt/Flash/discoveryd"),
        _flash_transfer("config", "/mnt/Flash/tcapsulesmb.conf"),
    ]

    peak = _required_flash_upload_peak(
        transfers,
        {"discovery": discovery, "config": config},
        {"/mnt/Flash/tcapsulesmb.conf": 900},
    )

    # The config temporary file is created after the new 3 KiB discoveryd is
    # installed; replacing its old 1 KiB allocation leaves a 5 KiB peak.
    assert peak == 5 * 1024 + 16 * 1024


def test_insufficient_flash_fails_before_stop_or_upload(tmp_path: Path) -> None:
    source = tmp_path / "discoveryd"
    source.write_bytes(b"x")
    transfer = _flash_transfer("discovery", "/mnt/Flash/discoveryd")
    plan = SimpleNamespace(uploads=[transfer], pre_upload_actions=[object()])
    prepared = SimpleNamespace(plan=plan, payload_home=SimpleNamespace())
    stopped: list[object] = []
    uploaded: list[object] = []

    with mock.patch(
        "timecapsulesmb.services.deploy._deployment_upload_sources",
        return_value={"discovery": source},
    ):
        with pytest.raises(DeployDeviceError, match="Not enough free space") as raised:
            upload_and_verify_deployment_payload(
                AppConfig.from_values({}),
                SimpleNamespace(),
                prepared,
                DeployRuntimeConfig(nbns_enabled=True),
                run_remote_actions_func=lambda *_args, **_kwargs: stopped.append(object()),
                render_flash_config_func=lambda *_args, **_kwargs: "config",
                render_rsync_config_func=lambda *_args, **_kwargs: "rsync",
                upload_payload_func=lambda *_args, **_kwargs: uploaded.append(object()),
                probe_flash_capacity_func=lambda *_args: (1024, 4096),
            )

    assert raised.value.code == "insufficient_flash_space"
    assert stopped == []
    assert uploaded == []


def test_verification_failure_retains_legacy_flash_binary(tmp_path: Path) -> None:
    source = tmp_path / "discoveryd"
    source.write_bytes(b"new")
    transfer = _flash_transfer("discovery", "/mnt/Flash/discoveryd")
    legacy_cleanup = RemovePathAction("/mnt/Flash/mdns-advertiser")
    permission_action = object()
    plan = SimpleNamespace(
        uploads=[transfer],
        migration_upload=transfer,
        pre_upload_actions=[object()],
        post_upload_actions=[permission_action],
        post_verify_actions=[legacy_cleanup],
        apple_mount_wait_seconds=0,
        payload_dir="/Volumes/dk2/.samba4",
    )
    payload_home = PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4")
    prepared = SimpleNamespace(plan=plan, payload_home=payload_home)
    action_batches: list[list[object]] = []

    with (
        mock.patch(
            "timecapsulesmb.services.deploy._deployment_upload_sources",
            return_value={"discovery": source},
        ),
        mock.patch("timecapsulesmb.services.deploy.replace", side_effect=lambda value, **_changes: value),
        pytest.raises(Exception, match="managed payload verification failed"),
    ):
        upload_and_verify_deployment_payload(
            AppConfig.from_values({}),
            SimpleNamespace(remote_has_scp=True),
            prepared,
            DeployRuntimeConfig(nbns_enabled=True),
            run_remote_actions_func=lambda _connection, actions, **_kwargs: action_batches.append(list(actions)),
            render_flash_config_func=lambda *_args, **_kwargs: "config",
            render_rsync_config_func=lambda *_args, **_kwargs: "rsync",
            upload_payload_func=lambda *_args, **_kwargs: None,
            probe_flash_capacity_func=lambda *_args: (100_000, 20_000),
            migrate_xattrs_func=lambda *_args, **_kwargs: "ok",
            verify_payload_home=lambda *_args, **_kwargs: PayloadVerificationResult(False, "bad replacement"),
        )

    assert action_batches[0] == plan.pre_upload_actions
    assert action_batches[1] == [permission_action]
    assert all(legacy_cleanup not in batch for batch in action_batches)
