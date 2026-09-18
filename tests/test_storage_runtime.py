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
from timecapsulesmb.services.deploy import render_flash_runtime_config, render_rsync_daemon_config
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
    StorageDeviceError,
    MaStProbeDiagnostics,
    MaStReadResult,
    MaStVolume,
    PayloadHome,
    ensure_volume_root_mounted_conn,
    mast_probe_debug_summary,
    mast_volumes_debug_summary,
    ordered_payload_candidate_volumes,
    payload_candidate_checks_debug_summary,
    parse_mast_inventory,
    parse_mast_plist,
    probe_mast_diagnostics_conn,
    render_ensure_volume_root_mounted_script,
    select_payload_home_with_diagnostics_conn,
    verify_payload_home_conn,
    volume_root_is_writable_conn,
    wait_for_mast_volumes_conn,
)
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection
from tests.storage_fixtures import EXTERNAL_BACKUP, INTERNAL_DATA, MAST_FIXTURES, SHELL_MAST_FIXTURES, MaStFixture


class StorageRuntimeTests(unittest.TestCase):
    _runtime_asset_texts: str | None = None

    @classmethod
    def runtime_asset_texts(cls) -> str:
        if cls._runtime_asset_texts is None:
            cls._runtime_asset_texts = load_boot_asset_text("common.sh")
        return cls._runtime_asset_texts

    def extract_shell_function(self, source: str, name: str) -> str:
        start = source.index(f"{name}()")
        brace_start = source.index("{", start)
        depth = 0
        for offset, char in enumerate(source[brace_start:], start=brace_start):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return source[start : offset + 1]
        self.fail(f"function {name} did not terminate")

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
        pkill = tmp_path / "pkill"
        pkill.write_text("#!/bin/sh\nexit 0\n")
        pkill.chmod(0o755)
        replacements = {
            "/mnt/Flash": str(flash),
            "/mnt/Memory": str(memory),
            "/mnt/Locks": str(locks),
            "/Volumes": str(volumes),
            "/usr/bin/acp": str(tmp_path / "acp"),
            "/usr/bin/pkill": str(pkill),
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
        common += "\nget_airport_prni_raw() { printf '%s\\n' '{' '    printers=[]' '}'; }\n"
        # The host has no Apple diskd: report ours as already on loopback so
        # the manager's per-pass relaunch retry stays quiet. The boot/diskd
        # tests put the real probe back on top of a fake ps.
        common += "tc_apple_diskd_probe() { TC_APPLE_DISKD_STATE=loopback; TC_APPLE_DISKD_STRAY_PIDS=; }\n"

        # Storage tests exercise the shell consumer of a successful native
        # capture; the real collector is covered in test_acp_capture/bounded_acp.
        common += "tc_read_mast() { " + shlex.quote(str(tmp_path / "acp")) + " -A MaSt; }\n"
        service = memory / "samba4/sbin/service"
        service.parent.mkdir(parents=True, exist_ok=True)
        service.write_text("#!/bin/sh\ncase \"$1\" in\n"
                           f"--print-samba-identity) cat {shlex.quote(str(flash / 'native-identity'))};;\n"
                           "--print-device-nt-hash) echo 0123456789ABCDEF0123456789ABCDEF;;\nesac\n")
        service.chmod(0o755)
        (flash / "native-identity").write_text("samba-identity 1\nTimeCapsule\nTimeCapsule\nTimeCapsule6,106\n")
        discovery = flash / "discoveryd"
        discovery.write_text("#!/bin/sh\nexit 0\n")
        discovery.chmod(0o755)
        common += "TC_MANAGER_LAST_DISCOVERY_SIGNATURE=$(printf '%s\\n%s\\n%s\\n%s\\n%s\\n' '' 0 '' 0x82 0)\n"
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
                TC_CONFIG_VERSION=3
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

    def write_fake_acp(self, tmp_path: Path, raw: str | bytes, *, final_newline: bool = True) -> Path:
        acp = tmp_path / "acp"
        raw_text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        if final_newline:
            acp.write_text(
                "#!/bin/sh\n"
                "if [ \"$1:$2\" = '-q:syPW' ]; then echo device-pass; exit 0; fi\n"
                "cat <<'OUT'\n" + raw_text + "\nOUT\n"
            )
        else:
            acp.write_text(
                "#!/bin/sh\n"
                "if [ \"$1:$2\" = '-q:syPW' ]; then echo device-pass; exit 0; fi\n"
                "printf %s " + shlex.quote(raw_text) + "\n"
            )
        acp.chmod(0o755)
        return acp

    def write_fake_service_hash_helper(
        self,
        flash: Path,
        *,
        nt_hash: str = "0123456789ABCDEF0123456789ABCDEF",
    ) -> Path:
        service = flash.parent / "Memory/samba4/sbin/service"
        service.parent.mkdir(parents=True, exist_ok=True)
        service.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = '--print-device-nt-hash' ]; then\n"
            "    cat >/dev/null\n"
            f"    echo {shlex.quote(nt_hash)}\n"
            "    exit 0\n"
            "fi\n"
            "if [ \"$1\" = '--print-samba-identity' ]; then printf '%s\\n' 'samba-identity 1' TimeCapsule TimeCapsule 'TimeCapsule6,106'; fi\n"
            "exit 0\n"
        )
        service.chmod(0o755)
        return service

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
            "if [ \"$1:$2\" = '-q:syPW' ]; then echo device-pass; exit 0; fi\n"
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

    def test_wait_for_mast_volumes_stops_when_disk_has_no_hfs_partitions(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        raw_output = """
MaSt = (
    {
        deviceName = "wd0";
        name = "Seagate Expansion HDD";
        builtin = true;
        partitions = (
            {
                deviceName = "dk2";
                name = "PS3FAT";
                format = "msdos";
            }
        );
    }
);
"""

        with mock.patch(
            "timecapsulesmb.device.storage.read_mast_volumes_with_output_conn",
            return_value=MaStReadResult((), raw_output),
        ) as read_mock:
            with mock.patch("timecapsulesmb.device.storage.time.sleep") as sleep_mock:
                result = wait_for_mast_volumes_conn(connection, attempts=10, delay_seconds=3)

        self.assertEqual(result.volumes, ())
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.raw_output, raw_output)
        read_mock.assert_called_once_with(connection)
        sleep_mock.assert_not_called()

    def test_wait_for_mast_volumes_stops_when_disk_has_empty_partition_table(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        raw_output = """
MaSt = (
    {
        deviceName = "wd0";
        model = "Seagate Expansion HDD";
        size = 8000000000000;
        builtin = true;
        partitions = (
        );
    }
);
"""

        with mock.patch(
            "timecapsulesmb.device.storage.read_mast_volumes_with_output_conn",
            return_value=MaStReadResult((), raw_output),
        ) as read_mock:
            with mock.patch("timecapsulesmb.device.storage.time.sleep") as sleep_mock:
                result = wait_for_mast_volumes_conn(connection, attempts=10, delay_seconds=3)

        inventory = parse_mast_inventory(raw_output)
        self.assertEqual(result.volumes, ())
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.raw_output, raw_output)
        self.assertEqual(inventory[0].name, "Seagate Expansion HDD")
        self.assertEqual(inventory[0].size, "8000000000000")
        self.assertEqual(inventory[0].partitions, ())
        read_mock.assert_called_once_with(connection)
        sleep_mock.assert_not_called()

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

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=True) as mount_mock:
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", side_effect=[True]) as writable_mock:
                selection = select_payload_home_with_diagnostics_conn(connection, (external, internal), ".samba4", wait_seconds=30)

        self.assertEqual(selection.payload_home, PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"))
        mount_mock.assert_called_once_with(connection, "/Volumes/dk2", "/dev/dk2", wait_seconds=30)
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
        self.assertIn("[ -d /Volumes/dk2/.samba4/private ]", remote_command)

    def test_verify_payload_home_conn_reports_mount_and_payload_failures(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        payload_home = PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4")
        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=False):
            result = verify_payload_home_conn(connection, payload_home, wait_seconds=5)
        self.assertEqual(result, PayloadVerificationResult(False, "volume /Volumes/dk2 is not mounted"))

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=True):
            with mock.patch(
                "timecapsulesmb.device.storage.run_ssh",
                return_value=mock.Mock(returncode=1, stdout="missing smbd; missing private directory\n"),
            ):
                result = verify_payload_home_conn(connection, payload_home, wait_seconds=5)
        self.assertEqual(result, PayloadVerificationResult(False, "missing smbd; missing private directory"))

    def test_select_payload_home_skips_unmountable_internal_before_external(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", side_effect=[False, True]) as mount_mock:
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", return_value=True) as writable_mock:
                selection = select_payload_home_with_diagnostics_conn(connection, (external, internal), ".samba4", wait_seconds=9)

        self.assertEqual(selection.payload_home, PayloadHome("/Volumes/dk3", "/dev/dk3", ".samba4"))
        self.assertEqual(
            mount_mock.call_args_list,
            [
                mock.call(connection, "/Volumes/dk2", "/dev/dk2", wait_seconds=9),
                mock.call(connection, "/Volumes/dk3", "/dev/dk3", wait_seconds=9),
            ],
        )
        writable_mock.assert_called_once_with(connection, "/Volumes/dk3")

    def test_select_payload_home_with_diagnostics_records_mount_and_write_results(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", side_effect=[False, True]):
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

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=True):
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

    def test_volume_root_writable_timeout_raises_coded_disk_write_error(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        with mock.patch(
            "timecapsulesmb.device.storage.run_ssh",
            side_effect=SshCommandTimeout("Timed out waiting for ssh command to finish: mkdir write test"),
        ):
            with self.assertRaises(StorageDeviceError) as raised:
                volume_root_is_writable_conn(connection, "/Volumes/dk2")

        self.assertEqual(raised.exception.code, "disk_write_test_unresponsive")
        self.assertIn("The disk did not respond when tested.", str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, SshCommandTimeout)

    def test_select_payload_home_records_unmountable_candidates(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=False):
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn") as writable_mock:
                selection = select_payload_home_with_diagnostics_conn(connection, (internal, external), ".samba4", wait_seconds=30)

        self.assertIsNone(selection.payload_home)
        self.assertEqual([check.mounted for check in selection.checks], [False, False])
        writable_mock.assert_not_called()

    def test_select_payload_home_falls_back_to_external_and_records_none_writable(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=True):
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", side_effect=[False, True]):
                selection = select_payload_home_with_diagnostics_conn(connection, (internal, external), ".samba4", wait_seconds=30)
        self.assertEqual(selection.payload_home, PayloadHome("/Volumes/dk3", "/dev/dk3", ".samba4"))

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=True):
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
                "TC_SMB_BROWSE_COMPATIBILITY": "true",
                "TC_ANY_PROTOCOL": "true",
                "TC_FRUIT_METADATA_NETATALK": "true",
                "TC_VFS_AIO_FORK_ENABLED": "true",
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
        self.assertIn("TELEMETRY=true\n", rendered)
        self.assertIn("INTERNAL_SHARE_USE_DISK_ROOT=1\n", rendered)
        self.assertNotIn("SMB_BIND_LAN_ONLY", rendered)
        self.assertIn("SMB_BROWSE_COMPATIBILITY=1\n", rendered)
        self.assertIn("MDNS_ADVERTISE_AFP=0\n", rendered)
        self.assertIn("ANY_PROTOCOL=1\n", rendered)
        self.assertIn("REQUIRE_SMB_ENCRYPTION=0\n", rendered)
        self.assertIn("FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION=0\n", rendered)
        self.assertIn("FRUIT_METADATA_NETATALK=1\n", rendered)
        self.assertIn("VFS_AIO_FORK_ENABLED=1\n", rendered)
        self.assertIn("DISKD_USE_VOLUME_ATTEMPTS=2\n", rendered)
        self.assertIn("ATA_IDLE_SECONDS=300\n", rendered)
        self.assertIn("ATA_STANDBY=''\n", rendered)
        self.assertIn("NBNS_ENABLED=1\n", rendered)
        self.assertIn("RSYNC_ENABLED=0\n", rendered)
        self.assertIn("SMBD_DEBUG_LOGGING=1\n", rendered)
        self.assertNotIn("SMB_NETBIOS_NAME", rendered)
        self.assertIn("TC_CONFIG_VERSION=3\n", rendered)
        # Name overrides travel only when the user set them (v3.1.0: the native
        # helpers read them from this file).
        self.assertNotIn("TC_MDNS_INSTANCE_NAME", rendered)
        self.assertNotIn("TC_NETBIOS_NAME", rendered)
        self.assertNotIn("MDNS_HOST_LABEL", rendered)

    def test_flash_runtime_config_ignores_deprecated_name_overrides(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_MDNS_INSTANCE_NAME": "James's Capsule",
                "TC_NETBIOS_NAME": "JAMESCAP",
                "TC_MDNS_DEVICE_MODEL": "TimeCapsule6,106",
            }
        )
        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            nbns_enabled=False,
            debug_logging=False,
        )
        # Old configs remain loadable, but neither shell nor native runtime
        # receives an override. Exercise the rendered shell environment too.
        proc = subprocess.run(["/bin/sh", "-c", rendered + "printf '%s|%s\\n' \"${TC_MDNS_INSTANCE_NAME-unset}\" \"${TC_NETBIOS_NAME-unset}\""],
                              capture_output=True, text=True, check=True)
        self.assertEqual(proc.stdout, "unset|unset\n")
        self.assertNotIn("TC_SHARE_NAME", rendered)

    def test_flash_runtime_config_can_disable_telemetry(self) -> None:
        rendered = render_flash_runtime_config(
            AppConfig.from_values({}),
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            nbns_enabled=True,
            telemetry_enabled=False,
        )

        self.assertIn("TELEMETRY=false\n", rendered)

    def test_flash_runtime_config_can_enable_rsync(self) -> None:
        rendered = render_flash_runtime_config(
            AppConfig.from_values({}),
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            nbns_enabled=True,
            rsync_enabled=True,
        )

        self.assertIn("RSYNC_ENABLED=1\n", rendered)

    def test_rsync_daemon_config_exposes_payload_volume_share_root_without_pid_file(self) -> None:
        rendered = render_rsync_daemon_config(PayloadHome("/Volumes/dk5", "/dev/dk5", ".samba4"))

        self.assertEqual(
            rendered,
            textwrap.dedent(
                """\
                port = 873
                log file = /mnt/Memory/samba4/var/rsync.log
                uid = root
                gid = wheel
                read only = false
                list = true

                [shareroot]
                path = /Volumes/dk5/ShareRoot
                """
            ),
        )
        self.assertNotIn("pid file", rendered.lower())

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
                "TC_SMB_BROWSE_COMPATIBILITY": "false",
                "TC_ANY_PROTOCOL": "false",
                "TC_FRUIT_METADATA_NETATALK": "false",
                "TC_VFS_AIO_FORK_ENABLED": "false",
            }
        )

        rendered = render_gui_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            nbns_enabled=True,
            debug_logging=False,
            internal_share_use_disk_root=True,
            smb_browse_compatibility=True,
            mdns_advertise_afp=True,
            any_protocol=True,
            fruit_metadata_netatalk=True,
            vfs_aio_fork_enabled=True,
        )

        self.assertIn("INTERNAL_SHARE_USE_DISK_ROOT=1\n", rendered)
        self.assertNotIn("SMB_BIND_LAN_ONLY", rendered)
        self.assertIn("SMB_BROWSE_COMPATIBILITY=1\n", rendered)
        self.assertIn("MDNS_ADVERTISE_AFP=1\n", rendered)
        self.assertIn("ANY_PROTOCOL=1\n", rendered)
        self.assertIn("REQUIRE_SMB_ENCRYPTION=0\n", rendered)
        self.assertIn("FRUIT_METADATA_NETATALK=1\n", rendered)
        self.assertIn("VFS_AIO_FORK_ENABLED=1\n", rendered)

    def test_flash_runtime_config_requires_smb_encryption(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_ANY_PROTOCOL": "false",
                "TC_REQUIRE_SMB_ENCRYPTION": "true",
            }
        )

        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            nbns_enabled=True,
        )

        self.assertIn("ANY_PROTOCOL=0\n", rendered)
        self.assertIn("REQUIRE_SMB_ENCRYPTION=1\n", rendered)

    def test_flash_runtime_config_forces_smb_signing_and_encryption_off(self) -> None:
        config = AppConfig.from_values(
            {"TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION": "true"}
        )

        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            nbns_enabled=True,
        )

        self.assertIn("REQUIRE_SMB_ENCRYPTION=0\n", rendered)
        self.assertIn("FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION=1\n", rendered)

    def test_flash_runtime_config_rejects_required_and_disabled_smb_encryption(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_REQUIRE_SMB_ENCRYPTION": "true",
                "TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION": "true",
            }
        )

        with self.assertRaisesRegex(ValueError, "cannot be used with Force Disable"):
            render_flash_runtime_config(
                config,
                PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
                nbns_enabled=True,
            )

    def test_flash_runtime_config_rejects_any_protocol_with_smb_encryption(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_ANY_PROTOCOL": "true",
                "TC_REQUIRE_SMB_ENCRYPTION": "true",
            }
        )

        with self.assertRaisesRegex(ValueError, "SMB encryption requires SMB3-only"):
            render_flash_runtime_config(
                config,
                PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
                nbns_enabled=True,
            )

    def test_flash_runtime_config_deploy_time_overrides_can_disable_saved_values(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_INTERNAL_SHARE_USE_DISK_ROOT": "true",
                "TC_SMB_BROWSE_COMPATIBILITY": "true",
                "TC_MDNS_ADVERTISE_AFP": "true",
                "TC_ANY_PROTOCOL": "true",
                "TC_FRUIT_METADATA_NETATALK": "true",
                "TC_VFS_AIO_FORK_ENABLED": "true",
            }
        )

        rendered = render_gui_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            nbns_enabled=True,
            debug_logging=False,
            internal_share_use_disk_root=False,
            smb_browse_compatibility=False,
            mdns_advertise_afp=False,
            any_protocol=False,
            fruit_metadata_netatalk=False,
            vfs_aio_fork_enabled=False,
        )

        self.assertIn("INTERNAL_SHARE_USE_DISK_ROOT=0\n", rendered)
        self.assertNotIn("SMB_BIND_LAN_ONLY", rendered)
        self.assertIn("SMB_BROWSE_COMPATIBILITY=0\n", rendered)
        self.assertIn("MDNS_ADVERTISE_AFP=0\n", rendered)
        self.assertIn("ANY_PROTOCOL=0\n", rendered)
        self.assertIn("FRUIT_METADATA_NETATALK=0\n", rendered)
        self.assertIn("VFS_AIO_FORK_ENABLED=0\n", rendered)

    def test_runtime_env_maps_afp_advertising_to_adisk_disk_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "mdns-advertise-afp-env.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    MDNS_ADVERTISE_AFP=0
                    tc_init_runtime_env
                    printf 'disabled=%s|%s\\n' "$MDNS_ADVERTISE_AFP" "$TC_ADISK_DISK_ADVF"
                    MDNS_ADVERTISE_AFP=1
                    tc_init_runtime_env
                    printf 'enabled=%s|%s\\n' "$MDNS_ADVERTISE_AFP" "$TC_ADISK_DISK_ADVF"
                    MDNS_ADVERTISE_AFP=invalid
                    tc_init_runtime_env
                    printf 'invalid=%s|%s\\n' "$MDNS_ADVERTISE_AFP" "$TC_ADISK_DISK_ADVF"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("disabled=0|0x82\n", proc.stdout)
        self.assertIn("enabled=1|0x83\n", proc.stdout)
        self.assertIn("invalid=0|0x82\n", proc.stdout)

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

    def test_common_runtime_identity_projection_is_atomic_and_literal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, *_ = self.write_runtime_harness(tmp_path)
            projection = flash / "native-identity"
            projection.write_text("samba-identity 1\nMYCAPSULE\nJames's 中文 $(false)\nTimeCapsule8,119\n")
            script = f"""
set -eu
. {flash}/common.sh
tc_init_runtime_identity
printf '%s|%s|%s\\n' "$SMB_NETBIOS_NAME" "$SMB_SERVER_STRING" "$SMB_FRUIT_MODEL"
printf '%s\\n' 'samba-identity 1' partial > {projection}
if tc_init_runtime_identity; then exit 9; fi
printf '%s|%s|%s\\n' "$SMB_NETBIOS_NAME" "$SMB_SERVER_STRING" "$SMB_FRUIT_MODEL"
"""
            proc = subprocess.run(["/bin/sh", "-c", script], text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), ["MYCAPSULE|James's 中文 $(false)|TimeCapsule8,119"] * 2)

    def test_common_runtime_identity_uses_final_netbios_fallback_for_punctuation_only_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path, hostname_output="---.local")
            (flash / "native-identity").write_text("samba-identity 1\nTimeCapsule\n极端 时间胶囊\nMacSamba\n")
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
                    printf 'identity=%s|%s|%s\\n' "$SMB_SERVER_STRING" "$SMB_FRUIT_MODEL" "$SMB_NETBIOS_NAME"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("identity=极端 时间胶囊|MacSamba|TimeCapsule\n", proc.stdout)
        self.assertIn("runtime identity: netbios=TimeCapsule server_string=极端 时间胶囊 model=MacSamba", proc.stdout)

    def test_common_runtime_identity_overwrites_legacy_values_and_feeds_runtime_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path, hostname_output="Time Capsule.local")
            (flash / "native-identity").write_text("samba-identity 1\nTimeCapsule\nJames's AirPort.Time Capsule\nTimeCapsule8,119\n")
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            discovery_args = tmp_path / "discovery.args"
            (flash / "discoveryd").write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >{shlex.quote(str(discovery_args))}\n"
            )
            (flash / "discoveryd").chmod(0o755)
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
                            syNm) echo "James's AirPort.Time Capsule" ;;
                            syVs) echo 7.9.1 ;;
                            srcv) echo 79100.2 ;;
                            syAP) echo 119 ;;
                            laMA) echo 80:EA:96:E6:58:68 ;;
                            *) return 1 ;;
                        esac
                    }}
                    stop_discovery_conflicts() {{ return 0; }}
                    tc_set_payload_log_dir {payload} {volumes}/dk2
                    share_rows=$(cat <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    )
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    tc_init_runtime_identity
                    tc_generate_smb_conf_from_share_rows {payload} "$share_rows"
                    tc_launch_discovery "discovery test" 0 0 0 "$MDNS_DEBUG_LOGGING" "$share_rows"
                    wait "$TC_DISCOVERY_PID" || true
                    printf 'identity=%s|%s|%s\\n' "$SMB_SERVER_STRING" "$SMB_FRUIT_MODEL" "$SMB_NETBIOS_NAME"
                    cat "$TC_SMBD_CONF"
                    printf 'discovery_args=%s\\n' "$(cat {discovery_args})"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("identity=James's AirPort.Time Capsule|TimeCapsule8,119|TimeCapsule", proc.stdout)
        self.assertIn("netbios name = TimeCapsule\n", proc.stdout)
        self.assertIn("server string = James's AirPort.Time Capsule\n", proc.stdout)
        # v3.1.0: the registrant reads its own identity from ACP/config; the
        # shell passes it nothing but the payload state.
        self.assertIn("discovery_args=--netbios-name TimeCapsule --adisk-share Data dk2 aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa 0x82", proc.stdout)
        self.assertNotIn("--instance", proc.stdout)
        self.assertNotIn("--host", proc.stdout)
        self.assertNotIn("--auto-ip", proc.stdout)
        self.assertIn("runtime identity: netbios=TimeCapsule server_string=James's AirPort.Time Capsule model=TimeCapsule8,119", proc.stdout)
        self.assertNotIn("LegacyInstance", proc.stdout)
        self.assertNotIn("legacy-host", proc.stdout)
        self.assertNotIn("LegacyNetbios", proc.stdout)
        self.assertNotIn("LegacyServer", proc.stdout)

    def test_deployment_plan_uses_flash_pointer_and_single_private_payload(self) -> None:
        plan = build_deployment_plan(
            "root@10.0.0.2",
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            Path("/tmp/smbd"),
            Path("/tmp/discoveryd"),
            xattr_migrator_path=Path("/tmp/xattr-hfs-migrate"),
            rsync_path=Path("/tmp/rsync"),
         service_path=Path("bin/service"), telemetry_path=Path("bin/telemetry"))
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
                        tc_relaunch_diskd_loopback() { echo diskd; return 0; }
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
        self.assertEqual(proc.stdout, "cleanup\ndiskd\ntune\nlocks\nram\nlegacy\n")
        self.assertIn("starting manager", log_text)
        self.assertIn("manager launched as pid", log_text)

    # ---- v3.1.0 boot: Apple mDNSResponder stays, diskd moves to loopback ----

    def write_apple_process_fakes(self, tmp_path: Path, flash: Path, *, diskd: str, afpserver: bool, mast_after: int = 1) -> dict[str, Path]:
        """Fake ps/pkill/diskd/acp driven by a state directory.

        `diskd` is the initial diskd state (acpd|loopback|absent); pkill of
        diskd/afpserver flips the state; launching diskd records its argv and
        moves the state to loopback; `acp -A MaSt` answers after `mast_after`
        calls once diskd is on loopback.
        """
        state = tmp_path / "apple-state"
        state.mkdir()
        (state / "diskd").write_text(diskd)
        (state / "afpserver").write_text("running" if afpserver else "absent")
        (state / "mast-calls").write_text("0")
        pkill_log = tmp_path / "pkill.log"
        ps = tmp_path / "ps"
        ps.write_text(
            "#!/bin/sh\n"
            f"state={shlex.quote(str(state))}\n"
            "echo '  1 Is   init /sbin/init'\n"
            "echo ' 371 Sa   mDNSResponder /sbin/mDNSResponder -d'\n"
            "case \"$(cat \"$state/diskd\")\" in\n"
            "  acpd) echo ' 232 S    diskd /sbin/diskd -i  -d local.' ;;\n"
            "  loopback) echo ' 559 S    diskd /sbin/diskd -i lo0 -d local.' ;;\n"
            "  both) echo ' 559 S    diskd /sbin/diskd -i lo0 -d local.'; echo ' 640 S    diskd /sbin/diskd -i  -d local.' ;;\n"
            "  zombie) echo ' 232 ZW   (diskd) (diskd)' ;;\n"
            "esac\n"
            "[ \"$(cat \"$state/afpserver\")\" = running ] && echo ' 353 Ia   afpserver /sbin/afpserver -debug'\n"
            "exit 0\n"
        )
        ps.chmod(0o755)
        pkill = tmp_path / "pkill"
        pkill.write_text(
            "#!/bin/sh\n"
            f"state={shlex.quote(str(state))}\n"
            f"printf '%s\\n' \"$*\" >>{shlex.quote(str(pkill_log))}\n"
            "for arg; do case \"$arg\" in\n"
            "  '^diskd$') echo absent >\"$state/diskd\" ;;\n"
            "  '^afpserver$') echo absent >\"$state/afpserver\" ;;\n"
            "esac; done\n"
            "exit 0\n"
        )
        pkill.chmod(0o755)
        diskd_bin = tmp_path / "diskd"
        diskd_bin.write_text(
            "#!/bin/sh\n"
            f"state={shlex.quote(str(state))}\n"
            f"printf '%s\\n' \"$*\" >>{shlex.quote(str(tmp_path / 'diskd.log'))}\n"
            "echo loopback >\"$state/diskd\"\n"
            "exit 0\n"
        )
        diskd_bin.chmod(0o755)
        acp = tmp_path / "acp"
        acp.write_text(
            "#!/bin/sh\n"
            f"state={shlex.quote(str(state))}\n"
            "if [ \"$1:$2\" = '-A:MaSt' ]; then\n"
            "  calls=$(cat \"$state/mast-calls\"); calls=$((calls + 1)); echo \"$calls\" >\"$state/mast-calls\"\n"
            f"  if [ \"$(cat \"$state/diskd\")\" = loopback ] && [ \"$calls\" -ge {mast_after} ]; then\n"
            "    printf '<?xml version=\"1.0\"?>\\n<plist version=\"1.0\"><array/></plist>\\n'; exit 0\n"
            "  fi\n"
            "  exit 1\n"
            "fi\n"
            "exit 1\n"
        )
        acp.chmod(0o755)
        for name in ("common.sh", "boot.sh"):
            path = flash / name
            path.write_text(path.read_text().replace("/bin/ps ", str(ps) + " ").replace("/sbin/diskd ", str(diskd_bin) + " ").replace(str(tmp_path / "pkill"), str(pkill)))
        kill_log = tmp_path / "kill.log"
        # Put the production probe back (the harness stubs it to loopback),
        # reading the fake ps.
        fragment = load_boot_asset_text("common.d/30-processes.sh")
        probe = fragment[fragment.index("tc_apple_diskd_probe() {"):]
        probe = probe[:probe.index("\n}\n") + 3].replace("/bin/ps ", str(ps) + " ")
        with (flash / "common.sh").open("a") as common:
            common.write("\n" + probe)
            common.write("\nsleep() { :; }\nwait_for_runtime_process_absent_by_ucomm() { ! runtime_process_present_by_ucomm \"$1\"; }\n")
            # Signals go to fake PIDs from the fake ps: record them and flip the
            # state the way the real process would (232/640 are the ACPd diskd).
            common.write(
                "tc_signal_pid() {\n"
                f"    printf '%s %s\\n' \"$1\" \"$2\" >>{shlex.quote(str(kill_log))}\n"
                f"    state={shlex.quote(str(state))}\n"
                "    case \"$2\" in\n"
                "        232) echo absent >\"$state/diskd\" ;;\n"
                "        640) echo loopback >\"$state/diskd\" ;;\n"
                "    esac\n"
                "}\n"
            )
        return {"state": state, "pkill_log": pkill_log, "diskd_log": tmp_path / "diskd.log", "kill_log": kill_log}

    def run_boot(self, flash: Path) -> subprocess.CompletedProcess[str]:
        with (flash / "common.sh").open("a") as common:
            common.write(
                "\ntc_tune_kernel_memory() { :; }\ntc_prepare_locks_ramdisk() { return 0; }\n"
                "tc_prepare_legacy_prefix() { :; }\nrun_manager_stub() { :; }\nrun_manager_present() { return 1; }\n"
                "runtime_manager_present() { return 1; }\nstop_manager_process() { return 0; }\n"
                "tc_prepare_telemetry_reset() { return 0; }\n"
            )
        (flash / "manager.sh").write_text("#!/bin/sh\nexit 0\n")
        (flash / "manager.sh").chmod(0o755)
        return subprocess.run(["/bin/sh", str(flash / "boot.sh")], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    def test_boot_relaunches_acpd_diskd_on_loopback_exactly_once_and_waits_for_mast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fakes = self.write_apple_process_fakes(tmp_path, flash, diskd="acpd", afpserver=True, mast_after=3)
            proc = self.run_boot(flash)
            log_text = (memory / "samba4/var/rc.local.log").read_text()
            pkill_log = fakes["pkill_log"].read_text()
            kill_log = fakes["kill_log"].read_text()
            diskd_log = fakes["diskd_log"].read_text()
            mast_calls = int((fakes["state"] / "mast-calls").read_text())
            final_state = (fakes["state"] / "diskd").read_text().strip()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Killed by PID (never by name, which would take a loopback diskd too).
        self.assertNotIn("^diskd$", pkill_log)
        self.assertEqual(kill_log, "TERM 232\n")
        self.assertEqual(diskd_log, "-i lo0 -d local.\n")
        self.assertEqual(final_state, "loopback")
        self.assertGreaterEqual(mast_calls, 3)
        self.assertIn("stopping ACPd's diskd so Apple's SMB/AFP names stay off the LAN", log_text)
        self.assertIn("stopping diskd pid 232 (not on loopback)", log_text)
        self.assertIn("diskd relaunched on loopback; MaSt available after", log_text)
        self.assertIn("starting manager", log_text)
        # The one thing boot must never do (F11).
        self.assertNotIn("mDNSResponder", pkill_log)

    def test_boot_leaves_diskd_alone_when_already_on_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fakes = self.write_apple_process_fakes(tmp_path, flash, diskd="loopback", afpserver=False)
            proc = self.run_boot(flash)
            log_text = (memory / "samba4/var/rc.local.log").read_text()
            pkill_log = fakes["pkill_log"].read_text() if fakes["pkill_log"].exists() else ""
            diskd_launched = fakes["diskd_log"].exists()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("^diskd$", pkill_log)
        self.assertFalse(diskd_launched)
        self.assertIn("diskd already running on loopback", log_text)
        self.assertNotIn("mDNSResponder", pkill_log)

    def test_boot_launches_diskd_when_absent_and_degrades_when_mast_never_appears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fakes = self.write_apple_process_fakes(tmp_path, flash, diskd="absent", afpserver=False, mast_after=1000)
            # The MaSt wait is elapsed-time bounded (each probe may spend its
            # own allowance while ACPd is wedged): a fake clock that advances
            # 7 s per reading stands in for slow probes.
            clock = tmp_path / "clock"
            clock.write_text("1000")
            with (flash / "common.sh").open("a") as common:
                common.write(f"tc_now_seconds() {{ c=$(cat {shlex.quote(str(clock))}); echo \"$c\"; echo $((c + 7)) >{shlex.quote(str(clock))}; }}\n")
            proc = self.run_boot(flash)
            log_text = (memory / "samba4/var/rc.local.log").read_text()
            diskd_log = fakes["diskd_log"].read_text()
            mast_calls = int((fakes["state"] / "mast-calls").read_text())

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(diskd_log, "-i lo0 -d local.\n")
        self.assertIn("diskd not running; launching it on loopback", log_text)
        self.assertRegex(log_text, r"diskd relaunch failed; Apple SMB/AFP names may be visible \(MaSt not served after 3\ds\)")
        self.assertLess(mast_calls, 8)   # bounded by elapsed time, not by a probe count
        # Degraded, not fatal: the manager still starts.
        self.assertIn("starting manager", log_text)

    def test_boot_gives_up_on_a_diskd_that_survives_the_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fakes = self.write_apple_process_fakes(tmp_path, flash, diskd="acpd", afpserver=False)
            with (flash / "common.sh").open("a") as common:
                common.write(f"tc_signal_pid() {{ printf '%s %s\\n' \"$1\" \"$2\" >>{shlex.quote(str(fakes['kill_log']))}; }}\n")
            proc = self.run_boot(flash)
            log_text = (memory / "samba4/var/rc.local.log").read_text()
            diskd_launched = fakes["diskd_log"].exists()
            kill_log = fakes["kill_log"].read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(diskd_launched)
        self.assertEqual(kill_log, "TERM 232\n")
        self.assertIn("diskd relaunch failed; Apple SMB/AFP names may be visible (old diskd still running)", log_text)
        # Best effort at boot (B.8 failure contract): the manager retries.
        self.assertIn("diskd relaunch will be retried by the manager", log_text)
        self.assertIn("starting manager", log_text)

    def test_stray_acpd_diskd_next_to_ours_is_reported_and_killed_by_pid(self) -> None:
        """ACPd's diskd came back next to our loopback one: the state is `acpd`
        (it advertises on the LAN regardless of ours), only its PID is signalled,
        and once it is gone no second diskd is launched."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fakes = self.write_apple_process_fakes(tmp_path, flash, diskd="both", afpserver=False)
            script = (
                f". {shlex.quote(str(flash / 'common.sh'))}\n"
                "tc_init_runtime_env\n"
                f"tc_set_log {shlex.quote(str(memory / 'samba4/var/rc.local.log'))} test\n"
                "tc_apple_diskd_probe; echo \"state=$TC_APPLE_DISKD_STATE strays=[$TC_APPLE_DISKD_STRAY_PIDS]\"\n"
                "if tc_relaunch_diskd_loopback; then echo relaunch=ok; else echo relaunch=failed; fi\n"
                "tc_apple_diskd_probe; echo \"state=$TC_APPLE_DISKD_STATE strays=[$TC_APPLE_DISKD_STRAY_PIDS]\"\n"
            )
            proc = subprocess.run(["/bin/sh", "-c", script], text=True, capture_output=True, check=False)
            kill_log = fakes["kill_log"].read_text()
            diskd_launched = fakes["diskd_log"].exists()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), ["state=acpd strays=[ 640]", "relaunch=ok", "state=loopback strays=[]"])
        self.assertEqual(kill_log, "TERM 640\n")
        self.assertFalse(diskd_launched)

    def test_boot_kills_afpserver_unless_afp_advertising_is_enabled(self) -> None:
        for advertise_afp, expect_kill in ((0, True), (1, False)):
            with self.subTest(advertise_afp=advertise_afp):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
                    with (flash / "tcapsulesmb.conf").open("a") as conf:
                        conf.write(f"MDNS_ADVERTISE_AFP={advertise_afp}\n")
                    fakes = self.write_apple_process_fakes(tmp_path, flash, diskd="loopback", afpserver=True)
                    proc = self.run_boot(flash)
                    log_text = (memory / "samba4/var/rc.local.log").read_text()
                    pkill_log = fakes["pkill_log"].read_text() if fakes["pkill_log"].exists() else ""
                    afp_state = (fakes["state"] / "afpserver").read_text().strip()

                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual("^afpserver$" in pkill_log, expect_kill, pkill_log)
                self.assertEqual(afp_state, "absent" if expect_kill else "running")
                # tc_cleanup_old_runtime wipes /mnt/Memory/samba4 (and this log) before
                # "cleanup complete"; the pkill transcript and state are the evidence.
                self.assertIn("old managed runtime cleanup complete", log_text)
                self.assertNotIn("mDNSResponder", pkill_log)

    def test_runtime_env_ignores_removed_smb_bind_lan_only_setting_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            with (flash / "tcapsulesmb.conf").open("a") as conf:
                conf.write("SMB_BIND_LAN_ONLY=1\n")
            script = tmp_path / "env.sh"
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
                    tc_log_runtime_env_warnings
                    tc_log_runtime_env_warnings
                    printf 'lan_only=%s\\n' "${{SMB_BIND_LAN_ONLY-unset}}"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)
            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("lan_only=unset\n", proc.stdout)
        self.assertEqual(proc.stdout.count("ignoring removed setting SMB_BIND_LAN_ONLY"), 1, proc.stdout)

    # ---- v3.1.0 manager: retained bind projection, registrant restarts ----

    def write_manager_bind_harness(self, tmp_path: Path, flash: Path, probe_sequence: list[str], *, passes: int) -> tuple[Path, Path]:
        """Manager stubs for the Samba lane with a scripted bind probe.

        `probe_sequence` entries are the two-line probe outputs (tokens, then
        the status line) returned on successive probes; the last one repeats.
        The fake clock advances 10 s per tc_now_seconds() call so stale ages
        are observable. Returns (events, clock) paths.
        """
        events = tmp_path / "events"
        probes = tmp_path / "probes"
        probes.mkdir()
        for index, text in enumerate(probe_sequence, start=1):
            (probes / str(index)).write_text(text)
        probe_count = tmp_path / "probe-count"
        clock = tmp_path / "clock"
        sleep_count = tmp_path / "sleep-count"
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
                        SMB_FRUIT_MODEL=TimeCapsule6,106
                    TC_RUNTIME_IDENTITY_READY=1
                    }}
                    tc_manager_refresh_runtime_identity_for_recovery() {{ :; }}
                    tc_wake_or_mount_volume() {{ return 0; }}
                    is_volume_root_mounted() {{ return 0; }}
                    tc_verify_payload_dir() {{ return 0; }}
                    tc_volume_is_writable() {{ return 0; }}
                    tc_prepare_share_path() {{ echo "$2/ShareRoot"; }}
                    tc_apply_ata_drive_setting() {{ :; }}
                    tc_find_payload_smbd() {{ echo "$1/smbd"; }}
                    tc_stage_runtime() {{
                        mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_PRIVATE"
                        printf '#!/bin/sh\\nexit 0\\n' >"$TC_SMBD_BIN"
                        chmod 755 "$TC_SMBD_BIN"
                        : >"$RAM_PRIVATE/smbpasswd"
                        : >"$RAM_PRIVATE/username.map"
                        return 0
                    }}
                    tc_generate_smb_conf_from_share_rows() {{
                        echo "smb.conf:$TC_SMB_BIND_INTERFACES" >>{shlex.quote(str(events))}
                        printf 'interfaces = %s\\n' "$TC_SMB_BIND_INTERFACES" >"$TC_SMBD_CONF"
                        return 0
                    }}
                    tc_now_seconds() {{
                        now=$(/bin/cat {shlex.quote(str(clock))} 2>/dev/null || echo 1000)
                        echo "$((now + 10))" >{shlex.quote(str(clock))}
                        echo "$now"
                    }}
                    tc_probe_smb_bind_interfaces() {{
                        count=$(/bin/cat {shlex.quote(str(probe_count))} 2>/dev/null || echo 0)
                        count=$((count + 1))
                        echo "$count" >{shlex.quote(str(probe_count))}
                        path={shlex.quote(str(probes))}/$count
                        [ -f "$path" ] || path={shlex.quote(str(probes))}/{len(probe_sequence)}
                        echo "probe:$count" >>{shlex.quote(str(events))}
                        TC_SMB_BIND_PROBE_TOKENS=$(sed -n '1p' "$path")
                        probe_line=$(sed -n '2p' "$path")
                        TC_SMB_BIND_STATUS=${{probe_line#status=}}
                        TC_SMB_BIND_STATUS=${{TC_SMB_BIND_STATUS%% *}}
                        TC_SMB_BIND_REASON=${{probe_line#*reason=}}
                        TC_SMB_BIND_POLICY='policy 1 0'
                    }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            smbd|discoveryd) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_smbd_bound_tcp_445() {{ return 0; }}
                    tc_reload_smbd_config() {{ echo reload >>{shlex.quote(str(events))}; return 0; }}
                    stop_runtime_process_by_ucomm() {{ echo "stop $1" >>{shlex.quote(str(events))}; }}
                    sleep() {{
                        case "$1" in
                            1|5) return 0 ;;
                            10)
                                count=$(/bin/cat {shlex.quote(str(sleep_count))} 2>/dev/null || echo 0)
                                count=$((count + 1))
                                echo "$count" >{shlex.quote(str(sleep_count))}
                                if [ "$count" -lt {passes} ]; then
                                    return 0
                                fi
                                echo "status=$manager_status bind=$manager_bind_status"
                                exit 0
                                ;;
                        esac
                        return 0
                    }}
                    """
                )
            )
        return events, clock

    def run_manager(self, flash: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["/bin/sh", str(flash / "manager.sh")], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    def test_manager_keeps_validated_bind_projection_across_incomplete_probes(self) -> None:
        validated = "127.0.0.1/8 ::1/128 10.0.1.1/24 fe80:9::1/64\nstatus=validated\n"
        incomplete = "127.0.0.1/8 ::1/128 10.0.1.1/24 fe80:9::1/64\nstatus=incomplete reason=mode\n"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            with (flash / "tcapsulesmb.conf").open("a") as conf:
                conf.write("MANAGER_BIND_POLL_SECONDS=10\n")
            self.write_sequence_acp(tmp_path, (self.internal_mast_raw_with_volatile_fields(users=1),))
            events, _clock = self.write_manager_bind_harness(tmp_path, flash, [validated, incomplete, incomplete, incomplete], passes=4)
            proc = self.run_manager(flash)
            events_text = events.read_text()
            log_text = (memory / "samba4/var/manager.log").read_text()
            smb_conf = (memory / "samba4/etc/smb.conf").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=0 bind=retained\n", proc.stdout)
        self.assertEqual(events_text.count("probe:"), 4, events_text)
        # One smb.conf render from the validated probe, none from the incomplete ones.
        self.assertEqual(events_text.count("smb.conf:"), 1, events_text)
        self.assertIn("smb.conf:127.0.0.1/8 ::1/128 10.0.1.1/24 fe80:9::1/64\n", events_text)
        self.assertEqual(smb_conf, "interfaces = 127.0.0.1/8 ::1/128 10.0.1.1/24 fe80:9::1/64\n")
        ages = [int(line.split("age=")[1].split("s")[0]) for line in log_text.splitlines() if "keeping last validated projection" in line]
        self.assertEqual(len(ages), 3, log_text)
        self.assertTrue(ages[0] < ages[1] < ages[2], ages)
        self.assertTrue(all("reason=mode" in line for line in log_text.splitlines() if "keeping last validated projection" in line))

    def test_manager_applies_a_validated_bind_change_once(self) -> None:
        first = "127.0.0.1/8 ::1/128 10.0.1.1/24\nstatus=validated\n"
        second = "127.0.0.1/8 ::1/128 10.0.1.1/24 192.168.1.10/24\nstatus=validated\n"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            with (flash / "tcapsulesmb.conf").open("a") as conf:
                conf.write("MANAGER_BIND_POLL_SECONDS=10\n")
            self.write_sequence_acp(tmp_path, (self.internal_mast_raw_with_volatile_fields(users=1),))
            events, _clock = self.write_manager_bind_harness(tmp_path, flash, [first, second, second, second], passes=4)
            proc = self.run_manager(flash)
            events_text = events.read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=0 bind=ok\n", proc.stdout)
        self.assertEqual(events_text.count("smb.conf:"), 2, events_text)
        self.assertIn("smb.conf:127.0.0.1/8 ::1/128 10.0.1.1/24\n", events_text)
        self.assertIn("smb.conf:127.0.0.1/8 ::1/128 10.0.1.1/24 192.168.1.10/24\n", events_text)
        # smbd restarts once for the initial projection and once for the change; never for the repeats.
        self.assertEqual(events_text.count("stop smbd\n"), 2, events_text)
        self.assertEqual(events_text.split("probe:3")[1].count("stop smbd"), 0, events_text)

    def test_manager_restart_without_history_starts_cold_and_reconfigures_on_first_validated_probe(self) -> None:
        incomplete = "127.0.0.1/8 ::1/128\nstatus=incomplete reason=iflist\n"
        validated = "127.0.0.1/8 ::1/128 10.0.1.1/24\nstatus=validated\n"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            with (flash / "tcapsulesmb.conf").open("a") as conf:
                conf.write("MANAGER_BIND_POLL_SECONDS=10\nTC_SMB_BIND_INTERFACES=\"192.0.2.99/24\"\n")
            self.write_sequence_acp(tmp_path, (self.internal_mast_raw_with_volatile_fields(users=1),))
            events, _clock = self.write_manager_bind_harness(tmp_path, flash, [incomplete, incomplete, validated], passes=3)
            proc = self.run_manager(flash)
            events_text = events.read_text()
            log_text = (memory / "samba4/var/manager.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        # No validated projection yet: Samba is not configured from an incomplete probe.
        self.assertEqual(events_text.count("smb.conf:"), 1, events_text)
        self.assertIn("smb.conf:127.0.0.1/8 ::1/128 10.0.1.1/24\n", events_text)
        self.assertEqual(log_text.count("Samba bind: no validated projection yet (reason=iflist)"), 2, log_text)
        self.assertEqual(log_text.count("waiting for a validated bind projection before configuring smbd"), 2, log_text)
        self.assertNotIn("keeping last validated projection", log_text)
        self.assertIn("manager Samba: initialized bind interfaces: 127.0.0.1/8 ::1/128 10.0.1.1/24", log_text)

    def test_manager_keeps_registrant_running_across_name_only_changes(self) -> None:
        validated = "127.0.0.1/8 ::1/128 10.0.1.1/24\nstatus=validated\n"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            self.write_sequence_acp(tmp_path, (self.internal_mast_raw_with_volatile_fields(users=1),))
            events, _clock = self.write_manager_bind_harness(tmp_path, flash, [validated], passes=6)
            names = tmp_path / "names"
            names.write_text("AirPort")
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                        tc_init_runtime_identity() {{
                            MDNS_INSTANCE_NAME=$(/bin/cat {shlex.quote(str(names))})
                            MDNS_HOST_LABEL=airport
                            SMB_NETBIOS_NAME=AIRPORT
                            SMB_SERVER_STRING=AirPort
                            SMB_FRUIT_MODEL=TimeCapsule6,106
                    TC_RUNTIME_IDENTITY_READY=1
                        }}
                        tc_launch_discovery() {{
                            echo "discovery-launch:$MDNS_INSTANCE_NAME" >>{shlex.quote(str(events))}
                            echo running >{shlex.quote(str(tmp_path / 'discovery-state'))}
                        }}
                        stop_runtime_process_by_ucomm() {{
                            echo "stop $1" >>{shlex.quote(str(events))}
                            [ "$1" = discoveryd ] && echo stopped >{shlex.quote(str(tmp_path / 'discovery-state'))}
                            return 0
                        }}
                        runtime_process_present_by_ucomm() {{
                            case "$1" in
                                smbd) return 0 ;;
                                discoveryd) [ "$(/bin/cat {shlex.quote(str(tmp_path / 'discovery-state'))} 2>/dev/null)" = running ] ;;
                                wcifsfs|wcifsnd) return 1 ;;
                                *) return 1 ;;
                            esac
                        }}
                        sleep() {{
                            case "$1" in
                                1|5) return 0 ;;
                                10)
                                    count=$(/bin/cat {shlex.quote(str(tmp_path / 'pass-count'))} 2>/dev/null || echo 0)
                                    count=$((count + 1))
                                    echo "$count" >{shlex.quote(str(tmp_path / 'pass-count'))}
                                    case "$count" in
                                        2) printf '%s' 'Renamed Capsule' >{shlex.quote(str(names))} ;;
                                        6) echo "status=$manager_status bind=$manager_bind_status"; exit 0 ;;
                                    esac
                                    ;;
                            esac
                            return 0
                        }}
                        """
                    )
                )
            proc = self.run_manager(flash)
            events_text = events.read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=0 bind=ok\n", proc.stdout)
        # The registrant tracks ACP names itself; the manager launches once
        # for the share arguments and never restarts it for a rename.
        self.assertEqual([line for line in events_text.splitlines() if line.startswith("discovery-launch") or line == "stop discoveryd"],
                         ["discovery-launch:AirPort"], events_text)
        self.assertEqual(events_text.count("discovery-launch"), 1, events_text)

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

                        tc_prepare_local_hostname_resolution() {
                            i=0
                            payload='abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz'
                            while [ "$i" -lt 130 ]; do
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
                            discoveryd) return 0 ;;
                                *) return 1 ;;
                            esac
                        }
                        stop_runtime_process_by_ucomm() { :; }
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
                            discoveryd) return 0 ;;
                            *) return 1 ;;
                        esac
                    }
                    stop_runtime_process_by_ucomm() { :; }
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
                            discoveryd) return 0 ;;
                            *) return 1 ;;
                        esac
                    }
                    stop_runtime_process_by_ucomm() { :; }
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

    def test_manager_diskless_pass_does_not_require_samba_identity(self) -> None:
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
                        SMB_FRUIT_MODEL=TimeCapsule6,106
                    TC_RUNTIME_IDENTITY_READY=1
                    }
                    runtime_process_present_by_ucomm() {
                        case "$1" in
                            discoveryd) return 0 ;;
                            afpserver|wcifsfs|wcifsnd) return 1 ;;
                            *) echo unexpected-runtime; return 1 ;;
                        esac
                    }
                    stop_runtime_process_by_ucomm() { :; }
                    tc_nbns_enabled() { return 0; }
                    tc_manager_stop_samba_lane_without_payload() { :; }
                    sleep() {
                        if [ "$1" = "1" ]; then
                            return 0
                        fi
                        echo "identity_ready=${TC_RUNTIME_IDENTITY_READY:-0}"
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
        self.assertEqual(proc.stdout, "prepare\nidentity_ready=0\n")

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
                            discoveryd) return 0 ;;
                            *) return 1 ;;
                        esac
                    }
                    stop_runtime_process_by_ucomm() { :; }
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
        self.assertEqual(proc.stdout, "no_payload\nstatus=0\n")
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
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                        SMB_FRUIT_MODEL=TimeCapsule6,106
                    TC_RUNTIME_IDENTITY_READY=1
                    }}
                    tc_manager_stop_samba_lane_without_payload() {{ :; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            discoveryd) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    stop_runtime_process_by_ucomm() {{ :; }}
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
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                        SMB_FRUIT_MODEL=TimeCapsule6,106
                    TC_RUNTIME_IDENTITY_READY=1
                    }}
                    tc_manager_stop_samba_lane_without_payload() {{ :; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            discoveryd) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    stop_runtime_process_by_ucomm() {{ :; }}
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
                        SMB_FRUIT_MODEL=TimeCapsule6,106
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
                        TC_SMB_BIND_PROBE_TOKENS=127.0.0.1/8
                        TC_SMB_BIND_STATUS=validated
                        TC_SMB_BIND_REASON=
                        TC_SMB_BIND_POLICY='policy 1 0'
                    }}
                    tc_launch_discovery() {{ echo discovery-launch >>{shlex.quote(str(events))}; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            smbd) echo smbd-process >>{shlex.quote(str(events))}; return 0 ;;
                            discoveryd) echo discovery-process >>{shlex.quote(str(events))}; return 0 ;;
                            wcifsfs|wcifsnd) return 1 ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_smbd_bound_tcp_445() {{ return 0; }}
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
        self.assertEqual(events_text.count("discovery-launch\n"), 1, events_text)
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
                        SMB_FRUIT_MODEL=TimeCapsule6,106
                    TC_RUNTIME_IDENTITY_READY=1
                    }}
                    tc_manager_stop_samba_lane_without_payload() {{ :; }}
                    tc_apple_diskd_state() {{ echo loopback; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            discoveryd) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
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
                        SMB_FRUIT_MODEL=TimeCapsule6,106
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
                        cp "$TC_SMBD_BIN" "$TC_SERVICE_BIN"
                        cp "$TC_SMBD_BIN" "$TC_TELEMETRY_BIN"
                        : >"$RAM_PRIVATE/smbpasswd"
                        : >"$RAM_PRIVATE/username.map"
                        return 0
                    }}
                    tc_probe_smb_bind_interfaces() {{
                        echo bind-probe >>{shlex.quote(str(events))}
                        TC_SMB_BIND_PROBE_TOKENS=127.0.0.1/8
                        TC_SMB_BIND_STATUS=validated
                        TC_SMB_BIND_REASON=
                        TC_SMB_BIND_POLICY='policy 1 0'
                    }}
                    tc_launch_discovery() {{ echo discovery-launch >>{shlex.quote(str(events))}; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            smbd) return 0 ;;
                            discoveryd) echo discovery-process >>{shlex.quote(str(events))}; return 0 ;;
                            wcifsfs|wcifsnd) return 1 ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_smbd_bound_tcp_445() {{ return 0; }}
                    tc_reload_smbd_config() {{ echo reload >>{shlex.quote(str(events))}; }}
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
        self.assertEqual(events_text.count("discovery-launch\n"), 2, events_text)
        self.assertEqual(events_text.count("bind-probe\n"), 2, events_text)
        self.assertNotIn("manager pass 2 step=identity start", log_text)
        self.assertNotIn("manager pass 2 step=samba start", log_text)
        self.assertIn("disk_probe=change_confirmed", log_text)

    def test_manager_restarts_smbd_when_inherited_bind_tokens_are_untrusted(self) -> None:
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
                        SMB_FRUIT_MODEL=TimeCapsule6,106
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
                    tc_probe_smb_bind_interfaces() {{ TC_SMB_BIND_PROBE_TOKENS=127.0.0.1/8; TC_SMB_BIND_STATUS=validated; TC_SMB_BIND_REASON=; TC_SMB_BIND_POLICY="policy 1 0"; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            smbd) [ "$(/bin/cat {shlex.quote(str(smbd_state))})" = "running" ] ;;
                            discoveryd) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_smbd_bound_tcp_445() {{ [ "$(/bin/cat {shlex.quote(str(smbd_state))})" = "running" ]; }}
                    tc_reload_smbd_config() {{ echo reload >>{shlex.quote(str(events))}; return 1; }}
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
            [
                "stage", "stop smbd", "launched", "stop discoveryd",
                "stop wcifsfs", "stop wcifsnd", "stop legacy mdns advertiser",
                "stop legacy nbns advertiser",
            ],
            events_text,
        )
        self.assertNotIn("reload", events_text)
        self.assertIn("manager smbd recovery: restarting smbd after staged runtime change", log_text)

    def test_manager_resets_ram_runtime_and_retries_staging_on_next_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            self.write_fake_acp(tmp_path, self.internal_mast_raw_with_volatile_fields(users=1))
            with (flash / "tcapsulesmb.conf").open("a") as conf:
                conf.write("NBNS_ENABLED=1\n")
            stale_file = memory / "samba4/stale-runtime-file"
            stale_file.parent.mkdir(parents=True, exist_ok=True)
            stale_file.write_text("stale\n")
            events = tmp_path / "events"
            stage_count = tmp_path / "stage-count"
            sleep_count = tmp_path / "sleep-count"
            smbd_state = tmp_path / "smbd-state"
            nbns_state = tmp_path / "nbns-state"
            smbd_state.write_text("running\n")
            nbns_state.write_text("running\n")
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                        SMB_FRUIT_MODEL=TimeCapsule6,106
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
                        count=$(/bin/cat {shlex.quote(str(stage_count))} 2>/dev/null || echo 0)
                        count=$((count + 1))
                        echo "$count" >{shlex.quote(str(stage_count))}
                        echo "stage:$count" >>{shlex.quote(str(events))}
                        if [ "$count" -eq 1 ]; then
                            return 1
                        fi
                        mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_PRIVATE"
                        {{
                            echo '#!/bin/sh'
                            echo "printf 'running\\\\n' >{shlex.quote(str(smbd_state))}"
                            echo "printf 'launched\\\\n' >>{shlex.quote(str(events))}"
                            echo 'exit 0'
                        }} >"$TC_SMBD_BIN"
                        chmod 755 "$TC_SMBD_BIN"
                        : >"$RAM_PRIVATE/smbpasswd"
                        : >"$RAM_PRIVATE/username.map"
                        return 0
                    }}
                    tc_probe_smb_bind_interfaces() {{ TC_SMB_BIND_PROBE_TOKENS=127.0.0.1/8; TC_SMB_BIND_STATUS=validated; TC_SMB_BIND_REASON=; TC_SMB_BIND_POLICY="policy 1 0"; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            smbd) [ "$(/bin/cat {shlex.quote(str(smbd_state))})" = "running" ] ;;
                            nbns-advertiser) [ "$(/bin/cat {shlex.quote(str(nbns_state))})" = "running" ] ;;
                            discoveryd) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_smbd_bound_tcp_445() {{ [ "$(/bin/cat {shlex.quote(str(smbd_state))})" = "running" ]; }}
                    wait_for_process() {{
                        case "$1" in
                            smbd) [ "$(/bin/cat {shlex.quote(str(smbd_state))})" = "running" ] ;;
                            *) return 0 ;;
                        esac
                    }}
                    stop_runtime_process_by_ucomm() {{
                        echo "stop $1" >>{shlex.quote(str(events))}
                        case "$1" in
                            smbd) printf 'stopped\\n' >{shlex.quote(str(smbd_state))} ;;
                            nbns-advertiser) printf 'stopped\\n' >{shlex.quote(str(nbns_state))} ;;
                        esac
                    }}
                    sleep() {{
                        case "$1" in
                            1|5) return 0 ;;
                            10)
                                count=$(/bin/cat {shlex.quote(str(sleep_count))} 2>/dev/null || echo 0)
                                count=$((count + 1))
                                echo "$count" >{shlex.quote(str(sleep_count))}
                                echo "sleep:$count" >>{shlex.quote(str(events))}
                                if [ "$count" -eq 1 ]; then
                                    return 0
                                fi
                                echo "status=$manager_status samba=$manager_samba_status"
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
            stage_count_text = stage_count.read_text().strip()
            sleep_count_text = sleep_count.read_text().strip()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=0 samba=ok\n", proc.stdout)
        self.assertEqual(stage_count_text, "2")
        self.assertEqual(sleep_count_text, "2")
        self.assertFalse(stale_file.exists())
        self.assertIn(
            "stage:1\nstop smbd\nstop discoveryd\nstop wcifsfs\nstop wcifsnd\n"
            "stop legacy mdns advertiser\nstop legacy nbns advertiser\nsleep:1\nstage:2\n",
            events_text,
        )
        self.assertIn("launched\n", events_text)
        self.assertIn("manager Samba runtime file staging failed status=1; resetting RAM runtime before next manager pass", log_text)
        self.assertIn("manager Samba staging recovery: RAM runtime reset complete", log_text)
        self.assertIn("manager Samba runtime file staging will retry on next manager pass after RAM runtime reset", log_text)
        self.assertIn("manager Samba runtime file staging complete", log_text)

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
                        SMB_FRUIT_MODEL=TimeCapsule6,106
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
                    tc_probe_smb_bind_interfaces() { TC_SMB_BIND_PROBE_TOKENS=127.0.0.1/8; TC_SMB_BIND_STATUS=validated; TC_SMB_BIND_REASON=; TC_SMB_BIND_POLICY="policy 1 0"; }
                    runtime_process_present_by_ucomm() {
                        case "$1" in
                            smbd) return 1 ;;
                            discoveryd) return 0 ;;
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
        self.assertIn("status=1 bind=changed samba=failed\n", proc.stdout)
        self.assertIn("manager Samba: smbd runtime apply failed reason=restart_failed; will retry on next reconciliation pass", log_text)
        self.assertIn("samba=failed bind=changed", log_text)
        self.assertNotIn("Samba bind discovery deferred; no usable address has appeared yet", log_text)
        self.assertNotIn("bind=deferred_no_ip", log_text)

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
                            discoveryd) return 0 ;;
                            *) return 1 ;;
                        esac
                    }
                    stop_runtime_process_by_ucomm() { :; }
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
                    tc_probe_smb_bind_interfaces() {{ TC_SMB_BIND_PROBE_TOKENS=127.0.0.1/8; TC_SMB_BIND_STATUS=validated; TC_SMB_BIND_REASON=; TC_SMB_BIND_POLICY="policy 1 0"; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            smbd|discoveryd) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_smbd_bound_tcp_445() {{ return 0; }}
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
                    tc_probe_smb_bind_interfaces() {{ TC_SMB_BIND_PROBE_TOKENS=127.0.0.1/8; TC_SMB_BIND_STATUS=validated; TC_SMB_BIND_REASON=; TC_SMB_BIND_POLICY="policy 1 0"; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            smbd|discoveryd) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_smbd_bound_tcp_445() {{ return 0; }}
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
                            discoveryd) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
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
                    tc_probe_smb_bind_interfaces() {{ TC_SMB_BIND_PROBE_TOKENS=127.0.0.1/8; TC_SMB_BIND_STATUS=validated; TC_SMB_BIND_REASON=; TC_SMB_BIND_POLICY="policy 1 0"; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            smbd) [ -f {shlex.quote(str(smbd_seen))} ] ;;
                            discoveryd) return 0 ;;
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

    def test_common_stage_runtime_installs_executables_with_temp_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            self.write_fake_acp(tmp_path, "")
            self.write_fake_service_hash_helper(flash)
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            (payload / "smbd").write_text("payload smbd\n")
            (payload / "smbd").chmod(0o755)
            service_source = flash.parent / "Memory/samba4/sbin/service"
            (payload / "service").write_text(service_source.read_text() if service_source.exists() else "#!/bin/sh\nexit 0\n")
            (payload / "service").chmod(0o755)
            (payload / "telemetry").write_text("#!/bin/sh\necho telemetry-ok\n")
            (payload / "telemetry").chmod(0o755)
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
                    /bin/rm -rf {payload}
                    "$TC_TELEMETRY_BIN" --version
                    printf 'hash-after-disk-removal='
                    "$TC_SERVICE_BIN" --print-device-nt-hash
                    printf 'dest='
                    /bin/cat "$TC_SMBD_BIN"
                    printf 'smbpasswd='
                    /bin/cat "$RAM_PRIVATE/smbpasswd"
                    printf 'username_map='
                    /bin/cat "$RAM_PRIVATE/username.map"
                    /bin/cat {shlex.quote(str(events))}
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dest=payload smbd\n", proc.stdout)
        self.assertIn("telemetry-ok\n", proc.stdout)
        self.assertIn("hash-after-disk-removal=0123456789ABCDEF0123456789ABCDEF\n", proc.stdout)
        self.assertRegex(
            proc.stdout,
            rf"root:0:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX:0123456789ABCDEF0123456789ABCDEF:\[U          \]:LCT-[0-9A-F]+:",
        )
        self.assertIn("username_map=!root = root\nroot = *\n", proc.stdout)
        self.assertRegex(
            proc.stdout,
            rf"cp:{payload}/smbd:{memory}/samba4/sbin/smbd\.tmp\.[0-9]+",
        )
        self.assertRegex(
            proc.stdout,
            rf"mv:{memory}/samba4/sbin/smbd\.tmp\.[0-9]+:{memory}/samba4/sbin/smbd",
        )
        self.assertNotIn(f"cp:{payload}/smbd:{memory}/samba4/sbin/smbd\n", proc.stdout)

    def test_common_stage_runtime_logs_executable_copy_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            self.write_fake_acp(tmp_path, "")
            self.write_fake_service_hash_helper(flash)
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            (payload / "smbd").write_text("payload smbd\n")
            (payload / "smbd").chmod(0o755)
            service_source = flash.parent / "Memory/samba4/sbin/service"
            (payload / "service").write_text(service_source.read_text() if service_source.exists() else "#!/bin/sh\nexit 0\n")
            (payload / "service").chmod(0o755)
            (payload / "telemetry").write_text("#!/bin/sh\necho telemetry-ok\n")
            (payload / "telemetry").chmod(0o755)
            script = tmp_path / "stage-runtime-copy-failure.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_prepare_ram_root
                    tc_set_log "$RAM_VAR/test.log" test
                    cp() {{
                        case "$1" in
                            {payload}/smbd) return 7 ;;
                        esac
                        /bin/cp "$1" "$2"
                    }}
                    if tc_stage_runtime {payload} {payload}/smbd ""; then
                        echo unexpected-success
                    else
                        echo "status=$?"
                    fi
                    /bin/cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=7\n", proc.stdout)
        self.assertIn(
            f"Samba runtime staging failed: copy executable failed: {payload}/smbd -> {memory}/samba4/sbin/smbd.tmp.",
            proc.stdout,
        )
        self.assertIn("status=7", proc.stdout)
        self.assertIn("runtime storage diagnostic:", proc.stdout)
        self.assertEqual(list((memory / "samba4/sbin").glob("smbd.tmp.*")), [])

    def test_common_stage_runtime_logs_hash_helper_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            self.write_fake_acp(tmp_path, "")
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            (payload / "smbd").write_text("payload smbd\n")
            (payload / "smbd").chmod(0o755)
            service_source = flash.parent / "Memory/samba4/sbin/service"
            (payload / "service").write_text(service_source.read_text() if service_source.exists() else "#!/bin/sh\nexit 0\n")
            (payload / "service").chmod(0o755)
            (payload / "telemetry").write_text("#!/bin/sh\necho telemetry-ok\n")
            (payload / "telemetry").chmod(0o755)
            (payload / "service").write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = '--print-device-nt-hash' ]; then cat >/dev/null; exit 8; fi\n"
                "exit 0\n"
            )
            (payload / "service").chmod(0o755)
            script = tmp_path / "stage-runtime-hash-helper-failure.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_prepare_ram_root
                    tc_set_log "$RAM_VAR/test.log" test
                    if tc_stage_runtime {payload} {payload}/smbd ""; then
                        echo unexpected-success
                    else
                        echo "status=$?"
                    fi
                    /bin/cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=8\n", proc.stdout)
        self.assertIn(
            "Samba runtime staging failed: device NT hash generation failed status=8",
            proc.stdout,
        )

    def test_common_stage_runtime_logs_acp_password_read_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            self.write_fake_service_hash_helper(flash)
            (tmp_path / "acp").write_text(
                "#!/bin/sh\n"
                "if [ \"$1:$2\" = '-q:syPW' ]; then exit 6; fi\n"
                "exit 1\n"
            )
            (tmp_path / "acp").chmod(0o755)
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            (payload / "smbd").write_text("payload smbd\n")
            (payload / "smbd").chmod(0o755)
            service_source = flash.parent / "Memory/samba4/sbin/service"
            (payload / "service").write_text(service_source.read_text() if service_source.exists() else "#!/bin/sh\nexit 0\n")
            (payload / "service").chmod(0o755)
            (payload / "telemetry").write_text("#!/bin/sh\necho telemetry-ok\n")
            (payload / "telemetry").chmod(0o755)
            (payload / "service").write_text("#!/bin/sh\nexit 6\n")
            script = tmp_path / "stage-runtime-acp-failure.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_prepare_ram_root
                    tc_set_log "$RAM_VAR/test.log" test
                    if tc_stage_runtime {payload} {payload}/smbd ""; then
                        echo unexpected-success
                    else
                        echo "status=$?"
                    fi
                    /bin/cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=6\n", proc.stdout)
        self.assertIn("Samba runtime staging failed: device NT hash generation failed status=6", proc.stdout)

    def test_common_stage_runtime_logs_invalid_hash_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            self.write_fake_acp(tmp_path, "")
            self.write_fake_service_hash_helper(flash, nt_hash="not-a-valid-hash")
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            (payload / "smbd").write_text("payload smbd\n")
            (payload / "smbd").chmod(0o755)
            service_source = flash.parent / "Memory/samba4/sbin/service"
            (payload / "service").write_text(service_source.read_text() if service_source.exists() else "#!/bin/sh\nexit 0\n")
            (payload / "service").chmod(0o755)
            (payload / "telemetry").write_text("#!/bin/sh\necho telemetry-ok\n")
            (payload / "telemetry").chmod(0o755)
            script = tmp_path / "stage-runtime-invalid-hash.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_prepare_ram_root
                    tc_set_log "$RAM_VAR/test.log" test
                    if tc_stage_runtime {payload} {payload}/smbd ""; then
                        echo unexpected-success
                    else
                        echo "status=$?"
                    fi
                    printf 'smbpasswd_exists=%s\\n' "$([ -f {memory}/samba4/private/smbpasswd ] && echo yes || echo no)"
                    /bin/cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=1\n", proc.stdout)
        self.assertIn("smbpasswd_exists=no\n", proc.stdout)
        self.assertIn("Samba runtime staging failed: generated NT hash had invalid shape", proc.stdout)

    def test_common_stage_runtime_logs_private_chmod_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            self.write_fake_acp(tmp_path, "")
            self.write_fake_service_hash_helper(flash)
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            (payload / "smbd").write_text("payload smbd\n")
            (payload / "smbd").chmod(0o755)
            service_source = flash.parent / "Memory/samba4/sbin/service"
            (payload / "service").write_text(service_source.read_text() if service_source.exists() else "#!/bin/sh\nexit 0\n")
            (payload / "service").chmod(0o755)
            (payload / "telemetry").write_text("#!/bin/sh\necho telemetry-ok\n")
            (payload / "telemetry").chmod(0o755)
            script = tmp_path / "stage-runtime-private-chmod-failure.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_prepare_ram_root
                    tc_set_log "$RAM_VAR/test.log" test
                    chmod() {{
                        case "$*" in
                            600*) return 9 ;;
                        esac
                        /bin/chmod "$@"
                    }}
                    if tc_stage_runtime {payload} {payload}/smbd ""; then
                        echo unexpected-success
                    else
                        echo "status=$?"
                    fi
                    /bin/cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=9\n", proc.stdout)
        self.assertIn(
            f"Samba runtime staging failed: chmod smbpasswd temp failed: {memory}/samba4/private/smbpasswd.tmp.",
            proc.stdout,
        )
        self.assertIn("status=9", proc.stdout)
        self.assertIn("runtime storage diagnostic:", proc.stdout)

    def test_common_stage_runtime_logs_smbpasswd_rename_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            self.write_fake_acp(tmp_path, "")
            self.write_fake_service_hash_helper(flash)
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            (payload / "smbd").write_text("payload smbd\n")
            (payload / "smbd").chmod(0o755)
            service_source = flash.parent / "Memory/samba4/sbin/service"
            (payload / "service").write_text(service_source.read_text() if service_source.exists() else "#!/bin/sh\nexit 0\n")
            (payload / "service").chmod(0o755)
            (payload / "telemetry").write_text("#!/bin/sh\necho telemetry-ok\n")
            (payload / "telemetry").chmod(0o755)
            script = tmp_path / "stage-runtime-smbpasswd-rename-failure.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_prepare_ram_root
                    tc_set_log "$RAM_VAR/test.log" test
                    mv() {{
                        case "$1:$2" in
                            {memory}/samba4/private/smbpasswd.tmp.*:{memory}/samba4/private/smbpasswd) return 10 ;;
                        esac
                        /bin/mv "$1" "$2"
                    }}
                    if tc_stage_runtime {payload} {payload}/smbd ""; then
                        echo unexpected-success
                    else
                        echo "status=$?"
                    fi
                    /bin/cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=10\n", proc.stdout)
        self.assertIn(
            f"Samba runtime staging failed: rename smbpasswd temp failed: {memory}/samba4/private/smbpasswd.tmp.",
            proc.stdout,
        )
        self.assertIn("status=10", proc.stdout)
        self.assertIn("runtime storage diagnostic:", proc.stdout)

    def test_common_generate_smb_conf_propagates_identity_failure_in_conditional_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "Data" / ".samba4"
            payload.mkdir(parents=True)
            script = tmp_path / "smb-conf-identity-failure.sh"
            share_rows = f"Data\t{volumes}/Data\tdk2\t1\t12345678-1234-1234-1234-123456789012"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_prepare_ram_root
                    tc_set_log "$RAM_VAR/test.log" test
                    TC_SMB_BIND_INTERFACES="192.168.1.2/24"
                    SMB_NETBIOS_NAME=TimeCapsule
                    SMB_SERVER_STRING=TimeCapsule
                    tc_ensure_runtime_identity() {{
                        echo identity-failed
                        return 1
                    }}
                    if ! tc_generate_smb_conf_from_share_rows {payload} {shlex.quote(share_rows)}; then
                        echo status=failed
                    else
                        echo status=unexpected-success
                    fi
                    if [ -f "$TC_SMBD_CONF" ]; then
                        echo conf=present
                    else
                        echo conf=absent
                    fi
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("identity-failed\n", proc.stdout)
        self.assertIn("status=failed\n", proc.stdout)
        self.assertIn("conf=absent\n", proc.stdout)

    def test_common_smb_bind_probe_rejects_invalid_cidr_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            (memory / "samba4/sbin").mkdir(parents=True, exist_ok=True)
            (memory / "samba4/sbin/service").write_text("#!/bin/sh\necho '192.168.1.40 bad/value'\necho status=validated\n")
            (memory / "samba4/sbin/service").chmod(0o755)
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
            self.write_fake_service_hash_helper(flash)
            payload = volumes / "dk5/.samba4"
            (payload / "private").mkdir(parents=True)
            (payload / "smbd").write_text("#!/bin/sh\nexit 0\n")
            (payload / "smbd").chmod(0o755)
            service_source = flash.parent / "Memory/samba4/sbin/service"
            (payload / "service").write_text(service_source.read_text() if service_source.exists() else "#!/bin/sh\nexit 0\n")
            (payload / "service").chmod(0o755)
            (payload / "telemetry").write_text("#!/bin/sh\necho telemetry-ok\n")
            (payload / "telemetry").chmod(0o755)
            (payload / "rsync").write_text("#!/bin/sh\nexit 0\n")
            (payload / "rsync").chmod(0o755)
            (payload / "rsyncd.conf").write_text("[shareroot]\n")
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
                        SMB_FRUIT_MODEL=TimeCapsule6,106
                    TC_RUNTIME_IDENTITY_READY=1
                    }}
                    tc_wake_or_mount_volume() {{ return 0; }}
                    is_volume_root_mounted() {{ return 0; }}
                    tc_apply_ata_drive_setting() {{ :; }}
                    tc_probe_smb_bind_interfaces() {{ TC_SMB_BIND_PROBE_TOKENS=127.0.0.1/8; TC_SMB_BIND_STATUS=validated; TC_SMB_BIND_REASON=; TC_SMB_BIND_POLICY="policy 1 0"; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            discoveryd) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    wait_for_process() {{ return 0; }}
                    tc_wait_for_smbd_ipv4_445() {{ return 0; }}
                    tc_smbd_bound_tcp_445() {{ return 0; }}
                    tc_manager_wait_for_nbns_ready() {{ return 0; }}
                    stop_runtime_process_by_ucomm() {{ :; }}
                    sleep() {{
                        case "$1" in
                            1|5) return 0 ;;
                            10)
                                printf 'payload=%s|%s|%s\\n' "$TC_PAYLOAD_DIR" "$TC_PAYLOAD_VOLUME" "$TC_PAYLOAD_DEVICE"
                                printf 'shares\\n%s\\n' "$manager_share_rows"
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
        self.assertIn("fruit:metadata = netatalk", proc.stdout)
        self.assertNotIn("fruit:time_capsule_native_metadata", proc.stdout)
        self.assertIn("restrict anonymous = 2", proc.stdout)
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
        self.assertNotIn("smb2 max read", proc.stdout)
        self.assertNotIn("smb2 max write", proc.stdout)
        self.assertNotIn("smb2 max credits", proc.stdout)
        self.assertIn("aio read size = 0", proc.stdout)
        self.assertIn("aio write size = 0", proc.stdout)
        self.assertNotIn("strict sync", proc.stdout)
        self.assertEqual(
            proc.stdout.count("vfs objects = catia fruit streams_xattr acl_xattr xattr_tdb"),
            2,
        )
        self.assertNotIn("aio_fork:max_children", proc.stdout)
        self.assertIn("deadtime = 720", proc.stdout)
        self.assertIn("smb3 directory leases = no", proc.stdout)
        self.assertIn("max smbd processes = 8", proc.stdout)
        self.assertNotIn("log level = 10", proc.stdout)
        self.assertNotIn("server signing = disabled", proc.stdout)
        self.assertNotIn("server smb encrypt = off", proc.stdout)
        self.assertTrue(smbd_core_dir_exists)
        self.assertEqual(smbd_core_parent_mode, 0o700)
        self.assertEqual(smbd_core_dir_mode, 0o700)

    def test_common_generate_smb_conf_enables_bounded_aio_fork_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            script = tmp_path / "smb-conf-aio-fork.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    VFS_AIO_FORK_ENABLED=1
                    tc_init_runtime_env
                    mkdir -p "$RAM_ETC" "$RAM_VAR"
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    share_rows=$(cat <<'EOF'
                    Data\t{volumes}/dk2/ShareRoot\tdk2\t1\taaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    USB\t{volumes}/dk3\tdk3\t0\tbbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
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
        self.assertEqual(proc.stdout.count("smb2 max read = 131072"), 1)
        self.assertEqual(proc.stdout.count("smb2 max write = 131072"), 1)
        self.assertEqual(proc.stdout.count("aio read size = 1"), 1)
        self.assertEqual(proc.stdout.count("aio write size = 1"), 1)
        self.assertNotIn("aio read size = 0", proc.stdout)
        self.assertNotIn("aio write size = 0", proc.stdout)
        self.assertEqual(
            proc.stdout.count("vfs objects = catia fruit streams_xattr acl_xattr xattr_tdb aio_fork"),
            2,
        )
        self.assertEqual(proc.stdout.count("aio_fork:max_children = 8"), 2)
        self.assertNotIn("smb2 max credits", proc.stdout)
        self.assertNotIn("strict sync", proc.stdout)

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

    def test_common_generate_smb_conf_requires_smb3_encryption_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            script = tmp_path / "smb-conf-require-encryption.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    ANY_PROTOCOL=0
                    REQUIRE_SMB_ENCRYPTION=1
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
        self.assertIn("server smb encrypt = required", proc.stdout)
        self.assertIn("server min protocol = SMB3_00", proc.stdout)
        self.assertIn("server max protocol = SMB3", proc.stdout)
        self.assertNotIn("min protocol = SMB2", proc.stdout)

    def test_common_generate_smb_conf_forces_signing_and_encryption_off_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            script = tmp_path / "smb-conf-disable-security.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION=1
                    tc_init_runtime_env
                    mkdir -p "$RAM_ETC" "$RAM_VAR"
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    share_rows=$(cat <<'EOF'
                    Data\t{volumes}/dk2/ShareRoot\tdk2\t1\taaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
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
        self.assertIn("server signing = disabled", proc.stdout)
        self.assertIn("server smb encrypt = off", proc.stdout)
        self.assertIn("min protocol = SMB2", proc.stdout)
        self.assertIn("max protocol = SMB3", proc.stdout)
        self.assertNotIn("server smb encrypt = required", proc.stdout)

    def test_common_generate_smb_conf_uses_browse_compatibility_restrict_anonymous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            script = tmp_path / "smb-conf-browse-compatibility.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    SMB_BROWSE_COMPATIBILITY=1
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
        self.assertIn("restrict anonymous = 0", proc.stdout)
        self.assertIn("map to guest = Never", proc.stdout)
        self.assertIn("null passwords = no", proc.stdout)

    def test_common_generate_smb_conf_uses_netatalk_metadata_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            script = tmp_path / "smb-conf-netatalk-metadata.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    FRUIT_METADATA_NETATALK=1
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
        self.assertIn("fruit:metadata = netatalk", proc.stdout)
        self.assertNotIn("fruit:metadata = stream", proc.stdout)
        self.assertNotIn("fruit:time_capsule_native_metadata", proc.stdout)

    def test_common_generate_smb_conf_uses_stream_metadata_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            script = tmp_path / "smb-conf-stream-metadata.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    FRUIT_METADATA_NETATALK=0
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
        self.assertIn("fruit:metadata = stream", proc.stdout)
        self.assertNotIn("fruit:metadata = netatalk", proc.stdout)
        self.assertNotIn("fruit:time_capsule_native_metadata", proc.stdout)

    def test_common_generate_smb_conf_uses_native_observed_fruit_model(self) -> None:
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
                    SMB_FRUIT_MODEL=TimeCapsule8,119
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

    def test_common_generate_smb_conf_uses_native_fallback_fruit_model(self) -> None:
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
                    SMB_FRUIT_MODEL=MacSamba
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
        self.assertIn("log level = 10", proc.stdout)

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
                    102 S discoveryd discoveryd
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
                                echo "root discoveryd 102 10 internet dgram udp 0x0 *:5353"
                                echo "root discoveryd 102 11 internet6 dgram udp 0x0 [*]:5353"
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
                    tc_process_bound_ipv4_udp_port "$DISCOVERY_PROC_NAME" 5353 || status=$?
                    echo "mdns4=$status"
                    status=0
                    tc_process_bound_ipv6_udp_port "$DISCOVERY_PROC_NAME" 5353 || status=$?
                    echo "mdns6=$status"
                    status=0
                    tc_process_bound_ipv4_udp_port "$DISCOVERY_PROC_NAME" 9999 || status=$?
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

    def test_common_discovery_cleanup_stops_native_and_legacy_conflicts_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "discovery-cleanup.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    stop_runtime_process_by_ucomm() {{ echo "$1:$2"; }}
                    stop_discovery_conflicts
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "wcifsfs:wcifsfs\nwcifsnd:wcifsnd\n"
            "legacy mdns advertiser:mdns-advertiser\n"
            "legacy nbns advertiser:nbns-advertiser\n",
        )

    def test_common_manager_disables_rsync_by_stopping_process_and_removing_ram_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            runtime_root = memory / "samba4"
            (runtime_root / "sbin").mkdir(parents=True, exist_ok=True)
            (runtime_root / "etc").mkdir(parents=True)
            (runtime_root / "sbin/rsync").write_text("stale\n")
            (runtime_root / "etc/rsyncd.conf").write_text("stale\n")
            script = tmp_path / "manager-rsync-disabled.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    RSYNC_ENABLED=0
                    tc_init_runtime_env
                    runtime_process_present_by_ucomm() {{ [ "$1" = "$RSYNC_PROC_NAME" ]; }}
                    stop_runtime_process_by_ucomm() {{ printf 'stop:%s:%s\\n' "$1" "$2"; }}
                    tc_manager_reconcile_rsync
                    [ ! -e "$TC_RSYNC_BIN" ]
                    [ ! -e "$TC_RSYNC_CONF" ]
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "stop:rsync:rsync\n")

    def test_common_manager_stages_and_starts_enabled_rsync_without_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            payload.mkdir(parents=True)
            started_args = tmp_path / "rsync-args.txt"
            (payload / "rsync").write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$@\" >{shlex.quote(str(started_args))}\n"
            )
            (payload / "rsync").chmod(0o755)
            (payload / "rsyncd.conf").write_text("[shareroot]\npath = /Volumes/dk2/ShareRoot\n")
            script = tmp_path / "manager-rsync-enabled.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    RSYNC_ENABLED=1
                    tc_init_runtime_env
                    mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_VAR"
                    tc_manager_select_current_payload() {{
                        manager_payload_dir={shlex.quote(str(payload))}
                        manager_payload_volume={shlex.quote(str(volumes / 'dk9'))}
                        manager_payload_device=/dev/dk9
                        return 0
                    }}
                    is_volume_root_mounted() {{ return 0; }}
                    runtime_process_present_by_ucomm() {{ return 1; }}
                    tc_manager_file_metadata_signature() {{ printf 'file:%s\\n' "$1"; }}
                    tc_wait_for_rsync_ready() {{ return 0; }}
                    tc_manager_reconcile_rsync
                    wait
                    [ -x "$TC_RSYNC_BIN" ]
                    [ -r "$TC_RSYNC_CONF" ]
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            started_arg_lines = started_args.read_text().splitlines() if started_args.exists() else []
            staged_config_path = memory / "samba4/etc/rsyncd.conf"
            staged_config = staged_config_path.read_text() if staged_config_path.exists() else ""
            pid_files = list((memory / "samba4").rglob("*.pid"))

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            started_arg_lines,
            ["--daemon", "--no-detach", f"--config={memory}/samba4/etc/rsyncd.conf"],
        )
        self.assertEqual(staged_config, f"[shareroot]\npath = {volumes}/dk9/ShareRoot\n")
        self.assertEqual(pid_files, [])

    def test_common_manager_stops_daemon_before_bounding_rsync_log_and_restarts_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            payload.mkdir(parents=True)
            (payload / "rsync").write_text("#!/bin/sh\nexit 0\n")
            (payload / "rsync").chmod(0o755)
            (payload / "rsyncd.conf").write_text("[shareroot]\npath = /Volumes/dk2/ShareRoot\n")
            runtime_root = memory / "samba4"
            (runtime_root / "sbin").mkdir(parents=True, exist_ok=True)
            (runtime_root / "etc").mkdir(parents=True)
            (runtime_root / "var").mkdir(parents=True)
            (runtime_root / "sbin/rsync").write_text("#!/bin/sh\nexit 0\n")
            (runtime_root / "sbin/rsync").chmod(0o755)
            (runtime_root / "etc/rsyncd.conf").write_text("[shareroot]\npath = /Volumes/dk2/ShareRoot\n")
            rsync_log = runtime_root / "var/rsync.log"
            rsync_log.write_text("x" * 65536)
            stop_calls = tmp_path / "stop-calls.txt"
            wait_calls = tmp_path / "wait-calls.txt"
            script = tmp_path / "manager-rsync-bound-log.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    RSYNC_ENABLED=1
                    tc_init_runtime_env
                    tc_manager_select_current_payload() {{
                        manager_payload_dir={shlex.quote(str(payload))}
                        manager_payload_volume={shlex.quote(str(volumes / 'dk2'))}
                        manager_payload_device=/dev/dk2
                        return 0
                    }}
                    is_volume_root_mounted() {{ return 0; }}
                    rsync_running=1
                    runtime_process_present_by_ucomm() {{
                        [ "$1" = "$RSYNC_PROC_NAME" ] && [ "$rsync_running" = "1" ]
                    }}
                    tc_stop_rsync_if_running() {{
                        printf 'stop\\n' >>{shlex.quote(str(stop_calls))}
                        rsync_running=0
                    }}
                    tc_rsync_bound_tcp_873() {{ return 0; }}
                    tc_wait_for_rsync_ready() {{
                        printf 'wait\\n' >>{shlex.quote(str(wait_calls))}
                        return 0
                    }}
                    tc_manager_file_metadata_signature() {{ printf 'file:%s\\n' "$1"; }}
                    TC_MANAGER_LAST_RSYNC_SIGNATURE=$(tc_manager_rsync_file_signature \
                        {shlex.quote(str(payload))} \
                        {shlex.quote(str(payload / 'rsync'))} \
                        {shlex.quote(str(payload / 'rsyncd.conf'))})
                    tc_manager_reconcile_rsync
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            bounded_log_size = rsync_log.stat().st_size
            recorded_stop_calls = stop_calls.read_text().splitlines()
            recorded_wait_calls = wait_calls.read_text().splitlines()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLessEqual(bounded_log_size, 32768)
        self.assertEqual(recorded_stop_calls, ["stop"])
        self.assertEqual(recorded_wait_calls, ["wait"])

    def test_common_discovery_launch_uses_single_controller_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            marker = tmp_path / "mdns.started"
            (flash / "discoveryd").write_text(
                "#!/bin/sh\n"
                "printf 'mdns-args:%s\\n' \"$*\"\n"
                f"echo started >{shlex.quote(str(marker))}\n"
                "exit 0\n"
            )
            (flash / "discoveryd").chmod(0o755)
            script = tmp_path / "mdns-generated-single-call.sh"
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
                    tc_ensure_runtime_identity() {{ SMB_NETBIOS_NAME=TIMECAPSULE; TC_RUNTIME_IDENTITY_READY=1; }}
                    runtime_process_present_by_ucomm() {{ return 1; }}
                    stop_discovery_conflicts() {{ return 0; }}
                    tc_launch_discovery "discovery test" 1 0
                    wait "$TC_DISCOVERY_PID" || true
                    [ -f {shlex.quote(str(marker))} ] || exit 99
                    cat "$TC_DISCOVERY_LOG_FILE"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("launching discovery\n", proc.stdout)
        # The registrant needs no identity, airport or auto-ip arguments.
        self.assertIn("mdns-args:--netbios-name TIMECAPSULE\n", proc.stdout)
        self.assertNotIn("--afp", proc.stdout)
        self.assertNotIn("--auto-ip", proc.stdout)
        self.assertNotIn("--instance", proc.stdout)

    def test_common_discovery_diskless_start_omits_name_and_share_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            args_file = tmp_path / "mdns-args.txt"
            (flash / "discoveryd").write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >{shlex.quote(str(args_file))}\n"
                "exit 0\n"
            )
            (flash / "discoveryd").chmod(0o755)
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
                    stale_shares=$(printf 'Stale\\t/Volumes/dk2\\tdk2\\t1\\taaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa')
                    tc_ensure_runtime_identity() {{
                        MDNS_INSTANCE_NAME=Diskless
                        MDNS_HOST_LABEL=diskless
                        MDNS_DEVICE_MODEL=TimeCapsule
                        SMB_FRUIT_MODEL=TimeCapsule6,106
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
                    stop_discovery_conflicts() {{ return 0; }}
                    tc_launch_discovery "discovery startup" 1 0 1 0 "$stale_shares"
                    wait "$TC_DISCOVERY_PID" || true
                    cat {shlex.quote(str(args_file))}
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--diskless", proc.stdout)
        self.assertNotIn("--auto-ip", proc.stdout)
        self.assertNotIn("--afp", proc.stdout)
        self.assertNotIn("--adisk-share", proc.stdout)
        self.assertNotIn("--debug-logging", proc.stdout)
        self.assertIn("discovery startup: starting discovery controller in diskless mode", proc.stdout)
        self.assertNotIn("--netbios-name", proc.stdout)

    def test_common_discovery_passes_debug_logging_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            args_file = tmp_path / "mdns-args.txt"
            (flash / "discoveryd").write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >{shlex.quote(str(args_file))}\n"
                "exit 0\n"
            )
            (flash / "discoveryd").chmod(0o755)
            script = tmp_path / "mdns-debug-logging.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    MDNS_DEBUG_LOGGING=1
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR"
                    tc_ensure_runtime_identity() {{
                        MDNS_INSTANCE_NAME=Debug
                        MDNS_HOST_LABEL=debug
                        MDNS_DEVICE_MODEL=TimeCapsule
                        SMB_FRUIT_MODEL=TimeCapsule6,106
                    SMB_NETBIOS_NAME=TIMECAPSULE
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
                    stop_runtime_process_by_ucomm() {{ :; }}
                    stop_discovery_conflicts() {{ :; }}
                    tc_launch_discovery "discovery startup" 1 0 0
                    wait "$TC_DISCOVERY_PID" || true
                    cat {shlex.quote(str(args_file))}
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--debug-logging", proc.stdout)
        self.assertNotIn("--auto-ip", proc.stdout)
        self.assertNotIn("--afp", proc.stdout)
        self.assertIn("discovery startup: debug logging enabled at", proc.stdout)

    def test_common_discovery_writes_one_payload_log_in_normal_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            payload.mkdir(parents=True)
            (flash / "discoveryd").write_text("#!/bin/sh\nprintf 'discovery-args:%s\\n' \"$*\"\necho discovery-stdout\necho discovery-stderr >&2\n")
            (flash / "discoveryd").chmod(0o755)
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
                    tc_select_live_iface_mac() {{ echo 02:00:00:00:00:01; }}
                    mkdir -p "$RAM_VAR"
                    is_volume_root_mounted() {{ [ "$1" = "{volumes}/dk2" ]; }}
                    get_radio_mac() {{
                        case "$1" in
                            bwl0) echo 80:EA:96:EB:2E:7D ;;
                            bwl1) echo 80:EA:96:EB:2E:7C ;;
                            *) return 1 ;;
                        esac
                    }}
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
                    tc_ensure_runtime_identity() {{ SMB_NETBIOS_NAME=TIMECAPSULE; TC_RUNTIME_IDENTITY_READY=1; }}
                    stop_discovery_conflicts() {{ return 0; }}
                    tc_set_payload_log_dir {payload} {volumes}/dk2
                    printf 'discovery-path=%s\\n' "$TC_DISCOVERY_LOG_FILE"
                    tc_launch_discovery "discovery test" 0 0
                    wait "$TC_DISCOVERY_PID" || true
                    cat "$TC_DISCOVERY_LOG_FILE"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("discovery-path=", proc.stdout)
        self.assertIn("/.samba4/logs/discovery.log", proc.stdout)
        self.assertIn("launching discovery\n", proc.stdout)
        self.assertNotIn("--instance ", proc.stdout)
        self.assertIn("discovery-stdout", proc.stdout)
        self.assertIn("discovery-stderr", proc.stdout)
        self.assertNotIn("--auto-ip", proc.stdout)

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

    def test_common_discovery_launch_passes_canonical_name_and_adisk_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            args_file = tmp_path / "discovery.args"
            (flash / "discoveryd").write_text(
                "#!/bin/sh\n" + f"printf '%s\\n' \"$*\" >{shlex.quote(str(args_file))}\n"
            )
            (flash / "discoveryd").chmod(0o755)
            script = tmp_path / "discovery-launch.sh"
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
                    tc_ensure_runtime_identity() {{ SMB_NETBIOS_NAME=TIMECAPSULE; TC_RUNTIME_IDENTITY_READY=1; }}
                    stop_discovery_conflicts() {{ return 0; }}
                    shares=$(printf 'Data\\t/Volumes/dk2\\tdk2\\t1\\t12345678-1234-1234-1234-123456789abc')
                    tc_launch_discovery "test discovery" 1 0 0 1 "$shares"
                    wait "$TC_DISCOVERY_PID"
                    cat {shlex.quote(str(args_file))}
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--netbios-name TIMECAPSULE", proc.stdout)
        self.assertIn("--adisk-share Data dk2 12345678-1234-1234-1234-123456789abc 0x82", proc.stdout)
        self.assertIn("--debug-logging", proc.stdout)

    def test_manager_absent_discovery_schedules_immediate_service_recovery(self) -> None:
        manager = load_boot_asset_text("manager.sh")
        function = self.extract_shell_function(manager, "tc_manager_reconcile_discovery_ownership")
        script = function + textwrap.dedent(
            """\

            DISCOVERY_PROC_NAME=discoveryd
            TC_MANAGER_LAST_DISCOVERY_SIGNATURE=old
            manager_service_seconds_until_due=20
            runtime_process_present_by_ucomm() { return 1; }
            tc_log() { :; }
            stop_runtime_process_by_ucomm() { echo unexpected-stop; return 1; }
            stop_discovery_conflicts() { echo unexpected-cleanup; return 1; }
            tc_manager_reconcile_discovery_ownership
            printf 'signature=%s due=%s\n' "$TC_MANAGER_LAST_DISCOVERY_SIGNATURE" "$manager_service_seconds_until_due"
            """
        )

        proc = subprocess.run(["/bin/sh", "-c", script], text=True, capture_output=True, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "signature= due=0\n")

    def test_manager_cleans_orphaned_wcifsnd_before_controller_recovery(self) -> None:
        function = self.extract_shell_function(
            load_boot_asset_text("manager.sh"), "tc_manager_reconcile_discovery_ownership"
        )
        script = function + textwrap.dedent(
            """\

            DISCOVERY_PROC_NAME=discoveryd
            TC_MANAGER_LAST_DISCOVERY_SIGNATURE=old
            manager_service_seconds_until_due=20
            runtime_process_present_by_ucomm() { [ "$1" = wcifsnd ]; }
            tc_log() { :; }
            stop_runtime_process_by_ucomm() { echo "stop:$1"; }
            stop_discovery_conflicts() { echo unexpected-cleanup; return 1; }
            tc_manager_reconcile_discovery_ownership
            printf 'signature=%s due=%s\n' "$TC_MANAGER_LAST_DISCOVERY_SIGNATURE" "$manager_service_seconds_until_due"
            """
        )

        proc = subprocess.run(["/bin/sh", "-c", script], text=True, capture_output=True, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "stop:wcifsnd\nsignature= due=0\n")

    def test_manager_wcifsfs_reappearance_resets_owned_generation(self) -> None:
        function = self.extract_shell_function(
            load_boot_asset_text("manager.sh"), "tc_manager_reconcile_discovery_ownership"
        )
        script = function + textwrap.dedent(
            """\

            DISCOVERY_PROC_NAME=discoveryd
            TC_MANAGER_LAST_DISCOVERY_SIGNATURE=old
            manager_service_seconds_until_due=20
            runtime_process_present_by_ucomm() { [ "$1" = wcifsfs ] || [ "$1" = discoveryd ]; }
            tc_log() { :; }
            stop_runtime_process_by_ucomm() { echo "stop:$1"; }
            stop_discovery_conflicts() { echo cleanup-conflicts; }
            tc_manager_reconcile_discovery_ownership
            printf 'signature=%s due=%s\n' "$TC_MANAGER_LAST_DISCOVERY_SIGNATURE" "$manager_service_seconds_until_due"
            """
        )

        proc = subprocess.run(["/bin/sh", "-c", script], text=True, capture_output=True, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "stop:discoveryd\ncleanup-conflicts\nsignature= due=0\n")


if __name__ == "__main__":
    unittest.main()
