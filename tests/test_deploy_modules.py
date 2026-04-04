from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.deploy.auth import nt_hash_hex, render_smbpasswd
from timecapsulesmb.deploy.dry_run import format_deployment_plan
from timecapsulesmb.deploy.executor import remote_install_permissions, remote_prepare_dirs, upload_deployment_payload
from timecapsulesmb.deploy.planner import build_deployment_plan
from timecapsulesmb.deploy.templates import build_template_bundle, load_boot_asset_text, render_template, render_template_text
from timecapsulesmb.device.probe import build_device_paths, discover_volume_root


class DeployModuleTests(unittest.TestCase):
    def test_nt_hash_hex_is_stable(self) -> None:
        self.assertEqual(nt_hash_hex("password"), "8846F7EAEE8FB117AD06BDD830B7586C")

    def test_render_smbpasswd_contains_root_mapping(self) -> None:
        smbpasswd_text, username_map = render_smbpasswd("admin", "password")
        self.assertTrue(smbpasswd_text.startswith("root:0:"))
        self.assertEqual(username_map, "root = admin\n")

    def test_build_template_bundle_contains_expected_keys(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        self.assertIn("__SMB_SHARE_NAME__", bundle.start_script_replacements)
        self.assertIn("__SMB_SAMBA_USER__", bundle.smbconf_replacements)

    def test_build_deployment_plan_uses_device_paths(self) -> None:
        paths = build_device_paths("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("root@10.0.0.2", paths, Path("bin/smbd"), Path("bin/mdns"))
        self.assertEqual(plan.payload_dir, "/Volumes/dk2/samba4")
        self.assertEqual(plan.private_dir, "/Volumes/dk2/samba4/private")
        self.assertEqual(plan.volume_root, "/Volumes/dk2")
        self.assertEqual(plan.remote_directories[0], "/Volumes/dk2/samba4")

    def test_render_template_text_replaces_tokens(self) -> None:
        self.assertEqual(render_template_text("hello __TOKEN__", {"__TOKEN__": "world"}), "hello world")

    def test_load_boot_asset_text_reads_packaged_asset(self) -> None:
        content = load_boot_asset_text("rc.local")
        self.assertIn("/mnt/Flash/start-samba.sh", content)

    def test_discover_volume_root_raises_when_no_output(self) -> None:
        proc = mock.Mock(stdout="\n")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            with self.assertRaises(SystemExit):
                discover_volume_root("root@10.0.0.2", "pw", "-o foo")

    def test_remote_prepare_dirs_builds_expected_command(self) -> None:
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            remote_prepare_dirs("host", "pw", "-o foo", "/Volumes/dk2/samba4")
        command = run_ssh_mock.call_args.args[3]
        self.assertIn("mkdir -p", command)
        self.assertIn("/Volumes/dk2/samba4/private", command)

    def test_remote_install_permissions_builds_expected_command(self) -> None:
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            remote_install_permissions("host", "pw", "-o foo", "/Volumes/dk2/samba4")
        command = run_ssh_mock.call_args.args[3]
        self.assertIn("chmod 755 /mnt/Flash/rc.local", command)
        self.assertIn("chmod 700", command)

    def test_upload_deployment_payload_uploads_all_expected_files(self) -> None:
        paths = build_device_paths("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("host", paths, Path("bin/smbd"), Path("bin/mdns"))
        with mock.patch("timecapsulesmb.deploy.executor.run_scp") as scp_mock:
            upload_deployment_payload(
                plan,
                host="host",
                password="pw",
                ssh_opts="-o foo",
                rc_local=Path("/tmp/rc.local"),
                rendered_start=Path("/tmp/start-samba.sh"),
                rendered_dfree=Path("/tmp/dfree.sh"),
                rendered_watchdog=Path("/tmp/watchdog.sh"),
                rendered_smbconf=Path("/tmp/smb.conf.template"),
            )
        self.assertEqual(scp_mock.call_count, 7)

    def test_format_deployment_plan_contains_concrete_actions(self) -> None:
        paths = build_device_paths("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("root@10.0.0.2", paths, Path("bin/smbd"), Path("bin/mdns"))
        text = format_deployment_plan(plan)
        self.assertIn("volume root: /Volumes/dk2", text)
        self.assertIn("mkdir -p /Volumes/dk2/samba4", text)
        self.assertIn("generated smbpasswd -> /Volumes/dk2/samba4/private/smbpasswd", text)
        self.assertIn("chmod 700 /Volumes/dk2/samba4/private", text)


if __name__ == "__main__":
    unittest.main()
