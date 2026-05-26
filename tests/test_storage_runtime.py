from __future__ import annotations

import shlex
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG
from timecapsulesmb.cli.deploy import render_flash_runtime_config
from timecapsulesmb.services.deploy import render_flash_runtime_config as render_gui_flash_runtime_config
from timecapsulesmb.deploy.executor import upload_flash_file
from timecapsulesmb.deploy.boot_assets import load_boot_asset_text
from timecapsulesmb.deploy.planner import (
    GENERATED_FLASH_CONFIG_SOURCE,
    build_deployment_plan,
)
from timecapsulesmb.device.probe import (
    normalize_runtime_mdns_host_label,
    normalize_runtime_mdns_instance_name,
    normalize_runtime_netbios_name,
)
from timecapsulesmb.device.storage import (
    MAST_PROBE_COMMAND,
    PayloadVerificationResult,
    MaStProbeDiagnostics,
    MaStReadResult,
    MaStVolume,
    PayloadHome,
    ensure_volume_root_mounted_conn,
    mast_probe_debug_summary,
    mast_volumes_debug_summary,
    ordered_payload_candidate_volumes,
    payload_candidate_checks_debug_summary,
    parse_mast_plist,
    probe_mast_diagnostics_conn,
    render_ensure_volume_root_mounted_script,
    select_payload_home_with_diagnostics_conn,
    verify_payload_home_conn,
    wait_for_mast_volumes_conn,
)
from timecapsulesmb.transport.ssh import SshConnection
from tests.storage_fixtures import EXTERNAL_BACKUP, INTERNAL_DATA, MAST_FIXTURES, SHELL_MAST_FIXTURES, MaStFixture


class StorageRuntimeTests(unittest.TestCase):
    _runtime_asset_texts: str | None = None

    @classmethod
    def runtime_asset_texts(cls) -> str:
        if cls._runtime_asset_texts is None:
            cls._runtime_asset_texts = load_boot_asset_text("common.sh")
        return cls._runtime_asset_texts

    def write_runtime_harness(self, tmp_path: Path, *, hostname_output: str | None = None) -> tuple[Path, Path, Path, Path]:
        flash = tmp_path / "Flash"
        memory = tmp_path / "Memory"
        locks = tmp_path / "Locks"
        volumes = tmp_path / "Volumes"
        flash.mkdir()
        memory.mkdir()
        locks.mkdir()
        volumes.mkdir()

        common = self.runtime_asset_texts()
        boot = load_boot_asset_text("boot.sh")
        manager = load_boot_asset_text("manager.sh")
        replacements = {
            "/mnt/Flash": str(flash),
            "/mnt/Memory": str(memory),
            "/mnt/Locks": str(locks),
            "/Volumes": str(volumes),
            "/usr/bin/acp": str(tmp_path / "acp"),
        }
        if hostname_output is not None:
            hostname = tmp_path / "hostname"
            hostname.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(hostname_output) + "\n")
            hostname.chmod(0o755)
            replacements["/bin/hostname"] = str(hostname)
        for old, new in replacements.items():
            common = common.replace(old, new)
            boot = boot.replace(old, new)
            manager = manager.replace(old, new)

        (flash / "common.sh").write_text(common)
        boot_path = flash / "boot.sh"
        boot_path.write_text(boot)
        boot_path.chmod(0o755)
        manager_path = flash / "manager.sh"
        manager_path.write_text(manager)
        manager_path.chmod(0o755)
        (flash / "tcapsulesmb.conf").write_text(
            textwrap.dedent(
                f"""\
                TC_CONFIG_VERSION=2
                PAYLOAD_DIR_NAME='.samba4'
                SMB_SAMBA_USER='admin'
                MDNS_DEVICE_MODEL='TimeCapsule6,106'
                AIRPORT_SYAP='106'
                INTERNAL_SHARE_USE_DISK_ROOT=0
                ANY_PROTOCOL=0
                DISKD_USE_VOLUME_ATTEMPTS=2
                ATA_IDLE_SECONDS=300
                ATA_STANDBY=''
                NBNS_ENABLED=0
                SMBD_DEBUG_LOGGING=0
                MDNS_DEBUG_LOGGING=0
                MANAGER_STOP_POLL_SECONDS=10
                """
            )
        )
        return flash, memory, locks, volumes

    def expected_topology_tsv(self, fixture: MaStFixture, volumes_root: Path) -> str:
        lines = []
        for volume in fixture.expected:
            volume_root = volume.volume_root.replace("/Volumes", str(volumes_root), 1)
            builtin = "1" if volume.builtin else "0"
            lines.append(
                "\t".join(
                    (
                        volume.disk_device,
                        builtin,
                        volume.partition_device,
                        volume_root,
                        volume.name,
                        volume.adisk_uuid,
                    )
                )
            )
        return "\n".join(lines) + ("\n" if lines else "")

    def expected_runtime_rows_tsv(
        self,
        fixture: MaStFixture,
        volumes_root: Path,
        *,
        users_by_partition: dict[str, str] | None = None,
    ) -> str:
        users_by_partition = users_by_partition or {}
        lines = []
        for volume in fixture.expected:
            volume_root = volume.volume_root.replace("/Volumes", str(volumes_root), 1)
            builtin = "1" if volume.builtin else "0"
            lines.append(
                "\t".join(
                    (
                        volume.disk_device,
                        builtin,
                        volume.partition_device,
                        volume_root,
                        volume.name,
                        volume.adisk_uuid,
                        volume.format,
                        users_by_partition.get(volume.partition_device, ""),
                    )
                )
            )
        return "\n".join(lines) + ("\n" if lines else "")

    def test_common_select_advertise_mac_prefers_acp_lama(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            acp = tmp_path / "acp"
            acp.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    case "$1:$2" in
                        -q:laMA) echo 80:EA:96:E6:58:68 ;;
                        -q:waMA) echo 80:EA:96:E6:58:69 ;;
                        *) exit 1 ;;
                    esac
                    """
                )
            )
            acp.chmod(0o755)
            script = tmp_path / "advertise-mac.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    mac=$(tc_select_advertise_mac || true)
                    printf 'mac=%s\\n' "$mac"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "mac=80:EA:96:E6:58:68\n")

    def test_common_select_advertise_mac_falls_back_to_live_interface_mac(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fake_ifconfig = tmp_path / "ifconfig"
            fake_ifconfig.write_text(
                "#!/bin/sh\n"
                "cat <<'OUT'\n"
                "bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500\n"
                "        address: 80:ea:96:e6:58:70\n"
                "OUT\n"
            )
            fake_ifconfig.chmod(0o755)
            common_path = flash / "common.sh"
            common_path.write_text(common_path.read_text().replace("/sbin/ifconfig", str(fake_ifconfig)))
            script = tmp_path / "advertise-mac-fallback.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    mac=$(tc_select_advertise_mac || true)
                    printf 'mac=%s\\n' "$mac"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "mac=80:ea:96:e6:58:70\n")

    def write_fake_acp(self, tmp_path: Path, raw: str | bytes, *, final_newline: bool = True) -> Path:
        acp = tmp_path / "acp"
        raw_text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        if final_newline:
            acp.write_text("#!/bin/sh\ncat <<'OUT'\n" + raw_text + "\nOUT\n")
        else:
            acp.write_text("#!/bin/sh\nprintf %s " + shlex.quote(raw_text) + "\n")
        acp.chmod(0o755)
        return acp

    def write_sequence_acp(self, tmp_path: Path, raws: tuple[str | bytes, ...]) -> Path:
        raw_dir = tmp_path / "acp-sequence"
        raw_dir.mkdir()
        count_path = tmp_path / "acp-count"
        for index, raw in enumerate(raws, start=1):
            raw_path = raw_dir / str(index)
            if isinstance(raw, bytes):
                raw_path.write_bytes(raw)
            else:
                raw_path.write_text(raw)
        acp = tmp_path / "acp"
        acp.write_text(
            "#!/bin/sh\n"
            f"count=$(/bin/cat {shlex.quote(str(count_path))} 2>/dev/null || echo 0)\n"
            "count=$((count + 1))\n"
            f"echo \"$count\" >{shlex.quote(str(count_path))}\n"
            f"path={shlex.quote(str(raw_dir))}/$count\n"
            f"last_path={shlex.quote(str(raw_dir))}/{len(raws)}\n"
            "[ -f \"$path\" ] || path=$last_path\n"
            "cat \"$path\"\n"
        )
        acp.chmod(0o755)
        return count_path

    def internal_mast_raw_with_volatile_fields(
        self,
        *,
        users: int,
        size_free: int = 100000,
        size_used: int = 200000,
        soft_disconnected: str = "false",
    ) -> str:
        return textwrap.dedent(
            f"""\
            MaSt = (
                {{
                    deviceName = "wd0";
                    builtin = true;
                    partitions = (
                        {{
                            deviceName = "dk2";
                            name = "Data";
                            format = "hfs";
                            uuid = <f42bdb83 c2655522 a0872560 6a4d0abf>;
                            sizeFree = {size_free};
                            sizeUsed = {size_used};
                            users = {users};
                            softDisconnected = {soft_disconnected};
                        }}
                    );
                }}
            );
            """
        )

    def write_selectable_fixture_acp(self, tmp_path: Path, fixtures: tuple[MaStFixture, ...]) -> Path:
        raw_dir = tmp_path / "mast-fixtures"
        raw_dir.mkdir()
        selector = tmp_path / "selected-fixture"
        selector.write_text("")
        for fixture in fixtures:
            raw = fixture.raw
            path = raw_dir / fixture.name
            if isinstance(raw, bytes):
                path.write_bytes(raw)
            else:
                path.write_text(raw)
        acp = tmp_path / "acp"
        acp.write_text(
            textwrap.dedent(
                f"""\
                #!/bin/sh
                selected=$(cat {shlex.quote(str(selector))})
                path={shlex.quote(str(raw_dir))}/$selected
                [ -f "$path" ] || exit 1
                cat "$path"
                """
            )
        )
        acp.chmod(0o755)
        return selector

    def parse_named_shell_sections(self, stdout: str) -> dict[str, tuple[int, str]]:
        sections: dict[str, tuple[int, str]] = {}
        current_name: str | None = None
        current_status = 0
        current_lines: list[str] = []
        for line in stdout.splitlines(keepends=True):
            if line.startswith("__TC_BEGIN__\t"):
                self.assertIsNone(current_name, stdout)
                _marker, name, status_text = line.rstrip("\n").split("\t", 2)
                current_name = name
                current_status = int(status_text)
                current_lines = []
            elif line.startswith("__TC_END__\t"):
                self.assertIsNotNone(current_name, stdout)
                _marker, name = line.rstrip("\n").split("\t", 1)
                self.assertEqual(name, current_name, stdout)
                sections[current_name] = (current_status, "".join(current_lines))
                current_name = None
                current_status = 0
                current_lines = []
            else:
                self.assertIsNotNone(current_name, stdout)
                current_lines.append(line)
        self.assertIsNone(current_name, stdout)
        return sections

    def parse_topology_tsv(self, text: str, volumes_root: Path) -> tuple[MaStVolume, ...]:
        volumes: list[MaStVolume] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            fields = line.split("\t")
            self.assertEqual(len(fields), 6, line)
            disk_device, builtin, partition_device, volume_root, name, adisk_uuid = fields
            normalized_root = volume_root.replace(str(volumes_root), "/Volumes", 1)
            volumes.append(
                MaStVolume(
                    disk_device,
                    partition_device,
                    normalized_root,
                    name,
                    adisk_uuid,
                    builtin == "1",
                    "hfs",
                )
            )
        return tuple(volumes)

    def test_parse_mast_plist_matches_golden_fixtures(self) -> None:
        for fixture in MAST_FIXTURES:
            with self.subTest(fixture=fixture.name):
                self.assertEqual(parse_mast_plist(fixture.raw), fixture.expected)

    def test_parse_mast_openstep_fallback_handles_current_acp_line_format(self) -> None:
        raw = """\
MaSt = (
    {
        deviceName = "wd0";
        builtin = true;
        partitions = (
            {
                deviceName = "dk2";
                name = "Data; Main";
                format = "hfs";
                uuid = <f42bdb83 c2655522 a0872560 6a4d0abf>;
            },
            {
                deviceName = "dk1";
                name = "APconfig";
                format = "msdos";
                uuid = <00000000 00000000 00000000 00000000>;
            }
        );
    },
    {
        deviceName = "sd0";
        builtin = false;
        partitions = (
            {
                deviceName = "dk3";
                name = "uuid = fake";
                format = "hfs";
                uuid = <51f93e6f dc69524d 986dcee4 d7cb3573>;
            }
        );
    }
);
"""

        self.assertEqual(
            parse_mast_plist(raw),
            (
                MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data; Main", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs"),
                MaStVolume("sd0", "dk3", "/Volumes/dk3", "uuid = fake", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs"),
            ),
        )

    def test_wait_for_mast_volumes_retries_until_available(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        volume = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")

        with mock.patch(
            "timecapsulesmb.device.storage.read_mast_volumes_with_output_conn",
            side_effect=[
                MaStReadResult((), "MaSt=first"),
                MaStReadResult((), "MaSt=second"),
                MaStReadResult((volume,), "MaSt=third"),
            ],
        ) as read_mock:
            with mock.patch("timecapsulesmb.device.storage.time.sleep") as sleep_mock:
                result = wait_for_mast_volumes_conn(connection, attempts=10, delay_seconds=3)

        self.assertEqual(result.volumes, (volume,))
        self.assertEqual(result.attempts, 3)
        self.assertEqual(result.raw_output, "MaSt=third")
        self.assertEqual(read_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_args_list, [mock.call(3), mock.call(3)])

    def test_wait_for_mast_volumes_returns_empty_after_exhaustion(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")

        with mock.patch(
            "timecapsulesmb.device.storage.read_mast_volumes_with_output_conn",
            return_value=MaStReadResult((), "MaSt=[]"),
        ) as read_mock:
            with mock.patch("timecapsulesmb.device.storage.time.sleep") as sleep_mock:
                result = wait_for_mast_volumes_conn(connection, attempts=3, delay_seconds=3)

        self.assertEqual(result.volumes, ())
        self.assertEqual(result.attempts, 3)
        self.assertEqual(result.raw_output, "MaSt=[]")
        self.assertEqual(read_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_args_list, [mock.call(3), mock.call(3)])

    def test_probe_mast_diagnostics_records_empty_success(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        raw_output = "MaSt = (\n);\n"
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=raw_output, stderr="")

        with mock.patch("timecapsulesmb.device.storage.run_ssh", return_value=proc) as run_mock:
            diagnostics = probe_mast_diagnostics_conn(connection)

        self.assertEqual(diagnostics.command, MAST_PROBE_COMMAND)
        self.assertEqual(diagnostics.returncode, 0)
        self.assertEqual(diagnostics.volumes, ())
        self.assertEqual(diagnostics.stdout, raw_output)
        self.assertEqual(diagnostics.stderr, "")
        run_mock.assert_called_once()
        self.assertEqual(run_mock.call_args.args[:2], (connection, MAST_PROBE_COMMAND))
        self.assertFalse(run_mock.call_args.kwargs["check"])
        summary = mast_probe_debug_summary(diagnostics)
        self.assertEqual(summary["mast_probe_volume_count"], 0)
        self.assertEqual(summary["mast_probe_stdout_chars"], len(raw_output))
        self.assertEqual(summary["mast_probe_stdout"], raw_output)
        self.assertEqual(summary["mast_probe_stderr"], "<empty>")

    def test_probe_mast_diagnostics_records_parsed_volume(self) -> None:
        fixture = SHELL_MAST_FIXTURES[0]
        raw_output = fixture.raw.decode("utf-8", errors="replace") if isinstance(fixture.raw, bytes) else fixture.raw
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=raw_output, stderr="")

        with mock.patch("timecapsulesmb.device.storage.run_ssh", return_value=proc):
            diagnostics = probe_mast_diagnostics_conn(connection)

        self.assertEqual(diagnostics.returncode, 0)
        self.assertEqual(diagnostics.volumes, fixture.expected)
        summary = mast_probe_debug_summary(diagnostics)
        self.assertEqual(summary["mast_probe_volume_count"], len(fixture.expected))
        self.assertEqual(summary["mast_probe_candidates"], mast_volumes_debug_summary(fixture.expected))

    def test_probe_mast_diagnostics_captures_failure_stderr(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=7, stdout="", stderr="acp failed\n")

        with mock.patch("timecapsulesmb.device.storage.run_ssh", return_value=proc):
            diagnostics = probe_mast_diagnostics_conn(connection)

        self.assertEqual(diagnostics.returncode, 7)
        self.assertEqual(diagnostics.volumes, ())
        self.assertEqual(diagnostics.stderr, "acp failed\n")
        summary = mast_probe_debug_summary(diagnostics)
        self.assertEqual(summary["mast_probe_stderr_chars"], len("acp failed\n"))
        self.assertEqual(summary["mast_probe_stderr"], "acp failed\n")

    def test_mast_probe_debug_summary_bounds_long_output(self) -> None:
        diagnostics = MaStProbeDiagnostics(
            command=MAST_PROBE_COMMAND,
            returncode=0,
            volumes=(),
            stdout="a" * 10000,
            stderr="b" * 10001,
        )

        summary = mast_probe_debug_summary(diagnostics)

        self.assertEqual(summary["mast_probe_stdout_chars"], 10000)
        self.assertEqual(summary["mast_probe_stderr_chars"], 10001)
        self.assertIn("<truncated", str(summary["mast_probe_stdout"]))
        self.assertIn("<truncated", str(summary["mast_probe_stderr"]))
        self.assertLess(len(str(summary["mast_probe_stdout"])), 10000)
        self.assertLess(len(str(summary["mast_probe_stderr"])), 10001)

    def test_payload_candidate_order_is_internal_first_then_external_mast_order_in_python(self) -> None:
        external_a = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB A", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external_b = MaStVolume("sd1", "dk4", "/Volumes/dk4", "USB B", "7d40eaac-182b-562b-a7b8-49bb5ed69c0f", False, "hfs")

        self.assertEqual(
            ordered_payload_candidate_volumes((external_a, internal, external_b)),
            (internal, external_a, external_b),
        )

    def test_select_payload_home_prefers_writable_internal_volume(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_mast_volume_mounted_conn", return_value=True) as mount_mock:
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", side_effect=[True]) as writable_mock:
                selection = select_payload_home_with_diagnostics_conn(connection, (external, internal), ".samba4", wait_seconds=30)

        self.assertEqual(selection.payload_home, PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"))
        mount_mock.assert_called_once_with(connection, internal, wait_seconds=30)
        writable_mock.assert_called_once_with(connection, "/Volumes/dk2")

    def test_ensure_volume_root_mounted_conn_claims_diskd_without_mount_hfs_fallback(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        with mock.patch("timecapsulesmb.device.storage.run_ssh", return_value=mock.Mock(returncode=0)) as run_ssh_mock:
            self.assertTrue(ensure_volume_root_mounted_conn(connection, "/Volumes/dk2", "/dev/dk2", wait_seconds=12))

        run_ssh_mock.assert_called_once()
        remote_command = run_ssh_mock.call_args.args[1]
        self.assertIn("/bin/df -k /Volumes/dk2", remote_command)
        self.assertIn("/usr/bin/tail -n +2", remote_command)
        self.assertIn("/usr/bin/acp rpc diskd.useVolume", remote_command)
        self.assertLess(remote_command.index("/usr/bin/acp rpc diskd.useVolume"), remote_command.index("/bin/df -k /Volumes/dk2"))
        self.assertIn('while [ "$diskd_attempt" -le 2 ]', remote_command)
        self.assertNotIn("mount_hfs", remote_command)
        self.assertNotIn("grep", remote_command)
        self.assertNotIn("awk", remote_command)
        self.assertNotIn("cut", remote_command)
        self.assertEqual(run_ssh_mock.call_args.kwargs["timeout"], 69)

    def test_ensure_volume_root_mounted_conn_reports_failure(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        with mock.patch("timecapsulesmb.device.storage.run_ssh", return_value=mock.Mock(returncode=1)):
            self.assertFalse(ensure_volume_root_mounted_conn(connection, "/Volumes/dk2", "/dev/dk2", wait_seconds=0))

    def test_render_ensure_volume_root_mounted_script_quotes_paths(self) -> None:
        script = render_ensure_volume_root_mounted_script("/Volumes/dk 2", "/dev/dk2", 1)
        self.assertIn("mkdir -p '/Volumes/dk 2'", script)
        self.assertIn("diskd.useVolume path:s:'/Volumes/dk 2'", script)

    def test_verify_payload_home_conn_passes_for_boot_compatible_payload(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        payload_home = PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4")
        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=True) as mount_mock:
            with mock.patch("timecapsulesmb.device.storage.run_ssh", return_value=mock.Mock(returncode=0, stdout="ok\n")) as run_ssh_mock:
                result = verify_payload_home_conn(connection, payload_home, wait_seconds=5)

        self.assertEqual(result, PayloadVerificationResult(True, "ok"))
        mount_mock.assert_called_once_with(connection, "/Volumes/dk2", "/dev/dk2", wait_seconds=5)
        remote_command = run_ssh_mock.call_args.args[1]
        self.assertIn("[ -d /Volumes/dk2/.samba4 ]", remote_command)
        self.assertIn("[ -x /Volumes/dk2/.samba4/smbd ]", remote_command)
        self.assertIn("[ -x /Volumes/dk2/.samba4/sbin/smbd ]", remote_command)
        self.assertIn("[ -f /Volumes/dk2/.samba4/private/smbpasswd ]", remote_command)
        self.assertIn("[ -f /Volumes/dk2/.samba4/private/username.map ]", remote_command)

    def test_verify_payload_home_conn_reports_mount_and_payload_failures(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        payload_home = PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4")
        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=False):
            result = verify_payload_home_conn(connection, payload_home, wait_seconds=5)
        self.assertEqual(result, PayloadVerificationResult(False, "volume /Volumes/dk2 is not mounted"))

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=True):
            with mock.patch(
                "timecapsulesmb.device.storage.run_ssh",
                return_value=mock.Mock(returncode=1, stdout="missing smbd; missing private/smbpasswd\n"),
            ):
                result = verify_payload_home_conn(connection, payload_home, wait_seconds=5)
        self.assertEqual(result, PayloadVerificationResult(False, "missing smbd; missing private/smbpasswd"))

    def test_select_payload_home_skips_unmountable_internal_before_external(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_mast_volume_mounted_conn", side_effect=[False, True]) as mount_mock:
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", return_value=True) as writable_mock:
                selection = select_payload_home_with_diagnostics_conn(connection, (external, internal), ".samba4", wait_seconds=9)

        self.assertEqual(selection.payload_home, PayloadHome("/Volumes/dk3", "/dev/dk3", ".samba4"))
        self.assertEqual(
            mount_mock.call_args_list,
            [
                mock.call(connection, internal, wait_seconds=9),
                mock.call(connection, external, wait_seconds=9),
            ],
        )
        writable_mock.assert_called_once_with(connection, "/Volumes/dk3")

    def test_select_payload_home_with_diagnostics_records_mount_and_write_results(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_mast_volume_mounted_conn", side_effect=[False, True]):
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", return_value=True):
                selection = select_payload_home_with_diagnostics_conn(
                    connection,
                    (external, internal),
                    ".samba4",
                    wait_seconds=9,
                )

        self.assertEqual(selection.payload_home, PayloadHome("/Volumes/dk3", "/dev/dk3", ".samba4"))
        self.assertEqual(selection.checks[0].volume, internal)
        self.assertFalse(selection.checks[0].mounted)
        self.assertIsNone(selection.checks[0].writable)
        self.assertEqual(selection.checks[1].volume, external)
        self.assertTrue(selection.checks[1].mounted)
        self.assertTrue(selection.checks[1].writable)
        self.assertEqual(
            payload_candidate_checks_debug_summary(selection.checks),
            [
                {
                    "disk": "wd0",
                    "part": "dk2",
                    "root": "/Volumes/dk2",
                    "name": "Data",
                    "format": "hfs",
                    "builtin": True,
                    "uuid": "f42bdb83-c265-5522-a087-25606a4d0abf",
                    "mounted": False,
                    "writable": None,
                },
                {
                    "disk": "sd0",
                    "part": "dk3",
                    "root": "/Volumes/dk3",
                    "name": "USB",
                    "format": "hfs",
                    "builtin": False,
                    "uuid": "51f93e6f-dc69-524d-986d-cee4d7cb3573",
                    "mounted": True,
                    "writable": True,
                },
            ],
        )

    def test_select_payload_home_with_diagnostics_returns_no_home_when_all_unwritable(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_mast_volume_mounted_conn", return_value=True):
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", return_value=False):
                selection = select_payload_home_with_diagnostics_conn(
                    connection,
                    (internal, external),
                    ".samba4",
                    wait_seconds=30,
                )

        self.assertIsNone(selection.payload_home)
        self.assertEqual([check.mounted for check in selection.checks], [True, True])
        self.assertEqual([check.writable for check in selection.checks], [False, False])

    def test_select_payload_home_records_unmountable_candidates(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_mast_volume_mounted_conn", return_value=False):
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn") as writable_mock:
                selection = select_payload_home_with_diagnostics_conn(connection, (internal, external), ".samba4", wait_seconds=30)

        self.assertIsNone(selection.payload_home)
        self.assertEqual([check.mounted for check in selection.checks], [False, False])
        writable_mock.assert_not_called()

    def test_select_payload_home_falls_back_to_external_and_records_none_writable(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_mast_volume_mounted_conn", return_value=True):
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", side_effect=[False, True]):
                selection = select_payload_home_with_diagnostics_conn(connection, (internal, external), ".samba4", wait_seconds=30)
        self.assertEqual(selection.payload_home, PayloadHome("/Volumes/dk3", "/dev/dk3", ".samba4"))

        with mock.patch("timecapsulesmb.device.storage.ensure_mast_volume_mounted_conn", return_value=True):
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", return_value=False):
                selection = select_payload_home_with_diagnostics_conn(connection, (internal, external), ".samba4", wait_seconds=30)
        self.assertIsNone(selection.payload_home)
        self.assertEqual([check.writable for check in selection.checks], [False, False])

    def test_flash_runtime_config_contains_runtime_settings_and_no_share_name(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_SAMBA_USER": "admin",
                "TC_MDNS_DEVICE_MODEL": "TimeCapsule6,106",
                "TC_AIRPORT_SYAP": "106",
                "TC_INTERNAL_SHARE_USE_DISK_ROOT": "true",
                "TC_ANY_PROTOCOL": "true",
            }
        )

        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            nbns_enabled=True,
            debug_logging=True,
        )

        self.assertNotIn("PAYLOAD_DIR_NAME", rendered)
        self.assertNotIn("SMB_SAMBA_USER", rendered)
        self.assertNotIn("MDNS_DEVICE_MODEL", rendered)
        self.assertNotIn("AIRPORT_SYAP", rendered)
        self.assertNotIn("NET_IFACE", rendered)
        self.assertNotIn("NET_IPV4_HINT", rendered)
        self.assertNotIn("PAYLOAD_VOLUME_HINT", rendered)
        self.assertNotIn("PAYLOAD_DEVICE_HINT", rendered)
        self.assertNotIn("PAYLOAD_INSTALL_ID", rendered)
        self.assertIn(f"TC_DEPLOY_RELEASE_TAG={RELEASE_TAG}\n", rendered)
        self.assertIn(f"TC_DEPLOY_CLI_VERSION_CODE={CLI_VERSION_CODE}\n", rendered)
        self.assertIn("INTERNAL_SHARE_USE_DISK_ROOT=1\n", rendered)
        self.assertIn("ANY_PROTOCOL=1\n", rendered)
        self.assertIn("DISKD_USE_VOLUME_ATTEMPTS=2\n", rendered)
        self.assertIn("ATA_IDLE_SECONDS=300\n", rendered)
        self.assertIn("ATA_STANDBY=''\n", rendered)
        self.assertIn("NBNS_ENABLED=1\n", rendered)
        self.assertIn("SMBD_DEBUG_LOGGING=1\n", rendered)
        self.assertNotIn("SMB_NETBIOS_NAME", rendered)
        self.assertNotIn("MDNS_INSTANCE_NAME", rendered)
        self.assertNotIn("MDNS_HOST_LABEL", rendered)
        self.assertNotIn("TC_SHARE_NAME", rendered)

    def test_flash_runtime_config_uses_saved_debug_logging(self) -> None:
        config = AppConfig.from_values({"TC_DEBUG_LOGGING": "true"})

        rendered = render_gui_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            nbns_enabled=True,
            debug_logging=None,
        )

        self.assertIn("SMBD_DEBUG_LOGGING=1\n", rendered)
        self.assertIn("MDNS_DEBUG_LOGGING=1\n", rendered)

    def test_flash_runtime_config_deploy_time_debug_override_can_disable_saved_value(self) -> None:
        config = AppConfig.from_values({"TC_DEBUG_LOGGING": "true"})

        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            nbns_enabled=True,
            debug_logging=False,
        )

        self.assertIn("SMBD_DEBUG_LOGGING=0\n", rendered)
        self.assertIn("MDNS_DEBUG_LOGGING=0\n", rendered)

    def test_flash_runtime_config_accepts_deploy_time_advanced_overrides(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_INTERNAL_SHARE_USE_DISK_ROOT": "false",
                "TC_ANY_PROTOCOL": "false",
            }
        )

        rendered = render_gui_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            nbns_enabled=True,
            debug_logging=False,
            internal_share_use_disk_root=True,
            any_protocol=True,
        )

        self.assertIn("INTERNAL_SHARE_USE_DISK_ROOT=1\n", rendered)
        self.assertIn("ANY_PROTOCOL=1\n", rendered)

    def test_flash_runtime_config_deploy_time_overrides_can_disable_saved_values(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_INTERNAL_SHARE_USE_DISK_ROOT": "true",
                "TC_ANY_PROTOCOL": "true",
            }
        )

        rendered = render_gui_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            nbns_enabled=True,
            debug_logging=False,
            internal_share_use_disk_root=False,
            any_protocol=False,
        )

        self.assertIn("INTERNAL_SHARE_USE_DISK_ROOT=0\n", rendered)
        self.assertIn("ANY_PROTOCOL=0\n", rendered)

    def test_flash_runtime_config_uses_drive_settings_from_config(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_ATA_IDLE_SECONDS": "0",
                "TC_ATA_STANDBY": "0",
            }
        )

        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            nbns_enabled=False,
            debug_logging=False,
        )

        self.assertIn("ATA_IDLE_SECONDS=0\n", rendered)
        self.assertIn("ATA_STANDBY=0\n", rendered)

    def test_common_runtime_identity_normalizers_match_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            system_name = "James's AirPort.Time Capsule"
            hostname = "Time Capsule.local"
            script = tmp_path / "runtime-identity-normalizers.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    awk() {{ echo "awk must not be called" >&2; return 127; }}
                    grep() {{ echo "grep must not be called" >&2; return 127; }}
                    wc() {{ echo "wc must not be called" >&2; return 127; }}
                    tr() {{ echo "tr must not be called" >&2; return 127; }}
                    cut() {{ echo "cut must not be called" >&2; return 127; }}
                    printf 'instance=%s\\n' "$(tc_normalize_mdns_instance_name {shlex.quote(system_name)})"
                    printf 'host=%s\\n' "$(tc_normalize_mdns_host_label {shlex.quote(hostname)})"
                    printf 'netbios=%s\\n' "$(tc_normalize_netbios_name {shlex.quote(hostname)})"
                    printf 'server=%s\\n' "$(tc_normalize_server_string {shlex.quote("  James's AirPort Time Capsule  ")})"
                    printf 'punct_netbios=%s\\n' "$(tc_normalize_netbios_name '---')"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = dict(line.split("=", 1) for line in proc.stdout.splitlines())
        self.assertEqual(lines["instance"], normalize_runtime_mdns_instance_name(system_name))
        self.assertEqual(lines["host"], normalize_runtime_mdns_host_label(hostname))
        self.assertEqual(lines["netbios"], normalize_runtime_netbios_name(hostname))
        self.assertEqual(lines["server"], "James's AirPort Time Capsule")
        self.assertEqual(lines["punct_netbios"], "")

    def test_common_runtime_identity_uses_final_netbios_fallback_for_punctuation_only_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path, hostname_output="---.local")
            script = tmp_path / "runtime-identity-netbios-fallback.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    tc_set_log "$RAM_VAR/test.log" test
                    SMBD_DEBUG_LOGGING=1
                    get_airport_acp_value() {{
                        case "$1" in
                            syNm) echo "极端 时间胶囊" ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_init_runtime_identity
                    printf 'identity=%s|%s|%s\\n' "$MDNS_INSTANCE_NAME" "$MDNS_HOST_LABEL" "$SMB_NETBIOS_NAME"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("identity=极端 时间胶囊|timecapsule|TimeCapsule\n", proc.stdout)
        self.assertIn("runtime identity: mdns_instance=极端 时间胶囊 mdns_host=timecapsule netbios=TimeCapsule server_string=极端 时间胶囊", proc.stdout)

    def test_common_runtime_identity_overwrites_legacy_values_and_feeds_runtime_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path, hostname_output="Time Capsule.local")
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            mdns_args = tmp_path / "mdns.args"
            nbns_args = tmp_path / "nbns.args"
            (flash / "mdns-advertiser").write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >{shlex.quote(str(mdns_args))}\n"
            )
            (flash / "mdns-advertiser").chmod(0o755)
            nbns_bin = memory / "samba4/sbin/nbns-advertiser"
            nbns_bin.parent.mkdir(parents=True)
            nbns_bin.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >{shlex.quote(str(nbns_args))}\n"
            )
            nbns_bin.chmod(0o755)
            script = tmp_path / "runtime-identity.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    MDNS_INSTANCE_NAME=LegacyInstance
                    MDNS_HOST_LABEL=legacy-host
                    SMB_NETBIOS_NAME=LegacyNetbios
                    SMB_SERVER_STRING=LegacyServer
                    NBNS_ENABLED=1
                    tc_init_runtime_env
                    mkdir -p "$RAM_ETC" "$RAM_VAR"
                    tc_set_log "$RAM_VAR/test.log" test
                    SMBD_DEBUG_LOGGING=1
                    get_airport_acp_value() {{
                        case "$1" in
                            syNm) echo "James's AirPort Time Capsule" ;;
                            syVs) echo 7.9.1 ;;
                            srcv) echo 79100.2 ;;
                            syAP) echo 119 ;;
                            laMA) echo 80:EA:96:E6:58:68 ;;
                            *) return 1 ;;
                        esac
                    }}
                    get_radio_mac() {{ return 1; }}
                    stop_nbns_conflicts() {{ return 0; }}
                    tc_set_payload_log_dir {payload} {volumes}/dk2
                    share_rows=$(cat <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    )
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    tc_init_runtime_identity
                    tc_generate_smb_conf_from_share_rows {payload} "$share_rows"
                    tc_launch_mdns_advertiser "mdns test" 0 0
                    wait "$mdns_launch_pid" || true
                    tc_launch_nbns "nbns test" 0
                    wait "$!" || true
                    printf 'identity=%s|%s|%s\\n' "$MDNS_INSTANCE_NAME" "$MDNS_HOST_LABEL" "$SMB_NETBIOS_NAME"
                    cat "$TC_SMBD_CONF"
                    printf 'mdns_args=%s\\n' "$(cat {mdns_args})"
                    printf 'nbns_args=%s\\n' "$(cat {nbns_args})"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("identity=James's AirPort Time Capsule|time-capsule|TimeCapsule", proc.stdout)
        self.assertIn("netbios name = TimeCapsule\n", proc.stdout)
        self.assertIn("server string = James's AirPort Time Capsule\n", proc.stdout)
        self.assertIn("--instance James's AirPort Time Capsule", proc.stdout)
        self.assertIn("--host time-capsule", proc.stdout)
        self.assertIn("--auto-ip", proc.stdout)
        self.assertIn("nbns_args=--name TimeCapsule", proc.stdout)
        self.assertIn("--auto-ip", proc.stdout)
        self.assertIn("runtime identity: mdns_instance=James's AirPort Time Capsule mdns_host=time-capsule netbios=TimeCapsule server_string=James's AirPort Time Capsule", proc.stdout)
        self.assertNotIn("LegacyInstance", proc.stdout)
        self.assertNotIn("legacy-host", proc.stdout)
        self.assertNotIn("LegacyNetbios", proc.stdout)
        self.assertNotIn("LegacyServer", proc.stdout)

    def test_deployment_plan_uses_flash_pointer_and_single_private_payload(self) -> None:
        plan = build_deployment_plan(
            "root@10.0.0.2",
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            Path("/tmp/smbd"),
            Path("/tmp/mdns-advertiser"),
            Path("/tmp/nbns-advertiser"),
        )
        source_ids = {upload.source_id for upload in plan.uploads}

        self.assertIn(GENERATED_FLASH_CONFIG_SOURCE, source_ids)
        self.assertNotIn("rendered:smb.conf.template", source_ids)
        self.assertNotIn("generated:adisk.uuid", source_ids)
        self.assertNotIn("generated:nbns.enabled", source_ids)
        self.assertNotIn("generated:install.id", source_ids)
        self.assertEqual(plan.private_dir, "/Volumes/dk2/.samba4/private")
        self.assertEqual(plan.flash_targets["tcapsulesmb.conf"], "/mnt/Flash/tcapsulesmb.conf")
        self.assertIn("/Volumes/dk2/.samba4/smb.conf.template", {action.path for action in plan.pre_upload_actions if hasattr(action, "path")})
        self.assertIn("/Volumes/dk2/.samba4/private/adisk.uuid", {action.path for action in plan.pre_upload_actions if hasattr(action, "path")})
        self.assertIn("/Volumes/dk2/.samba4/private/nbns.enabled", {action.path for action in plan.pre_upload_actions if hasattr(action, "path")})
        self.assertIn(
            ("/mnt/Flash/tcapsulesmb.conf", "600"),
            {(permission.path, permission.mode) for permission in plan.permissions},
        )

    def test_upload_flash_file_uses_requested_mode_before_atomic_rename(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "tcapsulesmb.conf"
            source.write_text("TC_CONFIG_VERSION=2\n")

            with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
                with mock.patch("timecapsulesmb.deploy.executor.run_scp") as run_scp_mock:
                    upload_flash_file(connection, source, "/mnt/Flash/tcapsulesmb.conf", mode="600")

        run_scp_mock.assert_called_once_with(connection, source, "/mnt/Flash/.tcapsulesmb.conf.tmp", timeout=120)
        install_command = run_ssh_mock.call_args_list[1].args[1]
        self.assertIn("chmod 600 /mnt/Flash/.tcapsulesmb.conf.tmp", install_command)
        self.assertIn("mv -f /mnt/Flash/.tcapsulesmb.conf.tmp /mnt/Flash/tcapsulesmb.conf", install_command)

    def test_common_mast_runtime_topology_projection_matches_shell_supported_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            selector = self.write_selectable_fixture_acp(tmp_path, SHELL_MAST_FIXTURES)
            names = " ".join(shlex.quote(fixture.name) for fixture in SHELL_MAST_FIXTURES)
            script = tmp_path / "signature-fixtures.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    for fixture_name in {names}; do
                        echo "$fixture_name" >{shlex.quote(str(selector))}
                        out={shlex.quote(str(tmp_path))}/"signature-$fixture_name.out"
                        err={shlex.quote(str(tmp_path))}/"signature-$fixture_name.err"
                        set +e
                        raw=$({shlex.quote(str(tmp_path / "acp"))} -A MaSt)
                        runtime_rows=$(printf '%s\\n' "$raw" | tc_mast_raw_to_runtime_rows)
                        tc_mast_runtime_rows_to_topology "$runtime_rows" >"$out" 2>"$err"
                        status=$?
                        set -e
                        printf '__TC_BEGIN__\\t%s\\t%s\\n' "$fixture_name" "$status"
                        cat "$out"
                        printf '__TC_END__\\t%s\\n' "$fixture_name"
                        cat "$err" >&2
                    done
                    """
                )
            )
            script.chmod(0o755)
            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        sections = self.parse_named_shell_sections(proc.stdout)
        for fixture in SHELL_MAST_FIXTURES:
            with self.subTest(fixture=fixture.name):
                expected_stdout = self.expected_topology_tsv(fixture, volumes)
                expected_rc = 0
                status, stdout = sections[fixture.name]
                self.assertEqual(status, expected_rc, proc.stderr)
                self.assertEqual(stdout, expected_stdout)
                self.assertEqual(self.parse_topology_tsv(stdout, volumes), parse_mast_plist(fixture.raw))

    def test_common_pure_shell_mast_runtime_parser_matches_shell_supported_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            fixture_dir = tmp_path / "runtime-parser-fixtures"
            fixture_dir.mkdir()
            for fixture in SHELL_MAST_FIXTURES:
                raw_path = fixture_dir / f"{fixture.name}.raw"
                if isinstance(fixture.raw, bytes):
                    raw_path.write_bytes(fixture.raw)
                else:
                    raw_path.write_text(fixture.raw)
            names = " ".join(shlex.quote(fixture.name) for fixture in SHELL_MAST_FIXTURES)
            script = tmp_path / "runtime-parser-fixtures.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    for fixture_name in {names}; do
                        raw={shlex.quote(str(fixture_dir))}/"$fixture_name.raw"
                        rows={shlex.quote(str(tmp_path))}/"$fixture_name.rows"
                        topology={shlex.quote(str(tmp_path))}/"$fixture_name.topology"
                        set +e
                        tc_mast_raw_to_runtime_rows <"$raw" >"$rows"
                        status=$?
                        set -e
                        runtime_rows=$(/bin/cat "$rows")
                        tc_mast_runtime_rows_to_topology "$runtime_rows" >"$topology"
                        printf '__TC_BEGIN__\\t%s\\t%s\\n' "$fixture_name" "$status"
                        printf 'runtime\\n'
                        /bin/cat "$rows"
                        printf 'topology\\n'
                        /bin/cat "$topology"
                        printf '__TC_END__\\t%s\\n' "$fixture_name"
                    done
                    """
                )
            )
            script.chmod(0o755)
            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        sections = self.parse_named_shell_sections(proc.stdout)
        for fixture in SHELL_MAST_FIXTURES:
            with self.subTest(fixture=fixture.name):
                status, stdout = sections[fixture.name]
                expected_runtime = self.expected_runtime_rows_tsv(fixture, volumes)
                expected_topology = self.expected_topology_tsv(fixture, volumes)
                self.assertEqual(status, 0)
                self.assertEqual(stdout, f"runtime\n{expected_runtime}topology\n{expected_topology}")

    def test_common_pure_shell_mast_runtime_parser_handles_xml_golden_path(self) -> None:
        raw = textwrap.dedent(
            """\
            <array>
                    <dict>
                            <key>blockSize</key>
                            <integer>512</integer>

                            <key>builtin</key>
                            <true/>

                            <key>deviceName</key>
                            <string>wd0</string>

                            <key>info</key>
                            <string>Disk 1</string>

                            <key>partitions</key>
                            <array>
                                    <dict>
                                            <key>deviceName</key>
                                            <string>dk2</string>

                                            <key>format</key>
                                            <string>hfs</string>

                                            <key>name</key>
                                            <string>Data</string>

                                            <key>size</key>
                                            <integer>474891</integer>

                                            <key>sizeFree</key>
                                            <integer>474763</integer>

                                            <key>sizeUsed</key>
                                            <integer>128</integer>

                                            <key>users</key>
                                            <integer>5</integer>

                                            <key>uuid</key>
                                            <data>
                                            9Cvbg8JlVSKghyVgak0Kvw==
                                            </data>
                                    </dict>
                            </array>

                            <key>product</key>
                            <string>9QGAHX9L</string>

                            <key>revision</key>
                            <string>3.BTJ</string>

                            <key>size</key>
                            <integer>476940</integer>

                            <key>smartStatus</key>
                            <string>verified</string>

                            <key>softDisconnected</key>
                            <false/>

                            <key>uuid</key>
                            <data>
                            cqEl/TpIVMS3S1L0bhbrVg==
                            </data>

                            <key>vendor</key>
                            <string>ST3500630NS Q</string>
                    </dict>
            </array>
            """
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            raw_path = tmp_path / "golden.xml"
            raw_path.write_text(raw)
            script = tmp_path / "runtime-parser-golden-xml.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    runtime_rows=$(tc_mast_raw_to_runtime_rows <{shlex.quote(str(raw_path))})
                    printf 'runtime\\n'
                    printf '%s\\n' "$runtime_rows"
                    printf 'topology\\n'
                    tc_mast_runtime_rows_to_topology "$runtime_rows"
                    """
                )
            )
            script.chmod(0o755)
            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        golden_fixture = MaStFixture("xml_golden", raw, (INTERNAL_DATA,))
        expected_runtime = self.expected_runtime_rows_tsv(golden_fixture, volumes, users_by_partition={"dk2": "5"})
        expected_topology = self.expected_topology_tsv(golden_fixture, volumes)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, f"runtime\n{expected_runtime}topology\n{expected_topology}")

    def test_common_pure_shell_mast_runtime_parser_handles_xml_edge_cases(self) -> None:
        raw = textwrap.dedent(
            """\
            <array>
              <dict>
                <key>partitions</key>
                <array>
                  <dict>
                    <key>uuid</key>
                    <data>qqqqqru7zMzd3e7u7u7u7g==</data>
                    <key>name</key>
                    <string>USB Backup</string>
                    <key>format</key>
                    <string>HFS</string>
                    <key>deviceName</key>
                    <string>dk5</string>
                  </dict>
                  <dict>
                    <key>deviceName</key>
                    <string>dk1</string>
                    <key>name</key>
                    <string>APconfig</string>
                    <key>format</key>
                    <string>msdos</string>
                    <key>uuid</key>
                    <data>AAAAAAAAAAAAAAAAAAAAAA==</data>
                  </dict>
                  <dict>
                    <key>deviceName</key>
                    <string>rd0</string>
                    <key>name</key>
                    <string>Not a dk partition</string>
                    <key>format</key>
                    <string>hfs</string>
                    <key>uuid</key>
                    <data>mZmZmZmZmZmZmZmZmZmZmQ==</data>
                  </dict>
                  <dict>
                    <key>deviceName</key>
                    <string>dk6</string>
                    <key>name</key>
                    <string>Bad UUID</string>
                    <key>format</key>
                    <string>hfs</string>
                    <key>uuid</key>
                    <data>bad</data>
                  </dict>
                  <dict>
                    <key>deviceName</key>
                    <string>dk7</string>
                    <key>name</key>
                    <string>Invalid Base64 UUID</string>
                    <key>format</key>
                    <string>hfs</string>
                    <key>uuid</key>
                    <data>AAAA!AAAAAAAAAAAAAAAAA==</data>
                  </dict>
                </array>
                <key>deviceName</key>
                <string>sd0</string>
              </dict>
            </array>
            """
        )
        expected_volume = EXTERNAL_BACKUP
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            raw_path = tmp_path / "edge.xml"
            raw_path.write_text(raw)
            script = tmp_path / "runtime-parser-xml-edge.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    runtime_rows=$(tc_mast_raw_to_runtime_rows <{shlex.quote(str(raw_path))})
                    printf '%s\\n' "$runtime_rows"
                    """
                )
            )
            script.chmod(0o755)
            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        expected_runtime = self.expected_runtime_rows_tsv(MaStFixture("xml_edge", raw, (expected_volume,)), volumes)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, expected_runtime)

    def test_common_pure_shell_mast_topology_projection_ignores_users_and_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            raw_one = tmp_path / "one.raw"
            raw_two = tmp_path / "two.raw"
            raw_one.write_text(self.internal_mast_raw_with_volatile_fields(users=1, size_free=100000, size_used=200000))
            raw_two.write_text(
                self.internal_mast_raw_with_volatile_fields(
                    users=9,
                    size_free=90000,
                    size_used=210000,
                    soft_disconnected="true",
                )
            )
            script = tmp_path / "runtime-parser-stable-projection.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    rows_one=$(tc_mast_raw_to_runtime_rows <{shlex.quote(str(raw_one))})
                    rows_two=$(tc_mast_raw_to_runtime_rows <{shlex.quote(str(raw_two))})
                    tc_mast_runtime_rows_to_topology "$rows_one" >{shlex.quote(str(tmp_path / "one.topology"))}
                    tc_mast_runtime_rows_to_topology "$rows_two" >{shlex.quote(str(tmp_path / "two.topology"))}
                    if cmp -s {shlex.quote(str(tmp_path / "one.topology"))} {shlex.quote(str(tmp_path / "two.topology"))}; then
                        echo same
                    else
                        echo changed
                    fi
                    printf 'rows_one\\n%s\\n' "$rows_one"
                    printf 'rows_two\\n%s\\n' "$rows_two"
                    """
                )
            )
            script.chmod(0o755)
            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("same\n", proc.stdout)
        self.assertIn("\thfs\t1\n", proc.stdout)
        self.assertIn("\thfs\t9\n", proc.stdout)

    def test_common_mast_runtime_topology_projection_handles_input_without_final_newline(self) -> None:
        raw = textwrap.dedent(
            """\
            [
                {
                    deviceName="wd0"
                    builtin=true
                    partitions=
                    [
                        {
                            deviceName="dk2"
                            name="Data"
                            format="hfs"
                            uuid=f42bdb83 c2655522 a0872560 6a4d0abf |binary| (16 bytes)
                        }"""
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            raw_path = tmp_path / "mast-no-final-newline.raw"
            raw_path.write_text(raw)
            script = tmp_path / "signature-no-final-newline.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    runtime_rows=$(tc_mast_raw_to_runtime_rows <{shlex.quote(str(raw_path))})
                    tc_mast_runtime_rows_to_topology "$runtime_rows"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run(
                [str(script)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        fixture = MaStFixture("no_final_newline", raw, (INTERNAL_DATA,))
        expected_stdout = self.expected_topology_tsv(fixture, volumes)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, expected_stdout)

    def test_boot_script_only_runs_one_time_boot_preparation_and_starts_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        """\

                        tc_cleanup_old_runtime() { echo cleanup; return 0; }
                        tc_tune_kernel_memory() { echo tune; }
                        tc_prepare_locks_ramdisk() { echo locks; return 0; }
                        tc_prepare_ram_root() { echo ram; }
                        tc_prepare_legacy_prefix() { echo legacy; }
                        runtime_manager_present() { return 1; }
                        """
                    )
                )
            (flash / "manager.sh").write_text("#!/bin/sh\nexit 0\n")
            (flash / "manager.sh").chmod(0o755)

            proc = subprocess.run(
                ["/bin/sh", str(flash / "boot.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            log_text = (memory / "samba4/var/rc.local.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "cleanup\ntune\nlocks\nram\nlegacy\n")
        self.assertIn("starting manager", log_text)
        self.assertIn("manager launched as pid", log_text)

    def test_manager_log_uses_second_timestamps_and_byte_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_no_valid_hfs_partitions")
            self.write_fake_acp(tmp_path, fixture.raw)
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        """\

                        tc_prepare_ram_root() { mkdir -p "$RAM_VAR"; }
                        tc_prepare_local_hostname_resolution() { :; }
                        tc_manager_reset_pass_state() {
                            i=0
                            payload='abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz'
                            while [ "$i" -lt 220 ]; do
                                tc_log "heavy manager log line $i $payload $payload $payload $payload $payload $payload $payload $payload"
                                i=$((i + 1))
                            done
                        }
                        tc_init_runtime_identity() {
                            MDNS_INSTANCE_NAME=AirPort
                            MDNS_HOST_LABEL=airport
                            SMB_NETBIOS_NAME=AIRPORT
                            SMB_SERVER_STRING=AirPort
                        }
                        tc_manager_stop_samba_lane_without_payload() { :; }
                        runtime_process_present_by_ucomm() {
                            case "$1" in
                                mdns-advertiser) return 0 ;;
                                *) return 1 ;;
                            esac
                        }
                        stop_runtime_process_by_ucomm() { :; }
                        tc_mdns_bound_udp_5353() { return 0; }
                        sleep() {
                            if [ "$1" = "1" ]; then
                                return 0
                            fi
                            exit 0
                        }
                        """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            log_path = memory / "samba4/var/manager.log"
            log_text = log_path.read_text()
            log_size = log_path.stat().st_size

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertGreater(log_size, 32768)
        self.assertLessEqual(log_size, 102400)
        self.assertRegex(log_text, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} manager: ")
        self.assertNotIn(".000 manager:", log_text)
        self.assertNotIn("manager sleeping 10s after ok pass", log_text)

    def test_manager_mast_refresh_retries_transient_failures_until_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_no_valid_hfs_partitions")
            acp_count = tmp_path / "manager-acp-count"
            acp = tmp_path / "acp"
            acp.write_text(
                "#!/bin/sh\n"
                f"count=$(/bin/cat {shlex.quote(str(acp_count))} 2>/dev/null || echo 0)\n"
                "count=$((count + 1))\n"
                f"echo \"$count\" >{shlex.quote(str(acp_count))}\n"
                "if [ \"$count\" -eq 1 ]; then\n"
                "    exit 1\n"
                "fi\n"
                "cat <<'OUT'\n"
                + fixture.raw
                + "\nOUT\n"
            )
            acp.chmod(0o755)
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        """\

                    tc_prepare_ram_root() { :; }
                    tc_prepare_local_hostname_resolution() { :; }
                    tc_init_runtime_identity() {
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }
                    tc_manager_stop_samba_lane_without_payload() { :; }
                    runtime_process_present_by_ucomm() {
                        case "$1" in
                            mdns-advertiser) return 0 ;;
                            *) return 1 ;;
                        esac
                    }
                    stop_runtime_process_by_ucomm() { :; }
                    tc_mdns_bound_udp_5353() { return 0; }
                    sleep() {
                        if [ "$1" = "1" ]; then
                            return 0
                        fi
                        if [ "$1" = "5" ]; then
                            echo "sleep $1"
                            return 0
                        fi
                        echo "status=$manager_status"
                        exit 0
                    }
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            acp_count_text = acp_count.read_text().strip()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "status=0\n")
        self.assertEqual(acp_count_text, "2")

    def test_manager_mast_refresh_does_not_retry_zero_disk_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_no_valid_hfs_partitions")
            acp_count = tmp_path / "manager-acp-count"
            acp = tmp_path / "acp"
            acp.write_text(
                "#!/bin/sh\n"
                f"count=$(/bin/cat {shlex.quote(str(acp_count))} 2>/dev/null || echo 0)\n"
                "count=$((count + 1))\n"
                f"echo \"$count\" >{shlex.quote(str(acp_count))}\n"
                "cat <<'OUT'\n"
                + fixture.raw
                + "\nOUT\n"
            )
            acp.chmod(0o755)
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        """\

                    tc_prepare_ram_root() { :; }
                    tc_prepare_local_hostname_resolution() { :; }
                    tc_init_runtime_identity() {
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }
                    tc_manager_stop_samba_lane_without_payload() { :; }
                    runtime_process_present_by_ucomm() {
                        case "$1" in
                            mdns-advertiser) return 0 ;;
                            *) return 1 ;;
                        esac
                    }
                    stop_runtime_process_by_ucomm() { :; }
                    tc_mdns_bound_udp_5353() { return 0; }
                    sleep() {
                        if [ "$1" = "1" ]; then
                            return 0
                        fi
                        if [ "$1" = "5" ]; then
                            echo "unexpected sleep $1"
                            return 0
                        fi
                        echo "status=$manager_status"
                        exit 0
                    }
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            acp_count_text = acp_count.read_text().strip()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "status=0\n")
        self.assertEqual(acp_count_text, "1")

    def test_manager_identity_reconcile_seeds_signature_on_first_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_no_valid_hfs_partitions")
            self.write_fake_acp(tmp_path, fixture.raw)
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        """\

                    tc_log() { :; }
                    tc_prepare_local_hostname_resolution() { echo prepare; }
                    tc_init_runtime_identity() {
                        echo init
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                        TC_RUNTIME_IDENTITY_READY=1
                    }
                    runtime_process_present_by_ucomm() {
                        case "$1" in
                            mdns-advertiser) return 0 ;;
                            *) echo unexpected-runtime; return 1 ;;
                        esac
                    }
                    stop_runtime_process_by_ucomm() { :; }
                    tc_mdns_bound_udp_5353() { return 0; }
                    tc_nbns_enabled() { return 0; }
                    TC_MANAGER_IDENTITY_SIGNATURE_READY=0
                    TC_MANAGER_LAST_IDENTITY_SIGNATURE=
                    tc_manager_stop_samba_lane_without_payload() { :; }
                    sleep() {
                        if [ "$1" = "1" ]; then
                            return 0
                        fi
                        echo "changed=$TC_MANAGER_IDENTITY_CHANGED"
                        echo "ready=$TC_MANAGER_IDENTITY_SIGNATURE_READY"
                        exit 0
                    }
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "prepare\ninit\nchanged=0\nready=1\n")

    def test_manager_iteration_reconciles_no_payload_without_samba_or_nbns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_no_valid_hfs_partitions")
            acp_count = tmp_path / "manager-acp-count"
            acp = tmp_path / "acp"
            acp.write_text(
                "#!/bin/sh\n"
                f"count=$(/bin/cat {shlex.quote(str(acp_count))} 2>/dev/null || echo 0)\n"
                "count=$((count + 1))\n"
                f"echo \"$count\" >{shlex.quote(str(acp_count))}\n"
                "cat <<'OUT'\n"
                + fixture.raw
                + "\nOUT\n"
            )
            acp.chmod(0o755)
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        """\

                    tc_log() { :; }
                    tc_now_seconds() { echo 1000; }
                    tc_manager_reset_pass_state() { echo reset; }
                    tc_prepare_local_hostname_resolution() { :; }
                    tc_init_runtime_identity() {
                        echo identity
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }
                    tc_stage_runtime() { echo unexpected-stage; return 1; }
                    tc_manager_stop_samba_lane_without_payload() { echo no_payload; }
                    runtime_process_present_by_ucomm() {
                        case "$1" in
                            mdns-advertiser) return 0 ;;
                            *) return 1 ;;
                        esac
                    }
                    stop_runtime_process_by_ucomm() { :; }
                    tc_mdns_bound_udp_5353() { return 0; }
                    tc_manager_reconcile_nbns() { echo unexpected-nbns; return 1; }
                    sleep() {
                        if [ "$1" = "1" ]; then
                            return 0
                        fi
                        echo "status=$manager_status"
                        exit 0
                    }
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            acp_count_text = acp_count.read_text().strip()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "reset\nidentity\nno_payload\nstatus=0\n")
        self.assertEqual(acp_count_text, "1")

    def test_manager_sleep_exits_promptly_after_term_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            with (flash / "tcapsulesmb.conf").open("a") as conf:
                conf.write("MANAGER_STOP_POLL_SECONDS=1\n")
            fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_no_valid_hfs_partitions")
            acp_count = self.write_sequence_acp(tmp_path, (fixture.raw, fixture.raw))
            sleep_count = tmp_path / "sleep-count"
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                    tc_log() {{ printf '%s\\n' "$*" >>"$TC_LOG_FILE"; }}
                    tc_manager_reset_pass_state() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                        TC_RUNTIME_IDENTITY_READY=1
                    }}
                    tc_manager_stop_samba_lane_without_payload() {{ :; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            mdns-advertiser) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    stop_runtime_process_by_ucomm() {{ :; }}
                    tc_mdns_bound_udp_5353() {{ return 0; }}
                    sleep() {{
                        echo "sleep $1"
                        count=$(/bin/cat {shlex.quote(str(sleep_count))} 2>/dev/null || echo 0)
                        count=$((count + 1))
                        echo "$count" >{shlex.quote(str(sleep_count))}
                        if [ "$count" -eq 2 ]; then
                            kill -TERM $$
                        fi
                        return 0
                    }}
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            acp_count_text = acp_count.read_text().strip()
            log_text = (memory / "samba4/var/manager.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "sleep 1\nsleep 1\n")
        self.assertEqual(acp_count_text, "1")
        self.assertIn("manager stop requested; exiting", log_text)

    def test_manager_sleep_completes_poll_interval_before_next_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            with (flash / "tcapsulesmb.conf").open("a") as conf:
                conf.write("MANAGER_STOP_POLL_SECONDS=1\n")
            fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_no_valid_hfs_partitions")
            acp_count = self.write_sequence_acp(tmp_path, (fixture.raw, fixture.raw, fixture.raw))
            sleep_count = tmp_path / "sleep-count"
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                    tc_log() {{ :; }}
                    tc_manager_reset_pass_state() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                        TC_RUNTIME_IDENTITY_READY=1
                    }}
                    tc_manager_stop_samba_lane_without_payload() {{ :; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            mdns-advertiser) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    stop_runtime_process_by_ucomm() {{ :; }}
                    tc_mdns_bound_udp_5353() {{ return 0; }}
                    sleep() {{
                        echo "sleep $1"
                        count=$(/bin/cat {shlex.quote(str(sleep_count))} 2>/dev/null || echo 0)
                        count=$((count + 1))
                        echo "$count" >{shlex.quote(str(sleep_count))}
                        if [ "$count" -eq 11 ]; then
                            echo "status=$manager_status"
                            exit 0
                        fi
                        return 0
                    }}
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            acp_count_text = acp_count.read_text().strip()
            sleep_count_text = sleep_count.read_text().strip()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.count("sleep 1\n"), 11, proc.stdout)
        self.assertIn("status=0\n", proc.stdout)
        self.assertEqual(acp_count_text, "2")
        self.assertEqual(sleep_count_text, "11")

    def test_manager_scheduler_runs_bind_only_between_service_ticks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            acp_count = self.write_sequence_acp(
                tmp_path,
                (
                    self.internal_mast_raw_with_volatile_fields(users=1),
                    self.internal_mast_raw_with_volatile_fields(users=1),
                ),
            )
            events = tmp_path / "events"
            sleep_count = tmp_path / "sleep-count"
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                    tc_prepare_ram_root() {{ mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_PRIVATE" "$RAM_VAR"; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        echo identity >>{shlex.quote(str(events))}
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                        TC_RUNTIME_IDENTITY_READY=1
                    }}
                    tc_manager_refresh_runtime_identity_for_recovery() {{ :; }}
                    tc_wake_or_mount_volume() {{ return 0; }}
                    is_volume_root_mounted() {{ return 0; }}
                    tc_verify_payload_dir() {{ return 0; }}
                    tc_volume_is_writable() {{ return 0; }}
                    tc_prepare_share_path() {{ echo "$2/ShareRoot"; }}
                    tc_apply_ata_drive_setting() {{ :; }}
                    tc_payload_log_dir_ready() {{ return 0; }}
                    tc_find_payload_smbd() {{ echo "$1/smbd"; }}
                    tc_stage_runtime() {{
                        echo stage >>{shlex.quote(str(events))}
                        mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_PRIVATE"
                        printf '#!/bin/sh\\nexit 0\\n' >"$TC_SMBD_BIN"
                        chmod 755 "$TC_SMBD_BIN"
                        : >"$RAM_PRIVATE/smbpasswd"
                        : >"$RAM_PRIVATE/username.map"
                        return 0
                    }}
                    tc_probe_smb_bind_interfaces() {{
                        echo bind-probe >>{shlex.quote(str(events))}
                        echo "127.0.0.1/8"
                    }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            smbd) echo smbd-process >>{shlex.quote(str(events))}; return 0 ;;
                            mdns-advertiser) echo mdns-process >>{shlex.quote(str(events))}; return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_smbd_bound_tcp_445() {{ return 0; }}
                    tc_mdns_bound_udp_5353() {{ return 0; }}
                    stop_runtime_process_by_ucomm() {{ :; }}
                    sleep() {{
                        case "$1" in
                            1|5) return 0 ;;
                            10)
                                count=$(/bin/cat {shlex.quote(str(sleep_count))} 2>/dev/null || echo 0)
                                count=$((count + 1))
                                echo "$count" >{shlex.quote(str(sleep_count))}
                                if [ "$count" -eq 1 ]; then
                                    return 0
                                fi
                                echo "status=$manager_status"
                                exit 0
                                ;;
                        esac
                        return 0
                    }}
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            events_text = events.read_text()
            log_text = (memory / "samba4/var/manager.log").read_text()
            acp_count_text = acp_count.read_text().strip()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=0\n", proc.stdout)
        self.assertEqual(acp_count_text, "2")
        self.assertEqual(events_text.count("identity\n"), 1, events_text)
        self.assertEqual(events_text.count("stage\n"), 1, events_text)
        self.assertEqual(events_text.count("mdns-process\n"), 1, events_text)
        self.assertEqual(events_text.count("bind-probe\n"), 2, events_text)
        self.assertNotIn("manager pass 2 step=samba_bind start", log_text)
        self.assertNotIn("manager scheduler: Samba bind reconciliation due", log_text)
        self.assertNotIn("scheduler=bind_only", log_text)

    def test_manager_smbd_debug_logging_prints_happy_path_pass_chatter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            with (flash / "tcapsulesmb.conf").open("a") as conf:
                conf.write("SMBD_DEBUG_LOGGING=1\n")
            fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_no_valid_hfs_partitions")
            acp_count = self.write_sequence_acp(tmp_path, (fixture.raw, fixture.raw))
            sleep_count = tmp_path / "sleep-count"
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                    tc_prepare_ram_root() {{ mkdir -p "$RAM_VAR"; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                        TC_RUNTIME_IDENTITY_READY=1
                    }}
                    tc_manager_stop_samba_lane_without_payload() {{ :; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            mdns-advertiser) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    stop_runtime_process_by_ucomm() {{ :; }}
                    tc_mdns_bound_udp_5353() {{ return 0; }}
                    sleep() {{
                        case "$1" in
                            1|5) return 0 ;;
                            10)
                                count=$(/bin/cat {shlex.quote(str(sleep_count))} 2>/dev/null || echo 0)
                                count=$((count + 1))
                                echo "$count" >{shlex.quote(str(sleep_count))}
                                if [ "$count" -eq 1 ]; then
                                    return 0
                                fi
                                echo "status=$manager_status"
                                exit 0
                                ;;
                        esac
                        return 0
                    }}
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            log_text = (memory / "samba4/var/manager.log").read_text()
            acp_count_text = acp_count.read_text().strip()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=0\n", proc.stdout)
        self.assertEqual(acp_count_text, "2")
        self.assertIn("manager pass 2 start", log_text)
        self.assertIn("manager MaSt stable signature unchanged; disk refresh skipped", log_text)
        self.assertIn("manager scheduler: Samba bind reconciliation due", log_text)
        self.assertIn("manager pass 2 step=samba_bind start", log_text)
        self.assertIn("scheduler=bind_only", log_text)

    def test_manager_scheduler_runs_full_services_immediately_after_disk_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            initial_raw = self.internal_mast_raw_with_volatile_fields(users=1)
            renamed_raw = initial_raw.replace('name = "Data";', 'name = "Data Two";')
            acp_count = self.write_sequence_acp(tmp_path, (initial_raw, renamed_raw, renamed_raw))
            events = tmp_path / "events"
            sleep_count = tmp_path / "sleep-count"
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                    tc_prepare_ram_root() {{ mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_PRIVATE" "$RAM_VAR"; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        echo identity >>{shlex.quote(str(events))}
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                        TC_RUNTIME_IDENTITY_READY=1
                    }}
                    tc_manager_refresh_runtime_identity_for_recovery() {{ :; }}
                    tc_wake_or_mount_volume() {{ return 0; }}
                    is_volume_root_mounted() {{ return 0; }}
                    tc_verify_payload_dir() {{ return 0; }}
                    tc_volume_is_writable() {{ return 0; }}
                    tc_prepare_share_path() {{ echo "$2/ShareRoot"; }}
                    tc_apply_ata_drive_setting() {{ :; }}
                    tc_payload_log_dir_ready() {{ return 0; }}
                    tc_find_payload_smbd() {{ echo "$1/smbd"; }}
                    tc_stage_runtime() {{
                        echo stage >>{shlex.quote(str(events))}
                        mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_PRIVATE"
                        printf '#!/bin/sh\\nexit 0\\n' >"$TC_SMBD_BIN"
                        chmod 755 "$TC_SMBD_BIN"
                        : >"$RAM_PRIVATE/smbpasswd"
                        : >"$RAM_PRIVATE/username.map"
                        return 0
                    }}
                    tc_probe_smb_bind_interfaces() {{
                        echo bind-probe >>{shlex.quote(str(events))}
                        echo "127.0.0.1/8"
                    }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            smbd) return 0 ;;
                            mdns-advertiser) echo mdns-process >>{shlex.quote(str(events))}; return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_smbd_bound_tcp_445() {{ return 0; }}
                    tc_reload_smbd_config() {{ echo reload >>{shlex.quote(str(events))}; }}
                    tc_mdns_bound_udp_5353() {{ return 0; }}
                    stop_runtime_process_by_ucomm() {{ :; }}
                    sleep() {{
                        case "$1" in
                            1) return 0 ;;
                            5) echo debounce; return 0 ;;
                            10)
                                count=$(/bin/cat {shlex.quote(str(sleep_count))} 2>/dev/null || echo 0)
                                count=$((count + 1))
                                echo "$count" >{shlex.quote(str(sleep_count))}
                                if [ "$count" -eq 1 ]; then
                                    return 0
                                fi
                                echo "status=$manager_status"
                                exit 0
                                ;;
                        esac
                        return 0
                    }}
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            events_text = events.read_text()
            log_text = (memory / "samba4/var/manager.log").read_text()
            acp_count_text = acp_count.read_text().strip()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=0\n", proc.stdout)
        self.assertIn("debounce\n", proc.stdout)
        self.assertEqual(acp_count_text, "3")
        self.assertEqual(events_text.count("identity\n"), 2, events_text)
        self.assertEqual(events_text.count("stage\n"), 1, events_text)
        self.assertEqual(events_text.count("reload\n"), 1, events_text)
        self.assertEqual(events_text.count("mdns-process\n"), 2, events_text)
        self.assertEqual(events_text.count("bind-probe\n"), 2, events_text)
        self.assertNotIn("manager pass 2 step=identity start", log_text)
        self.assertNotIn("manager pass 2 step=samba start", log_text)
        self.assertIn("disk_probe=change_confirmed", log_text)

    def test_manager_restarts_smbd_when_config_reload_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            self.write_fake_acp(tmp_path, self.internal_mast_raw_with_volatile_fields(users=1))
            with (flash / "tcapsulesmb.conf").open("a") as conf:
                conf.write("TC_SMB_BIND_INTERFACES='127.0.0.1/8'\n")
            events = tmp_path / "events"
            smbd_state = tmp_path / "smbd-state"
            smbd_state.write_text("running\n")
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                    tc_prepare_ram_root() {{ mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_PRIVATE" "$RAM_VAR"; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                        TC_RUNTIME_IDENTITY_READY=1
                    }}
                    tc_manager_refresh_runtime_identity_for_recovery() {{ :; }}
                    tc_wake_or_mount_volume() {{ return 0; }}
                    is_volume_root_mounted() {{ return 0; }}
                    tc_verify_payload_dir() {{ return 0; }}
                    tc_volume_is_writable() {{ return 0; }}
                    tc_prepare_share_path() {{ echo "$2/ShareRoot"; }}
                    tc_apply_ata_drive_setting() {{ :; }}
                    tc_payload_log_dir_ready() {{ return 0; }}
                    tc_find_payload_smbd() {{ echo "$1/smbd"; }}
                    tc_stage_runtime() {{
                        echo stage >>{shlex.quote(str(events))}
                        mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_PRIVATE"
                        cat >"$TC_SMBD_BIN" <<'EOF'
                    #!/bin/sh
                    printf 'running\\n' >{shlex.quote(str(smbd_state))}
                    printf 'launched\\n' >>{shlex.quote(str(events))}
                    exit 0
                    EOF
                        chmod 755 "$TC_SMBD_BIN"
                        : >"$RAM_PRIVATE/smbpasswd"
                        : >"$RAM_PRIVATE/username.map"
                        return 0
                    }}
                    tc_probe_smb_bind_interfaces() {{ echo "127.0.0.1/8"; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            smbd) [ "$(/bin/cat {shlex.quote(str(smbd_state))})" = "running" ] ;;
                            mdns-advertiser) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_smbd_bound_tcp_445() {{ [ "$(/bin/cat {shlex.quote(str(smbd_state))})" = "running" ]; }}
                    tc_reload_smbd_config() {{ echo reload >>{shlex.quote(str(events))}; return 1; }}
                    tc_mdns_bound_udp_5353() {{ return 0; }}
                    stop_runtime_process_by_ucomm() {{
                        echo "stop $1" >>{shlex.quote(str(events))}
                        if [ "$1" = "smbd" ]; then
                            printf 'stopped\\n' >{shlex.quote(str(smbd_state))}
                        fi
                    }}
                    sleep() {{
                        case "$1" in
                            1|5) return 0 ;;
                            10) echo "status=$manager_status"; exit 0 ;;
                        esac
                        return 0
                    }}
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            events_text = events.read_text()
            log_text = (memory / "samba4/var/manager.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=0\n", proc.stdout)
        self.assertEqual(
            events_text.splitlines(),
            ["stage", "reload", "stop smbd", "launched", "stop mdns-advertiser"],
            events_text,
        )
        self.assertIn("manager smbd recovery: smbd config reload failed; restarting", log_text)

    def test_manager_smbd_apply_failure_logs_runtime_reason_without_ip_defer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            self.write_fake_acp(tmp_path, self.internal_mast_raw_with_volatile_fields(users=1))
            with (flash / "tcapsulesmb.conf").open("a") as conf:
                conf.write("TC_SMB_BIND_INTERFACES='127.0.0.1/8'\n")
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        """\

                    tc_prepare_ram_root() { mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_PRIVATE" "$RAM_VAR"; }
                    tc_prepare_local_hostname_resolution() { :; }
                    tc_init_runtime_identity() {
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                        TC_RUNTIME_IDENTITY_READY=1
                    }
                    tc_manager_refresh_runtime_identity_for_recovery() { :; }
                    tc_wake_or_mount_volume() { return 0; }
                    is_volume_root_mounted() { return 0; }
                    tc_verify_payload_dir() { return 0; }
                    tc_volume_is_writable() { return 0; }
                    tc_prepare_share_path() { echo "$2/ShareRoot"; }
                    tc_apply_ata_drive_setting() { :; }
                    tc_payload_log_dir_ready() { return 0; }
                    tc_find_payload_smbd() { echo "$1/smbd"; }
                    tc_stage_runtime() {
                        mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_PRIVATE"
                        printf '#!/bin/sh\\nexit 0\\n' >"$TC_SMBD_BIN"
                        chmod 755 "$TC_SMBD_BIN"
                        : >"$RAM_PRIVATE/smbpasswd"
                        : >"$RAM_PRIVATE/username.map"
                        return 0
                    }
                    tc_probe_smb_bind_interfaces() { echo "127.0.0.1/8"; }
                    runtime_process_present_by_ucomm() {
                        case "$1" in
                            smbd) return 1 ;;
                            mdns-advertiser) return 0 ;;
                            *) return 1 ;;
                        esac
                    }
                    wait_for_process() {
                        case "$1" in
                            smbd) return 1 ;;
                            *) return 0 ;;
                        esac
                    }
                    tc_wait_for_smbd_ipv4_445() { return 1; }
                    tc_smbd_bound_tcp_445() { return 1; }
                    tc_mdns_bound_udp_5353() { return 0; }
                    stop_runtime_process_by_ucomm() { :; }
                    sleep() {
                        case "$1" in
                            1|5) return 0 ;;
                            10) echo "status=$manager_status bind=$manager_bind_status samba=$manager_samba_status"; exit 0 ;;
                        esac
                        return 0
                    }
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            log_text = (memory / "samba4/var/manager.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=1 bind=ok samba=failed\n", proc.stdout)
        self.assertIn("manager Samba: smbd runtime apply failed reason=start_failed; will retry on next reconciliation pass", log_text)
        self.assertIn("samba=failed bind=ok", log_text)
        self.assertNotIn("Samba bind discovery deferred; no usable address has appeared yet", log_text)
        self.assertNotIn("bind=deferred_no_ip", log_text)

    def test_manager_mdns_captures_with_one_retry_when_apple_responder_is_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_no_valid_hfs_partitions")
            self.write_fake_acp(tmp_path, fixture.raw)
            capture_count = tmp_path / "capture-count"
            launched = tmp_path / "mdns-launched"
            events = tmp_path / "mdns-events"
            (flash / "mdns-advertiser").write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >>{shlex.quote(str(events))}\n"
                "case \"$1\" in\n"
                "  --print-mdns-socket-families) echo ipv4; exit 0 ;;\n"
                "  --save-all-snapshot)\n"
                f"    count=$(/bin/cat {shlex.quote(str(capture_count))} 2>/dev/null || echo 0)\n"
                "    count=$((count + 1))\n"
                f"    echo \"$count\" >{shlex.quote(str(capture_count))}\n"
                "    if [ \"$count\" -eq 1 ]; then exit 12; fi\n"
                "    while [ \"$#\" -gt 0 ]; do\n"
                "      case \"$1\" in --save-snapshot) shift; echo captured >\"$1\" ;; esac\n"
                "      shift\n"
                "    done\n"
                "    exit 0 ;;\n"
                "  --snapshot-newer-than-boot) echo unexpected-freshness; exit 14 ;;\n"
                "  --save-airport-snapshot) echo unexpected-generation; exit 9 ;;\n"
                "  --load-snapshot) "
                f"touch {shlex.quote(str(launched))}; exit 0 ;;\n"
                "esac\n"
                "exit 0\n"
            )
            (flash / "mdns-advertiser").chmod(0o755)
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                    tc_prepare_ram_root() {{ mkdir -p "$RAM_VAR"; }}
                    tc_manager_reset_pass_state() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }}
                    tc_manager_refresh_runtime_identity_for_recovery() {{ :; }}
                    tc_prepare_mdns_identity() {{
                        TC_AIRPORT_FIELDS_ADVERTISE_MAC=80:EA:96:E6:58:68
                        AIRPORT_INSTANCE_NAME=AirPort
                        AIRPORT_HOST_LABEL=airport
                        AIRPORT_WAMA=80:EA:96:E6:58:68
                        AIRPORT_RAMA=
                        AIRPORT_RAM2=
                        AIRPORT_RAST=
                        AIRPORT_RANA=
                        AIRPORT_SYFL=
                        AIRPORT_SYAP=
                        AIRPORT_SYVS=
                        AIRPORT_SRCV=
                        AIRPORT_BJSD=
                        return 0
                    }}
                    tc_manager_stop_samba_lane_without_payload() {{ :; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            mDNSResponder) return 0 ;;
                            mdns-advertiser) [ -f {shlex.quote(str(launched))} ] ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_mdns_bound_udp_5353() {{ return 0; }}
                    sleep() {{
                        case "$1" in
                            1) return 0 ;;
                            3) echo "settle $1"; return 0 ;;
                            10) echo "status=$manager_status"; exit 0 ;;
                        esac
                        return 0
                    }}
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            log_text = (memory / "samba4/var/manager.log").read_text()
            events_text = events.read_text()
            capture_count_text = capture_count.read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("settle 3\n", proc.stdout)
        self.assertIn("status=0\n", proc.stdout)
        self.assertEqual(capture_count_text.strip(), "2")
        self.assertEqual(events_text.count("--save-all-snapshot"), 2, events_text)
        self.assertNotIn("--skip-capture-if-snapshot-newer-than-boot", events_text)
        self.assertNotIn("--snapshot-newer-than-boot", events_text)
        self.assertNotIn("--save-airport-snapshot", events_text)
        self.assertIn("manager mDNS snapshot: capture failed; retrying once", log_text)

    def test_manager_mdns_healthy_advertiser_does_not_probe_or_relaunch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_no_valid_hfs_partitions")
            self.write_fake_acp(tmp_path, fixture.raw)
            events = tmp_path / "mdns-events"
            (flash / "mdns-advertiser").write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >>{shlex.quote(str(events))}\n"
                "exit 0\n"
            )
            (flash / "mdns-advertiser").chmod(0o755)
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        """\

                    tc_prepare_ram_root() { mkdir -p "$RAM_VAR"; }
                    tc_manager_reset_pass_state() { :; }
                    tc_prepare_local_hostname_resolution() { :; }
                    tc_init_runtime_identity() {
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }
                    tc_manager_stop_samba_lane_without_payload() { :; }
                    runtime_process_present_by_ucomm() {
                        case "$1" in
                            mdns-advertiser) return 0 ;;
                            *) return 1 ;;
                        esac
                    }
                    stop_runtime_process_by_ucomm() { :; }
                    tc_mdns_bound_udp_5353() { return 0; }
                    sleep() {
                        case "$1" in
                            1) return 0 ;;
                            10) echo "status=$manager_status"; exit 0 ;;
                        esac
                        return 0
                    }
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            events_exists = events.exists()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=0\n", proc.stdout)
        self.assertFalse(events_exists)

    def test_manager_mdns_defers_when_auto_ip_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_no_valid_hfs_partitions")
            self.write_fake_acp(tmp_path, fixture.raw)
            events = tmp_path / "mdns-events"
            (flash / "mdns-advertiser").write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >>{shlex.quote(str(events))}\n"
                "case \"$1\" in\n"
                "  --print-mdns-socket-families) exit 11 ;;\n"
                "  *) echo unexpected-mdns-command; exit 9 ;;\n"
                "esac\n"
            )
            (flash / "mdns-advertiser").chmod(0o755)
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        """\

                    tc_prepare_ram_root() { mkdir -p "$RAM_VAR"; }
                    tc_manager_reset_pass_state() { :; }
                    tc_prepare_local_hostname_resolution() { :; }
                    tc_init_runtime_identity() {
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }
                    tc_manager_refresh_runtime_identity_for_recovery() { :; }
                    tc_manager_stop_samba_lane_without_payload() { :; }
                    runtime_process_present_by_ucomm() { return 1; }
                    tc_mdns_bound_udp_5353() { return 1; }
                    sleep() {
                        case "$1" in
                            1) return 0 ;;
                            3) echo unexpected-settle; return 0 ;;
                            10) echo "status=$manager_status deferred=$TC_MANAGER_MDNS_DEFERRED_NO_IP"; exit 0 ;;
                        esac
                        return 0
                    }
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            events_text = events.read_text()
            log_text = (memory / "samba4/var/manager.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=0 deferred=1\n", proc.stdout)
        self.assertNotIn("unexpected-settle", proc.stdout)
        self.assertEqual(events_text, "--print-mdns-socket-families\n")
        self.assertIn("mDNS startup deferred; no usable address has appeared yet", log_text)

    def test_manager_mdns_uses_first_successful_capture_without_retry_or_freshness_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_no_valid_hfs_partitions")
            self.write_fake_acp(tmp_path, fixture.raw)
            capture_count = tmp_path / "capture-count"
            launched = tmp_path / "mdns-launched"
            events = tmp_path / "mdns-events"
            (flash / "mdns-advertiser").write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >>{shlex.quote(str(events))}\n"
                "case \"$1\" in\n"
                "  --print-mdns-socket-families) echo ipv4; exit 0 ;;\n"
                "  --save-all-snapshot)\n"
                f"    echo 1 >{shlex.quote(str(capture_count))}\n"
                "    while [ \"$#\" -gt 0 ]; do\n"
                "      case \"$1\" in --save-snapshot) shift; echo captured >\"$1\" ;; esac\n"
                "      shift\n"
                "    done\n"
                "    exit 0 ;;\n"
                "  --snapshot-newer-than-boot) echo unexpected-freshness; exit 14 ;;\n"
                "  --save-airport-snapshot) echo unexpected-generation; exit 9 ;;\n"
                "  --load-snapshot) "
                f"touch {shlex.quote(str(launched))}; exit 0 ;;\n"
                "esac\n"
                "exit 0\n"
            )
            (flash / "mdns-advertiser").chmod(0o755)
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                    tc_prepare_ram_root() {{ mkdir -p "$RAM_VAR"; }}
                    tc_manager_reset_pass_state() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }}
                    tc_manager_refresh_runtime_identity_for_recovery() {{ :; }}
                    tc_prepare_mdns_identity() {{
                        TC_AIRPORT_FIELDS_ADVERTISE_MAC=80:EA:96:E6:58:68
                        AIRPORT_INSTANCE_NAME=AirPort
                        AIRPORT_HOST_LABEL=airport
                        AIRPORT_WAMA=80:EA:96:E6:58:68
                        AIRPORT_RAMA=
                        AIRPORT_RAM2=
                        AIRPORT_RAST=
                        AIRPORT_RANA=
                        AIRPORT_SYFL=
                        AIRPORT_SYAP=
                        AIRPORT_SYVS=
                        AIRPORT_SRCV=
                        AIRPORT_BJSD=
                        return 0
                    }}
                    tc_manager_stop_samba_lane_without_payload() {{ :; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            mDNSResponder) return 0 ;;
                            mdns-advertiser) [ -f {shlex.quote(str(launched))} ] ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_mdns_bound_udp_5353() {{ return 0; }}
                    sleep() {{
                        case "$1" in
                            1) return 0 ;;
                            3) echo "settle $1"; return 0 ;;
                            10) echo "status=$manager_status"; exit 0 ;;
                        esac
                        return 0
                    }}
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            events_text = events.read_text()
            capture_count_text = capture_count.read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("settle 3\n", proc.stdout)
        self.assertIn("status=0\n", proc.stdout)
        self.assertEqual(capture_count_text.strip(), "1")
        self.assertEqual(events_text.count("--save-all-snapshot"), 1, events_text)
        self.assertNotIn("--snapshot-newer-than-boot", events_text)
        self.assertNotIn("--save-airport-snapshot", events_text)

    def test_manager_mdns_reuses_fresh_snapshot_after_capture_attempts_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_no_valid_hfs_partitions")
            self.write_fake_acp(tmp_path, fixture.raw)
            capture_count = tmp_path / "capture-count"
            launched = tmp_path / "mdns-launched"
            events = tmp_path / "mdns-events"
            (flash / "mdns-advertiser").write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >>{shlex.quote(str(events))}\n"
                "case \"$1\" in\n"
                "  --print-mdns-socket-families) echo ipv4; exit 0 ;;\n"
                "  --save-all-snapshot)\n"
                f"    count=$(/bin/cat {shlex.quote(str(capture_count))} 2>/dev/null || echo 0)\n"
                "    count=$((count + 1))\n"
                f"    echo \"$count\" >{shlex.quote(str(capture_count))}\n"
                "    exit 12 ;;\n"
                "  --snapshot-newer-than-boot) exit 0 ;;\n"
                "  --save-airport-snapshot) echo unexpected-generation; exit 9 ;;\n"
                "  --load-snapshot) "
                f"touch {shlex.quote(str(launched))}; exit 0 ;;\n"
                "esac\n"
                "exit 0\n"
            )
            (flash / "mdns-advertiser").chmod(0o755)
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                    tc_prepare_ram_root() {{ mkdir -p "$RAM_VAR"; }}
                    tc_manager_reset_pass_state() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }}
                    tc_manager_refresh_runtime_identity_for_recovery() {{ :; }}
                    tc_prepare_mdns_identity() {{
                        TC_AIRPORT_FIELDS_ADVERTISE_MAC=80:EA:96:E6:58:68
                        AIRPORT_INSTANCE_NAME=AirPort
                        AIRPORT_HOST_LABEL=airport
                        AIRPORT_WAMA=80:EA:96:E6:58:68
                        AIRPORT_RAMA=
                        AIRPORT_RAM2=
                        AIRPORT_RAST=
                        AIRPORT_RANA=
                        AIRPORT_SYFL=
                        AIRPORT_SYAP=
                        AIRPORT_SYVS=
                        AIRPORT_SRCV=
                        AIRPORT_BJSD=
                        return 0
                    }}
                    tc_manager_stop_samba_lane_without_payload() {{ :; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            mDNSResponder) return 0 ;;
                            mdns-advertiser) [ -f {shlex.quote(str(launched))} ] ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_mdns_bound_udp_5353() {{ return 0; }}
                    sleep() {{
                        case "$1" in
                            1) return 0 ;;
                            3) echo "settle $1"; return 0 ;;
                            10) echo "status=$manager_status"; exit 0 ;;
                        esac
                        return 0
                    }}
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            events_text = events.read_text()
            capture_count_text = capture_count.read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("settle 3\n", proc.stdout)
        self.assertIn("status=0\n", proc.stdout)
        self.assertEqual(capture_count_text.strip(), "2")
        self.assertEqual(events_text.count("--save-all-snapshot"), 2, events_text)
        self.assertIn("--snapshot-newer-than-boot", events_text)
        self.assertNotIn("--save-airport-snapshot", events_text)
        self.assertIn("--load-snapshot", events_text)

    def test_manager_mdns_reuses_fresh_snapshot_when_apple_responder_is_dead(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_no_valid_hfs_partitions")
            self.write_fake_acp(tmp_path, fixture.raw)
            events = tmp_path / "mdns-events"
            launched = tmp_path / "mdns-launched"
            (flash / "mdns-advertiser").write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >>{shlex.quote(str(events))}\n"
                "case \"$1\" in\n"
                "  --print-mdns-socket-families) echo ipv4; exit 0 ;;\n"
                "  --snapshot-newer-than-boot) exit 0 ;;\n"
                "  --save-all-snapshot) echo unexpected-capture; exit 9 ;;\n"
                "  --save-airport-snapshot) echo unexpected-generation; exit 9 ;;\n"
                "  --load-snapshot) "
                f"touch {shlex.quote(str(launched))}; exit 0 ;;\n"
                "esac\n"
                "exit 0\n"
            )
            (flash / "mdns-advertiser").chmod(0o755)
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                    tc_prepare_ram_root() {{ mkdir -p "$RAM_VAR"; }}
                    tc_manager_reset_pass_state() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }}
                    tc_manager_refresh_runtime_identity_for_recovery() {{ :; }}
                    tc_prepare_mdns_identity() {{
                        TC_AIRPORT_FIELDS_ADVERTISE_MAC=80:EA:96:E6:58:68
                        AIRPORT_INSTANCE_NAME=AirPort
                        AIRPORT_HOST_LABEL=airport
                        AIRPORT_WAMA=80:EA:96:E6:58:68
                        AIRPORT_RAMA=
                        AIRPORT_RAM2=
                        AIRPORT_RAST=
                        AIRPORT_RANA=
                        AIRPORT_SYFL=
                        AIRPORT_SYAP=
                        AIRPORT_SYVS=
                        AIRPORT_SRCV=
                        AIRPORT_BJSD=
                        return 0
                    }}
                    tc_manager_stop_samba_lane_without_payload() {{ :; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            mdns-advertiser) [ -f {shlex.quote(str(launched))} ] ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_mdns_bound_udp_5353() {{ return 0; }}
                    sleep() {{
                        case "$1" in
                            1) return 0 ;;
                            3) echo unexpected-settle; return 0 ;;
                            10) echo "status=$manager_status"; exit 0 ;;
                        esac
                        return 0
                    }}
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            events_text = events.read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=0\n", proc.stdout)
        self.assertNotIn("unexpected-settle", proc.stdout)
        self.assertIn("--snapshot-newer-than-boot", events_text)
        self.assertNotIn("--save-all-snapshot", events_text)
        self.assertNotIn("--save-airport-snapshot", events_text)
        self.assertIn("--load-snapshot", events_text)

    def test_manager_mdns_generates_fallback_when_snapshot_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_no_valid_hfs_partitions")
            self.write_fake_acp(tmp_path, fixture.raw)
            events = tmp_path / "mdns-events"
            launched = tmp_path / "mdns-launched"
            (flash / "mdns-advertiser").write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >>{shlex.quote(str(events))}\n"
                "case \"$1\" in\n"
                "  --print-mdns-socket-families) echo ipv4; exit 0 ;;\n"
                "  --snapshot-newer-than-boot) exit 14 ;;\n"
                "  --save-airport-snapshot) shift; echo generated >\"$1\"; exit 0 ;;\n"
                "  --save-all-snapshot) echo unexpected-capture; exit 9 ;;\n"
                "  --load-snapshot) "
                f"touch {shlex.quote(str(launched))}; exit 0 ;;\n"
                "esac\n"
                "exit 0\n"
            )
            (flash / "mdns-advertiser").chmod(0o755)
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                    tc_prepare_ram_root() {{ mkdir -p "$RAM_VAR"; }}
                    tc_manager_reset_pass_state() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }}
                    tc_manager_refresh_runtime_identity_for_recovery() {{ :; }}
                    tc_prepare_mdns_identity() {{
                        TC_AIRPORT_FIELDS_ADVERTISE_MAC=80:EA:96:E6:58:68
                        AIRPORT_INSTANCE_NAME=AirPort
                        AIRPORT_HOST_LABEL=airport
                        AIRPORT_WAMA=80:EA:96:E6:58:68
                        AIRPORT_RAMA=
                        AIRPORT_RAM2=
                        AIRPORT_RAST=
                        AIRPORT_RANA=
                        AIRPORT_SYFL=
                        AIRPORT_SYAP=
                        AIRPORT_SYVS=
                        AIRPORT_SRCV=
                        AIRPORT_BJSD=
                        return 0
                    }}
                    tc_manager_stop_samba_lane_without_payload() {{ :; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            mdns-advertiser) [ -f {shlex.quote(str(launched))} ] ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_mdns_bound_udp_5353() {{ return 0; }}
                    sleep() {{
                        case "$1" in
                            1) return 0 ;;
                            10) echo "status=$manager_status"; exit 0 ;;
                        esac
                        return 0
                    }}
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            events_text = events.read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=0\n", proc.stdout)
        self.assertIn("--snapshot-newer-than-boot", events_text)
        self.assertIn("--save-airport-snapshot", events_text)
        self.assertNotIn("--save-all-snapshot", events_text)
        self.assertIn("--load-snapshot", events_text)

    def test_manager_diskless_state_resets_advertiser_logs_to_ram(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_no_valid_hfs_partitions")
            self.write_fake_acp(tmp_path, fixture.raw)
            old_payload_logs = tmp_path / "old-payload/logs"
            launched = tmp_path / "mdns-launched"
            (flash / "mdns-advertiser").write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  --print-mdns-socket-families) echo ipv4; exit 0 ;;\n"
                "  --snapshot-newer-than-boot) exit 14 ;;\n"
                "  --save-airport-snapshot) shift; echo generated >\"$1\"; exit 0 ;;\n"
                "  --load-snapshot) "
                f"touch {shlex.quote(str(launched))}; exit 0 ;;\n"
                "esac\n"
                "exit 0\n"
            )
            (flash / "mdns-advertiser").chmod(0o755)
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                    tc_prepare_ram_root() {{ mkdir -p "$RAM_VAR"; }}
                    tc_manager_reset_pass_state() {{
                        tc_set_payload_log_dir {shlex.quote(str(old_payload_logs.parent))} {shlex.quote(str(old_payload_logs.parent))}
                    }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }}
                    tc_manager_refresh_runtime_identity_for_recovery() {{ :; }}
                    tc_prepare_mdns_identity() {{
                        TC_AIRPORT_FIELDS_ADVERTISE_MAC=80:EA:96:E6:58:68
                        AIRPORT_INSTANCE_NAME=AirPort
                        AIRPORT_HOST_LABEL=airport
                        AIRPORT_WAMA=80:EA:96:E6:58:68
                        AIRPORT_RAMA=
                        AIRPORT_RAM2=
                        AIRPORT_RAST=
                        AIRPORT_RANA=
                        AIRPORT_SYFL=
                        AIRPORT_SYAP=
                        AIRPORT_SYVS=
                        AIRPORT_SRCV=
                        AIRPORT_BJSD=
                        return 0
                    }}
                    tc_manager_stop_samba_lane_without_payload() {{ :; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            mdns-advertiser) [ -f {shlex.quote(str(launched))} ] ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_mdns_bound_udp_5353() {{ return 0; }}
                    sleep() {{
                        case "$1" in
                            1) return 0 ;;
                            10) echo "status=$manager_status"; exit 0 ;;
                        esac
                        return 0
                    }}
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            ram_mdns_log_exists = (memory / "samba4/var/mdns.log").exists()
            old_payload_mdns_log_exists = (old_payload_logs / "mdns.log").exists()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=0\n", proc.stdout)
        self.assertTrue(ram_mdns_log_exists)
        self.assertFalse(old_payload_mdns_log_exists)

    def test_manager_ignores_volatile_mast_fields_when_comparing_topology(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            acp_count = self.write_sequence_acp(
                tmp_path,
                (
                    self.internal_mast_raw_with_volatile_fields(users=1, size_free=100000, size_used=200000),
                    self.internal_mast_raw_with_volatile_fields(users=2, size_free=90000, size_used=210000, soft_disconnected="true"),
                ),
            )
            outer_sleep_count = tmp_path / "outer-sleep-count"
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                    tc_prepare_ram_root() {{ mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_PRIVATE" "$RAM_VAR"; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }}
                    tc_wake_or_mount_volume() {{ echo "disk-mount $1 $2"; return 0; }}
                    is_volume_root_mounted() {{ return 0; }}
                    tc_verify_payload_dir() {{ return 0; }}
                    tc_volume_is_writable() {{ return 0; }}
                    tc_prepare_share_path() {{ echo "$2/ShareRoot"; }}
                    tc_apply_ata_drive_setting() {{ :; }}
                    tc_payload_log_dir_ready() {{ return 0; }}
                    tc_find_payload_smbd() {{ echo "$1/smbd"; }}
                    tc_stage_runtime() {{
                        echo stage-runtime
                        mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_PRIVATE"
                        printf '#!/bin/sh\\nexit 0\\n' >"$TC_SMBD_BIN"
                        chmod 755 "$TC_SMBD_BIN"
                        return 0
                    }}
                    tc_probe_smb_bind_interfaces() {{ echo "127.0.0.1/8"; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            smbd|mdns-advertiser) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_smbd_bound_tcp_445() {{ return 0; }}
                    tc_mdns_bound_udp_5353() {{ return 0; }}
                    stop_runtime_process_by_ucomm() {{ :; }}
                    sleep() {{
                        case "$1" in
                            1) return 0 ;;
                            5) echo "unexpected-debounce"; return 0 ;;
                            10)
                                count=$(/bin/cat {shlex.quote(str(outer_sleep_count))} 2>/dev/null || echo 0)
                                count=$((count + 1))
                                echo "$count" >{shlex.quote(str(outer_sleep_count))}
                                if [ "$count" -eq 1 ]; then
                                    return 0
                                fi
                                echo "status=$manager_status"
                                exit 0
                                ;;
                        esac
                        return 0
                    }}
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            acp_count_text = acp_count.read_text().strip()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(acp_count_text, "2")
        self.assertEqual(proc.stdout.count("disk-mount /dev/dk2"), 1, proc.stdout)
        self.assertIn("stage-runtime\n", proc.stdout)
        self.assertIn("status=0\n", proc.stdout)
        self.assertNotIn("unexpected-debounce", proc.stdout)
        self.assertNotIn("unexpected-manager-mount", proc.stdout)

    def test_manager_reclaims_active_disk_users_without_full_topology_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            acp_count = self.write_sequence_acp(
                tmp_path,
                (
                    self.internal_mast_raw_with_volatile_fields(users=1),
                    self.internal_mast_raw_with_volatile_fields(users=0),
                ),
            )
            outer_sleep_count = tmp_path / "outer-sleep-count"
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                    tc_prepare_ram_root() {{ mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_PRIVATE" "$RAM_VAR"; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }}
                    tc_wake_or_mount_volume() {{ echo "disk-mount $1 $2"; return 0; }}
                    is_volume_root_mounted() {{ return 0; }}
                    tc_verify_payload_dir() {{ return 0; }}
                    tc_volume_is_writable() {{ return 0; }}
                    tc_prepare_share_path() {{ echo "$2/ShareRoot"; }}
                    tc_apply_ata_drive_setting() {{ :; }}
                    tc_payload_log_dir_ready() {{ return 0; }}
                    tc_find_payload_smbd() {{ echo "$1/smbd"; }}
                    tc_stage_runtime() {{
                        echo stage-runtime
                        mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_PRIVATE"
                        printf '#!/bin/sh\\nexit 0\\n' >"$TC_SMBD_BIN"
                        chmod 755 "$TC_SMBD_BIN"
                        return 0
                    }}
                    tc_probe_smb_bind_interfaces() {{ echo "127.0.0.1/8"; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            smbd|mdns-advertiser) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_smbd_bound_tcp_445() {{ return 0; }}
                    tc_mdns_bound_udp_5353() {{ return 0; }}
                    stop_runtime_process_by_ucomm() {{ :; }}
                    sleep() {{
                        case "$1" in
                            1) return 0 ;;
                            5) echo "unexpected-debounce"; return 0 ;;
                            10)
                                count=$(/bin/cat {shlex.quote(str(outer_sleep_count))} 2>/dev/null || echo 0)
                                count=$((count + 1))
                                echo "$count" >{shlex.quote(str(outer_sleep_count))}
                                if [ "$count" -eq 1 ]; then
                                    return 0
                                fi
                                echo "status=$manager_status"
                                exit 0
                                ;;
                        esac
                        return 0
                    }}
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            acp_count_text = acp_count.read_text().strip()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(acp_count_text, "2")
        self.assertEqual(proc.stdout.count("disk-mount /dev/dk2"), 2, proc.stdout)
        self.assertIn("status=0\n", proc.stdout)
        self.assertNotIn("unexpected-debounce", proc.stdout)

    def test_manager_debounces_real_stable_mast_topology_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            empty_fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_no_valid_hfs_partitions")
            external_fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_external_only")
            acp_count = self.write_sequence_acp(
                tmp_path,
                (
                    empty_fixture.raw,
                    external_fixture.raw,
                    external_fixture.raw,
                ),
            )
            outer_sleep_count = tmp_path / "outer-sleep-count"
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                    tc_prepare_ram_root() {{ mkdir -p "$RAM_VAR"; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }}
                    tc_wake_or_mount_volume() {{ echo "disk-mount $1 $2"; return 0; }}
                    is_volume_root_mounted() {{ return 0; }}
                    tc_verify_payload_dir() {{ return 1; }}
                    tc_manager_stop_samba_lane_without_payload() {{ :; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            mdns-advertiser) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_mdns_bound_udp_5353() {{ return 0; }}
                    stop_runtime_process_by_ucomm() {{ :; }}
                    sleep() {{
                        case "$1" in
                            1) return 0 ;;
                            5) echo "debounce $1"; return 0 ;;
                            10)
                                count=$(/bin/cat {shlex.quote(str(outer_sleep_count))} 2>/dev/null || echo 0)
                                count=$((count + 1))
                                echo "$count" >{shlex.quote(str(outer_sleep_count))}
                                if [ "$count" -eq 1 ]; then
                                    return 0
                                fi
                                echo "status=$manager_status"
                                exit 0
                                ;;
                        esac
                        return 0
                    }}
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            acp_count_text = acp_count.read_text().strip()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(acp_count_text, "3")
        self.assertIn("debounce 5\n", proc.stdout)
        self.assertIn("disk-mount /dev/dk5", proc.stdout)
        self.assertIn("status=0\n", proc.stdout)

    def test_manager_smbd_validation_does_not_wake_or_mount_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            self.write_fake_acp(tmp_path, self.internal_mast_raw_with_volatile_fields(users=1))
            smbd_seen = tmp_path / "smbd-seen"
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                    tc_prepare_ram_root() {{ mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_PRIVATE" "$RAM_VAR"; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }}
                    tc_wake_or_mount_volume() {{ echo "disk-mount $1 $2"; return 0; }}
                    is_volume_root_mounted() {{ return 0; }}
                    tc_verify_payload_dir() {{ return 0; }}
                    tc_volume_is_writable() {{ return 0; }}
                    tc_prepare_share_path() {{ echo "$2/ShareRoot"; }}
                    tc_apply_ata_drive_setting() {{ :; }}
                    tc_payload_log_dir_ready() {{ return 0; }}
                    tc_find_payload_smbd() {{ echo "$1/smbd"; }}
                    tc_stage_runtime() {{
                        echo stage-runtime
                        mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_PRIVATE"
                        printf '#!/bin/sh\\nexit 0\\n' >"$TC_SMBD_BIN"
                        chmod 755 "$TC_SMBD_BIN"
                        return 0
                    }}
                    tc_probe_smb_bind_interfaces() {{ echo "127.0.0.1/8"; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            smbd) [ -f {shlex.quote(str(smbd_seen))} ] ;;
                            mdns-advertiser) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    wait_for_process() {{
                        if [ "$1" = "smbd" ]; then
                            : >{shlex.quote(str(smbd_seen))}
                        fi
                        return 0
                    }}
                    tc_wait_for_smbd_ipv4_445() {{ return 0; }}
                    tc_smbd_bound_tcp_445() {{ return 0; }}
                    tc_mdns_bound_udp_5353() {{ return 0; }}
                    stop_runtime_process_by_ucomm() {{ :; }}
                    sleep() {{
                        if [ "$1" = "1" ]; then
                            return 0
                        fi
                        echo "status=$manager_status"
                        exit 0
                    }}
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.count("disk-mount /dev/dk2"), 1, proc.stdout)
        self.assertIn("stage-runtime\n", proc.stdout)
        self.assertIn("status=0\n", proc.stdout)
        self.assertNotIn("unexpected-manager-mount", proc.stdout)

    def test_manager_waits_for_nbns_udp_137_after_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            self.write_fake_acp(tmp_path, self.internal_mast_raw_with_volatile_fields(users=1))
            with (flash / "tcapsulesmb.conf").open("a") as conf:
                conf.write("NBNS_ENABLED=1\n")
            nbns_bound_checks = tmp_path / "nbns-bound-checks"
            events = tmp_path / "events"
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                    tc_prepare_ram_root() {{ mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_PRIVATE" "$RAM_VAR"; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }}
                    tc_wake_or_mount_volume() {{ return 0; }}
                    is_volume_root_mounted() {{ return 0; }}
                    tc_verify_payload_dir() {{ return 0; }}
                    tc_volume_is_writable() {{ return 0; }}
                    tc_prepare_share_path() {{ echo "$2/ShareRoot"; }}
                    tc_apply_ata_drive_setting() {{ :; }}
                    tc_payload_log_dir_ready() {{ return 0; }}
                    tc_find_payload_smbd() {{ echo "$1/smbd"; }}
                    tc_find_payload_nbns() {{ echo "$1/nbns-advertiser"; }}
                    tc_stage_runtime() {{
                        mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_PRIVATE"
                        printf '#!/bin/sh\\nexit 0\\n' >"$TC_SMBD_BIN"
                        printf '#!/bin/sh\\nexit 0\\n' >"$TC_NBNS_BIN"
                        chmod 755 "$TC_SMBD_BIN" "$TC_NBNS_BIN"
                        return 0
                    }}
                    tc_probe_smb_bind_interfaces() {{ echo "127.0.0.1/8"; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            smbd) return 0 ;;
                            mdns-advertiser) echo mdns-process >>{shlex.quote(str(events))}; return 0 ;;
                            nbns-advertiser) echo nbns-process >>{shlex.quote(str(events))}; return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_smbd_bound_tcp_445() {{ return 0; }}
                    tc_mdns_bound_udp_5353() {{ return 0; }}
                    tc_manager_reconcile_nbns() {{ echo nbns-reconcile >>{shlex.quote(str(events))}; echo nbns-reconcile; return 0; }}
                    tc_nbns_bound_ipv4_udp_137() {{
                        echo nbns-socket >>{shlex.quote(str(events))}
                        count=$(/bin/cat {shlex.quote(str(nbns_bound_checks))} 2>/dev/null || echo 0)
                        count=$((count + 1))
                        echo "$count" >{shlex.quote(str(nbns_bound_checks))}
                        [ "$count" -ge 2 ]
                    }}
                    stop_runtime_process_by_ucomm() {{ :; }}
                    sleep() {{
                        case "$1" in
                            1) echo "sleep $1"; return 0 ;;
                            10) echo "status=$manager_status"; exit 0 ;;
                        esac
                        return 0
                    }}
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            log_text = (memory / "samba4/var/manager.log").read_text()
            nbns_bound_check_count = int(nbns_bound_checks.read_text().strip())
            events_text = events.read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("nbns-reconcile\n", proc.stdout)
        self.assertIn("sleep 1\n", proc.stdout)
        self.assertIn("status=0\n", proc.stdout)
        self.assertGreaterEqual(nbns_bound_check_count, 2)
        self.assertLess(events_text.index("nbns-reconcile"), events_text.index("mdns-process"))
        self.assertLess(events_text.index("mdns-process"), events_text.index("nbns-process"))
        self.assertLess(events_text.index("mdns-process"), events_text.index("nbns-socket"))
        self.assertNotIn("manager NBNS: reconcile requested; readiness check will run after mDNS", log_text)
        self.assertNotIn("manager NBNS: responder ready on required UDP 137 sockets", log_text)
        self.assertNotIn("manager pass 1 step=health", log_text)
        self.assertNotIn("manager health:", log_text)

    def test_manager_skips_complete_health_sweep_after_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_no_valid_hfs_partitions")
            self.write_fake_acp(tmp_path, fixture.raw)
            mdns_process_checks = tmp_path / "mdns-process-checks"
            mdns_socket_checks = tmp_path / "mdns-socket-checks"
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                    tc_prepare_ram_root() {{ mkdir -p "$RAM_VAR"; }}
                    tc_manager_reset_pass_state() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }}
                    tc_manager_stop_samba_lane_without_payload() {{ :; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            mdns-advertiser)
                                count=$(/bin/cat {shlex.quote(str(mdns_process_checks))} 2>/dev/null || echo 0)
                                count=$((count + 1))
                                echo "$count" >{shlex.quote(str(mdns_process_checks))}
                                return 0
                                ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_mdns_bound_udp_5353() {{
                        count=$(/bin/cat {shlex.quote(str(mdns_socket_checks))} 2>/dev/null || echo 0)
                        count=$((count + 1))
                        echo "$count" >{shlex.quote(str(mdns_socket_checks))}
                        return 0
                    }}
                    stop_runtime_process_by_ucomm() {{ :; }}
                    sleep() {{
                        case "$1" in
                            1) return 0 ;;
                            10) echo "status=$manager_status"; exit 0 ;;
                        esac
                        return 0
                    }}
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            log_text = (memory / "samba4/var/manager.log").read_text()
            mdns_process_check_count = int(mdns_process_checks.read_text().strip())
            mdns_socket_check_count = int(mdns_socket_checks.read_text().strip())

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=0\n", proc.stdout)
        self.assertEqual(mdns_process_check_count, 1)
        self.assertEqual(mdns_socket_check_count, 1)
        self.assertNotIn("manager pass 1 step=health", log_text)
        self.assertNotIn("manager health:", log_text)

    def test_manager_nbns_readiness_logs_unbound_socket_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            self.write_fake_acp(tmp_path, self.internal_mast_raw_with_volatile_fields(users=1))
            with (flash / "tcapsulesmb.conf").open("a") as conf:
                conf.write("NBNS_ENABLED=1\n")
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        """\

                    tc_prepare_ram_root() { mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_PRIVATE" "$RAM_VAR"; }
                    tc_prepare_local_hostname_resolution() { :; }
                    tc_init_runtime_identity() {
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }
                    tc_wake_or_mount_volume() { return 0; }
                    is_volume_root_mounted() { return 0; }
                    tc_verify_payload_dir() { return 0; }
                    tc_volume_is_writable() { return 0; }
                    tc_prepare_share_path() { echo "$2/ShareRoot"; }
                    tc_apply_ata_drive_setting() { :; }
                    tc_payload_log_dir_ready() { return 0; }
                    tc_find_payload_smbd() { echo "$1/smbd"; }
                    tc_find_payload_nbns() { echo "$1/nbns-advertiser"; }
                    tc_stage_runtime() {
                        mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_PRIVATE"
                        printf '#!/bin/sh\nexit 0\n' >"$TC_SMBD_BIN"
                        printf '#!/bin/sh\nexit 0\n' >"$TC_NBNS_BIN"
                        chmod 755 "$TC_SMBD_BIN" "$TC_NBNS_BIN"
                        return 0
                    }
                    tc_probe_smb_bind_interfaces() { echo "127.0.0.1/8"; }
                    runtime_process_present_by_ucomm() {
                        case "$1" in
                            smbd|mdns-advertiser|nbns-advertiser) return 0 ;;
                            *) return 1 ;;
                        esac
                    }
                    tc_smbd_bound_tcp_445() { return 0; }
                    tc_mdns_bound_udp_5353() { return 0; }
                    tc_manager_reconcile_nbns() { echo nbns-reconcile; return 0; }
                    tc_nbns_bound_ipv4_udp_137() { return 1; }
                    stop_runtime_process_by_ucomm() { :; }
                    sleep() {
                        case "$1" in
                            1) echo "sleep $1"; return 0 ;;
                            10) echo "status=$manager_status"; exit 0 ;;
                        esac
                        return 0
                    }
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            log_text = (memory / "samba4/var/manager.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("nbns-reconcile\n", proc.stdout)
        self.assertIn("status=1\n", proc.stdout)
        self.assertIn("manager NBNS: responder did not become ready on required UDP 137 sockets after 10s", log_text)
        self.assertIn("nbns=failed", log_text)
        self.assertNotIn("manager health:", log_text)

    def test_common_stage_runtime_installs_executables_with_temp_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            (payload / "smbd").write_text("payload smbd\n")
            (payload / "smbd").chmod(0o755)
            (payload / "private/smbpasswd").write_text("admin:x\n")
            (payload / "private/username.map").write_text("admin = root\n")
            events = tmp_path / "stage-events"
            script = tmp_path / "stage-runtime-temp-rename.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_prepare_ram_root
                    cp() {{
                        echo "cp:$1:$2" >>{shlex.quote(str(events))}
                        /bin/cp "$1" "$2"
                    }}
                    chmod() {{
                        echo "chmod:$*" >>{shlex.quote(str(events))}
                        /bin/chmod "$@"
                    }}
                    mv() {{
                        echo "mv:$1:$2" >>{shlex.quote(str(events))}
                        /bin/mv "$1" "$2"
                    }}
                    tc_stage_runtime {payload} {payload}/smbd ""
                    printf 'dest='
                    /bin/cat "$TC_SMBD_BIN"
                    /bin/cat {shlex.quote(str(events))}
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dest=payload smbd\n", proc.stdout)
        self.assertRegex(
            proc.stdout,
            rf"cp:{payload}/smbd:{memory}/samba4/sbin/smbd\.tmp\.[0-9]+",
        )
        self.assertRegex(
            proc.stdout,
            rf"mv:{memory}/samba4/sbin/smbd\.tmp\.[0-9]+:{memory}/samba4/sbin/smbd",
        )
        self.assertNotIn(f"cp:{payload}/smbd:{memory}/samba4/sbin/smbd\n", proc.stdout)

    def test_common_smb_bind_probe_rejects_invalid_cidr_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            (flash / "mdns-advertiser").write_text("#!/bin/sh\necho '192.168.1.40 bad/value'\n")
            (flash / "mdns-advertiser").chmod(0o755)
            script = tmp_path / "smb-bind-invalid.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR"
                    if bind=$(tc_probe_smb_bind_interfaces); then
                        echo status=0
                        printf 'bind=%s\\n' "$bind"
                    else
                        echo status=$?
                        printf 'bind=\\n'
                    fi
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=1\n", proc.stdout)
        self.assertIn("bind=\n", proc.stdout)

    def test_manager_serves_external_payload_disk_as_hidden_samba_share(self) -> None:
        fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_external_only")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            self.write_fake_acp(tmp_path, fixture.raw)
            payload = volumes / "dk5/.samba4"
            (payload / "private").mkdir(parents=True)
            (payload / "smbd").write_text("#!/bin/sh\nexit 0\n")
            (payload / "smbd").chmod(0o755)
            (payload / "private/smbpasswd").write_text("root:x\n")
            (payload / "private/username.map").write_text("root = *\n")
            marker = shlex.quote(str(volumes / "dk5/.com.apple.timemachine.supported"))
            with (flash / "tcapsulesmb.conf").open("a") as conf:
                conf.write("TC_SMB_BIND_INTERFACES='127.0.0.1/8'\n")
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AirPort
                        SMB_SERVER_STRING=AirPort
                        TC_RUNTIME_IDENTITY_READY=1
                    }}
                    tc_wake_or_mount_volume() {{ return 0; }}
                    is_volume_root_mounted() {{ return 0; }}
                    tc_apply_ata_drive_setting() {{ :; }}
                    tc_probe_smb_bind_interfaces() {{ echo "127.0.0.1/8"; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            mdns-advertiser) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    wait_for_process() {{ return 0; }}
                    tc_wait_for_smbd_ipv4_445() {{ return 0; }}
                    tc_smbd_bound_tcp_445() {{ return 0; }}
                    tc_mdns_bound_udp_5353() {{ return 0; }}
                    tc_manager_reconcile_nbns() {{ return 0; }}
                    tc_manager_wait_for_nbns_ready() {{ return 0; }}
                    stop_runtime_process_by_ucomm() {{ :; }}
                    sleep() {{
                        case "$1" in
                            1|5) return 0 ;;
                            10)
                                printf 'payload=%s|%s|%s\\n' "$TC_PAYLOAD_DIR" "$TC_PAYLOAD_VOLUME" "$TC_PAYLOAD_DEVICE"
                                printf 'shares\\n%s\\n' "$manager_share_rows"
                                printf 'adisk\\n'
                                cat "$TC_ADISK_TSV"
                                printf 'marker=%s\\n' "$([ -f {marker} ] && echo yes || echo no)"
                                printf 'runtime=%s\\n' "$([ -x "$TC_SMBD_BIN" ] && echo yes || echo no)"
                                cat "$TC_SMBD_CONF"
                                exit 0
                                ;;
                        esac
                        return 0
                    }}
                    """
                    )
                )

            proc = subprocess.run(
                ["/bin/sh", str(flash / "manager.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"payload={volumes}/dk5/.samba4|{volumes}/dk5|/dev/dk5\n", proc.stdout)
        self.assertIn(f"shares\nUSB Backup\t{volumes}/dk5\tdk5\t0\taaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n", proc.stdout)
        self.assertIn("adisk\nUSB Backup\tdk5\taaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\t0x82\n", proc.stdout)
        self.assertIn("marker=yes\n", proc.stdout)
        self.assertIn("runtime=yes\n", proc.stdout)
        self.assertIn("[USB Backup]\n", proc.stdout)
        self.assertIn(f"path = {volumes}/dk5\n", proc.stdout)
        self.assertIn("veto files = /.samba4/\n", proc.stdout)
        self.assertIn(f"xattr_tdb:file = {volumes}/dk5/.samba4/private/xattr.tdb\n", proc.stdout)

    def test_common_generate_smb_conf_uses_single_payload_private_db_for_all_shares(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            script = tmp_path / "smb-conf.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_ETC" "$RAM_VAR"
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    share_rows=$(cat <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    USB	{volumes}/dk3	dk3	0	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    )
                    tc_generate_smb_conf_from_share_rows {payload} "$share_rows"
                    cat "$TC_SMBD_CONF"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            smbd_core_dir = payload / "logs/cores/smbd"
            smbd_core_parent = payload / "logs/cores"
            smbd_core_dir_exists = smbd_core_dir.is_dir()
            smbd_core_parent_mode = smbd_core_parent.stat().st_mode & 0o777
            smbd_core_dir_mode = smbd_core_dir.stat().st_mode & 0o777

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("[Data]\n", proc.stdout)
        self.assertIn("[USB]\n", proc.stdout)
        self.assertEqual(proc.stdout.count(f"xattr_tdb:file = {payload}/private/xattr.tdb"), 2)
        self.assertEqual(proc.stdout.count("veto files = /.samba4/"), 2)
        self.assertIn(f"path = {volumes}/dk2/ShareRoot", proc.stdout)
        self.assertIn(f"path = {volumes}/dk3", proc.stdout)
        self.assertIn(f"log file = {payload}/logs/log.smbd", proc.stdout)
        self.assertIn("max log size = 128", proc.stdout)
        self.assertIn("fruit:model = TimeCapsule6,106", proc.stdout)
        self.assertIn("min protocol = SMB2", proc.stdout)
        self.assertIn("max protocol = SMB3", proc.stdout)
        self.assertIn(
            "dos charset = ASCII\n"
            "    min protocol = SMB2\n"
            "    max protocol = SMB3\n"
            "    server multi channel support = no",
            proc.stdout,
        )
        self.assertIn("max open files = 512", proc.stdout)
        self.assertIn("max smbd processes = 16", proc.stdout)
        self.assertNotIn("log level = 5", proc.stdout)
        self.assertTrue(smbd_core_dir_exists)
        self.assertEqual(smbd_core_parent_mode, 0o700)
        self.assertEqual(smbd_core_dir_mode, 0o700)

    def test_common_generate_smb_conf_omits_protocol_bounds_when_any_protocol_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            script = tmp_path / "smb-conf-any-protocol.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    ANY_PROTOCOL=1
                    tc_init_runtime_env
                    mkdir -p "$RAM_ETC" "$RAM_VAR"
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    share_rows=$(cat <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    )
                    tc_generate_smb_conf_from_share_rows {payload} "$share_rows"
                    cat "$TC_SMBD_CONF"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("[Data]\n", proc.stdout)
        self.assertNotIn("min protocol =", proc.stdout)
        self.assertNotIn("max protocol =", proc.stdout)
        self.assertIn("dos charset = ASCII\n    server multi channel support = no", proc.stdout)
        self.assertNotIn("dos charset = ASCII\n\n    server multi channel support = no", proc.stdout)

    def test_common_generate_smb_conf_derives_fruit_model_from_acp_syap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            script = tmp_path / "smb-conf-fruit-model-acp.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    MDNS_DEVICE_MODEL=
                    AIRPORT_SYAP=
                    MDNS_INSTANCE_NAME=AirPort
                    MDNS_HOST_LABEL=airport
                    SMB_NETBIOS_NAME=AirPort
                    SMB_SERVER_STRING=AirPort
                    TC_RUNTIME_IDENTITY_READY=1
                    get_airport_acp_value() {{
                        case "$1" in
                            syAP) echo 119 ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_init_runtime_env
                    mkdir -p "$RAM_ETC" "$RAM_VAR"
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    share_rows=$(cat <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    )
                    tc_generate_smb_conf_from_share_rows {payload} "$share_rows"
                    sed -n 's/^[[:space:]]*fruit:model = //p' "$TC_SMBD_CONF"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "TimeCapsule8,119\n")

    def test_common_generate_smb_conf_falls_back_to_macsamba_fruit_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            script = tmp_path / "smb-conf-fruit-model-fallback.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    MDNS_DEVICE_MODEL=
                    AIRPORT_SYAP=
                    MDNS_INSTANCE_NAME=AirPort
                    MDNS_HOST_LABEL=airport
                    SMB_NETBIOS_NAME=AirPort
                    SMB_SERVER_STRING=AirPort
                    TC_RUNTIME_IDENTITY_READY=1
                    get_airport_acp_value() {{ return 1; }}
                    tc_init_runtime_env
                    mkdir -p "$RAM_ETC" "$RAM_VAR"
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    share_rows=$(cat <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    )
                    tc_generate_smb_conf_from_share_rows {payload} "$share_rows"
                    sed -n 's/^[[:space:]]*fruit:model = //p' "$TC_SMBD_CONF"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "MacSamba\n")

    def test_common_generate_smb_conf_makes_smbd_debug_log_unbounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            script = tmp_path / "smb-conf-debug.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    SMBD_DEBUG_LOGGING=1
                    tc_init_runtime_env
                    mkdir -p "$RAM_ETC" "$RAM_VAR"
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    share_rows=$(cat <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    )
                    tc_generate_smb_conf_from_share_rows {payload} "$share_rows"
                    cat "$TC_SMBD_CONF"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"log file = {payload}/logs/log.smbd", proc.stdout)
        self.assertIn("max log size = 0", proc.stdout)
        self.assertIn("log level = 5 vfs:8 fruit:8", proc.stdout)

    def test_common_smbd_bound_tcp_445_requires_configured_socket_families(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "smbd-bound-families.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    v4_status=1
                    v6_status=1
                    tc_smbd_bound_ipv4_445() {{ return "$v4_status"; }}
                    tc_smbd_bound_ipv6_445() {{ return "$v6_status"; }}

                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 ::1/128 192.168.1.40/24"
                    v4_status=0
                    v6_status=1
                    status=0
                    tc_smbd_bound_tcp_445 || status=$?
                    echo "ipv4_only=$status"

                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 ::1/128 fdbb:1111:2222:3333::40/64"
                    v4_status=1
                    v6_status=0
                    status=0
                    tc_smbd_bound_tcp_445 || status=$?
                    echo "ipv6_only=$status"

                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 ::1/128 192.168.1.40/24 fdbb:1111:2222:3333::40/64"
                    v4_status=0
                    v6_status=1
                    status=0
                    tc_smbd_bound_tcp_445 || status=$?
                    echo "dual_missing_v6=$status"

                    v6_status=0
                    status=0
                    tc_smbd_bound_tcp_445 || status=$?
                    echo "dual_bound=$status"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "ipv4_only=0\nipv6_only=0\ndual_missing_v6=1\ndual_bound=0\n")

    def test_common_fstat_socket_scanner_matches_process_family_and_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            calls = tmp_path / "fstat-calls"
            script = tmp_path / "fstat-socket-scanner.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    : > {calls}
                    tc_runtime_process_table() {{
                        cat <<'EOF'
                    100 Z smbd smbd
                    101 S smbd smbd
                    102 S mdns-advertiser mdns-advertiser
                    103 S other other
                    EOF
                    }}
                    tc_runtime_fstat_pid() {{
                        echo "$1" >> {calls}
                        case "$1" in
                            100) echo "root smbd 100 10 internet stream tcp 0x0 *:445" ;;
                            101)
                                echo "root smbd 101 10 internet stream tcp 0x0 *:445"
                                echo "root smbd 101 11 internet6 stream tcp 0x0 [*]:445"
                                ;;
                            102)
                                echo "root mdns-advertiser 102 10 internet dgram udp 0x0 *:5353"
                                echo "root mdns-advertiser 102 11 internet6 dgram udp 0x0 [*]:5353"
                                ;;
                            *) echo "root other $1 10 internet dgram udp 0x0 *:5353" ;;
                        esac
                    }}

                    status=0
                    tc_smbd_bound_ipv4_445 || status=$?
                    echo "smbd4=$status"
                    status=0
                    tc_smbd_bound_ipv6_445 || status=$?
                    echo "smbd6=$status"
                    status=0
                    tc_process_bound_ipv4_udp_port "$MDNS_PROC_NAME" 5353 || status=$?
                    echo "mdns4=$status"
                    status=0
                    tc_process_bound_ipv6_udp_port "$MDNS_PROC_NAME" 5353 || status=$?
                    echo "mdns6=$status"
                    status=0
                    tc_process_bound_ipv4_udp_port "$MDNS_PROC_NAME" 9999 || status=$?
                    echo "mdns4_wrong_port=$status"
                    echo "calls=$(cat {calls})"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "smbd4=0\n"
            "smbd6=0\n"
            "mdns4=0\n"
            "mdns6=0\n"
            "mdns4_wrong_port=1\n"
            "calls=101\n"
            "101\n"
            "102\n"
            "102\n"
            "102\n",
        )

    def test_common_mdns_bound_udp_5353_requires_all_reported_socket_families(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            families_file = tmp_path / "families"
            families_file.write_text("ipv4 ipv6\n", encoding="utf-8")
            (flash / "mdns-advertiser").write_text(
                f"#!/bin/sh\n[ \"$1\" = \"--print-mdns-socket-families\" ] || exit 2\ncat {families_file}\n",
                encoding="utf-8",
            )
            (flash / "mdns-advertiser").chmod(0o755)
            script = tmp_path / "mdns-bound-families.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    v4_status=1
                    v6_status=1
                    tc_process_bound_ipv4_udp_port() {{ return "$v4_status"; }}
                    tc_process_bound_ipv6_udp_port() {{ return "$v6_status"; }}

                    v4_status=0
                    v6_status=1
                    status=0
                    tc_mdns_bound_udp_5353 || status=$?
                    echo "dual_prefers_ipv4=$status"

                    v4_status=1
                    v6_status=0
                    status=0
                    tc_mdns_bound_udp_5353 || status=$?
                    echo "dual_missing_ipv4=$status"

                    printf 'ipv4\\n' >{families_file}
                    v4_status=0
                    v6_status=1
                    status=0
                    tc_mdns_bound_udp_5353 || status=$?
                    echo "ipv4_only=$status"

                    printf 'ipv6\\n' >{families_file}
                    v4_status=1
                    v6_status=0
                    status=0
                    tc_mdns_bound_udp_5353 || status=$?
                    echo "ipv6_only=$status"

                    v4_status=0
                    v6_status=1
                    status=0
                    tc_mdns_bound_udp_5353 || status=$?
                    echo "ipv6_only_missing=$status"

                    printf 'ethernet\\n' >{families_file}
                    v4_status=0
                    v6_status=0
                    status=0
                    tc_mdns_bound_udp_5353 || status=$?
                    echo "unsupported_family=$status"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "dual_prefers_ipv4=1\n"
            "dual_missing_ipv4=1\n"
            "ipv4_only=0\n"
            "ipv6_only=0\n"
            "ipv6_only_missing=1\n"
            "unsupported_family=1\n",
        )

    def test_common_manager_restarts_nbns_when_running_without_udp_137_and_auto_ip_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "manager-nbns-restart-unbound.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    NBNS_ENABLED=1
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR"
                    nbns_present=1
                    runtime_process_present_by_ucomm() {{
                        [ "$1" = "$NBNS_PROC_NAME" ] && [ "$nbns_present" = "1" ]
                    }}
                    tc_nbns_bound_ipv4_udp_137() {{ return 1; }}
                    tc_nbns_auto_ip_available() {{ echo auto-ip; return 0; }}
                    stop_runtime_process_by_ucomm() {{ echo "stop $1"; nbns_present=0; }}
                    tc_manager_refresh_runtime_identity_for_recovery() {{ echo identity; }}
                    tc_restart_nbns() {{ echo restart; }}
                    tc_manager_reconcile_nbns
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "auto-ip\nstop nbns-advertiser\nidentity\nrestart\n")
        self.assertIn("manager NBNS recovery: nbns responder is running without required UDP 137 sockets", log_text)

    def test_common_manager_defers_nbns_when_running_without_udp_137_and_no_auto_ip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "manager-nbns-defer-unbound.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    NBNS_ENABLED=1
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR"
                    runtime_process_present_by_ucomm() {{
                        [ "$1" = "$NBNS_PROC_NAME" ]
                    }}
                    tc_nbns_bound_ipv4_udp_137() {{ return 1; }}
                    tc_nbns_auto_ip_available() {{ echo auto-ip; return 11; }}
                    stop_runtime_process_by_ucomm() {{ echo "unexpected-stop $1"; return 1; }}
                    tc_restart_nbns() {{ echo unexpected-restart; return 1; }}
                    tc_manager_reconcile_nbns
                    echo "deferred=$TC_MANAGER_NBNS_DEFERRED_NO_IP"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "auto-ip\ndeferred=1\n")
        self.assertIn("manager NBNS recovery: nbns responder is running without required UDP 137 sockets", log_text)
        self.assertIn("NBNS startup deferred; no usable address has appeared yet", log_text)
        self.assertNotIn("unexpected", proc.stdout)

    def test_common_manager_reports_nbns_hard_auto_ip_failure_when_unbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "manager-nbns-unbound-hard-fail.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    NBNS_ENABLED=1
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR"
                    runtime_process_present_by_ucomm() {{
                        [ "$1" = "$NBNS_PROC_NAME" ]
                    }}
                    tc_nbns_bound_ipv4_udp_137() {{ return 1; }}
                    tc_nbns_auto_ip_available() {{ echo auto-ip; return 13; }}
                    stop_runtime_process_by_ucomm() {{ echo "unexpected-stop $1"; return 1; }}
                    tc_restart_nbns() {{ echo unexpected-restart; return 1; }}
                    status=0
                    tc_manager_reconcile_nbns || status=$?
                    echo "status=$status"
                    echo "deferred=$TC_MANAGER_NBNS_DEFERRED_NO_IP"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "auto-ip\nstatus=1\ndeferred=0\n")
        self.assertIn("manager NBNS recovery: nbns responder is running without required UDP 137 sockets", log_text)
        self.assertIn("manager NBNS recovery: auto-ip check failed with exit code 13", log_text)
        self.assertNotIn("unexpected", proc.stdout)

    def test_common_mdns_capture_has_no_async_wait_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "mdns-capture-no-async-state.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR"
                    if command -v tc_wait_for_mdns_capture >/dev/null 2>&1; then
                        echo wait-function-present
                    else
                        echo wait-function-missing
                    fi
                    if command -v tc_finish_mdns_capture_wait >/dev/null 2>&1; then
                        echo finish-function-present
                    else
                        echo finish-function-missing
                    fi
                    case "${{TC_MDNS_CAPTURE_PID+set}}" in
                        set) echo pid-var-set ;;
                        *) echo pid-var-unset ;;
                    esac
                    case "${{MDNS_CAPTURE_WAIT_SECONDS+set}}" in
                        set) echo timeout-var-set ;;
                        *) echo timeout-var-unset ;;
                    esac
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("wait-function-missing\n", proc.stdout)
        self.assertIn("finish-function-missing\n", proc.stdout)
        self.assertIn("pid-var-unset\n", proc.stdout)
        self.assertIn("timeout-var-unset\n", proc.stdout)

    def test_common_mdns_capture_runs_foreground_without_status_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            marker = tmp_path / "capture.started"
            (flash / "mdns-advertiser").write_text(
                "#!/bin/sh\n"
                "printf 'capture-args:%s\\n' \"$*\"\n"
                f"echo started >{shlex.quote(str(marker))}\n"
                "exit 7\n"
            )
            (flash / "mdns-advertiser").chmod(0o755)
            script = tmp_path / "mdns-capture-foreground.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR"
                    echo stale >"$APPLE_MDNS_SNAPSHOT"
                    echo stale >"$ALL_MDNS_SNAPSHOT"
                    get_radio_mac() {{
                        case "$1" in
                            bwl0) echo 80:EA:96:EB:2E:7D ;;
                            bwl1) echo 80:EA:96:EB:2E:7C ;;
                            *) return 1 ;;
                        esac
                    }}
                    get_airport_host_label() {{ echo jamess-airport-time-capsule; }}
                    get_airport_acp_value() {{
                        case "$1" in
                            syNm) echo "James's AirPort Time Capsule" ;;
                            syFl) echo 0x00000A0C ;;
                            raNA) echo false ;;
                            syVs) echo 7.9.1 ;;
                            srcv) echo 79100.2 ;;
                            bjSd) echo 0x10 ;;
                            *) return 1 ;;
                        esac
                    }}
                    get_airport_rast() {{ echo 3; }}
                    tc_capture_mdns_snapshot_for_manager || true
                    [ -f {shlex.quote(str(marker))} ] || exit 99
                    cat "$TC_MDNS_LOG_FILE"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("launching mdns-advertiser capture", proc.stdout)
        self.assertIn("--save-all-snapshot", proc.stdout)
        self.assertIn("--save-snapshot", proc.stdout)
        self.assertIn("--auto-ip", proc.stdout)
        self.assertIn("mDNS snapshot capture exited with failure; final advertiser will use generated records if needed", proc.stdout)

    def test_common_mdns_advertiser_uses_capture_snapshot_without_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            (flash / "mdns-advertiser").write_text(
                "#!/bin/sh\n"
                "printf 'mdns-args:%s\\n' \"$*\"\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  case \"$1\" in\n"
                "    --save-all-snapshot) shift; echo all >\"$1\" ;;\n"
                "    --save-snapshot) shift; echo captured >\"$1\" ;;\n"
                "    --save-airport-snapshot) shift; echo generated >\"$1\" ;;\n"
                "    --load-snapshot) shift; echo load=\"$1\" ;;\n"
                "  esac\n"
                "  shift\n"
                "done\n"
            )
            (flash / "mdns-advertiser").chmod(0o755)
            script = tmp_path / "mdns-capture-used.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR"
                    get_radio_mac() {{ return 1; }}
                    get_airport_host_label() {{ echo jamess-airport-time-capsule; }}
                    get_airport_acp_value() {{
                        case "$1" in
                            syNm) echo "James's AirPort Time Capsule" ;;
                            syVs) echo 7.9.1 ;;
                            srcv) echo 79100.2 ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_capture_mdns_snapshot_for_manager
                    tc_launch_mdns_advertiser "mdns test" 1 0
                    wait "$mdns_launch_pid" || true
                    cat "$APPLE_MDNS_SNAPSHOT"
                    cat "$TC_MDNS_LOG_FILE"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("captured\n", proc.stdout)
        self.assertIn("launching mdns-advertiser capture", proc.stdout)
        self.assertIn("--save-all-snapshot", proc.stdout)
        self.assertIn("--save-snapshot", proc.stdout)
        self.assertIn("--auto-ip", proc.stdout)
        self.assertNotIn("--save-airport-snapshot", proc.stdout)
        self.assertIn("--load-snapshot", proc.stdout)

    def test_common_mdns_advertiser_generates_airport_snapshot_when_capture_has_no_trusted_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            (flash / "mdns-advertiser").write_text(
                "#!/bin/sh\n"
                "printf 'mdns-args:%s\\n' \"$*\"\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  case \"$1\" in\n"
                "    --save-all-snapshot) shift; echo all >\"$1\" ;;\n"
                "    --save-snapshot) shift ;;\n"
                "    --save-airport-snapshot) shift; echo generated >\"$1\" ;;\n"
                "    --load-snapshot) shift; echo load=\"$1\" ;;\n"
                "  esac\n"
                "  shift\n"
                "done\n"
            )
            (flash / "mdns-advertiser").chmod(0o755)
            script = tmp_path / "mdns-capture-fallback.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR"
                    get_radio_mac() {{ return 1; }}
                    get_airport_host_label() {{ echo jamess-airport-time-capsule; }}
                    get_airport_acp_value() {{
                        case "$1" in
                            syNm) echo "James's AirPort Time Capsule" ;;
                            syVs) echo 7.9.1 ;;
                            srcv) echo 79100.2 ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_capture_mdns_snapshot_for_manager || true
                    if [ ! -s "$APPLE_MDNS_SNAPSHOT" ]; then
                        tc_log "manager mDNS snapshot: no fresh snapshot exists; generating AirPort fallback"
                        tc_generate_mdns
                    fi
                    tc_launch_mdns_advertiser "mdns test" 1 0
                    wait "$mdns_launch_pid" || true
                    cat "$APPLE_MDNS_SNAPSHOT"
                    cat "$TC_MDNS_LOG_FILE"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("generated\n", proc.stdout)
        self.assertIn("launching mdns-advertiser capture", proc.stdout)
        self.assertIn("--save-all-snapshot", proc.stdout)
        self.assertIn("--auto-ip", proc.stdout)
        self.assertIn("manager mDNS snapshot: no fresh snapshot exists; generating AirPort fallback", proc.stdout)
        self.assertIn("launching mdns-advertiser airport snapshot", proc.stdout)
        self.assertIn("--save-airport-snapshot", proc.stdout)
        self.assertIn("--load-snapshot", proc.stdout)

    def test_common_mdns_diskless_start_omits_stale_adisk_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            args_file = tmp_path / "mdns-args.txt"
            (flash / "mdns-advertiser").write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >{shlex.quote(str(args_file))}\n"
                "exit 0\n"
            )
            (flash / "mdns-advertiser").chmod(0o755)
            script = tmp_path / "mdns-diskless-no-adisk.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR"
                    echo snapshot >"$APPLE_MDNS_SNAPSHOT"
                    cat >"$TC_ADISK_TSV" <<'EOF'
                    Stale	dk2	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa	0x82
                    EOF
                    tc_ensure_runtime_identity() {{
                        MDNS_INSTANCE_NAME=Diskless
                        MDNS_HOST_LABEL=diskless
                        MDNS_DEVICE_MODEL=TimeCapsule
                        TC_RUNTIME_IDENTITY_READY=1
                    }}
                    tc_prepare_mdns_identity() {{
                        TC_AIRPORT_FIELDS_ADVERTISE_MAC=80:EA:96:E6:58:68
                        AIRPORT_WAMA=
                        AIRPORT_RAMA=
                        AIRPORT_RAM2=
                        AIRPORT_RAST=
                        AIRPORT_RANA=
                        AIRPORT_SYFL=
                        AIRPORT_SYAP=
                        AIRPORT_SYVS=
                        AIRPORT_SRCV=
                        AIRPORT_BJSD=
                        return 0
                    }}
                    stop_runtime_process_by_ucomm() {{ echo "stop $1"; }}
                    tc_launch_mdns_advertiser "mdns startup" 1 0 1
                    wait "$mdns_launch_pid" || true
                    cat {shlex.quote(str(args_file))}
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--diskless", proc.stdout)
        self.assertIn("--auto-ip", proc.stdout)
        self.assertNotIn("--adisk-shares-file", proc.stdout)
        self.assertIn("mdns startup: starting mdns advertiser in diskless auto-ip mode", proc.stdout)

    def test_common_mdns_and_nbns_write_payload_logs_in_normal_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            payload.mkdir(parents=True)
            (flash / "mdns-advertiser").write_text("#!/bin/sh\nprintf 'mdns-args:%s\\n' \"$*\"\necho mdns-stdout\necho mdns-stderr >&2\n")
            (flash / "mdns-advertiser").chmod(0o755)
            nbns_bin = memory / "samba4/sbin/nbns-advertiser"
            nbns_bin.parent.mkdir(parents=True)
            nbns_bin.write_text("#!/bin/sh\nprintf 'nbns-args:%s\\n' \"$*\"\necho nbns-stdout\necho nbns-stderr >&2\n")
            nbns_bin.chmod(0o755)
            script = tmp_path / "process-logs.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    NBNS_ENABLED=1
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    is_volume_root_mounted() {{ [ "$1" = "{volumes}/dk2" ]; }}
                    get_radio_mac() {{
                        case "$1" in
                            bwl0) echo 80:EA:96:EB:2E:7D ;;
                            bwl1) echo 80:EA:96:EB:2E:7C ;;
                            *) return 1 ;;
                        esac
                    }}
                    get_airport_host_label() {{ echo jamess-airport-time-capsule; }}
                    get_airport_acp_value() {{
                        case "$1" in
                            syNm) echo "James's AirPort Time Capsule" ;;
                            syFl) echo 0x00000A0C ;;
                            raNA) echo false ;;
                            syVs) echo 7.9.1 ;;
                            srcv) echo 79100.2 ;;
                            bjSd) echo 0x10 ;;
                            *) return 1 ;;
                        esac
                    }}
                    get_airport_rast() {{ echo 3; }}
                    stop_nbns_conflicts() {{ return 0; }}
                    tc_set_payload_log_dir {payload} {volumes}/dk2
                    printf 'mdns-path=%s\\n' "$TC_MDNS_LOG_FILE"
                    printf 'nbns-path=%s\\n' "$TC_NBNS_LOG_FILE"
                    tc_generate_mdns
                    tc_launch_nbns "nbns test" 0
                    wait "$!" || true
                    printf 'mdns\\n'
                    cat "$TC_MDNS_LOG_FILE"
                    printf 'nbns\\n'
                    cat "$TC_NBNS_LOG_FILE"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("mdns-path=", proc.stdout)
        self.assertIn("/.samba4/logs/mdns.log", proc.stdout)
        self.assertIn("/.samba4/logs/nbns.log", proc.stdout)
        self.assertIn("mdns\n", proc.stdout)
        self.assertIn("launching mdns-advertiser airport snapshot", proc.stdout)
        self.assertIn("--save-airport-snapshot", proc.stdout)
        self.assertIn("James's AirPort Time Capsule", proc.stdout)
        self.assertIn("--airport-syfl 0xA0C", proc.stdout)
        self.assertNotIn("--save-all-snapshot", proc.stdout)
        self.assertIn("mdns-stdout", proc.stdout)
        self.assertIn("mdns-stderr", proc.stdout)
        self.assertIn("nbns\n", proc.stdout)
        self.assertIn("launching nbns-advertiser", proc.stdout)
        self.assertIn("--auto-ip", proc.stdout)
        self.assertIn("nbns-stdout", proc.stdout)
        self.assertIn("nbns-stderr", proc.stdout)

    def test_common_mdns_generation_failure_does_not_run_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            (flash / "mdns-advertiser").write_text(
                "#!/bin/sh\n"
                "printf 'mdns-args:%s\\n' \"$*\"\n"
                "if [ \"$1\" = \"--save-airport-snapshot\" ]; then\n"
                "  echo airport-fail >&2\n"
                "  exit 2\n"
                "fi\n"
                "echo unexpected-capture\n"
                "exit 9\n"
            )
            (flash / "mdns-advertiser").chmod(0o755)
            script = tmp_path / "mdns-generation-failure.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    get_radio_mac() {{ return 1; }}
                    get_airport_host_label() {{ echo jamess-airport-time-capsule; }}
                    get_airport_acp_value() {{
                        case "$1" in
                            syNm) echo "James's AirPort Time Capsule" ;;
                            syVs) echo 7.9.1 ;;
                            srcv) echo 79100.2 ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_set_log "$RAM_VAR/test.log" test
                    tc_generate_mdns
                    cat "$TC_MDNS_LOG_FILE"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("launching mdns-advertiser airport snapshot", proc.stdout)
        self.assertIn("airport-fail", proc.stdout)
        self.assertIn("mDNS AirPort snapshot generation failed; final advertiser will use generated records if needed", proc.stdout)
        self.assertNotIn("launching mdns-advertiser capture", proc.stdout)
        self.assertNotIn("--save-all-snapshot", proc.stdout)
        self.assertNotIn("unexpected-capture", proc.stdout)

    def test_common_wake_or_mount_uses_diskd_without_mount_hfs_fallback_when_it_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            acp = tmp_path / "acp"
            acp.write_text("#!/bin/sh\necho \"$@\" >>'%s/acp.log'\n" % tmp_path)
            acp.chmod(0o755)
            script = tmp_path / "wake.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    DISKD_USE_VOLUME_ATTEMPTS=2
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR" {volumes}/dk2
                    is_volume_root_mounted() {{
                        count=$(cat {tmp_path}/count 2>/dev/null || echo 0)
                        count=$((count + 1))
                        echo "$count" >{tmp_path}/count
                        [ "$count" -ge 4 ]
                    }}
                    sleep() {{ echo "sleep $1"; }}
                    tc_wake_or_mount_volume /dev/dk2 {volumes}/dk2
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            acp_log = (tmp_path / "acp.log").read_text()
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "sleep 3\nsleep 3\n")
        self.assertIn(f"rpc diskd.useVolume path:s:{volumes}/dk2", acp_log)
        self.assertIn(
            f"MaSt volume {volumes}/dk2: mounted at {volumes}/dk2 after diskd.useVolume attempt 1/2",
            log_text,
        )
        self.assertNotIn("mount_hfs", log_text)
        self.assertNotIn("Apple mount", log_text)

    def test_common_wake_or_mount_counts_diskd_rpc_time_when_reporting_mount_elapsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            clock = tmp_path / "clock"
            mounted = tmp_path / "mounted"
            clock.write_text("100")
            acp = tmp_path / "acp"
            acp.write_text(
                "#!/bin/sh\n"
                f"echo 108 >{shlex.quote(str(clock))}\n"
                f": >{shlex.quote(str(mounted))}\n"
            )
            acp.chmod(0o755)
            script = tmp_path / "wake-count-diskd-time.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    DISKD_USE_VOLUME_ATTEMPTS=1
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR" {volumes}/dk2
                    tc_now_seconds() {{ cat {shlex.quote(str(clock))}; }}
                    is_volume_root_mounted() {{ [ -f {shlex.quote(str(mounted))} ]; }}
                    sleep() {{ echo "unexpected sleep $1"; exit 99; }}
                    tc_wake_or_mount_volume /dev/dk2 {volumes}/dk2
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn(
            f"MaSt volume {volumes}/dk2: waiting up to 31s total for diskd.useVolume to mount {volumes}/dk2",
            log_text,
        )
        self.assertIn(f"MaSt volume {volumes}/dk2: {volumes}/dk2 is mounted after 8s", log_text)

    def test_common_wake_or_mount_claims_diskd_user_even_when_already_mounted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            acp = tmp_path / "acp"
            acp.write_text("#!/bin/sh\necho \"$@\" >>'%s/acp.log'\n" % tmp_path)
            acp.chmod(0o755)
            script = tmp_path / "wake-already-mounted.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    DISKD_USE_VOLUME_ATTEMPTS=2
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR" {volumes}/dk2
                    is_volume_root_mounted() {{ return 0; }}
                    sleep() {{ echo "sleep $1"; }}
                    tc_wake_or_mount_volume /dev/dk2 {volumes}/dk2
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            acp_log = (tmp_path / "acp.log").read_text()
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn(f"rpc diskd.useVolume path:s:{volumes}/dk2", acp_log)
        self.assertIn(
            f"MaSt volume {volumes}/dk2: volume already mounted at {volumes}/dk2 before diskd.useVolume; claiming a diskd user anyway",
            log_text,
        )
        self.assertIn(
            f"MaSt volume {volumes}/dk2: diskd.useVolume claim complete; {volumes}/dk2 remained mounted after attempt 1/2",
            log_text,
        )

    def test_common_wake_or_mount_logs_diskd_failure_without_mount_hfs_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            acp = tmp_path / "acp"
            acp.write_text("#!/bin/sh\necho \"$@\" >>'%s/acp.log'\n" % tmp_path)
            acp.chmod(0o755)
            script = tmp_path / "wake-timeout.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    DISKD_USE_VOLUME_ATTEMPTS=2
                    DISKD_USE_VOLUME_MOUNT_TIMEOUT_SECONDS=7
                    DISKD_USE_VOLUME_MOUNT_POLL_SECONDS=3
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR" {volumes}/dk2
                    is_volume_root_mounted() {{ return 1; }}
                    sleep() {{ echo "sleep $1"; }}
                    tc_wake_or_mount_volume /dev/dk2 {volumes}/dk2 || true
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "sleep 3\nsleep 3\nsleep 1\nsleep 1\nsleep 3\nsleep 3\nsleep 1\n")
        self.assertIn(
            f"MaSt volume {volumes}/dk2: diskd.useVolume did not mount {volumes}/dk2 after 2 attempt(s); leaving volume unavailable without mount_hfs fallback",
            log_text,
        )
        self.assertNotIn("launching mount_hfs", log_text)
        self.assertNotIn("Apple mount", log_text)

    def test_common_diskd_mount_wait_sanitizes_invalid_config_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "wake-invalid-wait-config.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    DISKD_USE_VOLUME_MOUNT_TIMEOUT_SECONDS=bogus
                    DISKD_USE_VOLUME_MOUNT_POLL_SECONDS=0
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    is_volume_root_mounted() {{ return 1; }}
                    sleep() {{ :; }}
                    tc_wait_for_diskd_volume_mount {volumes}/dk2 "test mount" || true
                    tc_wait_for_diskd_volume_mount {volumes}/dk2 "test mount" || true
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            log_text.count("runtime config: invalid DISKD_USE_VOLUME_MOUNT_TIMEOUT_SECONDS=bogus; using 31s"),
            1,
        )
        self.assertEqual(
            log_text.count("runtime config: invalid DISKD_USE_VOLUME_MOUNT_POLL_SECONDS=0; using 3s"),
            1,
        )
        self.assertEqual(log_text.count(f"test mount: waiting up to 31s for diskd.useVolume to mount {volumes}/dk2"), 2)

    def test_common_diskd_mount_wait_zero_timeout_checks_once_without_sleeping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            count_file = tmp_path / "mount-check-count"
            script = tmp_path / "wake-zero-timeout.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    DISKD_USE_VOLUME_MOUNT_TIMEOUT_SECONDS=0
                    DISKD_USE_VOLUME_MOUNT_POLL_SECONDS=3
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR"
                    is_volume_root_mounted() {{
                        count=$(/bin/cat {count_file} 2>/dev/null || echo 0)
                        count=$((count + 1))
                        echo "$count" >{count_file}
                        return 1
                    }}
                    sleep() {{ echo "unexpected sleep $1"; exit 99; }}
                    status=0
                    tc_wait_for_diskd_volume_mount {volumes}/dk2 "test mount" || status=$?
                    printf 'status=%s checks=%s\\n' "$status" "$(/bin/cat {count_file})"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "status=1 checks=1\n")
        self.assertIn(f"test mount: timed out after 0s waiting for {volumes}/dk2 to mount", log_text)

    def test_common_nbns_enabled_comes_from_flash_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "nbns-enabled.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_nbns_enabled && echo enabled || echo disabled
                    NBNS_ENABLED=1
                    tc_nbns_enabled && echo enabled || echo disabled
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "disabled\nenabled\n")


if __name__ == "__main__":
    unittest.main()
