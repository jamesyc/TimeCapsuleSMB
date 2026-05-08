from __future__ import annotations

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
from timecapsulesmb.device.storage import (
    NO_WRITABLE_PERSISTENT_VOLUME_MESSAGE,
    MaStVolume,
    PayloadHome,
    parse_mast_plist,
    select_payload_home_conn,
)
from timecapsulesmb.transport.ssh import SshConnection
from tests.storage_fixtures import MAST_FIXTURES, SHELL_MAST_FIXTURES, MaStFixture


class StorageRuntimeTests(unittest.TestCase):
    def write_runtime_harness(self, tmp_path: Path) -> tuple[Path, Path, Path, Path]:
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
                SMB_NETBIOS_NAME='TimeCapsule'
                MDNS_INSTANCE_NAME='Time Capsule Samba 4'
                MDNS_HOST_LABEL='timecapsulesamba4'
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

    def write_fake_acp(self, tmp_path: Path, raw: str | bytes) -> Path:
        acp = tmp_path / "acp"
        raw_text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        acp.write_text("#!/bin/sh\ncat <<'OUT'\n" + raw_text + "\nOUT\n")
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

    def unique_share_names(self, volumes: tuple[MaStVolume, ...]) -> list[str]:
        names: list[str] = []
        used: set[str] = set()
        for volume in volumes:
            base = volume.name.strip() or f"Disk {volume.partition_device}"
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
                tc_wake_or_mount_volume() {{ return 0; }}
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
                "TC_NETBIOS_NAME": "TimeCapsule",
                "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
                "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
                "TC_MDNS_DEVICE_MODEL": "TimeCapsule6,106",
                "TC_AIRPORT_SYAP": "106",
                "TC_INTERNAL_SHARE_USE_DISK_ROOT": "true",
            }
        )

        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            install_nbns=True,
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
        self.assertNotIn("TC_SHARE_NAME", rendered)
        self.assertNotIn("TC_SHARE_USE_DISK_ROOT", rendered)

    def test_flash_runtime_config_migrates_legacy_hidden_share_root_key(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_NET_IFACE": "bridge0",
                "TC_SAMBA_USER": "admin",
                "TC_NETBIOS_NAME": "TimeCapsule",
                "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
                "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
                "TC_MDNS_DEVICE_MODEL": "TimeCapsule6,106",
                "TC_AIRPORT_SYAP": "106",
                "TC_INTERNAL_SHARE_USE_DISK_ROOT": "false",
                "TC_SHARE_USE_DISK_ROOT": "true",
            },
            file_values={"TC_SHARE_USE_DISK_ROOT": "true"},
        )

        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            install_nbns=False,
            debug_logging=False,
        )

        self.assertIn("INTERNAL_SHARE_USE_DISK_ROOT=1\n", rendered)
        self.assertNotIn("TC_SHARE_USE_DISK_ROOT", rendered)

    def test_deployment_plan_uses_flash_pointer_and_single_private_payload(self) -> None:
        plan = build_deployment_plan(
            "root@10.0.0.2",
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            Path("/tmp/smbd"),
            Path("/tmp/mdns-advertiser"),
            Path("/tmp/nbns-advertiser"),
            install_nbns=True,
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
                        if volume.builtin:
                            marker = Path(self.mapped_volume_root(volume, volumes)) / "ShareRoot/.com.apple.timemachine.supported"
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
                    tc_wake_or_mount_volume() {{ printf '%s %s\\n' "$1" "$2" >>{tmp_path}/mounts.log; return 0; }}
                    cat >"$TC_VOLUMES_TSV" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    sd0	0	dk3	{volumes}/dk3	Data	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    EOF
                    tc_build_share_state "$TC_VOLUMES_TSV"
                    printf 'shares\\n'
                    cat "$TC_SHARES_TSV"
                    printf 'adisk\\n'
                    cat "$TC_ADISK_TSV"
                    printf 'marker=%s\\n' "$([ -f {volumes}/dk2/ShareRoot/.com.apple.timemachine.supported ] && echo yes || echo no)"
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
        self.assertIn("marker=yes\n", proc.stdout)

    def test_common_build_share_state_bounds_names_to_adisk_txt_budget(self) -> None:
        long_name = "A" * 250
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
                    tc_wake_or_mount_volume() {{ return 0; }}
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
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
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
                    tc_wake_or_mount_volume() {{ return 0; }}
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

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, f"{volumes}/dk3/.samba4\n{volumes}/dk3\n/dev/dk3\n")

    def test_common_generate_smb_conf_uses_single_payload_private_db_for_all_shares(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
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

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("[Data]\n", proc.stdout)
        self.assertIn("[USB]\n", proc.stdout)
        self.assertEqual(proc.stdout.count(f"xattr_tdb:file = {payload}/private/xattr.tdb"), 2)
        self.assertEqual(proc.stdout.count("veto files = /.samba4/"), 2)
        self.assertIn(f"path = {volumes}/dk2/ShareRoot", proc.stdout)
        self.assertIn(f"path = {volumes}/dk3", proc.stdout)
        self.assertIn(f"log file = {payload}/logs/log.smbd", proc.stdout)
        self.assertIn("max log size = 128", proc.stdout)
        self.assertNotIn("log level = 5", proc.stdout)

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

    def test_common_mdns_and_nbns_write_payload_logs_in_normal_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, memory, _locks, volumes = self.write_runtime_harness(tmp_path)
            payload = volumes / "dk2/.samba4"
            payload.mkdir(parents=True)
            (flash / "mdns-advertiser").write_text("#!/bin/sh\necho mdns-stdout\necho mdns-stderr >&2\n")
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
                    get_radio_mac() {{ return 1; }}
                    get_airport_srcv() {{ return 1; }}
                    stop_nbns_conflicts() {{ return 0; }}
                    TC_NET_IFACE_IP=192.168.1.2
                    tc_set_payload_log_dir {payload} {volumes}/dk2
                    tc_start_mdns_capture
                    wait "$TC_MDNS_CAPTURE_PID" || true
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
        self.assertIn("launching mdns-advertiser capture", proc.stdout)
        self.assertIn("mdns-stdout", proc.stdout)
        self.assertIn("mdns-stderr", proc.stdout)
        self.assertIn("nbns\n", proc.stdout)
        self.assertIn("launching nbns-advertiser", proc.stdout)
        self.assertIn("nbns-stdout", proc.stdout)
        self.assertIn("nbns-stderr", proc.stdout)

    def test_common_wake_or_mount_uses_apple_diskd_before_mount_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
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

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"rpc diskd.useVolume path:s:{volumes}/dk2", acp_log)
        self.assertFalse(fallback_exists)

    def test_common_watchdog_iteration_mounts_active_share_volumes_before_process_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, volumes = self.write_runtime_harness(tmp_path)
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
                    tc_wake_or_mount_volume() {{ echo "mount $1 $2"; return 0; }}
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
