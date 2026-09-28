"""The deploy command."""
from __future__ import annotations

import io
import json
import plistlib
import unittest
from contextlib import ExitStack
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock
from timecapsulesmb.cli import deploy
from timecapsulesmb.services.deploy import DEPLOY_REBOOT_NO_DOWN_MESSAGE
from timecapsulesmb.core.config import MANAGED_PAYLOAD_DIR_NAME
from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.device.probe import ProbeResult, ProbedDeviceState, SshAccessStatus
from timecapsulesmb.device.storage import (
    MaStDiscoveryResult,
    MaStVolume,
    PayloadCandidateCheck,
    PayloadHome,
    PayloadHomeSelection,
    PayloadVerificationResult,
    VolumeMountResult,
)
from timecapsulesmb.deploy.commands import RunScriptAction
from timecapsulesmb.deploy.planner import (
    DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
    DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
    DEPLOY_STARTUP_REBOOT_THEN_VERIFY,
    GENERATED_FLASH_CONFIG_SOURCE,
    GENERATED_RSYNC_CONFIG_SOURCE,
    PACKAGED_BOOT_SOURCE,
    BINARY_SERVICE_SOURCE,
)
from timecapsulesmb.transport.errors import ssh_timeout_slow_device_message
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection
from timecapsulesmb.cli.util import ANSI_RED, ANSI_RESET
from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG

from tests.cli_support import CliTestCase


class CliDeployTests(CliTestCase):
    def _payload_home(self, volume_root: str = "/Volumes/dk2", payload_dir_name: str = "samba4") -> PayloadHome:
        disk_key = volume_root.rstrip("/").rsplit("/", 1)[-1]
        return PayloadHome(volume_root, f"/dev/{disk_key}", payload_dir_name)

    def run_deploy_cli(
        self,
        argv: list[str] | None = None,
        *,
        values: dict[str, str] | None = None,
        artifacts: list[tuple[str, bool, str]] | None = None,
        compatibility: DeviceCompatibility | None = None,
        mount_root: str = "/Volumes/dk2",
        command_context=None,
        ensure_install_id: bool = False,
        telemetry_enabled: bool = True,
        patch_actions: bool = False,
        patch_upload: bool = False,
        upload_side_effect=None,
        mast_volumes: tuple[MaStVolume, ...] | None = None,
        mast_discovery: MaStDiscoveryResult | None = None,
        payload_home_selection: PayloadHomeSelection | None = None,
        select_payload_home_side_effect=None,
        payload_verification: PayloadVerificationResult | None = None,
        payload_verification_side_effect=None,
        login_autostart_enabled: bool = False,
        verify_runtime=None,
        reboot_side_effect=None,
        wait_side_effect=(True, True),
        input_side_effect=None,
        raises=None,
    ):
        output = io.StringIO()
        mocks = SimpleNamespace()
        raised = None
        if artifacts is None:
            artifacts = [("smbd", True, "ok"), ("discovery", True, "ok")]
        config_values = values or self.make_valid_env()
        payload_home = self._payload_home(mount_root, MANAGED_PAYLOAD_DIR_NAME)
        if mast_volumes is None:
            mast_volumes = (self._mast_volume(mount_root.rstrip("/").rsplit("/", 1)[-1]),)
        if mast_discovery is None:
            mast_discovery = MaStDiscoveryResult(mast_volumes, 1)
        if payload_home_selection is None:
            checks = (PayloadCandidateCheck(mast_volumes[0], VolumeMountResult(True), True),) if mast_volumes else ()
            payload_home_selection = PayloadHomeSelection(payload_home, checks)
        with ExitStack() as stack:
            if ensure_install_id:
                mocks.ensure_install_id = stack.enter_context(mock.patch("timecapsulesmb.cli.deploy.ensure_install_id"))
            mocks.load_install_identity = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.cli.deploy.load_install_identity",
                    return_value=SimpleNamespace(telemetry_enabled=telemetry_enabled),
                )
            )
            mocks.load_env_config = stack.enter_context(
                mock.patch("timecapsulesmb.cli.deploy.load_env_config", return_value=self.make_app_config(config_values))
            )
            if command_context is not None:
                mocks.command_context = stack.enter_context(mock.patch("timecapsulesmb.cli.deploy.CommandContext", return_value=command_context))
            mocks.validate_artifacts = stack.enter_context(mock.patch("timecapsulesmb.services.deploy.validate_artifacts", return_value=artifacts))
            mocks.wait_for_mast_volumes_conn = stack.enter_context(
                mock.patch("timecapsulesmb.services.storage.wait_for_mast_volumes_conn", return_value=mast_discovery)
            )
            if select_payload_home_side_effect is None:
                mocks.select_payload_home_with_diagnostics_conn = stack.enter_context(
                    mock.patch(
                        "timecapsulesmb.services.deploy.select_payload_home_with_diagnostics_conn",
                        return_value=payload_home_selection,
                    )
                )
            else:
                mocks.select_payload_home_with_diagnostics_conn = stack.enter_context(
                    mock.patch(
                        "timecapsulesmb.services.deploy.select_payload_home_with_diagnostics_conn",
                        side_effect=select_payload_home_side_effect,
                    )
                )
            deploy_compatibility = compatibility or self.make_supported_compatibility()
            deploy_probe_result = ProbeResult(
                ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
                error=None,
                os_name=deploy_compatibility.os_name,
                os_release=deploy_compatibility.os_release,
                arch=deploy_compatibility.arch,
                elf_endianness=deploy_compatibility.elf_endianness,
                airport_model=config_values.get("TC_MDNS_DEVICE_MODEL"),
                airport_syap=config_values.get("TC_AIRPORT_SYAP"),
            )
            deploy_probe_state = ProbedDeviceState(
                probe_result=deploy_probe_result,
                compatibility=deploy_compatibility,
            )
            deploy_target = SimpleNamespace(
                connection=SshConnection("root@10.0.0.2", "pw", "-o foo"),
                probe_state=deploy_probe_state,
            )

            def fake_resolve_validated_managed_target(command_context, *, profile, include_probe):
                return command_context._apply_managed_target_state(deploy_target)

            mocks.resolve_validated_managed_target = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.cli.context.CommandContext.resolve_validated_managed_target",
                    autospec=True,
                    side_effect=fake_resolve_validated_managed_target,
                )
            )
            mocks.require_compatibility = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.cli.context.CommandContext.require_compatibility",
                    return_value=deploy_compatibility,
                )
            )
            if patch_actions:
                mocks.run_remote_actions = stack.enter_context(mock.patch("timecapsulesmb.services.deploy.run_remote_actions"))
            if patch_upload:
                mocks.upload_deployment_payload = stack.enter_context(
                    mock.patch("timecapsulesmb.services.deploy.upload_deployment_payload", side_effect=upload_side_effect)
                )
            mocks.flush_remote_filesystem_writes = stack.enter_context(
                mock.patch("timecapsulesmb.services.deploy.flush_remote_filesystem_writes")
            )
            payload_verification_patch_kwargs = (
                {"side_effect": payload_verification_side_effect}
                if payload_verification_side_effect is not None
                else {"return_value": payload_verification or PayloadVerificationResult(True, "ok")}
            )
            mocks.verify_payload_home_conn = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.services.deploy.verify_payload_home_conn",
                    **payload_verification_patch_kwargs,
                )
            )
            mocks.verify_managed_runtime = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.services.runtime_verification.probe_managed_runtime_conn",
                    return_value=verify_runtime or self.managed_runtime_probe(True),
                )
            )
            mocks.probe_netbsd4_rc_local_autostart_conn = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.services.activation.probe_netbsd4_rc_local_autostart_conn",
                    return_value=SimpleNamespace(
                        enabled=login_autostart_enabled,
                        detail=(
                            "/etc/rc.d/LOGIN invokes /mnt/Flash/rc.local"
                            if login_autostart_enabled
                            else "/etc/rc.d/LOGIN does not invoke /mnt/Flash/rc.local"
                        ),
                        login_size=128,
                    ),
                )
            )
            mocks.remote_request_reboot = stack.enter_context(
                mock.patch("timecapsulesmb.services.reboot.remote_request_reboot", side_effect=reboot_side_effect)
            )
            mocks.acp_reboot = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.services.reboot.acp_reboot",
                    side_effect=AssertionError("deploy should not request ACP reboot"),
                )
            )
            if wait_side_effect is not None:
                mocks.wait_for_ssh_state_conn = stack.enter_context(
                    mock.patch("timecapsulesmb.services.reboot.wait_for_ssh_state_conn", side_effect=wait_side_effect)
                )
            mocks.runtime_wait_sleep = stack.enter_context(mock.patch("timecapsulesmb.services.runtime_verification.sleep"))
            if input_side_effect is not None:
                mocks.input = stack.enter_context(mock.patch("builtins.input", side_effect=input_side_effect))
            if raises is None:
                with redirect_stdout(output):
                    rc = deploy.main(argv or [])
            else:
                with self.assertRaises(raises) as raised_context:
                    with redirect_stdout(output):
                        deploy.main(argv or [])
                rc = None
                raised = raised_context.exception
        return SimpleNamespace(rc=rc, output=output, text=output.getvalue(), mocks=mocks, exception=raised)

    def test_deploy_dry_run_prints_mast_payload_placeholder(self) -> None:
        result = self.run_deploy_cli(
            ["--dry-run"],
            artifacts=[("smbd", True, "ok"), ("discovery", True, "ok")],
            patch_actions=True,
            patch_upload=True,
        )

        self.assertEqual(result.rc, 0)
        text = result.text
        self.assertIn("Dry run: deployment plan", text)
        self.assertIn("host: root@10.0.0.2", text)
        self.assertIn("volume root: resolved from MaSt at deploy time", text)
        self.assertIn("payload dir: resolved from MaSt at deploy time/.samba4", text)
        self.assertIn(f"diskd.useVolume wait: {DEFAULT_APPLE_MOUNT_WAIT_SECONDS}s per attempt", text)
        self.assertIn("generated flash runtime config", text)
        self.assertNotIn("generated smbpasswd", text)
        self.assertNotIn("generated:username.map", text)
        self.assertNotIn("rendered:smb.conf.template", text)
        self.assertNotIn("generated adisk", text)
        self.assertNotIn("generated nbns marker", text)
        result.mocks.run_remote_actions.assert_not_called()
        result.mocks.upload_deployment_payload.assert_not_called()
        result.mocks.wait_for_mast_volumes_conn.assert_not_called()
        result.mocks.select_payload_home_with_diagnostics_conn.assert_not_called()

    def test_deploy_no_input_requires_yes_before_remote_mutation(self) -> None:
        result = self.run_deploy_cli(
            ["--no-input"],
            patch_actions=True,
            patch_upload=True,
        )

        self.assertEqual(result.rc, 1)
        self.assertIn("Running `deploy` with reboot in non-interactive mode requires `--yes`", result.text)
        result.mocks.validate_artifacts.assert_not_called()
        result.mocks.wait_for_mast_volumes_conn.assert_not_called()
        result.mocks.run_remote_actions.assert_not_called()
        result.mocks.upload_deployment_payload.assert_not_called()

    def test_deploy_dry_run_json_outputs_modern_multivolume_plan(self) -> None:
        values = self.make_valid_env()
        result = self.run_deploy_cli(["--dry-run", "--json"], values=values)

        self.assertEqual(result.rc, 0)
        payload = json.loads(result.text)
        self.assertEqual(payload["startup_mode"], DEPLOY_STARTUP_REBOOT_THEN_VERIFY)
        self.assertEqual(payload["host"], "root@10.0.0.2")
        self.assertEqual(payload["volume_root"], "resolved from MaSt at deploy time")
        self.assertEqual(payload["device_path"], "resolved from MaSt at deploy time")
        self.assertEqual(payload["payload_dir"], "resolved from MaSt at deploy time/.samba4")
        self.assertEqual(payload["apple_mount_wait_seconds"], DEFAULT_APPLE_MOUNT_WAIT_SECONDS)
        self.assertEqual(payload["flash_targets"]["service"], "/mnt/Flash/service")
        self.assertIn(
            {
                "source_id": GENERATED_FLASH_CONFIG_SOURCE,
                "destination": "/mnt/Flash/tcapsulesmb.conf",
                "timeout_seconds": 120,
                "description": "generated flash runtime config",
            },
            [payload["config_upload"]],
        )
        self.assertNotIn("rendered:smb.conf.template", {upload["source_id"] for upload in payload["uploads"]})
        self.assertNotIn("generated:adisk.uuid", {upload["source_id"] for upload in payload["uploads"]})
        self.assertNotIn("generated:nbns.enabled", {upload["source_id"] for upload in payload["uploads"]})
        self.assertNotIn("initialize_data_root", {action["kind"] for action in payload["pre_upload_actions"]})
        self.assertIn("ensure_volume_mounted", {action["kind"] for action in payload["pre_upload_actions"]})
        self.assertEqual(payload["reboot_request"]["strategy"], "ssh_shutdown_then_reboot")
        self.assertEqual(
            [check["id"] for check in payload["post_deploy_checks"]],
            [
                "ssh_goes_down_after_reboot",
                "ssh_returns_after_reboot",
                "managed_runtime_smbd_binary_present",
                "managed_runtime_smb_conf_present",
                "active_smb_conf_passdb_ram",
                "active_smb_conf_username_map_ram",
                "active_smb_conf_xattr_tdb_persistent",
                "managed_share_volumes_mounted",
                "managed_runtime_manager_process",
                "managed_smbd_parent_process",
                "managed_smbd_bound_445",
                "managed_mdns_registrant_ready",
                "managed_mdns_settle_healthy",
                "managed_rsync_disabled",
            ],
        )

    def test_deploy_mount_wait_dry_run_json_uses_custom_value(self) -> None:
        result = self.run_deploy_cli(["--dry-run", "--json", "--mount-wait", "123"], values=self.make_valid_env())
        self.assertEqual(result.rc, 0)
        self.assertEqual(json.loads(result.text)["apple_mount_wait_seconds"], 123)

    def test_deploy_mount_wait_rejects_negative_values(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                deploy.main(["--dry-run", "--mount-wait", "-1"])
        self.assertEqual(raised.exception.code, 2)

    def test_deploy_selects_payload_home_from_mast_for_real_deploy(self) -> None:
        volumes = (
            self._mast_volume("dk3", disk_device="sd0", name="USB", builtin=False),
            self._mast_volume("dk2", disk_device="wd0", name="Data", builtin=True),
        )
        result = self.run_deploy_cli(
            ["--yes", "--mount-wait", "7"],
            mast_volumes=volumes,
            mount_root="/Volumes/dk2",
            patch_actions=True,
            patch_upload=True,
        )

        self.assertEqual(result.rc, 0)
        result.mocks.wait_for_mast_volumes_conn.assert_called_once()
        self.assertEqual(result.mocks.wait_for_mast_volumes_conn.call_args.kwargs["attempts"], 10)
        self.assertEqual(result.mocks.wait_for_mast_volumes_conn.call_args.kwargs["delay_seconds"], 3)
        result.mocks.select_payload_home_with_diagnostics_conn.assert_called_once_with(
            result.mocks.wait_for_mast_volumes_conn.call_args.args[0],
            volumes,
            ".samba4",
            wait_seconds=7,
        )
        self.assertEqual(result.mocks.run_remote_actions.call_count, 7)
        self.assertEqual(result.mocks.upload_deployment_payload.call_count, 4)
        payload_home = PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4")
        result.mocks.verify_payload_home_conn.assert_has_calls(
            [
                mock.call(result.mocks.wait_for_mast_volumes_conn.call_args.args[0], payload_home, wait_seconds=7),
                mock.call(result.mocks.wait_for_mast_volumes_conn.call_args.args[0], payload_home, wait_seconds=7),
            ]
        )
        self.assertEqual(result.mocks.verify_payload_home_conn.call_count, 2)
        self.assertEqual(result.mocks.flush_remote_filesystem_writes.call_count, 3)
        self.assertTrue(all(
            call.args == (result.mocks.wait_for_mast_volumes_conn.call_args.args[0],)
            for call in result.mocks.flush_remote_filesystem_writes.call_args_list
        ))
        self.assertIn("Deleting old deployed files...", result.text)
        self.assertIn("Flushing payload to disk...", result.text)
        self.assertIn("Deployed Samba payload to /Volumes/dk2/.samba4", result.text)
        self.assertIn("Updated /mnt/Flash boot files.", result.text)
        self.assertIn("Requesting reboot...", result.text)
        result.mocks.remote_request_reboot.assert_called_once()
        result.mocks.verify_managed_runtime.assert_called_once()
        self.assertIn("Deploy Finished.", result.text)

    def test_deploy_upload_source_resolver_contains_flash_config_and_no_legacy_generated_files(self) -> None:
        captured: dict[str, object] = {}

        def fake_upload(_plan, *, connection, source_resolver, on_uploading=None, on_uploaded=None):
            captured["host"] = connection.host
            captured["source_ids"] = set(source_resolver)
            captured["flash_config"] = source_resolver[GENERATED_FLASH_CONFIG_SOURCE].read_text()

        result = self.run_deploy_cli(
            ["--debug-logging", "--yes"],
            values=self.make_valid_env(TC_SAMBA_USER="admin"),
            patch_actions=True,
            patch_upload=True,
            upload_side_effect=fake_upload,
        )

        self.assertEqual(result.rc, 0)
        self.assertEqual(captured["host"], "root@10.0.0.2")
        self.assertNotIn("generated:smbpasswd", captured["source_ids"])
        self.assertNotIn("generated:username.map", captured["source_ids"])
        self.assertIn(GENERATED_FLASH_CONFIG_SOURCE, captured["source_ids"])
        self.assertIn(PACKAGED_BOOT_SOURCE, captured["source_ids"])
        self.assertIn(BINARY_SERVICE_SOURCE, captured["source_ids"])
        self.assertNotIn("rendered:smb.conf.template", captured["source_ids"])
        self.assertNotIn("generated:adisk.uuid", captured["source_ids"])
        self.assertNotIn("generated:nbns.enabled", captured["source_ids"])
        flash_config = str(captured["flash_config"])
        self.assertNotIn("TC_CONFIG_VERSION", flash_config)
        self.assertIn(f"TC_DEPLOY_RELEASE_TAG={RELEASE_TAG}\n", flash_config)
        self.assertIn(f"TC_DEPLOY_CLI_VERSION_CODE={CLI_VERSION_CODE}\n", flash_config)
        self.assertIn("TELEMETRY=true\n", flash_config)
        self.assertNotIn("PAYLOAD_DIR_NAME=", flash_config)
        self.assertNotIn("NBNS_ENABLED=", flash_config)
        self.assertIn("ANY_PROTOCOL=0\n", flash_config)
        self.assertIn("VFS_AIO_FORK_ENABLED=0\n", flash_config)
        self.assertIn("MDNS_ADVERTISE_AFP=0\n", flash_config)
        self.assertIn("SMBD_DEBUG_LOGGING=1\n", flash_config)
        self.assertIn("MDNS_DEBUG_LOGGING=1\n", flash_config)
        self.assertNotIn("SMB_SAMBA_USER", flash_config)
        self.assertNotIn("MDNS_DEVICE_MODEL", flash_config)
        self.assertNotIn("AIRPORT_SYAP", flash_config)
        self.assertNotIn("PAYLOAD_VOLUME_HINT", flash_config)
        self.assertNotIn("PAYLOAD_DEVICE_HINT", flash_config)
        self.assertNotIn("PAYLOAD_INSTALL_ID", flash_config)
        self.assertNotIn("TC_SHARE_NAME", flash_config)

    def test_deploy_writes_disabled_install_telemetry_preference_to_flash_config(self) -> None:
        captured: dict[str, str] = {}

        def fake_upload(_plan, *, connection, source_resolver, on_uploading=None, on_uploaded=None):
            captured["flash_config"] = source_resolver[GENERATED_FLASH_CONFIG_SOURCE].read_text()

        result = self.run_deploy_cli(
            ["--yes"],
            telemetry_enabled=False,
            patch_actions=True,
            patch_upload=True,
            upload_side_effect=fake_upload,
        )

        self.assertEqual(result.rc, 0)
        self.assertIn("TELEMETRY=false\n", captured["flash_config"])

    def test_deploy_rejects_removed_no_nbns_flag(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
            deploy.main(["--no-nbns", "--dry-run"])
        self.assertEqual(error.exception.code, 2)
        self.assertIn("unrecognized arguments: --no-nbns", stderr.getvalue())

    def test_deploy_enable_rsync_writes_flag_and_always_uploads_daemon_config(self) -> None:
        captured: dict[str, str] = {}

        def fake_upload(_plan, *, connection, source_resolver, on_uploading=None, on_uploaded=None):
            captured["flash_config"] = source_resolver[GENERATED_FLASH_CONFIG_SOURCE].read_text()
            captured["rsync_config"] = source_resolver[GENERATED_RSYNC_CONFIG_SOURCE].read_text()

        result = self.run_deploy_cli(
            ["--enable-rsync", "--yes"],
            patch_actions=True,
            patch_upload=True,
            upload_side_effect=fake_upload,
        )

        self.assertEqual(result.rc, 0)
        self.assertIn("RSYNC_ENABLED=1\n", captured["flash_config"])
        self.assertIn("port = 873\n", captured["rsync_config"])
        self.assertIn("path = /Volumes/dk2/ShareRoot\n", captured["rsync_config"])
        finished = self.telemetry_payload("deploy_finished")
        self.assertTrue(finished["rsync_enabled"])

    def test_deploy_debug_logging_arg_writes_enabled_flash_config(self) -> None:
        captured: dict[str, str] = {}

        def fake_upload(_plan, *, connection, source_resolver, on_uploading=None, on_uploaded=None):
            captured["flash_config"] = source_resolver[GENERATED_FLASH_CONFIG_SOURCE].read_text()

        result = self.run_deploy_cli(
            ["--debug-logging", "--yes"],
            values=self.make_valid_env(TC_DEBUG_LOGGING="false"),
            patch_actions=True,
            patch_upload=True,
            upload_side_effect=fake_upload,
        )

        self.assertEqual(result.rc, 0)
        self.assertIn("SMBD_DEBUG_LOGGING=1\n", captured["flash_config"])
        self.assertIn("MDNS_DEBUG_LOGGING=1\n", captured["flash_config"])

    def test_deploy_uses_configured_smb_browse_compatibility(self) -> None:
        captured: dict[str, str] = {}

        def fake_upload(_plan, *, connection, source_resolver, on_uploading=None, on_uploaded=None):
            captured["flash_config"] = source_resolver[GENERATED_FLASH_CONFIG_SOURCE].read_text()

        result = self.run_deploy_cli(
            ["--yes"],
            values=self.make_valid_env(TC_SMB_BROWSE_COMPATIBILITY="true"),
            patch_actions=True,
            patch_upload=True,
            upload_side_effect=fake_upload,
        )

        self.assertEqual(result.rc, 0)
        self.assertIn("SMB_BROWSE_COMPATIBILITY=1\n", captured["flash_config"])

    def test_deploy_uses_configured_mdns_advertise_afp(self) -> None:
        captured: dict[str, str] = {}

        def fake_upload(_plan, *, connection, source_resolver, on_uploading=None, on_uploaded=None):
            captured["flash_config"] = source_resolver[GENERATED_FLASH_CONFIG_SOURCE].read_text()

        result = self.run_deploy_cli(
            ["--yes"],
            values=self.make_valid_env(TC_MDNS_ADVERTISE_AFP="true"),
            patch_actions=True,
            patch_upload=True,
            upload_side_effect=fake_upload,
        )

        self.assertEqual(result.rc, 0)
        self.assertIn("MDNS_ADVERTISE_AFP=1\n", captured["flash_config"])

    def test_deploy_mdns_advertise_afp_arg_overrides_config(self) -> None:
        captured: dict[str, str] = {}

        def fake_upload(_plan, *, connection, source_resolver, on_uploading=None, on_uploaded=None):
            captured["flash_config"] = source_resolver[GENERATED_FLASH_CONFIG_SOURCE].read_text()

        result = self.run_deploy_cli(
            ["--yes", "--mdns-advertise-afp"],
            values=self.make_valid_env(TC_MDNS_ADVERTISE_AFP="false"),
            patch_actions=True,
            patch_upload=True,
            upload_side_effect=fake_upload,
        )

        self.assertEqual(result.rc, 0)
        self.assertIn("MDNS_ADVERTISE_AFP=1\n", captured["flash_config"])

    def test_deploy_require_smb_encryption_arg_overrides_config(self) -> None:
        captured: dict[str, str] = {}

        def fake_upload(_plan, *, connection, source_resolver, on_uploading=None, on_uploaded=None):
            captured["flash_config"] = source_resolver[GENERATED_FLASH_CONFIG_SOURCE].read_text()

        result = self.run_deploy_cli(
            ["--yes", "--require-smb-encryption"],
            values=self.make_valid_env(TC_REQUIRE_SMB_ENCRYPTION="false"),
            patch_actions=True,
            patch_upload=True,
            upload_side_effect=fake_upload,
        )

        self.assertEqual(result.rc, 0)
        self.assertIn("REQUIRE_SMB_ENCRYPTION=1\n", captured["flash_config"])

    def test_deploy_rejects_any_protocol_with_smb_encryption(self) -> None:
        with redirect_stderr(io.StringIO()):
            result = self.run_deploy_cli(
                ["--yes", "--any-protocol", "--require-smb-encryption"],
                raises=SystemExit,
            )
        self.assertEqual(result.exception.code, 2)

    def test_deploy_uses_configured_netatalk_metadata(self) -> None:
        captured: dict[str, str] = {}

        def fake_upload(_plan, *, connection, source_resolver, on_uploading=None, on_uploaded=None):
            captured["flash_config"] = source_resolver[GENERATED_FLASH_CONFIG_SOURCE].read_text()

        result = self.run_deploy_cli(
            ["--yes"],
            values=self.make_valid_env(TC_FRUIT_METADATA_NETATALK="true"),
            patch_actions=True,
            patch_upload=True,
            upload_side_effect=fake_upload,
        )

        self.assertEqual(result.rc, 0)
        self.assertIn("FRUIT_METADATA_NETATALK=1\n", captured["flash_config"])

    def test_deploy_vfs_aio_fork_args_override_config(self) -> None:
        captured: list[str] = []

        def fake_upload(_plan, *, connection, source_resolver, on_uploading=None, on_uploaded=None):
            if _plan.uploads != [_plan.config_upload]:
                return
            captured.append(source_resolver[GENERATED_FLASH_CONFIG_SOURCE].read_text())

        enabled = self.run_deploy_cli(
            ["--yes", "--enable-vfs-aio-fork"],
            values=self.make_valid_env(TC_VFS_AIO_FORK_ENABLED="false"),
            patch_actions=True,
            patch_upload=True,
            upload_side_effect=fake_upload,
        )
        disabled = self.run_deploy_cli(
            ["--yes", "--disable-vfs-aio-fork"],
            values=self.make_valid_env(TC_VFS_AIO_FORK_ENABLED="true"),
            patch_actions=True,
            patch_upload=True,
            upload_side_effect=fake_upload,
        )

        self.assertEqual(enabled.rc, 0)
        self.assertEqual(disabled.rc, 0)
        self.assertIn("VFS_AIO_FORK_ENABLED=1\n", captured[0])
        self.assertIn("VFS_AIO_FORK_ENABLED=0\n", captured[1])

    def test_deploy_preserves_configured_debug_logging_without_arg(self) -> None:
        captured: dict[str, str] = {}

        def fake_upload(_plan, *, connection, source_resolver, on_uploading=None, on_uploaded=None):
            captured["flash_config"] = source_resolver[GENERATED_FLASH_CONFIG_SOURCE].read_text()

        result = self.run_deploy_cli(
            ["--yes"],
            values=self.make_valid_env(TC_DEBUG_LOGGING="true"),
            patch_actions=True,
            patch_upload=True,
            upload_side_effect=fake_upload,
        )

        self.assertEqual(result.rc, 0)
        self.assertIn("SMBD_DEBUG_LOGGING=1\n", captured["flash_config"])
        self.assertIn("MDNS_DEBUG_LOGGING=1\n", captured["flash_config"])

    def test_deploy_profile_boolean_override_pairs_enable_and_disable_values(self) -> None:
        captured: list[str] = []

        def fake_upload(_plan, *, connection, source_resolver, on_uploading=None, on_uploaded=None):
            if _plan.uploads != [_plan.config_upload]:
                return
            captured.append(source_resolver[GENERATED_FLASH_CONFIG_SOURCE].read_text())

        enabled = self.run_deploy_cli(
            [
                "--yes",
                "--internal-share-use-disk-root",
                "--smb-browse-compatibility",
                "--netatalk",
                "--debug-logging",
            ],
            values=self.make_valid_env(
                TC_INTERNAL_SHARE_USE_DISK_ROOT="false",
                TC_SMB_BROWSE_COMPATIBILITY="false",
                TC_FRUIT_METADATA_NETATALK="false",
                TC_DEBUG_LOGGING="false",
            ),
            patch_actions=True,
            patch_upload=True,
            upload_side_effect=fake_upload,
        )
        disabled = self.run_deploy_cli(
            [
                "--yes",
                "--no-internal-share-use-disk-root",
                "--no-smb-browse-compatibility",
                "--no-netatalk",
                "--no-debug-logging",
            ],
            values=self.make_valid_env(
                TC_INTERNAL_SHARE_USE_DISK_ROOT="true",
                TC_SMB_BROWSE_COMPATIBILITY="true",
                TC_FRUIT_METADATA_NETATALK="true",
                TC_DEBUG_LOGGING="true",
            ),
            patch_actions=True,
            patch_upload=True,
            upload_side_effect=fake_upload,
        )

        self.assertEqual(enabled.rc, 0)
        self.assertEqual(disabled.rc, 0)
        for line in (
            "INTERNAL_SHARE_USE_DISK_ROOT=1\n",
            "SMB_BROWSE_COMPATIBILITY=1\n",
            "FRUIT_METADATA_NETATALK=1\n",
            "SMBD_DEBUG_LOGGING=1\n",
        ):
            self.assertIn(line, captured[0])
        for line in (
            "INTERNAL_SHARE_USE_DISK_ROOT=0\n",
            "SMB_BROWSE_COMPATIBILITY=0\n",
            "FRUIT_METADATA_NETATALK=0\n",
            "SMBD_DEBUG_LOGGING=0\n",
        ):
            self.assertIn(line, captured[1])

    def test_deploy_dry_run_no_wait_json_outputs_request_only_plan(self) -> None:
        result = self.run_deploy_cli(["--dry-run", "--json", "--no-wait"], values=self.make_valid_env())
        self.assertEqual(result.rc, 0)
        payload = json.loads(result.text)
        self.assertTrue(payload["reboot_required"])
        self.assertFalse(payload["wait_after_reboot"])
        self.assertEqual(payload["reboot_request"]["follow_up"], ["return_after_reboot_request"])
        self.assertEqual(payload["activation_actions"], [])
        self.assertEqual(payload["post_deploy_checks"], [])

    def test_deploy_netbsd4_dry_run_no_wait_json_outputs_request_only_plan(self) -> None:
        result = self.run_deploy_cli(
            ["--dry-run", "--json", "--no-wait"],
            artifacts=[("smbd-netbsd4le", True, "ok")],
            compatibility=self.make_supported_netbsd4_compatibility(),
        )
        self.assertEqual(result.rc, 0)
        payload = json.loads(result.text)
        self.assertEqual(payload["startup_mode"], DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE)
        self.assertTrue(payload["reboot_required"])
        self.assertFalse(payload["wait_after_reboot"])
        self.assertEqual(payload["reboot_request"]["follow_up"], ["return_after_reboot_request"])
        self.assertEqual(payload["activation_actions"], [])
        self.assertEqual(payload["post_deploy_checks"], [])

    def test_deploy_rejects_removed_install_nbns_flag(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                deploy.main(["--install-nbns", "--dry-run"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unrecognized arguments: --install-nbns", stderr.getvalue())

    def test_deploy_exits_when_mast_volumes_are_not_writable(self) -> None:
        volumes = (self._mast_volume("dk2"),)
        result = self.run_deploy_cli(
            ["--yes"],
            mast_volumes=volumes,
            payload_home_selection=PayloadHomeSelection(
                None, (PayloadCandidateCheck(volumes[0], VolumeMountResult(True, "use_volume_rcs=0 mounted=yes"), False),),
            ),
            patch_actions=True,
            patch_upload=True,
            raises=SystemExit,
        )

        self.assertEqual(
            str(result.exception),
            "MaSt found 1 deployable HFS volume(s), but deploy could not write to any of them.",
        )
        result.mocks.run_remote_actions.assert_not_called()
        result.mocks.upload_deployment_payload.assert_not_called()
        telemetry_error = self.telemetry_payload("deploy_finished")["error"]
        self.assertIn("stage=select_payload_home", telemetry_error)
        self.assertIn("mast_volume_count=1", telemetry_error)
        self.assertIn("mast_candidates=[{disk:wd0,part:dk2", telemetry_error)
        self.assertIn("mast_candidate_checks=[{disk:wd0,part:dk2", telemetry_error)
        self.assertIn("mounted:true", telemetry_error)
        self.assertIn("writable:false", telemetry_error)

    def test_deploy_says_not_mounted_when_no_mast_volume_could_be_mounted(self) -> None:
        # MaSt lists the volume but diskd never mounted it: "could not write"
        # would send the user to check free space for a disk that is asleep.
        volumes = (self._mast_volume("dk2"),)
        check = PayloadCandidateCheck(volumes[0], VolumeMountResult(False, "use_volume_rcs=1,1 mounted=no"), None)
        result = self.run_deploy_cli(
            ["--yes"],
            mast_volumes=volumes,
            payload_home_selection=PayloadHomeSelection(None, (check,)),
            patch_actions=True,
            patch_upload=True,
            raises=SystemExit,
        )

        self.assertEqual(
            str(result.exception),
            "MaSt found 1 deployable HFS volume(s), but none of them was mounted and the device did not "
            "mount one when asked. Wait a minute and retry, or restart the device.",
        )
        result.mocks.run_remote_actions.assert_not_called()
        telemetry_error = self.telemetry_payload("deploy_finished")["error"]
        self.assertIn("mounted:false", telemetry_error)
        self.assertIn("mount:use_volume_rcs=1,1 mounted=no", telemetry_error)

    def test_deploy_says_the_device_did_not_confirm_a_volume_that_is_mounted(self) -> None:
        # diskd refused the claim, but the volume is mounted: "none of them
        # was mounted" would be wrong, and deploy must still not use it.
        volumes = (self._mast_volume("dk2"),)
        check = PayloadCandidateCheck(volumes[0], VolumeMountResult(False, "use_volume_rcs=1,1 mounted=yes"), None)
        result = self.run_deploy_cli(
            ["--yes"],
            mast_volumes=volumes,
            payload_home_selection=PayloadHomeSelection(None, (check,)),
            patch_actions=True,
            patch_upload=True,
            raises=SystemExit,
        )

        self.assertEqual(
            str(result.exception),
            "MaSt found 1 deployable HFS volume(s). A volume was mounted, but the device did not confirm it "
            "for TimeCapsuleSMB, so it could be unmounted during deploy. Wait a minute and retry, or restart the device.",
        )
        result.mocks.run_remote_actions.assert_not_called()
        result.mocks.upload_deployment_payload.assert_not_called()
        telemetry_error = self.telemetry_payload("deploy_finished")["error"]
        self.assertIn("mounted:false", telemetry_error)
        self.assertIn("mount:use_volume_rcs=1,1 mounted=yes", telemetry_error)

    def test_deploy_exits_when_mast_discovery_never_finds_disks(self) -> None:
        raw_mast_output = "MaSt=<plist><array/></plist>"
        result = self.run_deploy_cli(
            ["--yes"],
            mast_volumes=(),
            mast_discovery=MaStDiscoveryResult((), 10, raw_mast_output),
            patch_actions=True,
            patch_upload=True,
            raises=SystemExit,
        )

        self.assertEqual(
            str(result.exception),
            "No internal disk was detected after 10 MaSt queries spaced 3 seconds apart.",
        )
        result.mocks.wait_for_mast_volumes_conn.assert_called_once()
        result.mocks.select_payload_home_with_diagnostics_conn.assert_not_called()
        result.mocks.run_remote_actions.assert_not_called()
        result.mocks.upload_deployment_payload.assert_not_called()
        telemetry_error = self.telemetry_payload("deploy_finished")["error"]
        self.assertIn("stage=read_mast", telemetry_error)
        self.assertIn("mast_read_attempts=10", telemetry_error)
        self.assertIn("mast_volume_count=0", telemetry_error)
        self.assertIn("mast_candidates=[]", telemetry_error)
        self.assertIn(f"mast_acp_output_chars={len(raw_mast_output)}", telemetry_error)
        self.assertIn(f"mast_acp_output={raw_mast_output}", telemetry_error)
        self.assertIn("mast_disk_inventory=[]", telemetry_error)

    def test_deploy_exits_when_mast_finds_disk_without_hfs_partition(self) -> None:
        raw_mast_output = plistlib.dumps(
            [
                {
                    "deviceName": "wd0",
                    "name": "Seagate Expansion HDD",
                    "size": 8_000_000_000_000,
                    "builtin": True,
                    "partitions": [
                        {
                            "deviceName": "dk2",
                            "name": "PS3FAT",
                            "format": "msdos",
                        }
                    ],
                }
            ]
        ).decode()
        result = self.run_deploy_cli(
            ["--yes"],
            mast_volumes=(),
            mast_discovery=MaStDiscoveryResult((), 1, raw_mast_output),
            patch_actions=True,
            patch_upload=True,
            raises=SystemExit,
        )

        self.assertEqual(
            str(result.exception),
            "A disk was found, but no valid HFS partition was detected. "
            "Retry again, or erase the disk with AirPort Utility (Erase Disk) to format it for the Time Capsule. "
            "Note: some devices cannot detect some partitions larger than 2 TB.",
        )
        result.mocks.wait_for_mast_volumes_conn.assert_called_once()
        result.mocks.select_payload_home_with_diagnostics_conn.assert_not_called()
        result.mocks.run_remote_actions.assert_not_called()
        result.mocks.upload_deployment_payload.assert_not_called()
        telemetry_error = self.telemetry_payload("deploy_finished")["error"]
        self.assertIn("stage=read_mast", telemetry_error)
        self.assertIn("mast_read_attempts=1", telemetry_error)
        self.assertIn("mast_volume_count=0", telemetry_error)
        self.assertIn("mast_disk_inventory=[{disk:wd0", telemetry_error)
        self.assertIn("format:msdos", telemetry_error)
        self.assertIn("name:PS3FAT", telemetry_error)
        self.assertIn("size:8000000000000", telemetry_error)

    def test_deploy_rejects_removed_no_reboot_before_remote_access(self) -> None:
        with mock.patch("timecapsulesmb.cli.deploy.load_env_config") as config:
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                deploy.main(["--no-reboot"])
        self.assertEqual(error.exception.code, 2)
        config.assert_not_called()

    def test_deploy_no_wait_requests_reboot_without_wait_or_runtime_verify(self) -> None:
        result = self.run_deploy_cli(
            ["--yes", "--no-wait"],
            artifacts=[("smbd", True, "ok"), ("discovery", True, "ok")],
            patch_actions=True,
            patch_upload=True,
            wait_side_effect=AssertionError("deploy --no-wait should not wait for SSH"),
            verify_runtime=AssertionError("deploy --no-wait should not verify runtime"),
        )

        self.assertEqual(result.rc, 0)
        result.mocks.remote_request_reboot.assert_called_once()
        result.mocks.wait_for_ssh_state_conn.assert_not_called()
        result.mocks.verify_managed_runtime.assert_not_called()
        self.assertEqual(result.mocks.run_remote_actions.call_count, 7)
        self.assertIn("Requesting reboot...", result.text)
        self.assertIn("Reboot requested; not waiting for the device to go down or come back.", result.text)
        self.assertIn("Post-reboot runtime verification skipped.", result.text)
        finished = self.telemetry_payload("deploy_finished")
        self.assertTrue(finished["reboot_was_attempted"])
        self.assertFalse(finished["device_came_back_after_reboot"])

    def test_deploy_netbsd4_no_wait_requests_reboot_without_activation(self) -> None:
        result = self.run_deploy_cli(
            ["--yes", "--no-wait"],
            values=self.make_valid_env(TC_PAYLOAD_DIR_NAME="samba4"),
            artifacts=[("smbd-netbsd4le", True, "ok")],
            compatibility=self.make_supported_netbsd4_compatibility(),
            patch_actions=True,
            patch_upload=True,
            wait_side_effect=AssertionError("deploy --no-wait should not wait for SSH"),
            verify_runtime=AssertionError("deploy --no-wait should not verify runtime"),
        )

        self.assertEqual(result.rc, 0)
        result.mocks.remote_request_reboot.assert_called_once()
        result.mocks.wait_for_ssh_state_conn.assert_not_called()
        result.mocks.verify_managed_runtime.assert_not_called()
        self.assertEqual(result.mocks.run_remote_actions.call_count, 7)
        self.assertNotIn("Activating deployed runtime after reboot.", result.text)
        self.assertNotIn("NetBSD4 activation completed.", result.text)
        self.assertIn("Post-reboot runtime verification skipped.", result.text)

    def test_deploy_payload_verification_failure_aborts_before_reboot(self) -> None:
        result = self.run_deploy_cli(
            ["--yes"],
            patch_actions=True,
            patch_upload=True,
            payload_verification=PayloadVerificationResult(False, "missing smbd"),
            reboot_side_effect=AssertionError("deploy should not request reboot after payload verification failure"),
            raises=SystemExit,
        )

        self.assertEqual(str(result.exception), "managed payload verification failed at /Volumes/dk2/.samba4: missing smbd")
        result.mocks.remote_request_reboot.assert_not_called()
        result.mocks.verify_payload_home_conn.assert_called_once()
        result.mocks.flush_remote_filesystem_writes.assert_called_once()
        telemetry_error = self.telemetry_payload("deploy_finished")["error"]
        self.assertIn("stage=verify_payload_upload", telemetry_error)
        self.assertIn("managed payload verification failed", telemetry_error)

    def test_deploy_post_sync_payload_verification_failure_aborts_before_reboot(self) -> None:
        result = self.run_deploy_cli(
            ["--yes"],
            patch_actions=True,
            patch_upload=True,
            payload_verification_side_effect=[
                PayloadVerificationResult(True, "ok"),
                PayloadVerificationResult(False, "missing payload directory"),
            ],
            reboot_side_effect=AssertionError("deploy should not request reboot after post-sync verification failure"),
            raises=SystemExit,
        )

        self.assertEqual(
            str(result.exception),
            "managed payload verification failed at /Volumes/dk2/.samba4: missing payload directory",
        )
        self.assertEqual(result.mocks.flush_remote_filesystem_writes.call_count, 2)
        result.mocks.remote_request_reboot.assert_not_called()
        self.assertEqual(result.mocks.verify_payload_home_conn.call_count, 2)
        telemetry_error = self.telemetry_payload("deploy_finished")["error"]
        self.assertIn("stage=verify_payload_upload_after_sync", telemetry_error)
        self.assertIn("payload_post_sync_verification=missing payload directory", telemetry_error)

    def test_deploy_declined_confirmation_returns_before_mutation(self) -> None:
        result = self.run_deploy_cli(
            [],
            artifacts=[("smbd", True, "ok"), ("discovery", True, "ok")],
            patch_actions=True,
            patch_upload=True,
            reboot_side_effect=AssertionError("declined deploy should not request a reboot"),
            input_side_effect=["n"],
        )

        self.assertEqual(result.rc, 0)
        self.assertIn("Deployment cancelled.", result.text)
        result.mocks.remote_request_reboot.assert_not_called()
        result.mocks.verify_payload_home_conn.assert_not_called()
        result.mocks.flush_remote_filesystem_writes.assert_not_called()
        result.mocks.run_remote_actions.assert_not_called()
        result.mocks.upload_deployment_payload.assert_not_called()

    def test_deploy_reboot_timeout_returns_failure(self) -> None:
        result = self.run_deploy_cli(
            ["--yes"],
            artifacts=[("smbd", True, "ok"), ("discovery", True, "ok")],
            patch_actions=True,
            patch_upload=True,
            reboot_side_effect=SshCommandTimeout("reboot timed out"),
            wait_side_effect=[False],
            verify_runtime=self.managed_runtime_probe(True),
        )

        self.assertEqual(result.rc, 1)
        self.assertIn("SSH reboot request timed out; checking whether the device is rebooting...", result.text)
        self.assertIn(DEPLOY_REBOOT_NO_DOWN_MESSAGE, result.text)
        result.mocks.remote_request_reboot.assert_called_once()
        result.mocks.acp_reboot.assert_not_called()
        result.mocks.verify_managed_runtime.assert_not_called()

    def test_deploy_failure_telemetry_includes_current_stage(self) -> None:
        result = self.run_deploy_cli(
            ["--yes"],
            patch_actions=True,
            patch_upload=True,
            upload_side_effect=RuntimeError("upload failed"),
            raises=RuntimeError,
        )

        self.assertEqual(str(result.exception), "upload failed")
        finished = self.telemetry_payload("deploy_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertIn("stage=upload_xattr_migrator", finished["error"])
        self.assertIn("RuntimeError: upload failed", finished["error"])

    def test_deploy_ssh_timeout_shows_red_slow_device_guidance_and_keeps_telemetry_detail(self) -> None:
        timeout = "Timed out waiting for ssh command to finish: /bin/sh -c 'wc -c < /mnt/Flash/service'"

        def timeout_upload(plan, *, connection, source_resolver, on_uploading=None, on_uploaded=None):
            if plan.uploads == [plan.migration_upload]:
                return
            if on_uploading is not None:
                on_uploading(next(transfer for transfer in plan.uploads if transfer.destination == "/mnt/Flash/service"))
            raise SshCommandTimeout(timeout)

        result = self.run_deploy_cli(
            ["--yes"],
            patch_actions=True,
            patch_upload=True,
            upload_side_effect=timeout_upload,
            raises=SystemExit,
        )

        slow_message = ssh_timeout_slow_device_message("Time Capsule 5th generation")
        self.assertIn(f"{ANSI_RED}{slow_message}{ANSI_RESET}", str(result.exception))
        self.assertIn(timeout, str(result.exception))
        finished = self.telemetry_payload("deploy_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertIn(slow_message, finished["error"])
        self.assertIn(timeout, finished["error"])
        self.assertIn("stage=upload_boot_files", finished["error"])
        self.assertNotIn(ANSI_RED, finished["error"])

    def test_deploy_payload_upload_timeout_shows_disk_guidance(self) -> None:
        timeout = "Timed out waiting for ssh command to finish: runtime probe"

        def timeout_upload(plan, *, connection, source_resolver, on_uploading=None, on_uploaded=None):
            if plan.uploads == [plan.migration_upload]:
                return
            if on_uploading is not None:
                on_uploading(plan.uploads[0])
            raise SshCommandTimeout(timeout)

        result = self.run_deploy_cli(
            ["--yes"],
            values=self.make_valid_env(TC_AIRPORT_SYAP="120", TC_MDNS_DEVICE_MODEL="AirPort7,120"),
            patch_actions=True,
            patch_upload=True,
            upload_side_effect=timeout_upload,
            raises=SystemExit,
        )

        self.assertIn("The disk did not respond while copying the SMB payload.", str(result.exception))
        self.assertNotIn(timeout, str(result.exception))
        finished = self.telemetry_payload("deploy_finished")
        self.assertEqual(finished["result"], "failure")
        self.assertIn("The disk did not respond while copying the SMB payload.", finished["error"])
        self.assertIn(f"Caused by: {timeout}", finished["error"])

    def test_deploy_finished_telemetry_reports_ssh_pipe_upload_transport(self) -> None:
        result = self.run_deploy_cli(
            ["--yes"],
            patch_actions=True,
            patch_upload=True,
            verify_runtime=self.managed_runtime_probe(True),
            wait_side_effect=[True, True],
        )

        self.assertEqual(result.rc, 0)
        finished = self.telemetry_payload("deploy_finished")
        self.assertEqual(finished["upload_transport"], "ssh_pipe")

    def test_deploy_netbsd4_dry_run_json_outputs_activation_plan(self) -> None:
        result = self.run_deploy_cli(
            ["--dry-run", "--json"],
            artifacts=[("smbd-netbsd4le", True, "ok")],
            compatibility=self.make_supported_netbsd4_compatibility(),
        )
        self.assertEqual(result.rc, 0)
        payload = json.loads(result.text)
        self.assertTrue(payload["reboot_required"])
        self.assertEqual(payload["startup_mode"], DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE)
        self.assertEqual(payload["reboot_request"]["strategy"], "ssh_shutdown_then_reboot")
        self.assertEqual(
            payload["runtime_startup"]["post_reboot_probe"],
            {
                "kind": "netbsd4_rc_local_autostart",
                "path": "/etc/rc.d/LOGIN",
                "marker": "/mnt/Flash/rc.local",
                "if_present": ["skip_post_reboot_start_actions", "verify_managed_runtime"],
                "if_missing": ["run_post_reboot_start_actions", "verify_managed_runtime"],
            },
        )
        self.assertEqual(
            [action["kind"] for action in payload["activation_actions"]],
            ["run_script"],
        )
        self.assertEqual(
            [action["args"] for action in payload["activation_actions"]],
            [["/mnt/Flash/rc.local"]],
        )
        self.assertEqual(
            [check["id"] for check in payload["post_deploy_checks"]],
            [
                "ssh_goes_down_after_reboot",
                "ssh_returns_after_reboot",
                "managed_runtime_smbd_binary_present",
                "managed_runtime_smb_conf_present",
                "active_smb_conf_passdb_ram",
                "active_smb_conf_username_map_ram",
                "active_smb_conf_xattr_tdb_persistent",
                "managed_share_volumes_mounted",
                "managed_runtime_manager_process",
                "managed_smbd_parent_process",
                "managed_smbd_bound_445",
                "managed_mdns_registrant_ready",
                "managed_mdns_settle_healthy",
                "managed_rsync_disabled",
            ],
        )

    def test_deploy_netbsd4_yes_reboots_then_runs_activation(self) -> None:
        result = self.run_deploy_cli(
            ["--yes"],
            values=self.make_valid_env(TC_PAYLOAD_DIR_NAME="samba4"),
            artifacts=[("smbd-netbsd4le", True, "ok")],
            compatibility=self.make_supported_netbsd4_compatibility(),
            patch_actions=True,
            patch_upload=True,
            verify_runtime=self.managed_runtime_probe(True),
            wait_side_effect=[True, True],
        )

        self.assertEqual(result.rc, 0)
        self.assertEqual(result.mocks.run_remote_actions.call_count, 8)
        self.assertEqual(result.mocks.verify_payload_home_conn.call_count, 2)
        self.assertEqual(result.mocks.flush_remote_filesystem_writes.call_count, 3)
        result.mocks.remote_request_reboot.assert_called_once()
        self.assertEqual(
            result.mocks.run_remote_actions.call_args_list[-1].args[1],
            [RunScriptAction("/mnt/Flash/rc.local")],
        )
        self.assertIn("Activating deployed runtime after reboot.", result.text)
        self.assertIn("NetBSD4 activation completed.", result.text)

    def test_deploy_netbsd4_uses_transport_neutral_connection(self) -> None:
        result = self.run_deploy_cli(
            ["--yes"],
            values=self.make_valid_env(TC_PAYLOAD_DIR_NAME="samba4"),
            artifacts=[("smbd-netbsd4le", True, "ok")],
            compatibility=self.make_supported_netbsd4_compatibility(),
            patch_actions=True,
            patch_upload=True,
            verify_runtime=self.managed_runtime_probe(True),
            wait_side_effect=[True, True],
        )

        self.assertEqual(result.rc, 0)
        upload_connection = result.mocks.upload_deployment_payload.call_args.kwargs["connection"]
        self.assertEqual(set(vars(upload_connection)), {"host", "password", "ssh_opts"})

    def test_deploy_netbsd6_uses_transport_neutral_connection(self) -> None:
        result = self.run_deploy_cli(
            ["--yes"],
            patch_actions=True,
            patch_upload=True,
            verify_runtime=self.managed_runtime_probe(True),
            wait_side_effect=[True, True],
        )

        self.assertEqual(result.rc, 0)
        upload_connection = result.mocks.upload_deployment_payload.call_args.kwargs["connection"]
        self.assertEqual(set(vars(upload_connection)), {"host", "password", "ssh_opts"})

    def test_deploy_netbsd4_yes_waits_when_firmware_autostarts_runtime(self) -> None:
        result = self.run_deploy_cli(
            ["--yes"],
            values=self.make_valid_env(TC_PAYLOAD_DIR_NAME="samba4"),
            artifacts=[("smbd-netbsd4le", True, "ok")],
            compatibility=self.make_supported_netbsd4_compatibility(),
            patch_actions=True,
            patch_upload=True,
            login_autostart_enabled=True,
            verify_runtime=self.managed_runtime_probe(True),
            wait_side_effect=[True, True],
        )

        self.assertEqual(result.rc, 0)
        self.assertEqual(result.mocks.run_remote_actions.call_count, 7)
        result.mocks.remote_request_reboot.assert_called_once()
        result.mocks.verify_managed_runtime.assert_called_once()
        self.assertIn("/etc/rc.d/LOGIN invokes /mnt/Flash/rc.local", result.text)
        self.assertIn("NetBSD4 firmware autostart is enabled", result.text)
        self.assertNotIn("Activating deployed runtime after reboot.", result.text)
        self.assertIn("NetBSD4 activation completed.", result.text)

    def test_deploy_rejects_unsupported_device(self) -> None:
        unsupported = DeviceCompatibility(
            os_name="Linux",
            os_release="6.8",
            arch="armv7",
            elf_endianness="unknown",
            payload_family=None,
            device_generation="unknown",
            supported=False,
            reason_code="unsupported_os",
        )
        result = self.run_deploy_cli(
            ["--dry-run"],
            values=self.make_valid_env(TC_PAYLOAD_DIR_NAME="samba4"),
            artifacts=[("smbd", True, "ok"), ("discovery", True, "ok")],
            compatibility=unsupported,
            raises=SystemExit,
        )

        self.assertIn("Linux", str(result.exception))

    def test_deploy_allow_unsupported_still_fails_without_payload_family(self) -> None:
        unsupported = DeviceCompatibility(
            os_name="Linux",
            os_release="6.8",
            arch="armv7",
            elf_endianness="unknown",
            payload_family=None,
            device_generation="unknown",
            supported=False,
            reason_code="unsupported_os",
        )
        result = self.run_deploy_cli(
            ["--dry-run", "--allow-unsupported"],
            values=self.make_valid_env(TC_PAYLOAD_DIR_NAME="samba4"),
            artifacts=[("smbd", True, "ok"), ("discovery", True, "ok")],
            compatibility=unsupported,
            raises=SystemExit,
        )

        text = str(result.exception)
        self.assertIn("Linux", text)
        self.assertIn("No deployable payload is available", text)


if __name__ == "__main__":
    unittest.main()
