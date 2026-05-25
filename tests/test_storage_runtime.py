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
    NO_WRITABLE_PERSISTENT_VOLUME_MESSAGE,
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
    select_payload_home_conn,
    select_payload_home_with_diagnostics_conn,
    verify_payload_home_conn,
    wait_for_mast_volumes_conn,
)
from timecapsulesmb.transport.ssh import SshConnection
from tests.storage_fixtures import EXTERNAL_BACKUP, INTERNAL_DATA, MAST_FIXTURES, SHELL_MAST_FIXTURES, MaStFixture


class StorageRuntimeTests(unittest.TestCase):
    _runtime_asset_texts: tuple[str, str, str] | None = None

    @classmethod
    def runtime_asset_texts(cls) -> tuple[str, str, str]:
        if cls._runtime_asset_texts is None:
            repo_root = Path(__file__).resolve().parent.parent
            cls._runtime_asset_texts = (
                load_boot_asset_text("common.sh"),
                (repo_root / "src/timecapsulesmb/assets/boot/samba4/start-samba.sh").read_text(),
                (repo_root / "src/timecapsulesmb/assets/boot/samba4/watchdog.sh").read_text(),
            )
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

        common, start, watchdog = self.runtime_asset_texts()
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
            start = start.replace(old, new)
            watchdog = watchdog.replace(old, new)

        (flash / "common.sh").write_text(common)
        boot_path = flash / "boot.sh"
        boot_path.write_text(boot)
        boot_path.chmod(0o755)
        manager_path = flash / "manager.sh"
        manager_path.write_text(manager)
        manager_path.chmod(0o755)
        start_path = flash / "start-samba.sh"
        start_path.write_text(start)
        start_path.chmod(0o755)
        watchdog_path = flash / "watchdog.sh"
        watchdog_path.write_text(watchdog)
        watchdog_path.chmod(0o755)
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

    def write_fixture_state_files(self, tmp_path: Path, fixture: MaStFixture, volumes_root: Path) -> tuple[Path, Path]:
        fixture_dir = tmp_path / "fixture-state"
        fixture_dir.mkdir(exist_ok=True)
        topology_path = fixture_dir / f"{fixture.name}.volumes.tsv"
        raw_path = fixture_dir / f"{fixture.name}.raw"
        topology_path.write_text(self.expected_topology_tsv(fixture, volumes_root))
        if isinstance(fixture.raw, bytes):
            raw_path.write_bytes(fixture.raw)
        else:
            raw_path.write_text(fixture.raw)
        return topology_path, raw_path

    def render_mast_wait_fixture_override(self, topology_path: Path, raw_path: Path) -> str:
        return (
            "tc_read_mast_volumes_to() { "
            f"/bin/cat {shlex.quote(str(topology_path))} >\"$1\"; "
            f"/bin/cat {shlex.quote(str(raw_path))} >\"$2\"; "
            "return 0; }; "
            "tc_wait_for_mast_volumes_to() { "
            "tc_read_mast_volumes_to \"$1\" \"$2\"; }"
        )

    def test_common_select_advertise_mac_prefers_acp_lama(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
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
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
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

    def mapped_volume_root(self, volume: MaStVolume, volumes_root: Path) -> str:
        return volume.volume_root.replace("/Volumes", str(volumes_root), 1)

    def sanitize_share_base(self, volume: MaStVolume) -> str:
        base = volume.name.strip()
        sanitized = "".join(
            "_"
            if ord(char) < 32 or char in '/\\:*?"<>|,=[]'
            else char
            for char in base
        ).strip()
        return sanitized or f"Disk {volume.partition_device}"

    def unique_share_names(self, volumes: tuple[MaStVolume, ...]) -> list[str]:
        names: list[str] = []
        used: set[str] = set()
        for volume in volumes:
            base = self.sanitize_share_base(volume)
            candidate = base
            suffix = 1
            if candidate in used:
                candidate = f"{base} ({volume.partition_device})"
            while candidate in used:
                candidate = f"{base} ({volume.partition_device}-{suffix})"
                suffix += 1
            used.add(candidate)
            names.append(candidate)
        return names

    def expected_share_state(
        self,
        fixture: MaStFixture,
        volumes_root: Path,
        *,
        internal_share_use_disk_root: bool,
    ) -> tuple[str, str]:
        share_lines: list[str] = []
        adisk_lines: list[str] = []
        for volume, share_name in zip(fixture.expected, self.unique_share_names(fixture.expected)):
            volume_root = self.mapped_volume_root(volume, volumes_root)
            if volume.builtin and not internal_share_use_disk_root:
                share_path = f"{volume_root}/ShareRoot"
            else:
                share_path = volume_root
            builtin = "1" if volume.builtin else "0"
            share_lines.append("\t".join((share_name, share_path, volume.partition_device, builtin, volume.adisk_uuid)))
            adisk_lines.append("\t".join((share_name, volume.partition_device, volume.adisk_uuid, "0x82")))
        return "\n".join(share_lines) + "\n", "\n".join(adisk_lines) + "\n"

    def run_share_state_fixture_batch(
        self,
        fixtures: tuple[MaStFixture, ...],
        tmp_path: Path,
        flash: Path,
        volumes_root: Path,
        *,
        internal_share_use_disk_root: bool,
    ) -> subprocess.CompletedProcess[str]:
        for fixture in fixtures:
            for volume in fixture.expected:
                Path(self.mapped_volume_root(volume, volumes_root)).mkdir(parents=True, exist_ok=True)
            self.write_fixture_state_files(tmp_path, fixture, volumes_root)
        override = "INTERNAL_SHARE_USE_DISK_ROOT=1" if internal_share_use_disk_root else ""
        names = " ".join(shlex.quote(fixture.name) for fixture in fixtures)
        fixture_dir = tmp_path / "fixture-state"
        script = tmp_path / "share-state-fixtures.sh"
        script.write_text(
            textwrap.dedent(
                f"""\
                #!/bin/sh
                set -eu
                . {flash}/common.sh
                . {flash}/tcapsulesmb.conf
                {override}
                tc_init_runtime_env
                tc_set_log "$RAM_VAR/test.log" test
                mkdir -p "$RAM_VAR"
                is_volume_root_mounted() {{ return 0; }}
                for fixture_name in {names}; do
                    topology_file={shlex.quote(str(fixture_dir))}/"$fixture_name.volumes.tsv"
                    set +e
                    tc_build_share_state "$topology_file"
                    status=$?
                    set -e
                    printf '__TC_BEGIN__\\t%s\\t%s\\n' "$fixture_name" "$status"
                    printf 'shares\\n'
                    cat "$TC_SHARES_TSV"
                    printf 'adisk\\n'
                    cat "$TC_ADISK_TSV"
                    printf '__TC_END__\\t%s\\n' "$fixture_name"
                done
                """
            )
        )
        script.chmod(0o755)
        return subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

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

    def test_common_waits_for_mast_volumes_to_become_available(self) -> None:
        fixture = SHELL_MAST_FIXTURES[0]
        raw_text = fixture.raw.decode("utf-8", errors="replace") if isinstance(fixture.raw, bytes) else fixture.raw
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            acp_counter = tmp_path / "acp-count"
            acp = tmp_path / "acp"
            acp.write_text(
                "#!/bin/sh\n"
                "count=0\n"
                f"if [ -f {shlex.quote(str(acp_counter))} ]; then\n"
                f"    count=$(cat {shlex.quote(str(acp_counter))})\n"
                "fi\n"
                "count=$((count + 1))\n"
                f"echo \"$count\" >{shlex.quote(str(acp_counter))}\n"
                "if [ \"$count\" -lt 3 ]; then\n"
                "    exit 1\n"
                "fi\n"
                "cat <<'OUT'\n"
                f"{raw_text}\n"
                "OUT\n"
            )
            acp.chmod(0o755)
            script = tmp_path / "wait-mast.sh"
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
                    sleep() {{ :; }}
                    tc_wait_for_mast_volumes_to "$RAM_VAR/test-volumes.tsv" "$RAM_VAR/test-mast.raw" 6
                    printf 'count=%s\\n' "$(cat {acp_counter})"
                    cat "$RAM_VAR/test-volumes.tsv"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("count=3\n", proc.stdout)
        self.assertIn(self.expected_topology_tsv(fixture, volumes).splitlines()[0], proc.stdout)
        self.assertIn("MaSt discovery not ready; waiting up to 6s for a successful MaSt read", proc.stdout)
        self.assertIn("MaSt discovery succeeded after 6s", proc.stdout)

    def test_common_waits_for_mast_volumes_times_out_after_three_second_polling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            acp_counter = tmp_path / "acp-count"
            acp = tmp_path / "acp"
            acp.write_text(
                "#!/bin/sh\n"
                "count=0\n"
                f"if [ -f {shlex.quote(str(acp_counter))} ]; then\n"
                f"    count=$(cat {shlex.quote(str(acp_counter))})\n"
                "fi\n"
                "count=$((count + 1))\n"
                f"echo \"$count\" >{shlex.quote(str(acp_counter))}\n"
                "exit 1\n"
            )
            acp.chmod(0o755)
            script = tmp_path / "wait-mast-timeout.sh"
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
                    sleep() {{ :; }}
                    tc_wait_for_mast_volumes_to "$RAM_VAR/test-volumes.tsv" "$RAM_VAR/test-mast.raw" 6 || status=$?
                    printf 'status=%s\\n' "${{status:-0}}"
                    printf 'count=%s\\n' "$(cat {acp_counter})"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=1\n", proc.stdout)
        self.assertIn("count=3\n", proc.stdout)
        self.assertIn("MaSt discovery timed out after 6s waiting for a successful MaSt read", proc.stdout)

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
                home = select_payload_home_conn(connection, (external, internal), ".samba4", wait_seconds=30)

        self.assertEqual(home, PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"))
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
                home = select_payload_home_conn(connection, (external, internal), ".samba4", wait_seconds=9)

        self.assertEqual(home, PayloadHome("/Volumes/dk3", "/dev/dk3", ".samba4"))
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

    def test_select_payload_home_fails_when_all_candidates_unmountable(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_mast_volume_mounted_conn", return_value=False):
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn") as writable_mock:
                with self.assertRaisesRegex(RuntimeError, NO_WRITABLE_PERSISTENT_VOLUME_MESSAGE):
                    select_payload_home_conn(connection, (internal, external), ".samba4", wait_seconds=30)

        writable_mock.assert_not_called()

    def test_select_payload_home_falls_back_to_external_and_fails_when_none_writable(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_mast_volume_mounted_conn", return_value=True):
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", side_effect=[False, True]):
                home = select_payload_home_conn(connection, (internal, external), ".samba4", wait_seconds=30)
        self.assertEqual(home, PayloadHome("/Volumes/dk3", "/dev/dk3", ".samba4"))

        with mock.patch("timecapsulesmb.device.storage.ensure_mast_volume_mounted_conn", return_value=True):
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", return_value=False):
                with self.assertRaisesRegex(RuntimeError, NO_WRITABLE_PERSISTENT_VOLUME_MESSAGE):
                    select_payload_home_conn(connection, (internal, external), ".samba4", wait_seconds=30)

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
                    get_iface_mac() {{ echo 80:EA:96:E6:58:68; }}
                    get_radio_mac() {{ return 1; }}
                    stop_nbns_conflicts() {{ return 0; }}
                    tc_set_payload_log_dir {payload} {volumes}/dk2
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    tc_init_runtime_identity
                    tc_generate_smb_conf {payload}
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

    def test_start_samba_signature_mode_matches_shell_supported_mast_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
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
                        tc_print_topology_signature >"$out" 2>"$err"
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
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
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

    def test_start_samba_signature_mode_handles_mast_without_final_newline(self) -> None:
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
            start_path = flash / "start-samba.sh"
            self.write_fake_acp(tmp_path, raw, final_newline=False)

            proc = subprocess.run(
                ["/bin/sh", str(start_path), "--print-topology-signature"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        fixture = MaStFixture("no_final_newline", raw, (INTERNAL_DATA,))
        expected_stdout = self.expected_topology_tsv(fixture, volumes)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, expected_stdout)

    def test_start_samba_rejects_removed_watchdog_restart_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            start_path = flash / "start-samba.sh"

            proc = subprocess.run(
                ["/bin/sh", str(start_path), "--watchdog-restart"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(proc.returncode, 2, proc.stderr)

    def test_start_samba_refresh_disk_state_mode_only_rebuilds_disk_state(self) -> None:
        fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_external_only")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            topology_path, raw_path = self.write_fixture_state_files(tmp_path, fixture, volumes)
            payload = volumes / "dk5/.samba4"
            (payload / "private").mkdir(parents=True)
            (payload / "smbd").write_text("")
            (payload / "smbd").chmod(0o755)
            (payload / "private/smbpasswd").write_text("")
            (payload / "private/username.map").write_text("")
            (volumes / "dk5").mkdir(exist_ok=True)
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                        {self.render_mast_wait_fixture_override(topology_path, raw_path)}
                        tc_mount_mast_volumes_for_boot() {{ :; }}
                        is_volume_root_mounted() {{ [ "$1" = "{volumes}/dk5" ]; }}
                        """
                    )
                )
            start_path = flash / "start-samba.sh"

            proc = subprocess.run(
                ["/bin/sh", str(start_path), "--refresh-disk-state"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            state_files_exist = (
                (memory / "samba4/var/shares.tsv").exists(),
                (memory / "samba4/var/adisk.tsv").exists(),
                (memory / "samba4/var/payload.tsv").exists(),
                (memory / "samba4/var/topology.signature").exists(),
            )
            removed_scratch_files_exist = (
                (memory / "samba4/var/volumes.tsv").exists(),
                (memory / "samba4/var/mast.raw").exists(),
                (memory / "samba4/var/share-names.txt").exists(),
            )
            runtime_files_exist = (
                (memory / "samba4/etc/smb.conf").exists(),
                (memory / "samba4/sbin/smbd").exists(),
            )
            refresh_log = (memory / "samba4/var/rc.local.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(state_files_exist, (True, True, True, True))
        self.assertEqual(removed_scratch_files_exist, (False, False, False))
        self.assertEqual(runtime_files_exist, (False, False))
        self.assertIn("managed Samba disk-state refresh beginning; services will not be restarted", refresh_log)
        self.assertIn("disk-state refresh complete: runtime state written", refresh_log)

    def test_start_samba_reload_disk_runtime_mode_refreshes_and_stages_before_services(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                        tc_cleanup_old_runtime() {{ echo cleanup; return 0; }}
                        tc_tune_kernel_memory() {{ echo tune; }}
                        tc_prepare_locks_ramdisk() {{ echo locks; return 0; }}
                        tc_prepare_legacy_prefix() {{ echo legacy; }}
                        tc_init_runtime_identity() {{ echo identity; }}
                        tc_prepare_smb_bind_context() {{ echo bind; }}
                        tc_refresh_disk_state() {{
                            echo refresh
                            mkdir -p "$RAM_VAR"
                            cat >"$TC_PAYLOAD_TSV" <<'EOF'
                        {volumes}/dk2/.samba4	{volumes}/dk2	/dev/dk2
                        EOF
                            cat >"$TC_SHARES_TSV" <<'EOF'
                        Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                        EOF
                            cat >"$TC_ADISK_TSV" <<'EOF'
                        Data	dk2	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa	0x82
                        EOF
                        }}
                        tc_stage_disk_runtime() {{ echo stage; }}
                        tc_start_smbd() {{ echo smbd; }}
                        tc_start_watchdog() {{ echo watchdog; }}
                        """
                    )
                )
            start_path = flash / "start-samba.sh"

            proc = subprocess.run(
                ["/bin/sh", str(start_path), "--reload-disk-runtime"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "\n".join(
                (
                    "cleanup",
                    "tune",
                    "locks",
                    "legacy",
                    "bind",
                    "refresh",
                    "identity",
                    "stage",
                    "smbd",
                    "watchdog",
                    "",
                )
            ),
        )

    def test_start_samba_diskless_refresh_starts_watchdog_without_smbd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        """\

                        tc_cleanup_old_runtime() { echo cleanup; return 0; }
                        tc_tune_kernel_memory() { echo tune; }
                        tc_prepare_locks_ramdisk() { echo locks; return 0; }
                        tc_prepare_legacy_prefix() { echo legacy; }
                        tc_prepare_smb_bind_context() { echo bind; }
                        tc_refresh_disk_state() {
                            echo refresh
                            TC_DISK_REFRESH_RESULT=no_payload
                            return 0
                        }
                        tc_init_runtime_identity() { echo identity; }
                        tc_stage_disk_runtime() { echo unexpected-stage; return 1; }
                        tc_start_smbd() { echo unexpected-smbd; return 1; }
                        tc_start_watchdog() { echo watchdog; }
                        """
                    )
                )
            start_path = flash / "start-samba.sh"

            proc = subprocess.run(["/bin/sh", str(start_path)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "cleanup\ntune\nlocks\nlegacy\nbind\nrefresh\nidentity\nwatchdog\n")

    def test_start_samba_cold_mast_failure_exits_without_retry_or_watchdog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        """\

                        tc_cleanup_old_runtime() { echo cleanup; return 0; }
                        tc_tune_kernel_memory() { echo tune; }
                        tc_prepare_locks_ramdisk() { echo locks; return 0; }
                        tc_prepare_legacy_prefix() { echo legacy; }
                        tc_prepare_smb_bind_context() { echo bind; }
                        sleep() { echo "unexpected sleep $1"; }
                        tc_refresh_disk_state() {
                            echo refresh-fail
                            TC_DISK_REFRESH_RESULT=mast_failed
                            return 1
                        }
                        tc_init_runtime_identity() { echo unexpected-identity; }
                        tc_start_watchdog() { echo unexpected-watchdog; }
                        """
                    )
                )
            start_path = flash / "start-samba.sh"

            proc = subprocess.run(["/bin/sh", str(start_path)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(proc.stdout, "cleanup\ntune\nlocks\nlegacy\nbind\nrefresh-fail\n")

    def test_start_samba_reload_retries_mast_failure_before_diskless_watchdog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            refresh_count = tmp_path / "refresh-count"
            with (flash / "common.sh").open("a") as common:
                common.write(
                    textwrap.dedent(
                        f"""\

                        tc_cleanup_old_runtime() {{ echo cleanup; return 0; }}
                        tc_tune_kernel_memory() {{ echo tune; }}
                        tc_prepare_locks_ramdisk() {{ echo locks; return 0; }}
                        tc_prepare_legacy_prefix() {{ echo legacy; }}
                        tc_prepare_smb_bind_context() {{ echo bind; }}
                        sleep() {{ echo "sleep $1"; }}
                        tc_refresh_disk_state() {{
                            count=$(/bin/cat {shlex.quote(str(refresh_count))} 2>/dev/null || echo 0)
                            count=$((count + 1))
                            echo "$count" >{shlex.quote(str(refresh_count))}
                            if [ "$count" -eq 1 ]; then
                                echo refresh-fail
                                TC_DISK_REFRESH_RESULT=mast_failed
                                return 1
                            fi
                            echo refresh-ready
                            TC_DISK_REFRESH_RESULT=no_payload
                            return 0
                        }}
                        tc_init_runtime_identity() {{ echo identity; }}
                        tc_start_watchdog() {{ echo watchdog; }}
                        """
                    )
                )
            start_path = flash / "start-samba.sh"

            proc = subprocess.run(
                ["/bin/sh", str(start_path), "--reload-disk-runtime"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "cleanup\ntune\nlocks\nlegacy\nbind\nrefresh-fail\nsleep 5\nrefresh-ready\nidentity\nwatchdog\n",
        )

    def test_boot_script_only_runs_one_time_boot_preparation_and_starts_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
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
                        tc_prepare_smb_bind_context() { echo unexpected-bind; return 1; }
                        tc_refresh_disk_state() { echo unexpected-disk; return 1; }
                        tc_stage_disk_runtime() { echo unexpected-stage; return 1; }
                        tc_start_smbd() { echo unexpected-smbd; return 1; }
                        tc_start_watchdog() { echo unexpected-watchdog; return 1; }
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
            log_text = (_memory / "samba4/var/rc.local.log").read_text()

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
                        tc_watchdog_reset_pass_state() {
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
                        tc_watchdog_stop_samba_lane_without_payload() { :; }
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
                    tc_watchdog_stop_samba_lane_without_payload() { :; }
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
                    tc_watchdog_stop_samba_lane_without_payload() { :; }
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
                    TC_WATCHDOG_IDENTITY_SIGNATURE_READY=0
                    TC_WATCHDOG_LAST_IDENTITY_SIGNATURE=
                    tc_watchdog_stop_samba_lane_without_payload() { :; }
                    sleep() {
                        if [ "$1" = "1" ]; then
                            return 0
                        fi
                        echo "changed=$TC_MANAGER_IDENTITY_CHANGED"
                        echo "ready=$TC_WATCHDOG_IDENTITY_SIGNATURE_READY"
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
                    tc_watchdog_reset_pass_state() { echo reset; }
                    tc_prepare_local_hostname_resolution() { :; }
                    tc_init_runtime_identity() {
                        echo identity
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }
                    tc_stage_runtime() { echo unexpected-stage; return 1; }
                    tc_watchdog_stop_samba_lane_without_payload() { echo no_payload; }
                    runtime_process_present_by_ucomm() {
                        case "$1" in
                            mdns-advertiser) return 0 ;;
                            *) return 1 ;;
                        esac
                    }
                    stop_runtime_process_by_ucomm() { :; }
                    tc_mdns_bound_udp_5353() { return 0; }
                    tc_watchdog_reconcile_nbns() { echo unexpected-nbns; return 1; }
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
                    tc_watchdog_reset_pass_state() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                        TC_RUNTIME_IDENTITY_READY=1
                    }}
                    tc_watchdog_stop_samba_lane_without_payload() {{ :; }}
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
                    tc_watchdog_reset_pass_state() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                        TC_RUNTIME_IDENTITY_READY=1
                    }}
                    tc_watchdog_stop_samba_lane_without_payload() {{ :; }}
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
                    tc_watchdog_refresh_runtime_identity_for_recovery() {{ :; }}
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
                    tc_refresh_smb_bind_interfaces() {{
                        echo refresh-bind >>{shlex.quote(str(events))}
                        TC_SMB_BIND_INTERFACES="127.0.0.1/8"
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
                    tc_watchdog_stop_samba_lane_without_payload() {{ :; }}
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
                    tc_watchdog_refresh_runtime_identity_for_recovery() {{ :; }}
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
                    tc_refresh_smb_bind_interfaces() {{
                        TC_SMB_BIND_INTERFACES="127.0.0.1/8"
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
                    tc_watchdog_refresh_runtime_identity_for_recovery() {{ :; }}
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
                    tc_watchdog_refresh_runtime_identity_for_recovery() { :; }
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
                    tc_watchdog_reset_pass_state() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }}
                    tc_watchdog_refresh_runtime_identity_for_recovery() {{ :; }}
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
                    tc_watchdog_stop_samba_lane_without_payload() {{ :; }}
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
                    tc_watchdog_reset_pass_state() { :; }
                    tc_prepare_local_hostname_resolution() { :; }
                    tc_init_runtime_identity() {
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }
                    tc_watchdog_stop_samba_lane_without_payload() { :; }
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
                    tc_watchdog_reset_pass_state() { :; }
                    tc_prepare_local_hostname_resolution() { :; }
                    tc_init_runtime_identity() {
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }
                    tc_watchdog_refresh_runtime_identity_for_recovery() { :; }
                    tc_watchdog_stop_samba_lane_without_payload() { :; }
                    runtime_process_present_by_ucomm() { return 1; }
                    tc_mdns_bound_udp_5353() { return 1; }
                    sleep() {
                        case "$1" in
                            1) return 0 ;;
                            3) echo unexpected-settle; return 0 ;;
                            10) echo "status=$manager_status deferred=$TC_WATCHDOG_MDNS_DEFERRED_NO_IP"; exit 0 ;;
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
                    tc_watchdog_reset_pass_state() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }}
                    tc_watchdog_refresh_runtime_identity_for_recovery() {{ :; }}
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
                    tc_watchdog_stop_samba_lane_without_payload() {{ :; }}
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
                    tc_watchdog_reset_pass_state() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }}
                    tc_watchdog_refresh_runtime_identity_for_recovery() {{ :; }}
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
                    tc_watchdog_stop_samba_lane_without_payload() {{ :; }}
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
                    tc_watchdog_reset_pass_state() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }}
                    tc_watchdog_refresh_runtime_identity_for_recovery() {{ :; }}
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
                    tc_watchdog_stop_samba_lane_without_payload() {{ :; }}
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
                    tc_watchdog_reset_pass_state() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }}
                    tc_watchdog_refresh_runtime_identity_for_recovery() {{ :; }}
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
                    tc_watchdog_stop_samba_lane_without_payload() {{ :; }}
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
                    tc_watchdog_reset_pass_state() {{
                        tc_set_payload_log_dir {shlex.quote(str(old_payload_logs.parent))} {shlex.quote(str(old_payload_logs.parent))}
                    }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }}
                    tc_watchdog_refresh_runtime_identity_for_recovery() {{ :; }}
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
                    tc_watchdog_stop_samba_lane_without_payload() {{ :; }}
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
                    tc_watchdog_wake_or_mount_volume() {{ echo "unexpected-watchdog-mount $1 $2"; return 1; }}
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
        self.assertNotIn("unexpected-watchdog-mount", proc.stdout)

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
                    tc_watchdog_stop_samba_lane_without_payload() {{ :; }}
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
                    tc_watchdog_wake_or_mount_volume() {{ echo "unexpected-watchdog-mount $1 $2"; return 1; }}
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
        self.assertNotIn("unexpected-watchdog-mount", proc.stdout)

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
                    tc_watchdog_reconcile_nbns() {{ echo nbns-reconcile >>{shlex.quote(str(events))}; echo nbns-reconcile; return 0; }}
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
                    tc_watchdog_reset_pass_state() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{
                        MDNS_INSTANCE_NAME=AirPort
                        MDNS_HOST_LABEL=airport
                        SMB_NETBIOS_NAME=AIRPORT
                        SMB_SERVER_STRING=AirPort
                    }}
                    tc_watchdog_stop_samba_lane_without_payload() {{ :; }}
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
                    tc_watchdog_reconcile_nbns() { echo nbns-reconcile; return 0; }
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

    def test_common_build_share_state_handles_volumes_tsv_without_final_newline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            volume_root = self.mapped_volume_root(INTERNAL_DATA, volumes)
            Path(volume_root).mkdir(parents=True, exist_ok=True)
            row = "\t".join(
                (
                    INTERNAL_DATA.disk_device,
                    "1",
                    INTERNAL_DATA.partition_device,
                    volume_root,
                    INTERNAL_DATA.name,
                    INTERNAL_DATA.adisk_uuid,
                )
            )
            script = tmp_path / "share-state-no-final-newline.sh"
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
                    is_volume_root_mounted() {{ return 0; }}
                    printf %s {shlex.quote(row)} >"$RAM_VAR/test-volumes.tsv"
                    tc_build_share_state "$RAM_VAR/test-volumes.tsv"
                    printf 'shares\\n'
                    cat "$TC_SHARES_TSV"
                    printf 'adisk\\n'
                    cat "$TC_ADISK_TSV"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run(
                ["/bin/sh", str(script)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        expected_shares, expected_adisk = self.expected_share_state(
            MaStFixture("no_final_newline_volumes", "", (INTERNAL_DATA,)),
            volumes,
            internal_share_use_disk_root=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, f"shares\n{expected_shares}adisk\n{expected_adisk}")

    def test_common_share_state_matches_python_policy_for_shell_supported_mast_fixtures(self) -> None:
        fixtures = tuple(fixture for fixture in SHELL_MAST_FIXTURES if fixture.expected)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)

            proc = self.run_share_state_fixture_batch(
                fixtures,
                tmp_path,
                flash,
                volumes,
                internal_share_use_disk_root=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            sections = self.parse_named_shell_sections(proc.stdout)
            for fixture in fixtures:
                with self.subTest(fixture=fixture.name):
                    expected_shares, expected_adisk = self.expected_share_state(
                        fixture,
                        volumes,
                        internal_share_use_disk_root=False,
                    )
                    expected_stdout = f"shares\n{expected_shares}adisk\n{expected_adisk}"
                    status, stdout = sections[fixture.name]
                    self.assertEqual(status, 0)
                    self.assertEqual(stdout, expected_stdout)
                    for volume in fixture.expected:
                        volume_root = Path(self.mapped_volume_root(volume, volumes))
                        if volume.builtin:
                            marker = volume_root / "ShareRoot/.com.apple.timemachine.supported"
                        else:
                            marker = volume_root / ".com.apple.timemachine.supported"
                        self.assertTrue(marker.exists(), str(marker))

    def test_common_share_state_internal_disk_root_override_matches_python_policy(self) -> None:
        fixtures = tuple(fixture for fixture in SHELL_MAST_FIXTURES if any(volume.builtin for volume in fixture.expected))
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)

            proc = self.run_share_state_fixture_batch(
                fixtures,
                tmp_path,
                flash,
                volumes,
                internal_share_use_disk_root=True,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            sections = self.parse_named_shell_sections(proc.stdout)
            for fixture in fixtures:
                with self.subTest(fixture=fixture.name):
                    expected_shares, expected_adisk = self.expected_share_state(
                        fixture,
                        volumes,
                        internal_share_use_disk_root=True,
                    )
                    expected_stdout = f"shares\n{expected_shares}adisk\n{expected_adisk}"
                    status, stdout = sections[fixture.name]
                    self.assertEqual(status, 0)
                    self.assertEqual(stdout, expected_stdout)
                    self.assertNotIn("/ShareRoot", stdout)

    def test_common_build_share_state_uses_modern_mast_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            (volumes / "dk2").mkdir()
            (volumes / "dk3").mkdir()
            script = tmp_path / "build-shares.sh"
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
                    is_volume_root_mounted() {{ return 0; }}
                    cat >"$RAM_VAR/test-volumes.tsv" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    sd0	0	dk3	{volumes}/dk3	Data	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    tc_build_share_state "$RAM_VAR/test-volumes.tsv"
                    printf 'shares\\n'
                    cat "$TC_SHARES_TSV"
                    printf 'adisk\\n'
                    cat "$TC_ADISK_TSV"
                    printf 'internal_marker=%s\\n' "$([ -f {volumes}/dk2/ShareRoot/.com.apple.timemachine.supported ] && echo yes || echo no)"
                    printf 'external_marker=%s\\n' "$([ -f {volumes}/dk3/.com.apple.timemachine.supported ] && echo yes || echo no)"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"Data\t{volumes}/dk2/ShareRoot\tdk2\t1\taaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n", proc.stdout)
        self.assertIn(f"Data (dk3)\t{volumes}/dk3\tdk3\t0\tbbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb\n", proc.stdout)
        self.assertIn("Data\tdk2\taaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\t0x82\n", proc.stdout)
        self.assertIn("Data (dk3)\tdk3\tbbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb\t0x82\n", proc.stdout)
        self.assertIn("internal_marker=yes\n", proc.stdout)
        self.assertIn("external_marker=yes\n", proc.stdout)

    def test_common_build_share_state_bounds_names_to_adisk_txt_budget(self) -> None:
        long_name = "é" * 100
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            (volumes / "dk2").mkdir()
            (volumes / "dk3").mkdir()
            script = tmp_path / "build-long-shares.sh"
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
                    is_volume_root_mounted() {{ return 0; }}
                    cat >"$RAM_VAR/test-volumes.tsv" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	{long_name}	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    sd0	0	dk3	{volumes}/dk3	{long_name}	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    tc_build_share_state "$RAM_VAR/test-volumes.tsv"
                    cat "$TC_ADISK_TSV"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = [line.split("\t") for line in proc.stdout.splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0][0].encode("utf-8")), 194)
        self.assertEqual(len(rows[1][0].encode("utf-8")), 194)
        self.assertTrue(rows[1][0].endswith(" (dk3)"))
        for share_name, disk_key, adisk_uuid, advf in rows:
            txt = f"{disk_key}=adVF={advf},adVN={share_name},adVU={adisk_uuid}"
            self.assertLessEqual(len(txt.encode("utf-8")), 255)

    def test_common_payload_recovery_writes_resolved_state_for_watchdog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            (volumes / "dk2").mkdir()
            (volumes / "dk3/.samba4/private").mkdir(parents=True)
            (volumes / "dk3/.samba4/smbd").write_text("")
            (volumes / "dk3/.samba4/smbd").chmod(0o755)
            (volumes / "dk3/.samba4/private/smbpasswd").write_text("")
            (volumes / "dk3/.samba4/private/username.map").write_text("")
            script = tmp_path / "payload-recovery.sh"
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
                    is_volume_root_mounted() {{ return 0; }}
                    cat >"$RAM_VAR/test-volumes.tsv" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    sd0	0	dk3	{volumes}/dk3	USB	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    tc_resolve_payload "$RAM_VAR/test-volumes.tsv"
                    tc_write_payload_state "$TC_RESOLVED_PAYLOAD_DIR" "$TC_RESOLVED_PAYLOAD_VOLUME" "$TC_RESOLVED_PAYLOAD_DEVICE"
                    tc_load_payload_state
                    printf '%s\\n%s\\n%s\\n' "$TC_PAYLOAD_DIR" "$TC_PAYLOAD_VOLUME" "$TC_PAYLOAD_DEVICE"
                    printf 'mdns-log=%s\\nnbns-log=%s\\n' "$TC_MDNS_LOG_FILE" "$TC_NBNS_LOG_FILE"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                proc.stdout,
                f"{volumes}/dk3/.samba4\n{volumes}/dk3\n/dev/dk3\n"
                f"mdns-log={volumes}/dk3/.samba4/logs/mdns.log\n"
                f"nbns-log={volumes}/dk3/.samba4/logs/nbns.log\n",
            )
            self.assertIn(f"payload directory selected from mounted MaSt volumes: {volumes}/dk3/.samba4", log_text)

    def test_common_payload_discovery_does_not_manual_mount_after_first_invalid_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            (volumes / "dk2").mkdir()
            mount_log = tmp_path / "mount.log"
            script = tmp_path / "payload-no-manual-mount.sh"
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
                    is_volume_root_mounted() {{ [ "$1" = "{volumes}/dk2" ]; }}
                    mount_hfs_bounded() {{
                        echo "unexpected manual mount" >>{mount_log}
                        return 0
                    }}
                    cat >"$RAM_VAR/test-volumes.tsv" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    tc_resolve_payload "$RAM_VAR/test-volumes.tsv" || status=$?
                    printf 'status=%s\\n' "${{status:-0}}"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "status=1\n")
        self.assertFalse(mount_log.exists())
        self.assertIn(f"payload discovery first invalid payload check failed for {volumes}/dk2/.samba4", log_text)
        self.assertIn("payload candidate diagnostics (first failure)", log_text)
        self.assertIn(f"payload diagnostic command: df -k {volumes}/dk2", log_text)
        self.assertIn(f"payload diagnostic command: ls -la {volumes}/dk2/.samba4", log_text)
        self.assertIn("payload discovery: mount_hfs retry skipped; runtime uses diskd.useVolume-only activation", log_text)
        self.assertIn("payload candidate diagnostics (after final failure)", log_text)
        self.assertIn("no valid payload directory found on mounted MaSt volumes", log_text)

    def test_common_payload_discovery_logs_final_diagnostics_for_invalid_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            (volumes / "dk2").mkdir()
            script = tmp_path / "payload-final-failure.sh"
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
                    is_volume_root_mounted() {{ [ "$1" = "{volumes}/dk2" ]; }}
                    cat >"$RAM_VAR/test-volumes.tsv" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    tc_resolve_payload "$RAM_VAR/test-volumes.tsv" || status=$?
                    printf 'status=%s\\n' "${{status:-0}}"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "status=1\n")
        self.assertIn(f"payload discovery first invalid payload check failed for {volumes}/dk2/.samba4", log_text)
        self.assertIn("payload candidate diagnostics (first failure)", log_text)
        self.assertIn("payload discovery failed: first mounted payload candidate is invalid", log_text)
        self.assertIn("payload discovery: mount_hfs retry skipped; runtime uses diskd.useVolume-only activation", log_text)
        self.assertIn("payload candidate diagnostics (after final failure)", log_text)
        self.assertIn(f"payload diagnostic command: df -k {volumes}/dk2", log_text)
        self.assertIn(f"payload diagnostic command: ls -la {volumes}/dk2", log_text)
        self.assertIn(f"payload diagnostic command: ls -la {volumes}/dk2/.samba4", log_text)
        self.assertIn(f"payload diagnostic command: ls -la {volumes}/dk2/.samba4/private", log_text)
        self.assertIn("no valid payload directory found on mounted MaSt volumes", log_text)

    def test_common_refresh_disk_state_succeeds_with_payload_and_one_share_when_external_optional_fails(self) -> None:
        fixture = SHELL_MAST_FIXTURES[0]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            topology_path, raw_path = self.write_fixture_state_files(tmp_path, fixture, volumes)
            (volumes / "dk2/.samba4/private").mkdir(parents=True)
            (volumes / "dk2/.samba4/smbd").write_text("")
            (volumes / "dk2/.samba4/smbd").chmod(0o755)
            (volumes / "dk2/.samba4/private/smbpasswd").write_text("")
            (volumes / "dk2/.samba4/private/username.map").write_text("")
            script = tmp_path / "refresh-disk-state.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR" {volumes}/dk2 {volumes}/dk3
                    {self.render_mast_wait_fixture_override(topology_path, raw_path)}
                    tc_mount_mast_volumes_for_boot() {{ :; }}
                    is_volume_root_mounted() {{ [ "$1" = "{volumes}/dk2" ]; }}
                    tc_refresh_disk_state
                    printf 'payload\\n'
                    cat "$TC_PAYLOAD_TSV"
                    printf 'shares\\n'
                    cat "$TC_SHARES_TSV"
                    [ -f {volumes}/dk2/ShareRoot/.com.apple.timemachine.supported ] && echo internal-marker
                    [ ! -f {volumes}/dk3/.com.apple.timemachine.supported ] && echo external-skipped
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"payload\n{volumes}/dk2/.samba4\t{volumes}/dk2\t/dev/dk2\n", proc.stdout)
        self.assertIn(f"Data\t{volumes}/dk2/ShareRoot\tdk2\t1\tf42bdb83-c265-5522-a087-25606a4d0abf\n", proc.stdout)
        self.assertIn("internal-marker\n", proc.stdout)
        self.assertIn("external-skipped\n", proc.stdout)

    def test_common_refresh_disk_state_requires_payload_even_when_share_is_writable(self) -> None:
        fixture = SHELL_MAST_FIXTURES[0]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            topology_path, raw_path = self.write_fixture_state_files(tmp_path, fixture, volumes)
            script = tmp_path / "refresh-disk-state-missing-payload.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR" {volumes}/dk2 {volumes}/dk3
                    {self.render_mast_wait_fixture_override(topology_path, raw_path)}
                    tc_mount_mast_volumes_for_boot() {{ :; }}
                    is_volume_root_mounted() {{ return 0; }}
                    tc_refresh_disk_state || status=$?
                    printf 'status=%s\\n' "${{status:-0}}"
                    printf 'payload-size=%s\\n' "$([ -s "$TC_PAYLOAD_TSV" ] && echo nonempty || echo empty)"
                    printf 'shares-size=%s\\n' "$([ -s "$TC_SHARES_TSV" ] && echo nonempty || echo empty)"
                    printf 'adisk-size=%s\\n' "$([ -s "$TC_ADISK_TSV" ] && echo nonempty || echo empty)"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=0\n", proc.stdout)
        self.assertIn("payload-size=empty\n", proc.stdout)
        self.assertIn("shares-size=empty\n", proc.stdout)
        self.assertIn("adisk-size=empty\n", proc.stdout)
        self.assertIn("no valid payload directory found on mounted MaSt volumes", proc.stdout)
        self.assertIn("payload discovery failed; writing no-payload runtime state", proc.stdout)

    def test_common_refresh_disk_state_zero_mast_clears_stale_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "refresh-zero-mast-clears-stale.sh"
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
                    cat >"$TC_TOPOLOGY_SIGNATURE" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    cat >"$TC_ADISK_TSV" <<'EOF'
                    Data	dk2	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa	0x82
                    EOF
                    tc_write_payload_state {volumes}/dk2/.samba4 {volumes}/dk2 /dev/dk2
                    tc_read_mast_volumes_to() {{ : >"$1"; : >"$2"; return 0; }}
                    tc_mount_mast_volumes_for_boot() {{ echo unexpected-mount; return 1; }}
                    tc_refresh_disk_state
                    printf 'result=%s\\n' "$TC_DISK_REFRESH_RESULT"
                    printf 'topology-size=%s\\n' "$([ -s "$TC_TOPOLOGY_SIGNATURE" ] && echo nonempty || echo empty)"
                    printf 'shares-size=%s\\n' "$([ -s "$TC_SHARES_TSV" ] && echo nonempty || echo empty)"
                    printf 'adisk-size=%s\\n' "$([ -s "$TC_ADISK_TSV" ] && echo nonempty || echo empty)"
                    printf 'payload-size=%s\\n' "$([ -s "$TC_PAYLOAD_TSV" ] && echo nonempty || echo empty)"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("result=no_payload\n", proc.stdout)
        self.assertIn("topology-size=empty\n", proc.stdout)
        self.assertIn("shares-size=empty\n", proc.stdout)
        self.assertIn("adisk-size=empty\n", proc.stdout)
        self.assertIn("payload-size=empty\n", proc.stdout)
        self.assertNotIn("unexpected-mount", proc.stdout)
        self.assertIn("MaSt reports zero managed HFS volumes; writing diskless runtime state", proc.stdout)

    def test_common_refresh_disk_state_publish_failure_fails_under_conditional_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "refresh-publish-failure.sh"
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
                    tc_read_mast_volumes_to() {{ : >"$1"; : >"$2"; return 0; }}
                    mv() {{
                        if [ "${{3:-}}" = "$TC_PAYLOAD_TSV" ]; then
                            echo "blocked publish $3"
                            return 1
                        fi
                        /bin/mv "$@"
                    }}
                    if tc_refresh_disk_state; then
                        echo refresh-ok
                    else
                        echo refresh-failed
                    fi
                    printf 'result=%s\\n' "$TC_DISK_REFRESH_RESULT"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("blocked publish ", proc.stdout)
        self.assertIn("refresh-failed\n", proc.stdout)
        self.assertIn("result=failed\n", proc.stdout)
        self.assertIn("disk-state refresh failed: could not publish diskless runtime state", proc.stdout)
        self.assertNotIn("disk-state refresh complete: diskless runtime state written", proc.stdout)

    def test_common_refresh_disk_state_mast_failure_keeps_existing_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "refresh-mast-failure-keeps-state.sh"
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
                    echo old-topology >"$TC_TOPOLOGY_SIGNATURE"
                    echo old-share >"$TC_SHARES_TSV"
                    echo old-adisk >"$TC_ADISK_TSV"
                    echo old-payload >"$TC_PAYLOAD_TSV"
                    tc_read_mast_volumes_to() {{ : >"$1"; : >"$2"; return 1; }}
                    tc_refresh_disk_state || status=$?
                    printf 'status=%s\\n' "${{status:-0}}"
                    printf 'result=%s\\n' "$TC_DISK_REFRESH_RESULT"
                    printf 'topology='
                    cat "$TC_TOPOLOGY_SIGNATURE"
                    printf 'shares='
                    cat "$TC_SHARES_TSV"
                    printf 'adisk='
                    cat "$TC_ADISK_TSV"
                    printf 'payload='
                    cat "$TC_PAYLOAD_TSV"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=1\n", proc.stdout)
        self.assertIn("result=mast_failed\n", proc.stdout)
        self.assertIn("topology=old-topology\n", proc.stdout)
        self.assertIn("shares=old-share\n", proc.stdout)
        self.assertIn("adisk=old-adisk\n", proc.stdout)
        self.assertIn("payload=old-payload\n", proc.stdout)
        self.assertIn("MaSt discovery failed", proc.stdout)

    def test_common_refresh_disk_state_sets_ata_drive_settings_after_share_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            events = tmp_path / "events.log"
            payload = volumes / "dk2/.samba4"
            script = tmp_path / "refresh-order.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR" {payload}
                    tc_read_mast_volumes_to() {{
                        cat >"$1" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                        : >"$2"
                        return 0
                    }}
                    tc_mount_mast_volumes_for_boot() {{ echo mount >>{events}; }}
                    tc_build_share_state() {{
                        echo share >>{events}
                        : >"$2"
                        echo "Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" >>"$2"
                        : >"$3"
                        return 0
                    }}
                    tc_configure_ata_drive_settings_for_mast_disks() {{ echo ata >>{events}; }}
                    tc_resolve_payload() {{
                        echo payload >>{events}
                        TC_RESOLVED_PAYLOAD_DIR={payload}
                        TC_RESOLVED_PAYLOAD_VOLUME={volumes}/dk2
                        TC_RESOLVED_PAYLOAD_DEVICE=/dev/dk2
                        return 0
                    }}
                    tc_payload_log_dir_ready() {{ return 0; }}
                    tc_refresh_disk_state
                    cat {events}
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), ["mount", "payload", "share", "ata"])

    def test_common_stage_disk_runtime_reads_payload_state_and_rebuilds_ram_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            (payload / "smbd").write_text("#!/bin/sh\n")
            (payload / "smbd").chmod(0o755)
            (payload / "private/smbpasswd").write_text("admin:x\n")
            (payload / "private/username.map").write_text("admin = root\n")
            script = tmp_path / "stage-disk-runtime.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    tc_prepare_ram_root
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    tc_write_payload_state {payload} {volumes}/dk2 /dev/dk2
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	f42bdb83-c265-5522-a087-25606a4d0abf
                    EOF
                    tc_stage_disk_runtime
                    [ -x {memory}/samba4/sbin/smbd ] && echo smbd-staged
                    [ -f {memory}/samba4/private/smbpasswd ] && echo private-staged
                    cat {memory}/samba4/etc/smb.conf
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("smbd-staged\n", proc.stdout)
        self.assertIn("private-staged\n", proc.stdout)
        self.assertIn("interfaces = 127.0.0.1/8 192.168.1.40/24", proc.stdout)
        self.assertIn("bind interfaces only = yes", proc.stdout)
        self.assertIn("nbns runtime staging skipped", log_text)
        self.assertNotIn("nbns binary not found", log_text)

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

    def test_common_stage_disk_runtime_fails_without_payload_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "stage-disk-runtime-no-state.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    tc_prepare_ram_root
                    tc_stage_disk_runtime || status=$?
                    printf 'status=%s\\n' "${{status:-0}}"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=1\n", proc.stdout)
        self.assertIn("payload discovery failed: payload state is unavailable", proc.stdout)

    def test_common_stage_disk_runtime_copy_failure_fails_under_conditional_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            (payload / "smbd").write_text("#!/bin/sh\n")
            (payload / "smbd").chmod(0o755)
            (payload / "private/smbpasswd").write_text("admin:x\n")
            (payload / "private/username.map").write_text("admin = root\n")
            script = tmp_path / "stage-copy-failure.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    tc_prepare_ram_root
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    tc_write_payload_state {payload} {volumes}/dk2 /dev/dk2
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	f42bdb83-c265-5522-a087-25606a4d0abf
                    EOF
                    cp() {{
                        echo "blocked cp $1 $2"
                        return 1
                    }}
                    if tc_stage_disk_runtime; then
                        echo stage-ok
                    else
                        echo stage-failed
                    fi
                    cat {memory}/samba4/var/test.log
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("blocked cp ", proc.stdout)
        self.assertIn("stage-failed\n", proc.stdout)
        self.assertNotIn("stage-ok", proc.stdout)
        self.assertNotIn("runtime staging complete under", proc.stdout)

    def test_common_stage_disk_runtime_config_failure_fails_under_conditional_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            (payload / "private").mkdir(parents=True)
            (payload / "smbd").write_text("#!/bin/sh\n")
            (payload / "smbd").chmod(0o755)
            (payload / "private/smbpasswd").write_text("admin:x\n")
            (payload / "private/username.map").write_text("admin = root\n")
            script = tmp_path / "stage-config-failure.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    tc_prepare_ram_root
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    tc_write_payload_state {payload} {volumes}/dk2 /dev/dk2
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	f42bdb83-c265-5522-a087-25606a4d0abf
                    EOF
                    tc_generate_smb_conf() {{
                        echo generate-failed
                        return 1
                    }}
                    if tc_stage_disk_runtime; then
                        echo stage-ok
                    else
                        echo stage-failed
                    fi
                    cat {memory}/samba4/var/test.log
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("generate-failed\n", proc.stdout)
        self.assertIn("stage-failed\n", proc.stdout)
        self.assertNotIn("stage-ok", proc.stdout)
        self.assertNotIn("runtime staging complete under", proc.stdout)

    def test_common_smb_bind_context_waits_settles_and_uses_fresh_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            probe_count = tmp_path / "probe-count"
            (flash / "mdns-advertiser").write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    [ "$1" = "--print-smb-bind-interfaces" ] || exit 2
                    if [ -f {probe_count} ]; then
                        echo 192.168.1.40/24
                    else
                        : >{probe_count}
                        echo 192.168.1.39/24
                    fi
                    """
                )
            )
            (flash / "mdns-advertiser").chmod(0o755)
            script = tmp_path / "smb-bind-context.sh"
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
                    sleep() {{ echo "sleep $1"; }}
                    tc_prepare_smb_bind_context
                    printf 'bind=%s\\n' "$TC_SMB_BIND_INTERFACES"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "sleep 3\nbind=127.0.0.1/8 ::1/128 192.168.1.40/24\n")
        self.assertIn("first usable address observed: 192.168.1.39/24", log_text)
        self.assertIn("Samba bind interfaces: 127.0.0.1/8 ::1/128 192.168.1.40/24", log_text)

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
                    if tc_refresh_smb_bind_interfaces; then
                        echo status=0
                    else
                        echo status=$?
                    fi
                    printf 'bind=%s\\n' "${{TC_SMB_BIND_INTERFACES:-}}"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=1\n", proc.stdout)
        self.assertIn("bind=\n", proc.stdout)
        self.assertIn("Samba bind interface probe failed with exit code 1", log_text)

    def test_common_smb_bind_context_fails_hard_on_probe_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            (flash / "mdns-advertiser").write_text("#!/bin/sh\nexit 13\n")
            (flash / "mdns-advertiser").chmod(0o755)
            script = tmp_path / "smb-bind-hard-fail.sh"
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
                    sleep() {{ echo "unexpected sleep"; }}
                    tc_prepare_smb_bind_context || status=$?
                    printf 'status=%s\\n' "${{status:-0}}"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=1\n", proc.stdout)
        self.assertIn("Samba bind discovery failed with exit code 13", proc.stdout)
        self.assertNotIn("no usable address has appeared yet", proc.stdout)
        self.assertNotIn("unexpected sleep", proc.stdout)

    def test_payload_on_external_disk_is_also_served_as_share_and_hidden_in_smb_conf(self) -> None:
        fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_external_only")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            topology_path, raw_path = self.write_fixture_state_files(tmp_path, fixture, volumes)
            payload = volumes / "dk5/.samba4"
            (payload / "private").mkdir(parents=True)
            (payload / "smbd").write_text("#!/bin/sh\n")
            (payload / "smbd").chmod(0o755)
            (payload / "private/smbpasswd").write_text("root:x\n")
            (payload / "private/username.map").write_text("root = *\n")
            script = tmp_path / "external-payload-integration.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    {self.render_mast_wait_fixture_override(topology_path, raw_path)}
                    tc_mount_mast_volumes_for_boot() {{ :; }}
                    is_volume_root_mounted() {{ return 0; }}
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    mkdir -p "$RAM_VAR" {volumes}/dk5
                    tc_refresh_disk_state
                    tc_prepare_ram_root
                    tc_stage_disk_runtime
                    printf 'payload\\n'
                    cat "$TC_PAYLOAD_TSV"
                    printf 'shares\\n'
                    cat "$TC_SHARES_TSV"
                    printf 'adisk\\n'
                    cat "$TC_ADISK_TSV"
                    printf 'marker=%s\\n' "$([ -f {volumes}/dk5/.com.apple.timemachine.supported ] && echo yes || echo no)"
                    printf 'runtime=%s\\n' "$([ -x {memory}/samba4/sbin/smbd ] && echo yes || echo no)"
                    cat "$TC_SMBD_CONF"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"payload\n{volumes}/dk5/.samba4\t{volumes}/dk5\t/dev/dk5\n", proc.stdout)
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
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    USB	{volumes}/dk3	dk3	0	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    tc_generate_smb_conf {payload}
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
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    tc_generate_smb_conf {payload}
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
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    tc_generate_smb_conf {payload}
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
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    tc_generate_smb_conf {payload}
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
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    tc_generate_smb_conf {payload}
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

    def test_common_watchdog_service_iteration_writes_ram_health_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-health-log.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/watchdog.log" watchdog
                    mkdir -p "$RAM_VAR"
                    sleep() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_samba_runtime_expected() {{ return 0; }}
                    tc_watchdog_reconcile_smb_bind_interfaces() {{ :; }}
                    tc_start_smbd_if_needed() {{ return 0; }}
                    tc_all_managed_services_healthy() {{ return 0; }}
                    runtime_process_present_by_ucomm() {{ return 0; }}
                    tc_watchdog_service_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/watchdog.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("watchdog service pass: checking managed services", log_text)
        self.assertIn("watchdog steady check: healthy", log_text)

    def test_common_watchdog_service_iteration_diskless_stops_stale_samba_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-diskless-stops-samba-lane.sh"
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
                    sleep() {{ :; }}
                    tc_watchdog_reconcile_identity() {{ echo identity; }}
                    tc_watchdog_reconcile_smb_bind_interfaces() {{ echo unexpected-bind; return 1; }}
                    tc_watchdog_reconcile_smbd() {{ echo unexpected-smbd; return 1; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            smbd|nbns-advertiser) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    stop_runtime_process_by_ucomm() {{ echo "stop $1"; }}
                    tc_watchdog_reconcile_mdns() {{ echo mdns; }}
                    tc_all_managed_services_healthy() {{ echo healthy; return 0; }}
                    tc_watchdog_service_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "identity\nstop smbd\nstop nbns-advertiser\nmdns\nhealthy\n")

    def test_common_watchdog_service_iteration_checks_daemons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-process-flow.sh"
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
                    sleep() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ echo hosts; }}
                    tc_samba_runtime_expected() {{ return 0; }}
                    tc_watchdog_reconcile_smb_bind_interfaces() {{ :; }}
                    tc_start_smbd_if_needed() {{ echo smbd; }}
                    runtime_process_present_by_ucomm() {{ echo "process $1"; return 0; }}
                    tc_nbns_bound_ipv4_udp_137() {{ return 0; }}
                    tc_all_managed_services_healthy() {{ echo healthy; return 0; }}
                    tc_watchdog_service_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "hosts\nsmbd\nprocess mdns-advertiser\nprocess nbns-advertiser\nhealthy\n")

    def test_common_watchdog_smb_bind_reconcile_skips_restart_when_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-bind-unchanged.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    tc_probe_smb_bind_interfaces() {{ echo "127.0.0.1/8 192.168.1.40/24"; }}
                    tc_generate_smb_conf() {{ echo "generate $1"; }}
                    tc_restart_smbd_for_bind_change() {{ echo restart; }}
                    tc_watchdog_reconcile_smb_bind_interfaces
                    printf 'bind=%s\\n' "$TC_SMB_BIND_INTERFACES"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "bind=127.0.0.1/8 192.168.1.40/24\n")

    def test_common_watchdog_smb_bind_reconcile_restarts_when_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            private = payload / "private"
            private.mkdir(parents=True)
            (payload / "smbd").write_text("#!/bin/sh\n")
            (payload / "smbd").chmod(0o755)
            (private / "smbpasswd").write_text("root:x\n")
            (private / "username.map").write_text("root = *\n")
            script = tmp_path / "watchdog-bind-changed.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    tc_probe_smb_bind_interfaces() {{ echo "127.0.0.1/8 192.168.1.41/24"; }}
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    USB	{volumes}/dk3	dk3	0	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    tc_load_payload_state() {{
                        TC_PAYLOAD_DIR={payload}
                        TC_PAYLOAD_VOLUME={volumes}/dk2
                        TC_PAYLOAD_DEVICE=/dev/dk2
                        return 0
                    }}
                    tc_watchdog_wake_or_mount_volume() {{ echo "mount $1 $2"; return 0; }}
                    tc_generate_smb_conf() {{ echo "generate $1 $TC_SMB_BIND_INTERFACES"; }}
                    tc_restart_smbd_for_bind_change() {{ echo "restart $1"; }}
                    tc_restart_mdns() {{ echo unexpected-mdns; return 1; }}
                    tc_restart_nbns() {{ echo unexpected-nbns; return 1; }}
                    tc_watchdog_reconcile_smb_bind_interfaces
                    printf 'bind=%s\\n' "$TC_SMB_BIND_INTERFACES"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            f"mount /dev/dk2 {volumes}/dk2\nmount /dev/dk2 {volumes}/dk2\nmount /dev/dk3 {volumes}/dk3\ngenerate {payload} 127.0.0.1/8 192.168.1.41/24\nrestart bind interfaces changed\nbind=127.0.0.1/8 192.168.1.41/24\n",
        )

    def test_common_watchdog_smb_bind_reconcile_restores_bind_when_disk_prepare_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            private = payload / "private"
            private.mkdir(parents=True)
            (payload / "smbd").write_text("#!/bin/sh\n")
            (payload / "smbd").chmod(0o755)
            (private / "smbpasswd").write_text("root:x\n")
            (private / "username.map").write_text("root = *\n")
            script = tmp_path / "watchdog-bind-prepare-fails.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    tc_probe_smb_bind_interfaces() {{ echo "127.0.0.1/8 192.168.1.41/24"; }}
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    USB	{volumes}/dk3	dk3	0	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    tc_load_payload_state() {{
                        TC_PAYLOAD_DIR={payload}
                        TC_PAYLOAD_VOLUME={volumes}/dk2
                        TC_PAYLOAD_DEVICE=/dev/dk2
                        return 0
                    }}
                    tc_watchdog_wake_or_mount_volume() {{
                        echo "mount $1 $2"
                        [ "$1" != "/dev/dk3" ]
                    }}
                    tc_generate_smb_conf() {{ echo generate; }}
                    tc_restart_smbd_for_bind_change() {{ echo restart; }}
                    tc_watchdog_reconcile_smb_bind_interfaces || status=$?
                    printf 'status=%s\\n' "${{status:-0}}"
                    printf 'bind=%s\\n' "$TC_SMB_BIND_INTERFACES"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"mount /dev/dk2 {volumes}/dk2\n", proc.stdout)
        self.assertIn(f"mount /dev/dk3 {volumes}/dk3\n", proc.stdout)
        self.assertIn("status=1\n", proc.stdout)
        self.assertIn("bind=127.0.0.1/8 192.168.1.40/24\n", proc.stdout)
        self.assertNotIn("generate\n", proc.stdout)
        self.assertNotIn("restart\n", proc.stdout)

    def test_common_watchdog_smb_bind_reconcile_restores_bind_when_config_generation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-bind-generate-fails.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    TC_PAYLOAD_DIR=/payload
                    tc_probe_smb_bind_interfaces() {{ echo "127.0.0.1/8 192.168.1.41/24"; }}
                    tc_prepare_smbd_recovery_disk_runtime() {{ echo prepare; return 0; }}
                    tc_generate_smb_conf() {{ echo "generate $TC_SMB_BIND_INTERFACES"; return 1; }}
                    tc_restart_smbd_for_bind_change() {{ echo restart; }}
                    tc_watchdog_reconcile_smb_bind_interfaces || status=$?
                    printf 'status=%s\\n' "${{status:-0}}"
                    printf 'bind=%s\\n' "$TC_SMB_BIND_INTERFACES"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "prepare\ngenerate 127.0.0.1/8 192.168.1.41/24\nstatus=1\nbind=127.0.0.1/8 192.168.1.40/24\n",
        )
        self.assertNotIn("restart\n", proc.stdout)

    def test_common_watchdog_smb_bind_reconcile_fails_hard_on_probe_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-bind-hard-fail.sh"
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
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    tc_probe_smb_bind_interfaces() {{ return 13; }}
                    tc_generate_smb_conf() {{ echo generate; }}
                    tc_restart_smbd_for_bind_change() {{ echo restart; }}
                    tc_watchdog_reconcile_smb_bind_interfaces || status=$?
                    printf 'status=%s\\n' "${{status:-0}}"
                    printf 'deferred=%s\\n' "$TC_WATCHDOG_SMB_DEFERRED_NO_IP"
                    printf 'bind=%s\\n' "$TC_SMB_BIND_INTERFACES"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=1\n", proc.stdout)
        self.assertIn("deferred=0\n", proc.stdout)
        self.assertIn("bind=127.0.0.1/8 192.168.1.40/24\n", proc.stdout)
        self.assertIn("watchdog pass: Samba bind probe failed with exit code 13", proc.stdout)
        self.assertNotIn("generate\n", proc.stdout)
        self.assertNotIn("restart\n", proc.stdout)

    def test_common_watchdog_initializes_smb_bind_interfaces_without_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            (flash / "mdns-advertiser").write_text("#!/bin/sh\necho 192.168.1.40/24\n")
            (flash / "mdns-advertiser").chmod(0o755)
            script = tmp_path / "watchdog-bind-init.sh"
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
                    TC_SMB_BIND_INTERFACES=
                    tc_watchdog_initialize_smb_bind_interfaces
                    printf 'bind=%s\\n' "$TC_SMB_BIND_INTERFACES"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "bind=127.0.0.1/8 ::1/128 192.168.1.40/24\n")
        self.assertIn("watchdog startup: initialized Samba bind interfaces from live probe", log_text)

    def test_common_watchdog_defers_first_mdns_start_until_auto_ip_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            (flash / "mdns-advertiser").write_text("#!/bin/sh\nexit 1\n")
            (flash / "mdns-advertiser").chmod(0o755)
            script = tmp_path / "watchdog-mdns-defer.sh"
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
                    runtime_process_present_by_ucomm() {{ return 1; }}
                    tc_watchdog_refresh_runtime_identity_for_recovery() {{ :; }}
                    tc_mdns_auto_ip_available() {{ echo print-mdns-socket-families; return 11; }}
                    tc_start_mdns_capture() {{ echo capture; }}
                    tc_start_mdns_advertiser() {{ echo advertise; }}
                    tc_watchdog_start_mdns_if_needed
                    echo "deferred=$TC_WATCHDOG_MDNS_DEFERRED_NO_IP"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("print-mdns-socket-families\n", proc.stdout)
        self.assertNotIn("capture\n", proc.stdout)
        self.assertNotIn("advertise\n", proc.stdout)
        self.assertIn("deferred=1\n", proc.stdout)
        self.assertIn("mDNS auto-ip check: running", proc.stdout)
        self.assertIn("--print-mdns-socket-families", proc.stdout)
        self.assertIn("mDNS auto-ip check: no usable address yet", proc.stdout)
        self.assertIn("mDNS startup deferred; no usable address has appeared yet", proc.stdout)

    def test_common_watchdog_reports_mdns_auto_ip_probe_failure_without_deferral(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            (flash / "mdns-advertiser").write_text("#!/bin/sh\nexit 13\n")
            (flash / "mdns-advertiser").chmod(0o755)
            script = tmp_path / "watchdog-mdns-hard-fail.sh"
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
                    runtime_process_present_by_ucomm() {{ return 1; }}
                    tc_watchdog_refresh_runtime_identity_for_recovery() {{ :; }}
                    tc_mdns_auto_ip_available() {{ echo print-mdns-socket-families; return 13; }}
                    tc_start_mdns_capture() {{ echo capture; }}
                    tc_start_mdns_advertiser() {{ echo advertise; }}
                    tc_watchdog_start_mdns_if_needed
                    echo "deferred=$TC_WATCHDOG_MDNS_DEFERRED_NO_IP"
                    echo "unavailable=$TC_WATCHDOG_MDNS_UNAVAILABLE"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("print-mdns-socket-families\n", proc.stdout)
        self.assertNotIn("capture\n", proc.stdout)
        self.assertNotIn("advertise\n", proc.stdout)
        self.assertIn("deferred=0\n", proc.stdout)
        self.assertIn("unavailable=1\n", proc.stdout)
        self.assertIn("mDNS auto-ip check failed with exit code 13", proc.stdout)
        self.assertNotIn("mDNS startup deferred; no usable address has appeared yet", proc.stdout)

    def test_common_watchdog_starts_first_mdns_capture_after_auto_ip_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            (flash / "mdns-advertiser").write_text("#!/bin/sh\nexit 0\n")
            (flash / "mdns-advertiser").chmod(0o755)
            script = tmp_path / "watchdog-mdns-auto-ip-ready.sh"
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
                    runtime_process_present_by_ucomm() {{ return 1; }}
                    tc_watchdog_refresh_runtime_identity_for_recovery() {{ :; }}
                    tc_mdns_auto_ip_available() {{ echo print-mdns-socket-families; return 0; }}
                    tc_start_mdns_capture() {{ echo capture; }}
                    tc_start_mdns_advertiser() {{ echo advertise; }}
                    tc_watchdog_start_mdns_if_needed
                    echo "seen=$TC_MDNS_AUTO_IP_SEEN"
                    echo "capture_attempted=$TC_MDNS_CAPTURE_ATTEMPTED"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("print-mdns-socket-families\n", proc.stdout)
        self.assertIn("capture\n", proc.stdout)
        self.assertIn("advertise\n", proc.stdout)
        self.assertLess(proc.stdout.index("capture\n"), proc.stdout.index("advertise\n"))
        self.assertIn("seen=1\n", proc.stdout)
        self.assertIn("capture_attempted=1\n", proc.stdout)
        self.assertIn("mDNS auto-ip check: running", proc.stdout)
        self.assertIn("--print-mdns-socket-families", proc.stdout)
        self.assertIn("mDNS auto-ip check: usable address is available", proc.stdout)
        self.assertIn("mDNS auto-ip is available; starting capture and advertiser", proc.stdout)

    def test_common_watchdog_later_mdns_restart_skips_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            (flash / "mdns-advertiser").write_text("#!/bin/sh\nexit 0\n")
            (flash / "mdns-advertiser").chmod(0o755)
            script = tmp_path / "watchdog-mdns-restart-skips-capture.sh"
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
                    TC_MDNS_CAPTURE_ATTEMPTED=1
                    runtime_process_present_by_ucomm() {{ return 1; }}
                    tc_watchdog_refresh_runtime_identity_for_recovery() {{ :; }}
                    tc_mdns_auto_ip_available() {{ echo print-mdns-socket-families; return 0; }}
                    tc_start_mdns_capture() {{ echo capture; }}
                    tc_start_mdns_advertiser() {{ echo advertise; }}
                    tc_restart_mdns() {{ echo restart; }}
                    tc_watchdog_start_mdns_if_needed
                    echo "capture_attempted=$TC_MDNS_CAPTURE_ATTEMPTED"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("print-mdns-socket-families\n", proc.stdout)
        self.assertNotIn("capture\n", proc.stdout)
        self.assertNotIn("advertise\n", proc.stdout)
        self.assertIn("restart\n", proc.stdout)
        self.assertIn("capture_attempted=1\n", proc.stdout)

    def test_common_watchdog_health_requires_mdns_udp_5353(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-mdns-health-udp.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    tc_samba_runtime_expected() {{ return 1; }}
                    mdns_present=1
                    runtime_process_present_by_ucomm() {{
                        [ "$1" = "$MDNS_PROC_NAME" ] && [ "$mdns_present" = "1" ]
                    }}
                    tc_mdns_bound_udp_5353() {{ return 1; }}
                    status=0
                    tc_all_managed_services_healthy || status=$?
                    echo "unbound=$status"
                    tc_mdns_bound_udp_5353() {{ return 0; }}
                    status=0
                    tc_all_managed_services_healthy || status=$?
                    echo "bound=$status"
                    mdns_present=0
                    status=0
                    tc_all_managed_services_healthy || status=$?
                    echo "missing=$status"
                    TC_WATCHDOG_MDNS_DEFERRED_NO_IP=1
                    status=0
                    tc_all_managed_services_healthy || status=$?
                    echo "deferred=$status"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "unbound=1\nbound=0\nmissing=1\ndeferred=0\n")

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

    def test_common_watchdog_accepts_ipv6_udp_5353_when_advertiser_is_ipv6_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-mdns-ipv6-only-bound.sh"
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
                    runtime_process_present_by_ucomm() {{
                        [ "$1" = "$MDNS_PROC_NAME" ]
                    }}
                    tc_probe_mdns_socket_families() {{ echo ipv6; }}
                    tc_process_bound_ipv4_udp_port() {{ echo unexpected-ipv4; return 1; }}
                    tc_process_bound_ipv6_udp_port() {{ echo ipv6-bound; return 0; }}
                    tc_mdns_auto_ip_available() {{ echo unexpected-auto-ip; return 0; }}
                    stop_runtime_process_by_ucomm() {{ echo "unexpected-stop $1"; return 1; }}
                    tc_restart_mdns() {{ echo unexpected-restart; return 1; }}
                    tc_watchdog_reconcile_mdns
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_path = memory / "samba4/var/test.log"
            log_text = log_path.read_text() if log_path.exists() else ""

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "ipv6-bound\n")
        self.assertNotIn("watchdog recovery: mdns advertiser is running without required UDP 5353 listeners", log_text)
        self.assertNotIn("unexpected", proc.stdout)

    def test_common_watchdog_restarts_mdns_when_running_without_udp_5353_and_auto_ip_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-mdns-restart-unbound.sh"
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
                    TC_MDNS_CAPTURE_ATTEMPTED=1
                    mdns_present=1
                    runtime_process_present_by_ucomm() {{
                        [ "$1" = "$MDNS_PROC_NAME" ] && [ "$mdns_present" = "1" ]
                    }}
                    tc_mdns_bound_udp_5353() {{ return 1; }}
                    tc_mdns_auto_ip_available() {{ echo auto-ip; return 0; }}
                    stop_runtime_process_by_ucomm() {{ echo "stop $1"; mdns_present=0; }}
                    tc_watchdog_refresh_runtime_identity_for_recovery() {{ echo identity; }}
                    tc_restart_mdns() {{ echo restart; }}
                    tc_start_mdns_capture() {{ echo unexpected-capture; return 1; }}
                    tc_start_mdns_advertiser() {{ echo unexpected-advertise; return 1; }}
                    tc_watchdog_reconcile_mdns
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "auto-ip\nstop mdns-advertiser\nidentity\nrestart\n")
        self.assertIn("watchdog recovery: mdns advertiser is running without required UDP 5353 listeners", log_text)
        self.assertNotIn("unexpected", proc.stdout)

    def test_common_watchdog_defers_mdns_when_running_without_udp_5353_and_no_auto_ip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-mdns-defer-unbound.sh"
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
                    runtime_process_present_by_ucomm() {{
                        [ "$1" = "$MDNS_PROC_NAME" ]
                    }}
                    tc_mdns_bound_udp_5353() {{ return 1; }}
                    tc_mdns_auto_ip_available() {{ echo auto-ip; return 11; }}
                    stop_runtime_process_by_ucomm() {{ echo "unexpected-stop $1"; return 1; }}
                    tc_restart_mdns() {{ echo unexpected-restart; return 1; }}
                    tc_watchdog_reconcile_mdns
                    echo "deferred=$TC_WATCHDOG_MDNS_DEFERRED_NO_IP"
                    tc_samba_runtime_expected() {{ return 1; }}
                    status=0
                    tc_all_managed_services_healthy || status=$?
                    echo "healthy=$status"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "auto-ip\ndeferred=1\nhealthy=0\n")
        self.assertIn("watchdog recovery: mdns advertiser is running without required UDP 5353 listeners", log_text)
        self.assertIn("mDNS startup deferred; no usable address has appeared yet", log_text)
        self.assertNotIn("unexpected", proc.stdout)

    def test_common_watchdog_marks_mdns_unavailable_when_unbound_auto_ip_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-mdns-unbound-hard-fail.sh"
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
                    runtime_process_present_by_ucomm() {{
                        [ "$1" = "$MDNS_PROC_NAME" ]
                    }}
                    tc_mdns_bound_udp_5353() {{ return 1; }}
                    tc_mdns_auto_ip_available() {{ echo auto-ip; return 13; }}
                    stop_runtime_process_by_ucomm() {{ echo "unexpected-stop $1"; return 1; }}
                    tc_restart_mdns() {{ echo unexpected-restart; return 1; }}
                    tc_watchdog_reconcile_mdns
                    echo "deferred=$TC_WATCHDOG_MDNS_DEFERRED_NO_IP"
                    echo "unavailable=$TC_WATCHDOG_MDNS_UNAVAILABLE"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "auto-ip\ndeferred=0\nunavailable=1\n")
        self.assertIn("watchdog recovery: mdns advertiser is running without required UDP 5353 listeners", log_text)
        self.assertIn("watchdog recovery: mDNS auto-ip check failed with exit code 13", log_text)
        self.assertNotIn("unexpected", proc.stdout)

    def test_common_watchdog_health_requires_nbns_udp_137(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-nbns-health-udp.sh"
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
                    tc_samba_runtime_expected() {{ return 0; }}
                    runtime_process_present_by_ucomm() {{ return 0; }}
                    tc_smbd_bound_tcp_445() {{ return 0; }}
                    tc_mdns_bound_udp_5353() {{ return 0; }}
                    tc_nbns_bound_ipv4_udp_137() {{ return 1; }}
                    status=0
                    tc_all_managed_services_healthy || status=$?
                    echo "unbound=$status"
                    tc_nbns_bound_ipv4_udp_137() {{ return 0; }}
                    status=0
                    tc_all_managed_services_healthy || status=$?
                    echo "bound=$status"
                    TC_WATCHDOG_NBNS_DEFERRED_NO_IP=1
                    tc_nbns_bound_ipv4_udp_137() {{ return 1; }}
                    status=0
                    tc_all_managed_services_healthy || status=$?
                    echo "deferred=$status"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "unbound=1\nbound=0\ndeferred=0\n")

    def test_common_watchdog_restarts_nbns_when_running_without_udp_137_and_auto_ip_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-nbns-restart-unbound.sh"
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
                    tc_watchdog_refresh_runtime_identity_for_recovery() {{ echo identity; }}
                    tc_restart_nbns() {{ echo restart; }}
                    tc_watchdog_reconcile_nbns
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "auto-ip\nstop nbns-advertiser\nidentity\nrestart\n")
        self.assertIn("watchdog recovery: nbns responder is running without required UDP 137 sockets", log_text)

    def test_common_watchdog_defers_nbns_when_running_without_udp_137_and_no_auto_ip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-nbns-defer-unbound.sh"
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
                    tc_watchdog_reconcile_nbns
                    echo "deferred=$TC_WATCHDOG_NBNS_DEFERRED_NO_IP"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "auto-ip\ndeferred=1\n")
        self.assertIn("watchdog recovery: nbns responder is running without required UDP 137 sockets", log_text)
        self.assertIn("NBNS startup deferred; no usable address has appeared yet", log_text)
        self.assertNotIn("unexpected", proc.stdout)

    def test_common_watchdog_reports_nbns_hard_auto_ip_failure_when_unbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-nbns-unbound-hard-fail.sh"
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
                    tc_watchdog_reconcile_nbns || status=$?
                    echo "status=$status"
                    echo "deferred=$TC_WATCHDOG_NBNS_DEFERRED_NO_IP"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "auto-ip\nstatus=1\ndeferred=0\n")
        self.assertIn("watchdog recovery: nbns responder is running without required UDP 137 sockets", log_text)
        self.assertIn("watchdog recovery: NBNS auto-ip check failed with exit code 13", log_text)
        self.assertNotIn("unexpected", proc.stdout)

    def test_common_smbd_recovery_mounts_payload_and_share_volumes_before_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            private = payload / "private"
            private.mkdir(parents=True)
            (payload / "smbd").write_text("payload smbd")
            (payload / "smbd").chmod(0o755)
            (private / "smbpasswd").write_text("user\n")
            (private / "username.map").write_text("root = user\n")
            script = tmp_path / "watchdog-smbd-recovery.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR" "$RAM_SBIN" "$RAM_ETC" "$LOCKS_ROOT"
                    printf '%s\\t%s\\t%s\\n' {payload} {volumes}/dk2 /dev/dk2 >"$TC_PAYLOAD_TSV"
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    USB	{volumes}/dk3	dk3	0	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    cat >"$TC_SMBD_BIN" <<'EOF'
                    #!/bin/sh
                    echo "start-smbd $*"
                    EOF
                    chmod 755 "$TC_SMBD_BIN"
                    : >"$TC_SMBD_CONF"
                    tc_watchdog_refresh_runtime_identity_for_recovery() {{ :; }}
                    runtime_process_present_by_ucomm() {{ return 1; }}
                    wait_for_process() {{ return 0; }}
                    tc_smbd_bound_tcp_445() {{ return 0; }}
                    tc_watchdog_wake_or_mount_volume() {{ echo "mount $1 $2"; return 0; }}
                    tc_start_smbd_if_needed
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"mount /dev/dk2 {volumes}/dk2\n", proc.stdout)
        self.assertIn(f"mount /dev/dk3 {volumes}/dk3\n", proc.stdout)
        self.assertIn("watchdog recovery: ensuring payload volume is mounted before smbd restart", log_text)
        self.assertIn("watchdog recovery: ensuring active share volume is mounted before smbd restart: share=USB", log_text)
        self.assertIn("watchdog recovery: smbd restart requested", log_text)

    def test_watchdog_loop_continues_after_failed_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            common_path = flash / "common.sh"
            common_path.write_text(
                common_path.read_text()
                + textwrap.dedent(
                    f"""\

                    tc_load_payload_state() {{ return 1; }}
                    sleep() {{ echo "sleep $1"; }}
                    tc_watchdog_disk_iteration() {{
                        count=$(cat {tmp_path}/watchdog-count 2>/dev/null || echo 0)
                        count=$((count + 1))
                        echo "$count" >{tmp_path}/watchdog-count
                        echo "iteration $count"
                        if [ "$count" -ge 2 ]; then
                            exit 0
                        fi
                        return 1
                    }}
                    tc_watchdog_service_iteration() {{ echo service; }}
                    """
                )
            )

            proc = subprocess.run(
                [str(flash / "watchdog.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            log_text = (memory / "samba4/var/watchdog.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "iteration 1\nsleep 10\niteration 2\n")
        self.assertIn("watchdog startup beginning", log_text)

    def test_watchdog_startup_cleans_stale_mast_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            state_dir = memory / "samba4/var"
            state_dir.mkdir(parents=True)
            stale_volumes = state_dir / "watchdog-volumes.tsv.123"
            stale_raw = state_dir / "watchdog-mast.raw.123.debounce"
            keep_file = state_dir / "topology.signature"
            stale_volumes.write_text("stale volumes")
            stale_raw.write_text("stale raw")
            keep_file.write_text("keep")
            common_path = flash / "common.sh"
            common_path.write_text(
                common_path.read_text()
                + textwrap.dedent(
                    f"""\

                    tc_load_payload_state() {{ return 1; }}
                    tc_watchdog_disk_iteration() {{
                        [ ! -e {stale_volumes} ] || echo stale-volumes-present
                        [ ! -e {stale_raw} ] || echo stale-raw-present
                        [ -e {keep_file} ] && echo keep-present
                        exit 0
                    }}
                    """
                )
            )

            proc = subprocess.run(
                [str(flash / "watchdog.sh")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "keep-present\n")

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
                    get_iface_mac() {{ echo 80:EA:96:E6:58:68; }}
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
                    tc_start_mdns_capture
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
        self.assertIn("--skip-capture-if-snapshot-newer-than-boot", proc.stdout)
        self.assertIn("--auto-ip", proc.stdout)
        self.assertIn("mDNS snapshot capture exited with failure; final advertiser will use generated records if needed", proc.stdout)

    def test_common_mdns_capture_skips_when_snapshot_is_newer_than_boot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            marker = tmp_path / "capture.started"
            (flash / "mdns-advertiser").write_text(
                "#!/bin/sh\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  case \"$1\" in\n"
                "    --skip-capture-if-snapshot-newer-than-boot) shift; echo \"mDNS snapshot capture skipped; $1 is newer than current boot\" >&2; exit 0 ;;\n"
                "  esac\n"
                "  shift\n"
                "done\n"
                f"echo started >{shlex.quote(str(marker))}\n"
                "printf 'capture-args:%s\\n' \"$*\"\n"
                "exit 0\n"
            )
            (flash / "mdns-advertiser").chmod(0o755)
            script = tmp_path / "mdns-capture-skip-fresh-snapshot.sh"
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
                    echo trusted >"$APPLE_MDNS_SNAPSHOT"
                    echo raw >"$ALL_MDNS_SNAPSHOT"
                    tc_start_mdns_capture
                    [ ! -f {shlex.quote(str(marker))} ] && echo capture-skipped
                    printf 'apple='
                    cat "$APPLE_MDNS_SNAPSHOT"
                    printf 'all='
                    cat "$ALL_MDNS_SNAPSHOT"
                    cat "$TC_MDNS_LOG_FILE"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("capture-skipped\n", proc.stdout)
        self.assertIn("apple=trusted\n", proc.stdout)
        self.assertIn("all=raw\n", proc.stdout)
        self.assertIn("mDNS snapshot capture skipped;", proc.stdout)
        self.assertIn("is newer than current boot", proc.stdout)
        self.assertNotIn("capture-args:", proc.stdout)
        self.assertIn("launching mdns-advertiser capture", proc.stdout)

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
                    get_iface_mac() {{ echo 80:EA:96:E6:58:68; }}
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
                    tc_start_mdns_capture
                    tc_finalize_mdns_snapshot_after_capture
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
        self.assertIn("trusted Apple mDNS snapshot present:", proc.stdout)
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
                    get_iface_mac() {{ echo 80:EA:96:E6:58:68; }}
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
                    tc_start_mdns_capture
                    tc_finalize_mdns_snapshot_after_capture
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
        self.assertIn("mDNS snapshot capture did not produce trusted Apple snapshot; generating AirPort fallback", proc.stdout)
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
                    get_iface_mac() {{ echo 80:EA:96:E6:58:68; }}
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
                    get_iface_mac() {{ echo 80:EA:96:E6:58:68; }}
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

    def test_common_boot_mount_uses_diskd_usevolume_without_shared_wait_when_it_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            events = tmp_path / "events.log"
            acp = tmp_path / "acp"
            acp.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    echo "acp $@" >>{shlex.quote(str(events))}
                    root=${{3#path:s:}}
                    touch "$root/.mounted"
                    """
                )
            )
            acp.chmod(0o755)
            script = tmp_path / "boot-mount-diskd.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    DISKD_USE_VOLUME_ATTEMPTS=5
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR"
                    is_volume_root_mounted() {{ [ -f "$1/.mounted" ]; }}
                    sleep() {{ echo "sleep $1" >>{events}; }}
                    cat >"$RAM_VAR/test-volumes.tsv" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    sd0	0	dk3	{volumes}/dk3	USB	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    tc_mount_mast_volumes_for_boot "$RAM_VAR/test-volumes.tsv"
                    cat {events}
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.splitlines(),
            [
                f"acp rpc diskd.useVolume path:s:{volumes}/dk2",
                f"acp rpc diskd.useVolume path:s:{volumes}/dk3",
            ],
        )

    def test_common_boot_mount_retries_diskd_per_volume_without_mount_hfs_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            events = tmp_path / "events.log"
            acp = tmp_path / "acp"
            acp.write_text(f"#!/bin/sh\necho \"acp $@\" >>{shlex.quote(str(events))}\n")
            acp.chmod(0o755)
            script = tmp_path / "boot-mount-global-deadline.sh"
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
                    mkdir -p "$RAM_VAR"
                    is_volume_root_mounted() {{ return 1; }}
                    sleep() {{ echo "sleep $1" >>{events}; }}
                    cat >"$RAM_VAR/test-volumes.tsv" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    sd0	0	dk3	{volumes}/dk3	USB	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    tc_mount_mast_volumes_for_boot "$RAM_VAR/test-volumes.tsv"
                    cat {events}
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.splitlines(),
            [
                f"acp rpc diskd.useVolume path:s:{volumes}/dk2",
                "sleep 3",
                "sleep 3",
                "sleep 1",
                "sleep 1",
                f"acp rpc diskd.useVolume path:s:{volumes}/dk2",
                "sleep 3",
                "sleep 3",
                "sleep 1",
                f"acp rpc diskd.useVolume path:s:{volumes}/dk3",
                "sleep 3",
                "sleep 3",
                "sleep 1",
                "sleep 1",
                f"acp rpc diskd.useVolume path:s:{volumes}/dk3",
                "sleep 3",
                "sleep 3",
                "sleep 1",
            ],
        )
        self.assertIn("boot disk load: activating MaSt volumes through diskd.useVolume", log_text)
        self.assertIn(
            f"MaSt volume {volumes}/dk2: diskd.useVolume did not mount {volumes}/dk2 after 2 attempt(s); leaving volume unavailable without mount_hfs fallback",
            log_text,
        )
        self.assertIn(
            f"MaSt volume {volumes}/dk3: diskd.useVolume did not mount {volumes}/dk3 after 2 attempt(s); leaving volume unavailable without mount_hfs fallback",
            log_text,
        )
        self.assertNotIn("MaSt volume diskd.useVolume wait beginning", log_text)
        self.assertNotIn("launching mount_hfs", log_text)
        self.assertNotIn("Apple mount", log_text)

    def test_common_ata_idle_tunes_only_builtin_wd_disks_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            atactl_log = tmp_path / "atactl.log"
            atactl = tmp_path / "atactl"
            atactl.write_text(f"#!/bin/sh\necho \"$@\" >>{shlex.quote(str(atactl_log))}\n")
            atactl.chmod(0o755)
            common_path = flash / "common.sh"
            common_path.write_text(common_path.read_text().replace("/sbin/atactl", str(atactl)))
            script = tmp_path / "ata-idle.sh"
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
                    is_volume_root_mounted() {{ return 0; }}
                    cat >"$RAM_VAR/test-volumes.tsv" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    wd0	1	dk3	{volumes}/dk3	More	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    sd0	0	dk4	{volumes}/dk4	USB	cccccccc-cccc-cccc-cccc-cccccccccccc
                    sd1	1	dk5	{volumes}/dk5	SSD	dddddddd-dddd-dddd-dddd-dddddddddddd
                    EOF
                    tc_configure_ata_idle_for_mast_disks "$RAM_VAR/test-volumes.tsv"
                    cat {atactl_log}
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "wd0 setidle 300\n")
        self.assertIn("ATA drive settings: set wd0 idle timer to 300s", log_text)
        self.assertIn("ATA drive settings: skipping sd0 for /dev/dk4; MaSt marks disk as external", log_text)
        self.assertIn("ATA drive settings: skipping sd1 for /dev/dk5; not a wd ATA disk", log_text)

    def test_common_ata_idle_zero_disables_tuning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            atactl_log = tmp_path / "atactl.log"
            atactl = tmp_path / "atactl"
            atactl.write_text(f"#!/bin/sh\necho \"$@\" >>{shlex.quote(str(atactl_log))}\n")
            atactl.chmod(0o755)
            common_path = flash / "common.sh"
            common_path.write_text(common_path.read_text().replace("/sbin/atactl", str(atactl)))
            script = tmp_path / "ata-idle-disabled.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    ATA_IDLE_SECONDS=0
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR"
                    is_volume_root_mounted() {{ return 0; }}
                    cat >"$RAM_VAR/test-volumes.tsv" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    tc_configure_ata_idle_for_mast_disks "$RAM_VAR/test-volumes.tsv"
                    cat {atactl_log}
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "wd0 setidle 0\n")
        self.assertIn("ATA drive settings: disabled wd0 idle timer", log_text)

    def test_common_ata_standby_applies_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            atactl_log = tmp_path / "atactl.log"
            atactl = tmp_path / "atactl"
            atactl.write_text(f"#!/bin/sh\necho \"$@\" >>{shlex.quote(str(atactl_log))}\n")
            atactl.chmod(0o755)
            common_path = flash / "common.sh"
            common_path.write_text(common_path.read_text().replace("/sbin/atactl", str(atactl)))
            script = tmp_path / "ata-standby.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    ATA_STANDBY=0
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR"
                    is_volume_root_mounted() {{ return 0; }}
                    cat >"$RAM_VAR/test-volumes.tsv" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    tc_configure_ata_drive_settings_for_mast_disks "$RAM_VAR/test-volumes.tsv"
                    cat {atactl_log}
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "wd0 setidle 300\nwd0 setstandby 0\n")
        self.assertIn("ATA drive settings: disabled wd0 standby timer", log_text)

    def test_common_ata_idle_failure_logs_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            atactl = tmp_path / "atactl"
            atactl.write_text("#!/bin/sh\nexit 1\n")
            atactl.chmod(0o755)
            common_path = flash / "common.sh"
            common_path.write_text(common_path.read_text().replace("/sbin/atactl", str(atactl)))
            script = tmp_path / "ata-idle-failure.sh"
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
                    is_volume_root_mounted() {{ return 0; }}
                    cat >"$RAM_VAR/test-volumes.tsv" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    tc_configure_ata_idle_for_mast_disks "$RAM_VAR/test-volumes.tsv"
                    echo continued
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "continued\n")
        self.assertIn("ATA drive settings: failed to set wd0 idle timer to 300s", log_text)

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

    def test_common_boot_and_watchdog_mount_policies_use_separate_attempt_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "mount-policies.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    DISKD_USE_VOLUME_ATTEMPTS=7
                    WATCHDOG_DISKD_USE_VOLUME_ATTEMPTS=2
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR" {volumes}/dk2
                    tc_wake_or_mount_volume_with_policy() {{
                        printf '%s %s %s %s\\n' "$1" "$2" "$3" "$4"
                        return 1
                    }}
                    tc_wake_or_mount_volume /dev/dk2 {volumes}/dk2 || true
                    tc_watchdog_wake_or_mount_volume /dev/dk2 {volumes}/dk2 || true
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(lines[0], f"/dev/dk2 {volumes}/dk2 7 MaSt volume {volumes}/dk2")
        self.assertEqual(lines[1], f"/dev/dk2 {volumes}/dk2 2 watchdog volume {volumes}/dk2")

    def test_common_watchdog_mast_users_reclaims_only_active_zero_user_shares(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            events = tmp_path / "events.log"
            acp = tmp_path / "acp"
            acp.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    case "$*" in
                        "-A MaSt")
                            cat <<'OUT'
                    [
                        {{
                            deviceName="wd0"
                            partitions=
                            [
                                {{
                                    deviceName="dk2"
                                    format="hfs"
                                    users=1
                                    name="Data"
                                    uuid=f42bdb83 c2655522 a0872560 6a4d0abf |binary| (16 bytes)
                                }}
                            ]
                            builtin=true
                        }}
                        {{
                            deviceName="sd0"
                            partitions=
                            [
                                {{
                                    deviceName="dk3"
                                    format="hfs"
                                    users=0
                                    name="USB"
                                    uuid=51f93e6f dc69524d 986dcee4 d7cb3573 |binary| (16 bytes)
                                }}
                                {{
                                    deviceName="dk4"
                                    format="hfs"
                                    users=0
                                    name="Ignored"
                                    uuid=aaaaaaaa bbbbbbbb cccccccc dddddddd |binary| (16 bytes)
                                }}
                            ]
                        }}
                    ]
                    OUT
                            ;;
                        rpc*diskd.useVolume*)
                            echo "$@" >>{shlex.quote(str(events))}
                            root=${{3#path:s:}}
                            mkdir -p "$root"
                            touch "$root/.mounted"
                            ;;
                    esac
                    """
                )
            )
            acp.chmod(0o755)
            script = tmp_path / "watchdog-mast-users-reclaim.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR" {volumes}/dk2 {volumes}/dk3 {volumes}/dk4
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    USB	{volumes}/dk3	dk3	0	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    is_volume_root_mounted() {{ [ -f "$1/.mounted" ]; }}
                    sleep() {{ echo "sleep $1" >>{events}; }}
                    tc_watchdog_capture_mast_state "$RAM_VAR/mast-users.tsv" "$RAM_VAR/mast-users.raw"
                    tc_watchdog_check_active_mast_users "$RAM_VAR/mast-users.raw"
                    cat {events}
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.splitlines(),
            [
                f"rpc diskd.useVolume path:s:{volumes}/dk3",
            ],
        )
        self.assertIn("watchdog disk check: managed volume dk3 users=0 requires diskd reclaim", log_text)
        self.assertIn("watchdog disk check: reclaimed 1 managed volume(s) with users=0", log_text)
        self.assertNotIn("dk4 requires diskd reclaim", log_text)

    def test_common_watchdog_mast_users_requires_snapshot_argument_for_active_shares(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-mast-users-requires-snapshot.sh"
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
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    status=0
                    tc_watchdog_check_active_mast_users || status=$?
                    echo "status=$status"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "status=1\n")
        self.assertIn("watchdog disk check: MaSt users snapshot argument is required", log_text)

    def test_common_watchdog_mast_users_failure_falls_back_to_disk_runtime_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            acp = tmp_path / "acp"
            acp.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    case "$*" in
                        "-A MaSt")
                            cat <<'OUT'
                    [
                        {
                            deviceName="sd0"
                            partitions=
                            [
                                {
                                    deviceName="dk3"
                                    format="hfs"
                                    users=0
                                    name="USB"
                                    uuid=51f93e6f dc69524d 986dcee4 d7cb3573 |binary| (16 bytes)
                                }
                            ]
                        }
                    ]
                    OUT
                            ;;
                        rpc*diskd.useVolume*) exit 1 ;;
                    esac
                    """
                )
            )
            acp.chmod(0o755)
            script = tmp_path / "watchdog-mast-users-reload.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR" {volumes}/dk3
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    USB	{volumes}/dk3	dk3	0	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    tc_topology_changed_debounced_from_snapshot() {{ return 1; }}
                    is_volume_root_mounted() {{ return 1; }}
                    sleep() {{ echo "sleep $1"; }}
                    tc_live_reload_disk_runtime() {{ echo "reload $1"; return 0; }}
                    tc_exec_start_samba() {{ echo "exec $1"; exit 42; }}
                    tc_watchdog_disk_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "sleep 1\nreload managed diskd users dropped to zero\n")

    def test_common_watchdog_disk_iteration_reuses_single_mast_snapshot_for_topology_and_users(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            count_file = tmp_path / "acp-count"
            acp = tmp_path / "acp"
            acp.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    count=$(cat {shlex.quote(str(count_file))} 2>/dev/null || echo 0)
                    count=$((count + 1))
                    echo "$count" >{shlex.quote(str(count_file))}
                    case "$*" in
                        "-A MaSt")
                            cat <<'OUT'
                    [
                        {{
                            deviceName="wd0"
                            partitions=
                            [
                                {{
                                    deviceName="dk2"
                                    format="hfs"
                                    users=1
                                    name="Data"
                                    uuid=f42bdb83 c2655522 a0872560 6a4d0abf |binary| (16 bytes)
                                }}
                            ]
                            builtin=true
                        }}
                    ]
                    OUT
                            ;;
                    esac
                    """
                )
            )
            acp.chmod(0o755)
            script = tmp_path / "watchdog-single-mast-snapshot.sh"
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
                    cat >"$TC_TOPOLOGY_SIGNATURE" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	f42bdb83-c265-5522-a087-25606a4d0abf
                    EOF
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	f42bdb83-c265-5522-a087-25606a4d0abf
                    EOF
                    tc_write_payload_state {volumes}/dk2/.samba4 {volumes}/dk2 /dev/dk2
                    sleep() {{ echo "unexpected sleep $1"; }}
                    tc_live_reload_disk_runtime() {{ echo "unexpected reload $1"; return 0; }}
                    tc_exec_start_samba() {{ echo "unexpected exec $1"; exit 42; }}
                    tc_watchdog_wake_or_mount_volume() {{ echo "unexpected reclaim $1 $2"; return 0; }}
                    tc_watchdog_disk_iteration
                    printf 'acp_count=%s\\n' "$(cat {shlex.quote(str(count_file))})"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "acp_count=1\n")

    def test_common_topology_compare_treats_empty_snapshot_as_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "topology-empty-valid.sh"
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
                    fresh_path="$RAM_VAR/fresh.tsv"
                    row='wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'

                    : >"$TC_TOPOLOGY_SIGNATURE"
                    : >"$fresh_path"
                    tc_topology_changed_from_file "$fresh_path" && echo empty-empty=changed || echo empty-empty=same

                    printf '%s\\n' "$row" >"$TC_TOPOLOGY_SIGNATURE"
                    : >"$fresh_path"
                    tc_topology_changed_from_file "$fresh_path" && echo nonempty-empty=changed || echo nonempty-empty=same

                    : >"$TC_TOPOLOGY_SIGNATURE"
                    printf '%s\\n' "$row" >"$fresh_path"
                    tc_topology_changed_from_file "$fresh_path" && echo empty-nonempty=changed || echo empty-nonempty=same

                    rm -f "$fresh_path"
                    tc_topology_changed_from_file "$fresh_path" && echo missing=changed || echo missing=failed
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "empty-empty=same\nnonempty-empty=changed\nempty-nonempty=changed\nmissing=failed\n",
        )

    def test_common_watchdog_disk_iteration_probes_same_topology_no_payload_without_reexec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-no-payload-nonempty-mast.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    cat >"$TC_TOPOLOGY_SIGNATURE" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    tc_watchdog_capture_mast_state() {{
                        cat >"$1" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                        : >"$2"
                        return 0
                    }}
                    tc_refresh_disk_state() {{ echo refresh-no-payload; : >"$TC_PAYLOAD_TSV"; return 0; }}
                    tc_exec_start_samba() {{ echo "unexpected exec $1"; exit 42; }}
                    tc_watchdog_disk_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "refresh-no-payload\n")

    def test_common_watchdog_disk_iteration_keeps_probing_same_topology_until_payload_appears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            refresh_count = tmp_path / "refresh-count"
            script = tmp_path / "watchdog-no-payload-keeps-probing.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    cat >"$TC_TOPOLOGY_SIGNATURE" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    tc_watchdog_capture_mast_state() {{
                        cat >"$1" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                        : >"$2"
                        return 0
                    }}
                    tc_refresh_disk_state() {{
                        count=$(/bin/cat {shlex.quote(str(refresh_count))} 2>/dev/null || echo 0)
                        count=$((count + 1))
                        echo "$count" >{shlex.quote(str(refresh_count))}
                        echo "refresh-$count"
                        : >"$TC_PAYLOAD_TSV"
                        return 0
                    }}
                    tc_exec_start_samba() {{ echo "unexpected exec $1"; exit 42; }}
                    tc_watchdog_disk_iteration
                    tc_watchdog_disk_iteration
                    printf 'refresh-count=%s\\n' "$(/bin/cat {shlex.quote(str(refresh_count))})"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "refresh-1\nrefresh-2\nrefresh-count=2\n")

    def test_common_watchdog_disk_iteration_reexecs_when_same_topology_payload_appears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-same-topology-payload-appears.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    cat >"$TC_TOPOLOGY_SIGNATURE" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    tc_watchdog_capture_mast_state() {{
                        cat >"$1" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                        : >"$2"
                        return 0
                    }}
                    tc_refresh_disk_state() {{
                        echo refresh-payload
                        tc_write_payload_state {volumes}/dk2/.samba4 {volumes}/dk2 /dev/dk2
                        return 0
                    }}
                    tc_exec_start_samba() {{ echo "exec $1"; exit 42; }}
                    tc_watchdog_disk_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 42, proc.stderr)
        self.assertEqual(proc.stdout, "refresh-payload\nexec managed MaSt disks now have payload state\n")

    def test_common_watchdog_disk_iteration_mast_failure_during_payload_probe_keeps_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-payload-probe-mast-failure.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    echo old-topology >"$TC_TOPOLOGY_SIGNATURE"
                    echo old-shares >"$TC_SHARES_TSV"
                    echo old-adisk >"$TC_ADISK_TSV"
                    : >"$TC_PAYLOAD_TSV"
                    tc_watchdog_capture_mast_state() {{
                        cat >"$1" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                        : >"$2"
                        return 0
                    }}
                    tc_topology_changed_debounced_from_snapshot() {{ return 1; }}
                    tc_watchdog_check_active_mast_users() {{ return 0; }}
                    tc_refresh_disk_state() {{ echo refresh-failed; return 1; }}
                    tc_exec_start_samba() {{ echo "unexpected exec $1"; exit 42; }}
                    tc_watchdog_disk_iteration
                    printf 'topology='
                    /bin/cat "$TC_TOPOLOGY_SIGNATURE"
                    printf 'shares='
                    /bin/cat "$TC_SHARES_TSV"
                    printf 'adisk='
                    /bin/cat "$TC_ADISK_TSV"
                    printf 'payload-size=%s\\n' "$([ -s "$TC_PAYLOAD_TSV" ] && echo nonempty || echo empty)"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "refresh-failed\ntopology=old-topology\nshares=old-shares\nadisk=old-adisk\npayload-size=empty\n",
        )

    def test_common_watchdog_disk_iteration_empty_topology_no_payload_does_not_probe_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-empty-topology-no-payload.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    : >"$TC_TOPOLOGY_SIGNATURE"
                    tc_watchdog_capture_mast_state() {{
                        : >"$1"
                        : >"$2"
                        return 0
                    }}
                    tc_refresh_disk_state() {{ echo unexpected-refresh; return 1; }}
                    tc_exec_start_samba() {{ echo "unexpected exec $1"; exit 42; }}
                    tc_watchdog_disk_iteration
                    echo done
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "done\n")

    def test_common_watchdog_disk_iteration_skips_users_check_when_mast_snapshot_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            acp = tmp_path / "acp"
            acp.write_text("#!/bin/sh\nexit 1\n")
            acp.chmod(0o755)
            script = tmp_path / "watchdog-mast-snapshot-fails.sh"
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
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	f42bdb83-c265-5522-a087-25606a4d0abf
                    EOF
                    tc_live_reload_disk_runtime() {{ echo "unexpected reload $1"; return 0; }}
                    tc_exec_start_samba() {{ echo "unexpected exec $1"; exit 42; }}
                    tc_watchdog_wake_or_mount_volume() {{ echo "unexpected reclaim $1 $2"; return 0; }}
                    tc_watchdog_disk_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("watchdog disk check: MaSt snapshot read failed", log_text)
        self.assertIn("watchdog disk check: skipping MaSt users check because snapshot is unavailable", log_text)
        self.assertNotIn("MaSt users recovery requires full disk runtime reload", log_text)

    def test_common_watchdog_service_iteration_checks_processes_without_mounting_active_shares(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            (volumes / "dk2/ShareRoot").mkdir(parents=True)
            (volumes / "dk3").mkdir()
            script = tmp_path / "watchdog-flow.sh"
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
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    USB	{volumes}/dk3	dk3	0	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    sleep() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_samba_runtime_expected() {{ return 0; }}
                    tc_watchdog_reconcile_smb_bind_interfaces() {{ :; }}
                    tc_watchdog_wake_or_mount_volume() {{ echo "mount $1 $2"; return 0; }}
                    tc_start_smbd_if_needed() {{ echo smbd; }}
                    runtime_process_present_by_ucomm() {{ return 0; }}
                    tc_all_managed_services_healthy() {{ return 0; }}
                    tc_watchdog_service_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "smbd\n",
        )

    def test_common_watchdog_service_iteration_refreshes_identity_once_before_service_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            (flash / "mdns-advertiser").write_text("#!/bin/sh\nexit 0\n")
            (flash / "mdns-advertiser").chmod(0o755)
            script = tmp_path / "watchdog-identity-refresh.sh"
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
                    NBNS_ENABLED=1
                    TC_PAYLOAD_DIR={volumes}/dk2/.samba4
                    TC_PAYLOAD_VOLUME={volumes}/dk2
                    MDNS_HOST_LABEL=stale-host
                    SMB_NETBIOS_NAME=StaleName
                    TC_RUNTIME_IDENTITY_READY=1
                    sleep() {{ :; }}
                    tc_samba_runtime_expected() {{ return 0; }}
                    tc_watchdog_reconcile_smb_bind_interfaces() {{ :; }}
                    tc_start_smbd_if_needed() {{ return 0; }}
                    tc_init_runtime_identity() {{
                        echo identity-refresh
                        MDNS_HOST_LABEL=fresh-host
                        SMB_NETBIOS_NAME=FreshName
                        TC_RUNTIME_IDENTITY_READY=1
                    }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            smbd) return 0 ;;
                            mdns-advertiser) return 1 ;;
                            nbns-advertiser) return 1 ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_launch_mdns_advertiser() {{ echo "mdns-host=$MDNS_HOST_LABEL"; }}
                    tc_launch_nbns() {{ echo "nbns-name=$SMB_NETBIOS_NAME"; }}
                    tc_mdns_auto_ip_available() {{ return 0; }}
                    tc_nbns_auto_ip_available() {{ return 0; }}
                    tc_all_managed_services_healthy() {{ return 1; }}
                    status=0
                    tc_watchdog_service_iteration || status=$?
                    echo "status=$status"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "identity-refresh\nmdns-host=fresh-host\nnbns-name=FreshName\nstatus=1\n")

    def test_common_watchdog_service_iteration_reloads_services_on_identity_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            payload.mkdir(parents=True)
            script = tmp_path / "watchdog-identity-change.sh"
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
                    TC_WATCHDOG_LAST_IDENTITY_SIGNATURE=$(printf 'Old\\nold\\nOld\\nOld\\n')
                    TC_WATCHDOG_IDENTITY_SIGNATURE_READY=1
                    sleep() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ echo hosts; }}
                    tc_watchdog_reconcile_smb_bind_interfaces() {{ :; }}
                    tc_init_runtime_identity() {{
                        echo identity
                        MDNS_INSTANCE_NAME=Fresh
                        MDNS_HOST_LABEL=fresh
                        SMB_NETBIOS_NAME=Fresh
                        SMB_SERVER_STRING=Fresh
                        TC_RUNTIME_IDENTITY_READY=1
                    }}
                    tc_load_payload_state() {{
                        TC_PAYLOAD_DIR={payload}
                        TC_PAYLOAD_VOLUME={volumes}/dk2
                        TC_PAYLOAD_DEVICE=/dev/dk2
                        return 0
                    }}
                    tc_generate_smb_conf() {{ echo "generate $1"; }}
                    runtime_process_present_by_ucomm() {{ echo "process $1"; return 0; }}
                    tc_nbns_bound_ipv4_udp_137() {{ return 0; }}
                    tc_reload_smbd_config() {{ echo reload; }}
                    stop_runtime_process_by_ucomm() {{ echo "stop $1"; }}
                    tc_start_smbd_if_needed() {{ echo smbd; }}
                    tc_watchdog_reconcile_mdns() {{ echo mdns-if-needed; }}
                    tc_all_managed_services_healthy() {{ echo healthy; return 0; }}
                    tc_watchdog_service_iteration
                    printf 'signature\\n'
                    printf '%s\\n' "$TC_WATCHDOG_LAST_IDENTITY_SIGNATURE"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "\n".join(
                (
                    "hosts",
                    "identity",
                    f"generate {payload}",
                    "process smbd",
                    "reload",
                    "stop mdns-advertiser",
                    "stop nbns-advertiser",
                    "smbd",
                    "mdns-if-needed",
                    "process nbns-advertiser",
                    "healthy",
                    "signature",
                    "Fresh",
                    "fresh",
                    "Fresh",
                    "Fresh",
                    "",
                )
            ),
        )

    def test_common_smbd_recovery_fails_when_active_share_mount_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            private = payload / "private"
            private.mkdir(parents=True)
            (payload / "smbd").write_text("payload smbd")
            (payload / "smbd").chmod(0o755)
            (private / "smbpasswd").write_text("user\n")
            (private / "username.map").write_text("root = user\n")
            script = tmp_path / "watchdog-smbd-recovery-share-missing.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR" "$RAM_SBIN" "$RAM_ETC"
                    printf '%s\\t%s\\t%s\\n' {payload} {volumes}/dk2 /dev/dk2 >"$TC_PAYLOAD_TSV"
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    USB	{volumes}/dk3	dk3	0	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    cat >"$TC_SMBD_BIN" <<'EOF'
                    #!/bin/sh
                    echo start
                    EOF
                    chmod 755 "$TC_SMBD_BIN"
                    : >"$TC_SMBD_CONF"
                    tc_watchdog_refresh_runtime_identity_for_recovery() {{ :; }}
                    runtime_process_present_by_ucomm() {{ return 1; }}
                    tc_watchdog_wake_or_mount_volume() {{
                        echo "mount $1 $2"
                        [ "$1" != "/dev/dk3" ]
                    }}
                    if tc_start_smbd_if_needed; then
                        echo status=0
                    else
                        echo status=$?
                    fi
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"mount /dev/dk2 {volumes}/dk2\n", proc.stdout)
        self.assertIn(f"mount /dev/dk3 {volumes}/dk3\n", proc.stdout)
        self.assertIn("status=1\n", proc.stdout)
        self.assertNotIn("start\n", proc.stdout)
        self.assertIn("watchdog recovery: one or more active share volumes are unavailable before smbd restart", log_text)

    def test_common_watchdog_service_iteration_does_not_reexec_when_share_state_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-share-state-missing.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR" "$RAM_SBIN" "$RAM_ETC"
                    : >"$TC_SMBD_BIN"
                    chmod 755 "$TC_SMBD_BIN"
                    : >"$TC_SMBD_CONF"
                    sleep() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_samba_runtime_expected() {{ return 0; }}
                    tc_watchdog_reconcile_smb_bind_interfaces() {{ :; }}
                    tc_start_smbd_if_needed() {{ echo smbd; return 0; }}
                    runtime_process_present_by_ucomm() {{ return 0; }}
                    tc_all_managed_services_healthy() {{ return 0; }}
                    tc_exec_start_samba() {{ echo "exec $1"; exit 42; }}
                    tc_watchdog_service_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "smbd\n")

    def test_common_watchdog_disk_iteration_reexecs_start_samba_on_topology_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-topology.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    sleep() {{ :; }}
                    tc_watchdog_capture_mast_state() {{ : >"$1"; : >"$2"; return 0; }}
                    tc_topology_changed_debounced_from_snapshot() {{ return 0; }}
                    tc_exec_start_samba() {{ echo "exec $1"; exit 42; }}
                    tc_watchdog_disk_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 42, proc.stderr)
        self.assertEqual(proc.stdout, "exec MaSt topology changed\n")

    def test_common_watchdog_disk_iteration_ignores_transient_topology_change_after_debounce(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-transient-topology.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    sleep() {{ :; }}
                    tc_watchdog_capture_mast_state() {{ : >"$1"; : >"$2"; return 0; }}
                    tc_topology_changed_debounced_from_snapshot() {{ return 1; }}
                    tc_start_smbd_if_needed() {{ echo smbd; }}
                    runtime_process_present_by_ucomm() {{ return 0; }}
                    tc_exec_start_samba() {{ echo "exec $1"; exit 42; }}
                    tc_watchdog_disk_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_common_watchdog_disk_iteration_live_reloads_on_topology_change_without_reexec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            payload.mkdir(parents=True)
            script = tmp_path / "watchdog-live-topology.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR" "$RAM_SBIN" "$RAM_ETC"
                    : >"$TC_SMBD_BIN"
                    chmod 755 "$TC_SMBD_BIN"
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    tc_write_payload_state {payload} {volumes}/dk2 /dev/dk2
                    sleep() {{ :; }}
                    tc_probe_smb_bind_interfaces() {{ echo "$TC_SMB_BIND_INTERFACES"; }}
                    tc_watchdog_capture_mast_state() {{ : >"$1"; : >"$2"; return 0; }}
                    tc_topology_changed_debounced_from_snapshot() {{ return 0; }}
                    tc_refresh_disk_state() {{
                        echo refresh
                        tc_write_payload_state {payload} {volumes}/dk2 /dev/dk2
                        cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                        cat >"$TC_ADISK_TSV" <<'EOF'
                    Data	dk2	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa	0x82
                    EOF
                    }}
                    tc_prepare_local_hostname_resolution() {{ echo hostname; }}
                    tc_init_runtime_identity() {{
                        echo identity
                        MDNS_INSTANCE_NAME=Live
                        MDNS_HOST_LABEL=live
                        SMB_NETBIOS_NAME=Live
                        SMB_SERVER_STRING=Live
                        TC_RUNTIME_IDENTITY_READY=1
                    }}
                    tc_generate_smb_conf() {{ echo "generate $1"; }}
                    tc_reload_smbd_config() {{ echo reload; }}
                    tc_launch_mdns_advertiser() {{ echo "mdns $1 $2 $3"; }}
                    tc_mdns_auto_ip_available() {{ return 0; }}
                    tc_start_smbd_if_needed() {{ echo smbd; }}
                    runtime_process_present_by_ucomm() {{ return 0; }}
                    tc_exec_start_samba() {{ echo "exec $1"; exit 42; }}
                    tc_watchdog_disk_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "\n".join(
                (
                    "refresh",
                    "hostname",
                    "identity",
                    f"generate {payload}",
                    "reload",
                    "",
                )
            ),
        )
        self.assertIn("watchdog recovery: attempting live disk runtime refresh: MaSt topology changed", log_text)
        self.assertIn("mDNS auto-ip check failed; missing", log_text)
        self.assertIn("watchdog recovery: live disk runtime refresh complete", log_text)
        self.assertNotIn("re-execing start-samba.sh", log_text)

    def test_common_watchdog_live_reload_restarts_mdns_but_leaves_healthy_nbns_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            payload.mkdir(parents=True)
            script = tmp_path / "watchdog-live-healthy-nbns.sh"
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
                    mkdir -p "$RAM_VAR" "$RAM_SBIN" "$RAM_ETC"
                    : >"$TC_SMBD_BIN"
                    chmod 755 "$TC_SMBD_BIN"
                    TC_MDNS_AUTO_IP_SEEN=1
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    tc_write_payload_state {payload} {volumes}/dk2 /dev/dk2
                    sleep() {{ :; }}
                    tc_probe_smb_bind_interfaces() {{ echo "$TC_SMB_BIND_INTERFACES"; }}
                    tc_watchdog_capture_mast_state() {{ : >"$1"; : >"$2"; return 0; }}
                    tc_topology_changed_debounced_from_snapshot() {{ return 0; }}
                    tc_refresh_disk_state() {{
                        echo refresh
                        tc_write_payload_state {payload} {volumes}/dk2 /dev/dk2
                        cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                        cat >"$TC_ADISK_TSV" <<'EOF'
                    Data	dk2	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa	0x82
                    EOF
                    }}
                    tc_prepare_local_hostname_resolution() {{ echo hostname; }}
                    tc_init_runtime_identity() {{
                        echo identity
                        MDNS_INSTANCE_NAME=Live
                        MDNS_HOST_LABEL=live
                        SMB_NETBIOS_NAME=Live
                        SMB_SERVER_STRING=Live
                        TC_RUNTIME_IDENTITY_READY=1
                    }}
                    tc_generate_smb_conf() {{ echo "generate $1"; }}
                    tc_reload_smbd_config() {{ echo reload; }}
                    tc_launch_mdns_advertiser() {{ echo "mdns $1 $2 $3"; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            smbd|nbns-advertiser) return 0 ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_nbns_bound_ipv4_udp_137() {{ return 0; }}
                    tc_restart_nbns() {{ echo unexpected-nbns; return 1; }}
                    tc_exec_start_samba() {{ echo "exec $1"; exit 42; }}
                    tc_watchdog_disk_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "\n".join(
                (
                    "refresh",
                    "hostname",
                    "identity",
                    f"generate {payload}",
                    "reload",
                    "mdns watchdog topology refresh 1 10",
                    "",
                )
            ),
        )
        self.assertNotIn("unexpected-nbns", proc.stdout)
        self.assertIn("watchdog recovery: live disk runtime refresh complete", log_text)

    def test_common_watchdog_live_reload_reconciles_unbound_nbns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            payload.mkdir(parents=True)
            script = tmp_path / "watchdog-live-unbound-nbns.sh"
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
                    mkdir -p "$RAM_VAR" "$RAM_SBIN" "$RAM_ETC"
                    : >"$TC_SMBD_BIN"
                    chmod 755 "$TC_SMBD_BIN"
                    TC_MDNS_AUTO_IP_SEEN=1
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    tc_write_payload_state {payload} {volumes}/dk2 /dev/dk2
                    nbns_present=1
                    sleep() {{ :; }}
                    tc_probe_smb_bind_interfaces() {{ echo "$TC_SMB_BIND_INTERFACES"; }}
                    tc_watchdog_capture_mast_state() {{ : >"$1"; : >"$2"; return 0; }}
                    tc_topology_changed_debounced_from_snapshot() {{ return 0; }}
                    tc_refresh_disk_state() {{
                        echo refresh
                        tc_write_payload_state {payload} {volumes}/dk2 /dev/dk2
                        cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                        cat >"$TC_ADISK_TSV" <<'EOF'
                    Data	dk2	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa	0x82
                    EOF
                    }}
                    tc_prepare_local_hostname_resolution() {{ echo hostname; }}
                    tc_init_runtime_identity() {{
                        echo identity
                        MDNS_INSTANCE_NAME=Live
                        MDNS_HOST_LABEL=live
                        SMB_NETBIOS_NAME=Live
                        SMB_SERVER_STRING=Live
                        TC_RUNTIME_IDENTITY_READY=1
                    }}
                    tc_generate_smb_conf() {{ echo "generate $1"; }}
                    tc_reload_smbd_config() {{ echo reload; }}
                    tc_launch_mdns_advertiser() {{ echo "mdns $1 $2 $3"; }}
                    runtime_process_present_by_ucomm() {{
                        case "$1" in
                            smbd) return 0 ;;
                            nbns-advertiser) [ "$nbns_present" = "1" ] ;;
                            *) return 1 ;;
                        esac
                    }}
                    tc_nbns_bound_ipv4_udp_137() {{ return 1; }}
                    tc_nbns_auto_ip_available() {{ echo nbns-auto-ip; return 0; }}
                    stop_runtime_process_by_ucomm() {{ echo "stop $1"; nbns_present=0; }}
                    tc_restart_nbns() {{ echo restart-nbns; }}
                    tc_exec_start_samba() {{ echo "exec $1"; exit 42; }}
                    tc_watchdog_disk_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "\n".join(
                (
                    "refresh",
                    "hostname",
                    "identity",
                    f"generate {payload}",
                    "reload",
                    "mdns watchdog topology refresh 1 10",
                    "nbns-auto-ip",
                    "stop nbns-advertiser",
                    "restart-nbns",
                    "",
                )
            ),
        )
        self.assertIn("watchdog recovery: nbns responder is running without required UDP 137 sockets", log_text)
        self.assertIn("watchdog recovery: live disk runtime refresh complete", log_text)

    def test_common_watchdog_live_reload_restores_bind_when_config_generation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            script = tmp_path / "watchdog-live-bind-generate-fails.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR" "$RAM_SBIN" "$RAM_ETC"
                    : >"$TC_SMBD_BIN"
                    chmod 755 "$TC_SMBD_BIN"
                    TC_SMB_BIND_INTERFACES="127.0.0.1/8 192.168.1.40/24"
                    tc_load_payload_state() {{
                        TC_PAYLOAD_DIR={payload}
                        TC_PAYLOAD_VOLUME={volumes}/dk2
                        TC_PAYLOAD_DEVICE=/dev/dk2
                        return 0
                    }}
                    tc_refresh_disk_state() {{ echo refresh; return 0; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_init_runtime_identity() {{ :; }}
                    tc_probe_smb_bind_interfaces() {{ echo "127.0.0.1/8 192.168.1.41/24"; }}
                    tc_generate_smb_conf() {{ echo "generate $TC_SMB_BIND_INTERFACES"; return 1; }}
                    runtime_process_present_by_ucomm() {{ echo unexpected; return 0; }}
                    tc_live_reload_disk_runtime "unit" || status=$?
                    printf 'status=%s\\n' "${{status:-0}}"
                    printf 'bind=%s\\n' "$TC_SMB_BIND_INTERFACES"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "refresh\ngenerate 127.0.0.1/8 192.168.1.41/24\nstatus=1\nbind=127.0.0.1/8 192.168.1.40/24\n",
        )
        self.assertNotIn("unexpected\n", proc.stdout)

    def test_common_watchdog_disk_iteration_reexecs_when_live_topology_reload_changes_payload_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            old_payload = volumes / "dk2/.samba4"
            new_payload = volumes / "dk3/.samba4"
            old_payload.mkdir(parents=True)
            new_payload.mkdir(parents=True)
            script = tmp_path / "watchdog-live-topology-payload-change.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR" "$RAM_SBIN" "$RAM_ETC"
                    : >"$TC_SMBD_BIN"
                    chmod 755 "$TC_SMBD_BIN"
                    tc_write_payload_state {old_payload} {volumes}/dk2 /dev/dk2
                    sleep() {{ :; }}
                    tc_watchdog_capture_mast_state() {{ : >"$1"; : >"$2"; return 0; }}
                    tc_topology_changed_debounced_from_snapshot() {{ return 0; }}
                    tc_refresh_disk_state() {{
                        echo refresh
                        tc_write_payload_state {new_payload} {volumes}/dk3 /dev/dk3
                    }}
                    tc_reload_smbd_config() {{ echo reload; }}
                    tc_exec_start_samba() {{ echo "exec $1"; exit 42; }}
                    tc_watchdog_disk_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 42, proc.stderr)
        self.assertEqual(proc.stdout, "refresh\nexec MaSt topology changed\n")

    def test_common_watchdog_reexec_uses_reload_disk_runtime_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            (flash / "start-samba.sh").write_text("#!/bin/sh\nprintf 'mode=%s\\n' \"$1\"\nexit 43\n")
            (flash / "start-samba.sh").chmod(0o755)
            script = tmp_path / "watchdog-reexec-mode.sh"
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
                    tc_exec_start_samba "unit test"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 43, proc.stderr)
        self.assertEqual(proc.stdout, "mode=--reload-disk-runtime\n")

    def test_common_watchdog_service_iteration_skips_smbd_when_payload_state_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-payload-missing.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR" "$RAM_SBIN" "$RAM_ETC"
                    : >"$TC_SMBD_BIN"
                    chmod 755 "$TC_SMBD_BIN"
                    : >"$TC_SMBD_CONF"
                    sleep() {{ :; }}
                    tc_prepare_local_hostname_resolution() {{ :; }}
                    tc_watchdog_reconcile_smb_bind_interfaces() {{ :; }}
                    runtime_process_present_by_ucomm() {{ return 1; }}
                    tc_watchdog_refresh_runtime_identity_for_recovery() {{ :; }}
                    tc_watchdog_reconcile_mdns() {{ echo mdns; }}
                    tc_all_managed_services_healthy() {{ return 0; }}
                    tc_watchdog_service_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "mdns\n")
        self.assertNotIn("smbd restart skipped; payload state is unavailable", log_text)
        self.assertNotIn("watchdog pass: smbd recovery did not complete", log_text)

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
