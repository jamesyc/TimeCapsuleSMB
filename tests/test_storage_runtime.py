from __future__ import annotations

import shlex
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.cli.deploy import render_flash_runtime_config
from timecapsulesmb.deploy.executor import upload_flash_file
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
    NO_WRITABLE_PERSISTENT_VOLUME_MESSAGE,
    MaStVolume,
    PayloadHome,
    ordered_payload_candidate_volumes,
    payload_candidate_checks_debug_summary,
    parse_mast_plist,
    select_payload_home_conn,
    select_payload_home_with_diagnostics_conn,
    wait_for_mast_volumes_conn,
)
from timecapsulesmb.transport.ssh import SshConnection
from tests.storage_fixtures import INTERNAL_DATA, MAST_FIXTURES, SHELL_MAST_FIXTURES, MaStFixture


class StorageRuntimeTests(unittest.TestCase):
    def write_runtime_harness(self, tmp_path: Path, *, hostname_output: str | None = None) -> tuple[Path, Path, Path, Path]:
        repo_root = Path(__file__).resolve().parent.parent
        flash = tmp_path / "Flash"
        memory = tmp_path / "Memory"
        locks = tmp_path / "Locks"
        volumes = tmp_path / "Volumes"
        flash.mkdir()
        memory.mkdir()
        locks.mkdir()
        volumes.mkdir()

        common = (repo_root / "src/timecapsulesmb/assets/boot/samba4/common.sh").read_text()
        start = (repo_root / "src/timecapsulesmb/assets/boot/samba4/start-samba.sh").read_text()
        watchdog = (repo_root / "src/timecapsulesmb/assets/boot/samba4/watchdog.sh").read_text()
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
            start = start.replace(old, new)
            watchdog = watchdog.replace(old, new)

        (flash / "common.sh").write_text(common)
        start_path = flash / "start-samba.sh"
        start_path.write_text(start)
        start_path.chmod(0o755)
        watchdog_path = flash / "watchdog.sh"
        watchdog_path.write_text(watchdog)
        watchdog_path.chmod(0o755)
        (flash / "tcapsulesmb.conf").write_text(
            textwrap.dedent(
                f"""\
                TC_CONFIG_VERSION=1
                PAYLOAD_DIR_NAME='.samba4'
                NET_IFACE='bridge0'
                SMB_SAMBA_USER='admin'
                MDNS_DEVICE_MODEL='TimeCapsule6,106'
                AIRPORT_SYAP='106'
                INTERNAL_SHARE_USE_DISK_ROOT=0
                APPLE_MOUNT_WAIT_SECONDS=0
                NBNS_ENABLED=0
                SMBD_DEBUG_LOGGING=0
                MDNS_DEBUG_LOGGING=0
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

    def write_fake_acp(self, tmp_path: Path, raw: str | bytes, *, final_newline: bool = True) -> Path:
        acp = tmp_path / "acp"
        raw_text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        if final_newline:
            acp.write_text("#!/bin/sh\ncat <<'OUT'\n" + raw_text + "\nOUT\n")
        else:
            acp.write_text("#!/bin/sh\nprintf %s " + shlex.quote(raw_text) + "\n")
        acp.chmod(0o755)
        return acp

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
            adisk_lines.append("\t".join((share_name, volume.partition_device, volume.adisk_uuid, "0x1093")))
        return "\n".join(share_lines) + "\n", "\n".join(adisk_lines) + "\n"

    def run_share_state_fixture(
        self,
        fixture: MaStFixture,
        tmp_path: Path,
        flash: Path,
        volumes_root: Path,
        *,
        internal_share_use_disk_root: bool,
    ) -> subprocess.CompletedProcess[str]:
        self.write_fake_acp(tmp_path, fixture.raw)
        for volume in fixture.expected:
            Path(self.mapped_volume_root(volume, volumes_root)).mkdir(parents=True, exist_ok=True)
        override = "INTERNAL_SHARE_USE_DISK_ROOT=1" if internal_share_use_disk_root else ""
        script = tmp_path / f"share-state-{fixture.name}.sh"
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
                tc_read_mast_volumes_to "$TC_VOLUMES_TSV" "$TC_MAST_RAW"
                tc_build_share_state "$TC_VOLUMES_TSV"
                printf 'shares\\n'
                cat "$TC_SHARES_TSV"
                printf 'adisk\\n'
                cat "$TC_ADISK_TSV"
                """
            )
        )
        script.chmod(0o755)
        return subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    def test_parse_mast_plist_matches_golden_fixtures(self) -> None:
        for fixture in MAST_FIXTURES:
            with self.subTest(fixture=fixture.name):
                self.assertEqual(parse_mast_plist(fixture.raw), fixture.expected)

    def test_wait_for_mast_volumes_retries_until_available(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        volume = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")

        with mock.patch("timecapsulesmb.device.storage.read_mast_volumes_conn", side_effect=[(), (), (volume,)]) as read_mock:
            with mock.patch("timecapsulesmb.device.storage.time.sleep") as sleep_mock:
                result = wait_for_mast_volumes_conn(connection, attempts=10, delay_seconds=3)

        self.assertEqual(result.volumes, (volume,))
        self.assertEqual(result.attempts, 3)
        self.assertEqual(read_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_args_list, [mock.call(3), mock.call(3)])

    def test_wait_for_mast_volumes_returns_empty_after_exhaustion(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")

        with mock.patch("timecapsulesmb.device.storage.read_mast_volumes_conn", return_value=()) as read_mock:
            with mock.patch("timecapsulesmb.device.storage.time.sleep") as sleep_mock:
                result = wait_for_mast_volumes_conn(connection, attempts=3, delay_seconds=3)

        self.assertEqual(result.volumes, ())
        self.assertEqual(result.attempts, 3)
        self.assertEqual(read_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_args_list, [mock.call(3), mock.call(3)])

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
                    tc_wait_for_mast_volumes_to "$TC_VOLUMES_TSV" "$TC_MAST_RAW" 6
                    printf 'count=%s\\n' "$(cat {acp_counter})"
                    cat "$TC_VOLUMES_TSV"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("count=3\n", proc.stdout)
        self.assertIn(self.expected_topology_tsv(fixture, volumes).splitlines()[0], proc.stdout)
        self.assertIn("MaSt discovery not ready; waiting up to 6s for valid HFS volumes", proc.stdout)
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
                    tc_wait_for_mast_volumes_to "$TC_VOLUMES_TSV" "$TC_MAST_RAW" 6 || status=$?
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
        self.assertIn("MaSt discovery timed out after 6s with no valid HFS volumes", proc.stdout)

    def test_payload_candidate_order_is_internal_first_then_external_mast_order_in_python(self) -> None:
        external_a = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB A", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external_b = MaStVolume("sd1", "dk4", "/Volumes/dk4", "USB B", "7d40eaac-182b-562b-a7b8-49bb5ed69c0f", False, "hfs")

        self.assertEqual(
            ordered_payload_candidate_volumes((external_a, internal, external_b)),
            (internal, external_a, external_b),
        )

    def test_common_payload_candidate_order_is_internal_first_then_external_mast_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "payload-order.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    cat >"$TC_VOLUMES_TSV" <<'EOF'
                    sd0	0	dk3	{volumes}/dk3	USB A	51f93e6f-dc69-524d-986d-cee4d7cb3573
                    wd0	1	dk2	{volumes}/dk2	Data	f42bdb83-c265-5522-a087-25606a4d0abf
                    sd1	0	dk4	{volumes}/dk4	USB B	7d40eaac-182b-562b-a7b8-49bb5ed69c0f
                    EOF
                    tc_emit_payload_candidate_volumes "$TC_VOLUMES_TSV"
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
                    f"wd0\t1\tdk2\t{volumes}/dk2\tData\tf42bdb83-c265-5522-a087-25606a4d0abf",
                    f"sd0\t0\tdk3\t{volumes}/dk3\tUSB A\t51f93e6f-dc69-524d-986d-cee4d7cb3573",
                    f"sd1\t0\tdk4\t{volumes}/dk4\tUSB B\t7d40eaac-182b-562b-a7b8-49bb5ed69c0f",
                    "",
                )
            ),
        )

    def test_common_payload_candidate_order_matches_python_for_shell_fixtures(self) -> None:
        for fixture in SHELL_MAST_FIXTURES:
            if not fixture.expected:
                continue
            with self.subTest(fixture=fixture.name):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
                    expected_topology = self.expected_topology_tsv(fixture, volumes)
                    heredoc_topology = textwrap.indent(expected_topology.rstrip(), " " * 28)
                    script = tmp_path / "payload-order-fixture.sh"
                    script.write_text(
                        textwrap.dedent(
                            f"""\
                            #!/bin/sh
                            set -eu
                            . {flash}/common.sh
                            . {flash}/tcapsulesmb.conf
                            tc_init_runtime_env
                            mkdir -p "$RAM_VAR"
                            cat >"$TC_VOLUMES_TSV" <<'EOF'
{heredoc_topology}
                            EOF
                            tc_emit_payload_candidate_volumes "$TC_VOLUMES_TSV"
                            """
                        )
                    )
                    script.chmod(0o755)

                    proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

                ordered = ordered_payload_candidate_volumes(fixture.expected)
                expected_lines = []
                for volume in ordered:
                    volume_root = self.mapped_volume_root(volume, volumes)
                    builtin = "1" if volume.builtin else "0"
                    expected_lines.append(
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
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, "\n".join(expected_lines) + "\n")

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
                "TC_NET_IFACE": "bridge0",
                "TC_SAMBA_USER": "admin",
                "TC_MDNS_DEVICE_MODEL": "TimeCapsule6,106",
                "TC_AIRPORT_SYAP": "106",
                "TC_INTERNAL_SHARE_USE_DISK_ROOT": "true",
            }
        )

        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            nbns_enabled=True,
            debug_logging=True,
            apple_mount_wait_seconds=12,
        )

        self.assertIn("PAYLOAD_DIR_NAME=.samba4\n", rendered)
        self.assertNotIn("PAYLOAD_VOLUME_HINT", rendered)
        self.assertNotIn("PAYLOAD_DEVICE_HINT", rendered)
        self.assertNotIn("PAYLOAD_INSTALL_ID", rendered)
        self.assertIn("INTERNAL_SHARE_USE_DISK_ROOT=1\n", rendered)
        self.assertIn("NBNS_ENABLED=1\n", rendered)
        self.assertIn("SMBD_DEBUG_LOGGING=1\n", rendered)
        self.assertNotIn("SMB_NETBIOS_NAME", rendered)
        self.assertNotIn("MDNS_INSTANCE_NAME", rendered)
        self.assertNotIn("MDNS_HOST_LABEL", rendered)
        self.assertNotIn("TC_SHARE_NAME", rendered)

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
                    NBNS_ENABLED=1
                    tc_init_runtime_env
                    mkdir -p "$RAM_ETC" "$RAM_VAR"
                    tc_set_log "$RAM_VAR/test.log" test
                    get_airport_acp_value() {{
                        case "$1" in
                            syNm) echo "James's AirPort Time Capsule" ;;
                            syVs) echo 7.9.1 ;;
                            srcv) echo 79100.2 ;;
                            syAP) echo 119 ;;
                            *) return 1 ;;
                        esac
                    }}
                    get_iface_mac() {{ echo 80:EA:96:E6:58:68; }}
                    get_radio_mac() {{ return 1; }}
                    get_iface_ipv4() {{ echo 192.168.1.2; }}
                    stop_nbns_conflicts() {{ return 0; }}
                    TC_NET_IFACE_IP=192.168.1.2
                    tc_set_payload_log_dir {payload} {volumes}/dk2
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    tc_init_runtime_identity
                    tc_generate_smb_conf {payload} "127.0.0.1/8 192.168.1.2/24"
                    tc_launch_mdns_advertiser "mdns test" 0 0 0
                    tc_launch_nbns "nbns test" 0
                    sleep 1
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
        self.assertIn("--instance James's AirPort Time Capsule", proc.stdout)
        self.assertIn("--host time-capsule", proc.stdout)
        self.assertIn("nbns_args=--name TimeCapsule --ipv4 192.168.1.2", proc.stdout)
        self.assertIn("runtime identity: mdns_instance=James's AirPort Time Capsule mdns_host=time-capsule netbios=TimeCapsule", proc.stdout)
        self.assertNotIn("LegacyInstance", proc.stdout)
        self.assertNotIn("legacy-host", proc.stdout)
        self.assertNotIn("LegacyNetbios", proc.stdout)

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
            source.write_text("TC_CONFIG_VERSION=1\n")

            with mock.patch("timecapsulesmb.deploy.executor.run_ssh") as run_ssh_mock:
                with mock.patch("timecapsulesmb.deploy.executor.run_scp") as run_scp_mock:
                    upload_flash_file(connection, source, "/mnt/Flash/tcapsulesmb.conf", mode="600")

        run_scp_mock.assert_called_once_with(connection, source, "/mnt/Flash/.tcapsulesmb.conf.tmp", timeout=120)
        install_command = run_ssh_mock.call_args_list[1].args[1]
        self.assertIn("chmod 600 /mnt/Flash/.tcapsulesmb.conf.tmp", install_command)
        self.assertIn("mv -f /mnt/Flash/.tcapsulesmb.conf.tmp /mnt/Flash/tcapsulesmb.conf", install_command)

    def test_start_samba_signature_mode_matches_shell_supported_mast_fixtures(self) -> None:
        for fixture in SHELL_MAST_FIXTURES:
            with self.subTest(fixture=fixture.name):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
                    start_path = flash / "start-samba.sh"
                    self.write_fake_acp(tmp_path, fixture.raw)

                    proc = subprocess.run(
                        ["/bin/sh", str(start_path), "--print-topology-signature"],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )

                expected_stdout = self.expected_topology_tsv(fixture, volumes)
                expected_rc = 0 if fixture.expected else 1
                self.assertEqual(proc.returncode, expected_rc, proc.stderr)
                self.assertEqual(proc.stdout, expected_stdout)
                self.assertEqual(self.parse_topology_tsv(proc.stdout, volumes), parse_mast_plist(fixture.raw))

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
            self.write_fake_acp(tmp_path, fixture.raw)
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
                        tc_wait_for_bind_interfaces() {{ echo "127.0.0.1/8 192.168.1.2/24"; }}
                        tc_prepare_local_hostname_resolution() {{ echo hostname; }}
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
                        Data	dk2	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa	0x1093
                        EOF
                        }}
                        tc_start_mdns_capture() {{ echo mdns-capture; }}
                        tc_stage_disk_runtime() {{ echo "stage $1"; }}
                        tc_start_smbd() {{ echo smbd; }}
                        tc_start_mdns_advertiser() {{ echo mdns; }}
                        tc_start_nbns() {{ echo nbns; }}
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
                    "hostname",
                    "refresh",
                    "mdns-capture",
                    "stage 127.0.0.1/8 192.168.1.2/24",
                    "smbd",
                    "mdns",
                    "nbns",
                    "watchdog",
                    "",
                )
            ),
        )

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
                    printf %s {shlex.quote(row)} >"$TC_VOLUMES_TSV"
                    tc_build_share_state "$TC_VOLUMES_TSV"
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
        for fixture in SHELL_MAST_FIXTURES:
            if not fixture.expected:
                continue
            with self.subTest(fixture=fixture.name):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)

                    proc = self.run_share_state_fixture(
                        fixture,
                        tmp_path,
                        flash,
                        volumes,
                        internal_share_use_disk_root=False,
                    )
                    expected_shares, expected_adisk = self.expected_share_state(
                        fixture,
                        volumes,
                        internal_share_use_disk_root=False,
                    )
                    expected_stdout = f"shares\n{expected_shares}adisk\n{expected_adisk}"
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(proc.stdout, expected_stdout)
                    for volume in fixture.expected:
                        volume_root = Path(self.mapped_volume_root(volume, volumes))
                        if volume.builtin:
                            marker = volume_root / "ShareRoot/.com.apple.timemachine.supported"
                        else:
                            marker = volume_root / ".com.apple.timemachine.supported"
                        self.assertTrue(marker.exists(), str(marker))

    def test_common_share_state_internal_disk_root_override_matches_python_policy(self) -> None:
        for fixture in SHELL_MAST_FIXTURES:
            if not any(volume.builtin for volume in fixture.expected):
                continue
            with self.subTest(fixture=fixture.name):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)

                    proc = self.run_share_state_fixture(
                        fixture,
                        tmp_path,
                        flash,
                        volumes,
                        internal_share_use_disk_root=True,
                    )
                    expected_shares, expected_adisk = self.expected_share_state(
                        fixture,
                        volumes,
                        internal_share_use_disk_root=True,
                    )
                    expected_stdout = f"shares\n{expected_shares}adisk\n{expected_adisk}"
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(proc.stdout, expected_stdout)
                    self.assertNotIn("/ShareRoot", proc.stdout)

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
                    cat >"$TC_VOLUMES_TSV" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    sd0	0	dk3	{volumes}/dk3	Data	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    tc_build_share_state "$TC_VOLUMES_TSV"
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
        self.assertIn("Data\tdk2\taaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\t0x1093\n", proc.stdout)
        self.assertIn("Data (dk3)\tdk3\tbbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb\t0x1093\n", proc.stdout)
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
                    cat >"$TC_VOLUMES_TSV" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	{long_name}	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    sd0	0	dk3	{volumes}/dk3	{long_name}	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    tc_build_share_state "$TC_VOLUMES_TSV"
                    cat "$TC_ADISK_TSV"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = [line.split("\t") for line in proc.stdout.splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0][0].encode("utf-8")), 192)
        self.assertEqual(len(rows[1][0].encode("utf-8")), 192)
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
                    cat >"$TC_VOLUMES_TSV" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    sd0	0	dk3	{volumes}/dk3	USB	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    tc_resolve_payload "$TC_VOLUMES_TSV"
                    tc_write_payload_state "$TC_RESOLVED_PAYLOAD_DIR" "$TC_RESOLVED_PAYLOAD_VOLUME" "$TC_RESOLVED_PAYLOAD_DEVICE"
                    tc_read_payload_state
                    printf '%s\\n%s\\n%s\\n' "$TC_PAYLOAD_DIR" "$TC_PAYLOAD_VOLUME" "$TC_PAYLOAD_DEVICE"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, f"{volumes}/dk3/.samba4\n{volumes}/dk3\n/dev/dk3\n")
        self.assertIn(f"payload directory selected from mounted MaSt volumes: {volumes}/dk3/.samba4", log_text)

    def test_common_refresh_disk_state_succeeds_with_payload_and_one_share_when_external_optional_fails(self) -> None:
        fixture = SHELL_MAST_FIXTURES[0]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            self.write_fake_acp(tmp_path, fixture.raw)
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
            self.write_fake_acp(tmp_path, fixture.raw)
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
                    tc_mount_mast_volumes_for_boot() {{ :; }}
                    is_volume_root_mounted() {{ return 0; }}
                    tc_refresh_disk_state || status=$?
                    printf 'status=%s\\n' "${{status:-0}}"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=1\n", proc.stdout)
        self.assertIn("no valid payload directory found on mounted MaSt volumes", proc.stdout)
        self.assertIn("payload discovery failed", proc.stdout)

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
                    tc_write_payload_state {payload} {volumes}/dk2 /dev/dk2
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	f42bdb83-c265-5522-a087-25606a4d0abf
                    EOF
                    tc_stage_disk_runtime "127.0.0.1/8 192.168.1.2/24"
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
        self.assertIn("interfaces = 127.0.0.1/8 192.168.1.2/24", proc.stdout)
        self.assertIn("nbns runtime staging skipped", log_text)
        self.assertNotIn("nbns binary not found", log_text)

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
                    tc_stage_disk_runtime "127.0.0.1/8" || status=$?
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

    def test_payload_on_external_disk_is_also_served_as_share_and_hidden_in_smb_conf(self) -> None:
        fixture = next(fixture for fixture in SHELL_MAST_FIXTURES if fixture.name == "openstep_external_only")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            self.write_fake_acp(tmp_path, fixture.raw)
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
                    tc_mount_mast_volumes_for_boot() {{ :; }}
                    is_volume_root_mounted() {{ return 0; }}
                    mkdir -p "$RAM_VAR" {volumes}/dk5
                    tc_refresh_disk_state
                    tc_prepare_ram_root
                    tc_stage_disk_runtime "127.0.0.1/8 192.168.1.2/24"
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
        self.assertIn("adisk\nUSB Backup\tdk5\taaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\t0x1093\n", proc.stdout)
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
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    USB	{volumes}/dk3	dk3	0	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    tc_generate_smb_conf {payload} "127.0.0.1/8 192.168.1.2/24"
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
        self.assertIn("max open files = 512", proc.stdout)
        self.assertIn("max smbd processes = 16", proc.stdout)
        self.assertNotIn("log level = 5", proc.stdout)
        self.assertTrue(smbd_core_dir_exists)
        self.assertEqual(smbd_core_parent_mode, 0o700)
        self.assertEqual(smbd_core_dir_mode, 0o700)

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
                    cat >"$TC_SHARES_TSV" <<'EOF'
                    Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    tc_generate_smb_conf {payload} "127.0.0.1/8 192.168.1.2/24"
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

    def test_common_payload_append_log_uses_payload_disk_and_bounds_normal_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            payload.mkdir(parents=True)
            script = tmp_path / "payload-log.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    TC_RUNTIME_LOG_MAX_BYTES=120
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    is_volume_root_mounted() {{ [ "$1" = "{volumes}/dk2" ]; }}
                    tc_set_payload_log_dir {payload} {volumes}/dk2
                    mkdir -p "$TC_PAYLOAD_LOG_DIR"
                    printf 'old-old-old-old-old-old-old-old-old-old-old-old-old-old-old-old-old-old\\n' >"$TC_PAYLOAD_LOG_DIR/watchdog.log"
                    tc_set_payload_append_log "$TC_PAYLOAD_LOG_DIR/watchdog.log" watchdog {volumes}/dk2 "$RAM_VAR/watchdog.log"
                    tc_log "final payload line"
                    size=$(/usr/bin/wc -c <"$TC_PAYLOAD_LOG_DIR/watchdog.log" | sed 's/[^0-9]//g')
                    printf 'size=%s\\n' "$size"
                    printf 'ram=%s\\n' "$([ -f "$RAM_VAR/watchdog.log" ] && echo yes || echo no)"
                    cat "$TC_PAYLOAD_LOG_DIR/watchdog.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        first_line = proc.stdout.splitlines()[0]
        self.assertTrue(first_line.startswith("size="), proc.stdout)
        self.assertLessEqual(int(first_line.removeprefix("size=")), 120)
        self.assertIn("ram=no\n", proc.stdout)
        self.assertIn("final payload line", proc.stdout)

    def test_common_payload_append_log_falls_back_to_ram_when_payload_unmounted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            payload.mkdir(parents=True)
            script = tmp_path / "payload-log-fallback.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    is_volume_root_mounted() {{ return 1; }}
                    tc_set_payload_log_dir {payload} {volumes}/dk2
                    tc_set_payload_append_log "$TC_PAYLOAD_LOG_DIR/watchdog.log" watchdog {volumes}/dk2 "$RAM_VAR/watchdog.log"
                    tc_log "fallback line"
                    printf 'payload=%s\\n' "$([ -f "$TC_PAYLOAD_LOG_DIR/watchdog.log" ] && echo yes || echo no)"
                    cat "$RAM_VAR/watchdog.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("payload=no\n", proc.stdout)
        self.assertIn("fallback line", proc.stdout)

    def test_common_watchdog_iteration_writes_payload_health_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            payload.mkdir(parents=True)
            script = tmp_path / "watchdog-health-log.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    TC_PAYLOAD_DIR={payload}
                    TC_PAYLOAD_VOLUME={volumes}/dk2
                    TC_PAYLOAD_DEVICE=/dev/dk2
                    is_volume_root_mounted() {{ [ "$1" = "{volumes}/dk2" ]; }}
                    tc_set_payload_log_dir "$TC_PAYLOAD_DIR" "$TC_PAYLOAD_VOLUME"
                    mkdir -p "$TC_PAYLOAD_LOG_DIR"
                    tc_set_payload_append_log "$TC_PAYLOAD_LOG_DIR/watchdog.log" watchdog "$TC_PAYLOAD_VOLUME" "$RAM_VAR/watchdog.log"
                    tc_topology_changed() {{ return 1; }}
                    tc_payload_available() {{ return 0; }}
                    tc_mount_active_volumes_from_state() {{ return 0; }}
                    tc_start_smbd_if_needed() {{ return 0; }}
                    tc_all_managed_services_healthy() {{ return 0; }}
                    tc_watchdog_iteration
                    printf 'ram=%s\\n' "$([ -f "$RAM_VAR/watchdog.log" ] && echo yes || echo no)"
                    cat "$TC_PAYLOAD_LOG_DIR/watchdog.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ram=no\n", proc.stdout)
        self.assertIn("watchdog pass: checking topology, payload, active shares, and managed services", proc.stdout)
        self.assertIn(f"watchdog pass: payload available at {payload}", proc.stdout)
        self.assertIn("watchdog pass: healthy", proc.stdout)

    def test_common_watchdog_steady_sleep_logs_each_healthy_mount_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            payload.mkdir(parents=True)
            script = tmp_path / "watchdog-steady-log.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    TC_PAYLOAD_DIR={payload}
                    TC_PAYLOAD_VOLUME={volumes}/dk2
                    TC_PAYLOAD_DEVICE=/dev/dk2
                    MOUNT_POLL_SECONDS=1
                    is_volume_root_mounted() {{ [ "$1" = "{volumes}/dk2" ]; }}
                    tc_set_payload_log_dir "$TC_PAYLOAD_DIR" "$TC_PAYLOAD_VOLUME"
                    mkdir -p "$TC_PAYLOAD_LOG_DIR"
                    tc_set_payload_append_log "$TC_PAYLOAD_LOG_DIR/watchdog.log" watchdog "$TC_PAYLOAD_VOLUME" "$RAM_VAR/watchdog.log"
                    tc_payload_available() {{ return 0; }}
                    tc_mount_active_volumes_from_state() {{ return 0; }}
                    tc_sleep_with_runtime_checks 2
                    cat "$TC_PAYLOAD_LOG_DIR/watchdog.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("watchdog steady check: healthy after 1s of 2s", proc.stdout)
        self.assertIn("watchdog steady check: healthy after 2s of 2s", proc.stdout)

    def test_common_watchdog_steady_sleep_returns_immediately_when_payload_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-steady-payload-missing.sh"
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
                    MOUNT_POLL_SECONDS=1
                    sleep() {{ echo "sleep $1"; }}
                    tc_payload_available() {{ echo payload; return 1; }}
                    tc_mount_active_volumes_from_state() {{ echo mounts; return 0; }}
                    if tc_sleep_with_runtime_checks 5; then
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
        self.assertEqual(proc.stdout, "sleep 1\npayload\nstatus=1\n")
        self.assertIn("watchdog steady check: payload unavailable while sleeping", log_text)
        self.assertNotIn("watchdog steady check: healthy", log_text)

    def test_common_watchdog_steady_sleep_returns_immediately_when_active_share_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-steady-active-share-missing.sh"
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
                    MOUNT_POLL_SECONDS=1
                    sleep() {{ echo "sleep $1"; }}
                    tc_payload_available() {{ echo payload; return 0; }}
                    tc_mount_active_volumes_from_state() {{ echo mounts; return 1; }}
                    if tc_sleep_with_runtime_checks 5; then
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
        self.assertEqual(proc.stdout, "sleep 1\npayload\nmounts\nstatus=1\n")
        self.assertIn(
            "watchdog steady check: one or more active share volumes are unavailable while sleeping",
            log_text,
        )
        self.assertNotIn("watchdog steady check: healthy", log_text)

    def test_common_watchdog_steady_sleep_propagates_missing_share_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-steady-share-state-missing.sh"
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
                    MOUNT_POLL_SECONDS=1
                    sleep() {{ echo "sleep $1"; }}
                    tc_payload_available() {{ echo payload; return 0; }}
                    tc_mount_active_volumes_from_state() {{ echo mounts; return 2; }}
                    if tc_sleep_with_runtime_checks 5; then
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
        self.assertEqual(proc.stdout, "sleep 1\npayload\nmounts\nstatus=2\n")
        self.assertIn("watchdog steady check: active share state unavailable while sleeping", log_text)
        self.assertNotIn("watchdog steady check: healthy", log_text)

    def test_watchdog_loop_continues_after_interrupted_steady_sleep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            common_path = flash / "common.sh"
            common_path.write_text(
                common_path.read_text()
                + textwrap.dedent(
                    f"""\

                    tc_read_payload_state() {{ return 1; }}
                    tc_sleep_with_runtime_checks() {{ echo "steady sleep $1"; return 1; }}
                    sleep() {{ echo "recovery sleep $1"; exit 99; }}
                    tc_watchdog_iteration() {{
                        count=$(cat {tmp_path}/watchdog-count 2>/dev/null || echo 0)
                        count=$((count + 1))
                        echo "$count" >{tmp_path}/watchdog-count
                        echo "iteration $count"
                        if [ "$count" -ge 2 ]; then
                            exit 0
                        fi
                        return 0
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
            log_text = (memory / "samba4/var/watchdog.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "iteration 1\nsteady sleep 300\niteration 2\n")
        self.assertIn("watchdog steady check interrupted; running recovery pass", log_text)
        self.assertNotIn("recovery sleep", proc.stdout)

    def test_common_mdns_capture_wait_times_out_and_continues_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            events = tmp_path / "events.log"
            script = tmp_path / "mdns-capture-timeout.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    MDNS_CAPTURE_WAIT_SECONDS=2
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR"
                    TC_MDNS_CAPTURE_PID=12345
                    TC_MDNS_CAPTURE_STATUS_FILE="$RAM_VAR/mdns-capture.status.test"
                    kill() {{ echo "kill $*" >>{events}; return 0; }}
                    wait() {{ echo "wait $*" >>{events}; return 0; }}
                    sleep() {{ echo "sleep $*" >>{events}; }}
                    stop_runtime_process_by_ucomm() {{ echo "stop $1 $2" >>{events}; return 0; }}
                    tc_wait_for_mdns_capture
                    [ -z "$TC_MDNS_CAPTURE_PID" ] && echo pid-cleared
                    [ -z "$TC_MDNS_CAPTURE_STATUS_FILE" ] && echo status-cleared
                    cat {events}
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("pid-cleared\n", proc.stdout)
        self.assertIn("status-cleared\n", proc.stdout)
        self.assertIn("stop mdns-advertiser mdns-advertiser\n", proc.stdout)
        self.assertIn("kill -9 12345\n", proc.stdout)
        self.assertIn(
            "mDNS snapshot capture timed out after 2s; stopping capture and continuing with generated records if needed",
            proc.stdout,
        )

    def test_common_mdns_capture_launch_sets_pid_and_status_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            marker = tmp_path / "capture.started"
            release = tmp_path / "capture.release"
            (flash / "mdns-advertiser").write_text(
                "#!/bin/sh\n"
                "printf 'capture-args:%s\\n' \"$*\"\n"
                f"echo started >{shlex.quote(str(marker))}\n"
                f"while [ ! -f {shlex.quote(str(release))} ]; do sleep 1; done\n"
                "echo capture-release\n"
                "exit 7\n"
            )
            (flash / "mdns-advertiser").chmod(0o755)
            script = tmp_path / "mdns-capture-async.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    MDNS_CAPTURE_WAIT_SECONDS=5
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
                    TC_NET_IFACE_IP=192.168.1.2
                    tc_start_mdns_capture
                    case "$TC_MDNS_CAPTURE_STATUS_FILE" in
                        "$RAM_VAR"/mdns-capture.status.*) echo status-path-ok ;;
                        *) echo "status-path-bad=$TC_MDNS_CAPTURE_STATUS_FILE" ;;
                    esac
                    [ -n "$TC_MDNS_CAPTURE_PID" ] && echo pid-set
                    count=0
                    while [ ! -f {shlex.quote(str(marker))} ] && [ "$count" -lt 5 ]; do
                        count=$((count + 1))
                        sleep 1
                    done
                    [ -f {shlex.quote(str(marker))} ] || exit 99
                    [ ! -f "$APPLE_MDNS_SNAPSHOT" ] && echo stale-apple-removed
                    [ ! -f "$ALL_MDNS_SNAPSHOT" ] && echo stale-all-removed
                    [ ! -f "$TC_MDNS_CAPTURE_STATUS_FILE" ] && echo status-pending
                    touch {shlex.quote(str(release))}
                    tc_wait_for_mdns_capture
                    [ -z "$TC_MDNS_CAPTURE_PID" ] && echo pid-cleared
                    [ -z "$TC_MDNS_CAPTURE_STATUS_FILE" ] && echo status-cleared
                    cat "$TC_MDNS_LOG_FILE"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status-path-ok\n", proc.stdout)
        self.assertIn("pid-set\n", proc.stdout)
        self.assertIn("stale-apple-removed\n", proc.stdout)
        self.assertIn("stale-all-removed\n", proc.stdout)
        self.assertIn("status-pending\n", proc.stdout)
        self.assertIn("pid-cleared\n", proc.stdout)
        self.assertIn("status-cleared\n", proc.stdout)
        self.assertIn("launching mdns-advertiser capture", proc.stdout)
        self.assertIn("--save-all-snapshot", proc.stdout)
        self.assertIn("--save-snapshot", proc.stdout)
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
                    TC_NET_IFACE_IP=192.168.1.2
                    tc_start_mdns_capture
                    tc_launch_mdns_advertiser "mdns test" 1 0 0
                    sleep 1
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
        self.assertNotIn("--save-airport-snapshot", proc.stdout)
        self.assertIn("trusted Apple mDNS snapshot was created during this boot run", proc.stdout)
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
                    TC_NET_IFACE_IP=192.168.1.2
                    tc_start_mdns_capture
                    tc_launch_mdns_advertiser "mdns test" 1 0 0
                    sleep 1
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
        self.assertIn("mDNS snapshot capture did not produce trusted Apple snapshot; generating AirPort fallback", proc.stdout)
        self.assertIn("launching mdns-advertiser airport snapshot", proc.stdout)
        self.assertIn("--save-airport-snapshot", proc.stdout)
        self.assertIn("--load-snapshot", proc.stdout)

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
            nbns_bin.write_text("#!/bin/sh\necho nbns-stdout\necho nbns-stderr >&2\n")
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
                    TC_NET_IFACE_IP=192.168.1.2
                    tc_set_payload_log_dir {payload} {volumes}/dk2
                    tc_generate_mdns
                    tc_launch_nbns "nbns test" 0
                    sleep 1
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
                    TC_NET_IFACE_IP=192.168.1.2
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

    def test_common_boot_mount_requests_all_apple_mounts_before_shared_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            events = tmp_path / "events.log"
            acp = tmp_path / "acp"
            acp.write_text(f"#!/bin/sh\necho \"acp $@\" >>{shlex.quote(str(events))}\n")
            acp.chmod(0o755)
            script = tmp_path / "boot-mount-shared-wait.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    APPLE_MOUNT_WAIT_SECONDS=5
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR"
                    is_volume_root_mounted() {{ [ -f "$1/.mounted" ]; }}
                    sleep() {{
                        echo "sleep $1" >>{events}
                        count=$(cat {tmp_path}/sleep-count 2>/dev/null || echo 0)
                        count=$((count + 1))
                        echo "$count" >{tmp_path}/sleep-count
                        if [ "$count" -eq 1 ]; then
                            touch {volumes}/dk2/.mounted
                        elif [ "$count" -eq 2 ]; then
                            touch {volumes}/dk3/.mounted
                        fi
                    }}
                    mount_hfs_bounded() {{ echo "fallback $1 $2" >>{events}; return 1; }}
                    cat >"$TC_VOLUMES_TSV" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    sd0	0	dk3	{volumes}/dk3	USB	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    tc_mount_mast_volumes_for_boot "$TC_VOLUMES_TSV"
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
                "sleep 1",
                "sleep 1",
            ],
        )

    def test_common_boot_mount_uses_one_global_apple_wait_before_fallbacks(self) -> None:
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
                    APPLE_MOUNT_WAIT_SECONDS=2
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR"
                    is_volume_root_mounted() {{ return 1; }}
                    sleep() {{ echo "sleep $1" >>{events}; }}
                    mount_hfs_bounded() {{ echo "fallback $1 $2" >>{events}; return 1; }}
                    cat >"$TC_VOLUMES_TSV" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    sd0	0	dk3	{volumes}/dk3	USB	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    tc_mount_mast_volumes_for_boot "$TC_VOLUMES_TSV"
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
                f"acp rpc diskd.useVolume path:s:{volumes}/dk3",
                "sleep 1",
                "sleep 1",
                f"fallback /dev/dk2 {volumes}/dk2",
                f"fallback /dev/dk3 {volumes}/dk3",
            ],
        )
        self.assertIn("boot disk load: requesting diskd.useVolume for all MaSt volumes", log_text)
        self.assertIn("MaSt volume diskd.useVolume wait beginning for up to 2s", log_text)
        self.assertIn(
            "MaSt volume diskd.useVolume wait timed out after 2s; manual fallback will handle remaining unmounted volumes",
            log_text,
        )
        self.assertIn("boot disk load: checking for unmounted volumes after shared diskd wait", log_text)
        self.assertNotIn("Apple mount", log_text)

    def test_common_wake_or_mount_uses_apple_diskd_before_mount_fallback(self) -> None:
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
                    APPLE_MOUNT_WAIT_SECONDS=2
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR" {volumes}/dk2
                    is_volume_root_mounted() {{
                        count=$(cat {tmp_path}/count 2>/dev/null || echo 0)
                        count=$((count + 1))
                        echo "$count" >{tmp_path}/count
                        [ "$count" -ge 2 ]
                    }}
                    mount_hfs_bounded() {{ echo fallback >>{tmp_path}/fallback.log; return 1; }}
                    tc_wake_or_mount_volume /dev/dk2 {volumes}/dk2
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            acp_log = (tmp_path / "acp.log").read_text()
            fallback_exists = (tmp_path / "fallback.log").exists()
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"rpc diskd.useVolume path:s:{volumes}/dk2", acp_log)
        self.assertFalse(fallback_exists)
        self.assertIn(f"MaSt volume {volumes}/dk2: requesting diskd.useVolume for {volumes}/dk2", log_text)
        self.assertIn(
            f"MaSt volume {volumes}/dk2: observed {volumes}/dk2 mounted after diskd.useVolume wait: 0s",
            log_text,
        )
        self.assertNotIn("Apple mount", log_text)

    def test_common_wake_or_mount_logs_diskd_timeout_before_manual_fallback(self) -> None:
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
                    APPLE_MOUNT_WAIT_SECONDS=1
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR" {volumes}/dk2
                    is_volume_root_mounted() {{ return 1; }}
                    sleep() {{ :; }}
                    mount_hfs_bounded() {{ echo "fallback $1 $2"; return 1; }}
                    tc_wake_or_mount_volume /dev/dk2 {volumes}/dk2 || true
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, f"fallback /dev/dk2 {volumes}/dk2\n")
        self.assertIn(
            f"MaSt volume {volumes}/dk2: diskd.useVolume wait timed out after 1s; "
            "manual fallback will handle remaining unmounted volumes",
            log_text,
        )
        self.assertNotIn("manual mount fallback starting", log_text)
        self.assertNotIn("Apple mount", log_text)

    def test_common_boot_and_watchdog_mount_policies_use_separate_waits(self) -> None:
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
                    APPLE_MOUNT_WAIT_SECONDS=7
                    WATCHDOG_MOUNT_WAIT_SECONDS=2
                    tc_init_runtime_env
                    tc_set_log "$RAM_VAR/test.log" test
                    mkdir -p "$RAM_VAR" {volumes}/dk2
                    tc_wake_or_mount_volume_with_policy() {{
                        printf '%s %s %s %s %s\\n' "$1" "$2" "$3" "$4" "$5"
                        return 1
                    }}
                    tc_boot_wake_or_mount_volume /dev/dk2 {volumes}/dk2 || true
                    tc_watchdog_wake_or_mount_volume /dev/dk2 {volumes}/dk2 || true
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(lines[0], f"/dev/dk2 {volumes}/dk2 7 30 MaSt volume {volumes}/dk2")
        self.assertEqual(lines[1], f"/dev/dk2 {volumes}/dk2 2 30 watchdog volume {volumes}/dk2")

    def test_common_watchdog_iteration_mounts_active_share_volumes_before_process_checks(self) -> None:
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
                    tc_topology_changed() {{ return 1; }}
                    tc_payload_available() {{ echo payload; return 0; }}
                    tc_watchdog_wake_or_mount_volume() {{ echo "mount $1 $2"; return 0; }}
                    tc_start_smbd_if_needed() {{ echo smbd; }}
                    runtime_process_present_by_ucomm() {{ return 0; }}
                    tc_watchdog_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            f"payload\nmount /dev/dk2 {volumes}/dk2\nmount /dev/dk3 {volumes}/dk3\nsmbd\n",
        )

    def test_common_watchdog_iteration_stops_services_when_active_share_mount_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            (volumes / "dk2/ShareRoot").mkdir(parents=True)
            script = tmp_path / "watchdog-active-share-missing.sh"
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
                    tc_topology_changed() {{ return 1; }}
                    tc_payload_available() {{ echo payload; return 0; }}
                    tc_watchdog_wake_or_mount_volume() {{
                        echo "mount $1 $2"
                        [ "$1" != "/dev/dk3" ]
                    }}
                    tc_stop_managed_services() {{ echo stop; }}
                    tc_exec_start_samba() {{ echo "exec $1"; exit 42; }}
                    tc_watchdog_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("payload\n", proc.stdout)
        self.assertIn(f"mount /dev/dk2 {volumes}/dk2\n", proc.stdout)
        self.assertIn(f"mount /dev/dk3 {volumes}/dk3\n", proc.stdout)
        self.assertIn("stop\n", proc.stdout)
        self.assertNotIn("exec active share volume unavailable\n", proc.stdout)
        self.assertIn("active share check row 2: share=USB", log_text)
        self.assertIn("active share volume unavailable: /dev/dk3", log_text)
        self.assertIn("watchdog recovery: active share volume unavailable; stopping managed services and retrying", log_text)

    def test_common_watchdog_iteration_reexecs_when_share_state_is_missing(self) -> None:
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
                    mkdir -p "$RAM_VAR"
                    tc_topology_changed() {{ return 1; }}
                    tc_payload_available() {{ echo payload; return 0; }}
                    tc_exec_start_samba() {{ echo "exec $1"; exit 42; }}
                    tc_watchdog_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 42, proc.stderr)
        self.assertEqual(proc.stdout, "payload\nexec active share state unavailable\n")

    def test_common_watchdog_iteration_reexecs_start_samba_on_topology_change(self) -> None:
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
                    tc_topology_changed() {{ return 0; }}
                    tc_exec_start_samba() {{ echo "exec $1"; exit 42; }}
                    tc_watchdog_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 42, proc.stderr)
        self.assertEqual(proc.stdout, "exec MaSt topology changed\n")

    def test_common_watchdog_iteration_ignores_transient_topology_change_after_debounce(self) -> None:
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
                    tc_topology_changed() {{
                        count=$(cat {tmp_path}/topology-count 2>/dev/null || echo 0)
                        count=$((count + 1))
                        echo "$count" >{tmp_path}/topology-count
                        [ "$count" -eq 1 ]
                    }}
                    tc_payload_available() {{ echo payload; return 0; }}
                    tc_mount_active_volumes_from_state() {{ echo mounts; return 0; }}
                    tc_start_smbd_if_needed() {{ echo smbd; }}
                    runtime_process_present_by_ucomm() {{ return 0; }}
                    tc_exec_start_samba() {{ echo "exec $1"; exit 42; }}
                    tc_watchdog_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "payload\nmounts\nsmbd\n")

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

    def test_common_watchdog_iteration_stops_services_when_payload_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            script = tmp_path / "watchdog-payload-missing.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    . {flash}/tcapsulesmb.conf
                    tc_init_runtime_env
                    mkdir -p "$RAM_VAR"
                    tc_topology_changed() {{ return 1; }}
                    tc_payload_available() {{ echo missing; return 1; }}
                    tc_stop_managed_services() {{ echo stop; }}
                    tc_watchdog_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(proc.stdout, "missing\nstop\n")

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
