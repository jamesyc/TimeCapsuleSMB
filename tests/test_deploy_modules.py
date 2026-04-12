from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import uuid


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.deploy.auth import nt_hash_hex, render_smbpasswd
from timecapsulesmb.deploy.dry_run import format_deployment_plan
from timecapsulesmb.deploy.executor import (
    remote_enable_nbns,
    remote_ensure_adisk_uuid,
    remote_install_permissions,
    remote_prepare_dirs,
    upload_deployment_payload,
)
from timecapsulesmb.deploy.planner import build_deployment_plan, build_uninstall_plan
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
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        self.assertIn("__SMB_SHARE_NAME__", bundle.start_script_replacements)
        self.assertIn("__SMB_SAMBA_USER__", bundle.smbconf_replacements)
        self.assertEqual(bundle.start_script_replacements["__MDNS_DEVICE_MODEL__"], "TimeCapsule")
        self.assertEqual(bundle.start_script_replacements["__ADISK_DISK_KEY__"], "dk0")
        self.assertEqual(bundle.start_script_replacements["__ADISK_UUID__"], "''")

    def test_build_template_bundle_defaults_mdns_device_model(self) -> None:
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
        self.assertEqual(bundle.start_script_replacements["__MDNS_DEVICE_MODEL__"], "TimeCapsule")

    def test_build_deployment_plan_uses_device_paths(self) -> None:
        paths = build_device_paths("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("root@10.0.0.2", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"))
        self.assertEqual(plan.payload_dir, "/Volumes/dk2/samba4")
        self.assertEqual(plan.private_dir, "/Volumes/dk2/samba4/private")
        self.assertEqual(plan.volume_root, "/Volumes/dk2")
        self.assertEqual(plan.disk_key, "dk2")
        self.assertEqual(plan.remote_directories[0], "/Volumes/dk2/samba4")
        self.assertEqual(plan.payload_targets["nbns-advertiser"], "/Volumes/dk2/samba4/nbns-advertiser")

    def test_render_template_text_replaces_tokens(self) -> None:
        self.assertEqual(render_template_text("hello __TOKEN__", {"__TOKEN__": "world"}), "hello world")

    def test_load_boot_asset_text_reads_packaged_asset(self) -> None:
        content = load_boot_asset_text("rc.local")
        self.assertIn("/mnt/Flash/start-samba.sh", content)

    def test_render_start_script_includes_device_model_flag(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("start-samba.sh", bundle.start_script_replacements)
        self.assertIn('MDNS_DEVICE_MODEL=AirPortTimeCapsule', rendered)
        self.assertIn('--device-model "$MDNS_DEVICE_MODEL"', rendered)
        self.assertIn('ADISK_DISK_KEY=dk0', rendered)
        self.assertIn("ADISK_UUID=''", rendered)
        self.assertIn('--adisk-disk-key "$ADISK_DISK_KEY"', rendered)
        self.assertIn('--adisk-uuid "$ADISK_UUID"', rendered)
        self.assertIn('--adisk-sys-wama "$iface_mac"', rendered)
        self.assertIn("ether[[:space:]]", rendered)
        self.assertIn("address[[:space:]]", rendered)
        self.assertNotIn("tr '[:lower:]' '[:upper:]'", rendered)
        self.assertIn('if [ -f "$payload_dir/private/nbns.enabled" ]', rendered)
        self.assertIn('cp "$nbns_src" "$RAM_SBIN/nbns-advertiser"', rendered)
        self.assertIn('"$RAM_SBIN/nbns-advertiser" \\', rendered)
        self.assertNotIn('cp "$nbns_src" "$RAM_SBIN/nbns-advertiser"\n    chmod 755 "$RAM_SBIN/nbns-advertiser"', rendered)

    def test_render_watchdog_script_includes_device_model_flag(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values)
        rendered = render_template("watchdog.sh", bundle.watchdog_replacements)
        self.assertIn('MDNS_DEVICE_MODEL=AirPortTimeCapsule', rendered)
        self.assertIn('--device-model "$MDNS_DEVICE_MODEL"', rendered)
        self.assertIn('ADISK_DISK_KEY=dk0', rendered)
        self.assertIn("ADISK_UUID=''", rendered)
        self.assertIn('--adisk-disk-key "$ADISK_DISK_KEY"', rendered)
        self.assertIn('--adisk-uuid "$ADISK_UUID"', rendered)
        self.assertIn('--adisk-sys-wama "$iface_mac"', rendered)
        self.assertIn("ether[[:space:]]", rendered)
        self.assertIn("address[[:space:]]", rendered)
        self.assertNotIn("tr '[:lower:]' '[:upper:]'", rendered)
        self.assertIn('if [ ! -f "$RAM_PRIVATE/nbns.enabled" ]; then', rendered)
        self.assertIn('"$NBNS_BIN" \\', rendered)
        self.assertIn('--name "$SMB_NETBIOS_NAME"', rendered)
        self.assertNotIn('if [ ! -f "$RAM_PRIVATE/nbns.enabled" ]; then\n        return 0\n    fi\n\n    if [ ! -x "$NBNS_BIN" ]; then\n        log_msg "NBNS restart skipped; missing $NBNS_BIN"\n        return 0\n    fi\n\n    iface_ip="$(get_iface_ipv4 "$NET_IFACE")"\n    if [ -z "$iface_ip" ]; then\n        log_msg "NBNS restart skipped; missing IPv4 for $NET_IFACE"\n        return 0\n    fi\n\n    pkill "$NBNS_PROC_NAME" >/dev/null 2>&1 || true\n    "$NBNS_BIN"', rendered)

    def test_build_template_bundle_accepts_adisk_values(self) -> None:
        values = {
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_SHARE_NAME": "Data",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_NET_IFACE": "bridge0",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "AirPortTimeCapsule",
            "TC_SAMBA_USER": "admin",
        }
        bundle = build_template_bundle(values, adisk_disk_key="dk3", adisk_uuid="12345678-1234-1234-1234-123456789012")
        self.assertEqual(bundle.start_script_replacements["__ADISK_DISK_KEY__"], "dk3")
        self.assertEqual(bundle.start_script_replacements["__ADISK_UUID__"], "12345678-1234-1234-1234-123456789012")

    def test_mdns_advertiser_accepts_lowercase_wama_and_normalizes_output(self) -> None:
        if shutil.which("cc") is None:
            self.skipTest("cc not available")

        mdns_source = (REPO_ROOT / "build" / "mdns-advertiser.c").as_posix()
        source = '''
#include <stdio.h>
#define main mdns_advertiser_main
#include "{mdns_source}"
#undef main

int main(void) {{
    char out[256];
    if (build_adisk_system_txt(out, sizeof(out), "80:ea:96:e6:58:68") != 0) {{
        return 1;
    }}
    puts(out);
    return 0;
}}
'''.format(mdns_source=mdns_source)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            c_path = tmp / "mdns_test.c"
            bin_path = tmp / "mdns_test"
            c_path.write_text(source)
            proc = subprocess.run(
                ["cc", "-Wall", "-Wextra", "-Werror", str(c_path), "-o", str(bin_path)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            run = subprocess.run([str(bin_path)], capture_output=True, text=True, check=False)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.strip(), "sys=waMA=80:EA:96:E6:58:68,adVF=0x1010")

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
        self.assertIn("/Volumes/dk2/samba4/nbns-advertiser", command)
        self.assertIn("/Volumes/dk2/samba4/private/adisk.uuid", command)

    def test_remote_ensure_adisk_uuid_reuses_existing_file(self) -> None:
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh", return_value=mock.Mock(stdout="12345678-1234-1234-1234-123456789012\n")):
            with mock.patch("timecapsulesmb.deploy.executor.run_scp") as scp_mock:
                result = remote_ensure_adisk_uuid("host", "pw", "-o foo", "/Volumes/dk2/samba4/private")
        self.assertEqual(result, "12345678-1234-1234-1234-123456789012")
        scp_mock.assert_not_called()

    def test_remote_ensure_adisk_uuid_creates_new_file_when_missing(self) -> None:
        fixed_uuid = uuid.UUID("12345678-1234-1234-1234-123456789012")
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh", return_value=mock.Mock(stdout="\n")):
            with mock.patch("timecapsulesmb.deploy.executor.uuid.uuid4", return_value=fixed_uuid):
                with mock.patch("timecapsulesmb.deploy.executor.run_scp") as scp_mock:
                    result = remote_ensure_adisk_uuid("host", "pw", "-o foo", "/Volumes/dk2/samba4/private")
        self.assertEqual(result, str(fixed_uuid))
        self.assertEqual(scp_mock.call_count, 1)

    def test_remote_enable_nbns_creates_marker_without_touch(self) -> None:
        with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
            remote_enable_nbns("host", "pw", "-o foo", "/Volumes/dk2/samba4/private")
        self.assertIn("nbns.enabled", run_ssh_mock.call_args.args[3])
        self.assertIn(": >", run_ssh_mock.call_args.args[3])

    def test_upload_deployment_payload_uploads_all_expected_files(self) -> None:
        paths = build_device_paths("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("host", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"))
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
        self.assertEqual(scp_mock.call_count, 8)
        destinations = [call.args[4] for call in scp_mock.call_args_list]
        self.assertEqual(
            destinations,
            [
                "/Volumes/dk2/samba4/smbd",
                "/Volumes/dk2/samba4/mdns-smbd-advertiser",
                "/Volumes/dk2/samba4/nbns-advertiser",
                "/mnt/Flash/rc.local",
                "/mnt/Flash/start-samba.sh",
                "/mnt/Flash/watchdog.sh",
                "/mnt/Flash/dfree.sh",
                "/Volumes/dk2/samba4/smb.conf.template",
            ],
        )

    def test_format_deployment_plan_contains_concrete_actions(self) -> None:
        paths = build_device_paths("/Volumes/dk2", "samba4")
        plan = build_deployment_plan("root@10.0.0.2", paths, Path("bin/smbd"), Path("bin/mdns"), Path("bin/nbns"), install_nbns=True)
        text = format_deployment_plan(plan)
        self.assertIn("volume root: /Volumes/dk2", text)
        self.assertIn("mkdir -p /Volumes/dk2/samba4", text)
        self.assertIn("generated smbpasswd -> /Volumes/dk2/samba4/private/smbpasswd", text)
        self.assertIn("generated adisk UUID -> /Volumes/dk2/samba4/private/adisk.uuid", text)
        self.assertIn("generated nbns marker -> /Volumes/dk2/samba4/private/nbns.enabled", text)
        self.assertIn("chmod 700 /Volumes/dk2/samba4/private", text)

    def test_build_uninstall_plan_stops_nbns_process(self) -> None:
        paths = build_device_paths("/Volumes/dk2", "samba4")
        plan = build_uninstall_plan("root@10.0.0.2", paths)
        self.assertIn("pkill nbns-advertiser >/dev/null 2>&1 || true", plan.stop_commands)


if __name__ == "__main__":
    unittest.main()
