from __future__ import annotations

import shlex
import socket
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.cli.deploy import derive_net_ipv4_hint, render_flash_runtime_config
from timecapsulesmb.deploy.executor import upload_flash_file
from timecapsulesmb.deploy.planner import (
    GENERATED_FLASH_CONFIG_SOURCE,
    build_deployment_plan,
)
from timecapsulesmb.device.probe import (
    normalize_runtime_mdns_host_label,
    normalize_runtime_mdns_instance_name,
    normalize_runtime_netbios_name,
    runtime_ipv4_cidr_from_ifconfig,
)
from timecapsulesmb.device.storage import (
    PayloadVerificationResult,
    NO_WRITABLE_PERSISTENT_VOLUME_MESSAGE,
    MaStReadResult,
    MaStVolume,
    PayloadHome,
    ensure_volume_root_mounted_conn,
    ordered_payload_candidate_volumes,
    payload_candidate_checks_debug_summary,
    parse_mast_plist,
    render_ensure_volume_root_mounted_script,
    select_payload_home_conn,
    select_payload_home_with_diagnostics_conn,
    verify_payload_home_conn,
    wait_for_mast_volumes_conn,
)
from timecapsulesmb.transport.ssh import SshConnection
from tests.storage_fixtures import INTERNAL_DATA, MAST_FIXTURES, SHELL_MAST_FIXTURES, MaStFixture


class StorageRuntimeTests(unittest.TestCase):
    _runtime_asset_texts: tuple[str, str, str] | None = None

    @classmethod
    def runtime_asset_texts(cls) -> tuple[str, str, str]:
        if cls._runtime_asset_texts is None:
            repo_root = Path(__file__).resolve().parent.parent
            cls._runtime_asset_texts = (
                (repo_root / "src/timecapsulesmb/assets/boot/samba4/common.sh").read_text(),
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
                NET_IPV4_HINT=''
                SMB_SAMBA_USER='admin'
                MDNS_DEVICE_MODEL='TimeCapsule6,106'
                AIRPORT_SYAP='106'
                INTERNAL_SHARE_USE_DISK_ROOT=0
                DISKD_USE_VOLUME_ATTEMPTS=2
                ATA_IDLE_SECONDS=300
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

    def test_common_wait_for_bind_interfaces_logs_filtered_network_diagnostics_on_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fake_ifconfig = tmp_path / "ifconfig"
            fake_ifconfig.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    case "$1" in
                        bridge0)
                            cat <<'OUT'
                    bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
                            ether 00:11:22:33:44:55
                            status: active
                    OUT
                            ;;
                        -a)
                            cat <<'OUT'
                    bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
                            ether 66:77:88:99:aa:bb
                            inet 169.254.44.9 netmask 0xffff0000 broadcast 169.254.255.255
                            status: active
                    bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
                            ether 00:11:22:33:44:55
                            status: active
                    OUT
                            ;;
                        *)
                            exit 1
                            ;;
                    esac
                    """
                )
            )
            fake_ifconfig.chmod(0o755)
            common_path = flash / "common.sh"
            common_path.write_text(common_path.read_text().replace("/sbin/ifconfig", str(fake_ifconfig)))
            script = tmp_path / "network-timeout-diagnostics.sh"
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
                    tc_wait_for_bind_interfaces || status=$?
                    printf 'status=%s\\n' "${{status:-0}}"
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=1\n", proc.stdout)
        self.assertIn("timed out waiting for IPv4 on bridge0", proc.stdout)
        self.assertIn("network diagnostics: configured NET_IFACE=bridge0", proc.stdout)
        self.assertIn("network diagnostics: bridge0: bridge0: flags=", proc.stdout)
        self.assertIn("network diagnostics: ifconfig -a: bcmeth1: flags=", proc.stdout)
        self.assertIn("network diagnostics: ifconfig -a:         inet 169.254.44.9", proc.stdout)
        self.assertNotIn("00:11:22:33:44:55", proc.stdout)
        self.assertNotIn("66:77:88:99:aa:bb", proc.stdout)

    def test_common_wait_for_bind_interfaces_uses_first_non_link_local_ipv4(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            fake_ifconfig = tmp_path / "ifconfig"
            fake_ifconfig.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    cat <<'OUT'
                    bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
                            inet 0.0.0.0 netmask 0xff000000 broadcast 255.255.255.255
                            inet 169.254.44.9 netmask 0xffff0000 broadcast 169.254.255.255
                            inet 192.168.1.2 netmask 0xffffff00 broadcast 192.168.1.255
                    OUT
                    """
                )
            )
            fake_ifconfig.chmod(0o755)
            common_path = flash / "common.sh"
            common_path.write_text(common_path.read_text().replace("/sbin/ifconfig", str(fake_ifconfig)))
            script = tmp_path / "network-ready.sh"
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
                    tc_wait_for_bind_interfaces
                    cat "$RAM_VAR/test.log"
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("127.0.0.1/8 192.168.1.2/24", proc.stdout)
        self.assertIn("network interface bridge0 ready with IPv4 192.168.1.2/24", proc.stdout)

    def test_common_runtime_interface_ipv4_cidr_matches_python_policy(self) -> None:
        multi_ipv4 = textwrap.dedent(
            """\
            bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
                    inet 10.0.1.3 netmask 0xffff0000 broadcast 10.0.255.255
                    inet 192.168.1.2 netmask 0xffffff00 broadcast 192.168.1.255
            """
        )
        cases = {
            "exact_hint": (multi_ipv4, "192.168.1.2"),
            "stale_hint": (multi_ipv4, "192.168.99.99"),
            "absent_hint": (multi_ipv4, ""),
            "usable_after_bad": (
                textwrap.dedent(
                    """\
                    bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
                            inet 0.0.0.0 netmask 0xff000000 broadcast 255.255.255.255
                            inet 127.0.0.1 netmask 0xff000000 broadcast 127.255.255.255
                            inet 169.254.44.9 netmask 0xffff0000 broadcast 169.254.255.255
                            inet 192.168.1.2 netmask 0xffffff00 broadcast 192.168.1.255
                    """
                ),
                "169.254.44.9",
            ),
            "netbsd_alias": (
                textwrap.dedent(
                    """\
                    bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
                            inet alias 10.0.1.13 netmask 0xffffff00 broadcast 10.0.1.255
                    """
                ),
                "",
            ),
            "netbsd_alias_hint": (
                textwrap.dedent(
                    """\
                    bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
                            inet 10.0.1.3 netmask 0xffff0000 broadcast 10.0.255.255
                            inet alias 192.168.1.2 netmask 0xffffff00 broadcast 192.168.1.255
                    """
                ),
                "192.168.1.2",
            ),
            "link_local_alias_before_usable": (
                textwrap.dedent(
                    """\
                    bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
                            inet alias 169.254.44.9 netmask 0xffff0000 broadcast 169.254.255.255
                            inet 192.168.1.2 netmask 0xffffff00 broadcast 192.168.1.255
                    """
                ),
                "",
            ),
            "link_local_only": (
                textwrap.dedent(
                    """\
                    bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
                            inet 0.0.0.0 netmask 0xff000000 broadcast 255.255.255.255
                            inet 169.254.44.9 netmask 0xffff0000 broadcast 169.254.255.255
                    """
                ),
                "169.254.44.9",
            ),
            "active_no_ipv4": (
                textwrap.dedent(
                    """\
                    bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
                            status: active
                    """
                ),
                "",
            ),
            "missing_netmask": (
                textwrap.dedent(
                    """\
                    bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
                            inet 10.20.3.4 broadcast 10.20.3.255
                    """
                ),
                "",
            ),
            "dotted_netmask": (
                textwrap.dedent(
                    """\
                    bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
                            inet 10.20.3.4 netmask 255.255.254.0 broadcast 10.20.3.255
                    """
                ),
                "",
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            flash, _memory, _locks, _volumes = self.write_runtime_harness(tmp_path)
            case_dir = tmp_path / "ifconfig-cases"
            case_dir.mkdir()
            for name, (raw, hint) in cases.items():
                (case_dir / f"{name}.txt").write_text(raw)
                (case_dir / f"{name}.hint").write_text(hint)
            fake_ifconfig = tmp_path / "ifconfig"
            fake_ifconfig.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    case "$1" in
                        bridge0)
                            cat {shlex.quote(str(case_dir))}/"$IFCONFIG_CASE.txt"
                            ;;
                        *)
                            exit 1
                            ;;
                    esac
                    """
                )
            )
            fake_ifconfig.chmod(0o755)
            common_path = flash / "common.sh"
            common_path.write_text(common_path.read_text().replace("/sbin/ifconfig", str(fake_ifconfig)))
            case_names = " ".join(shlex.quote(name) for name in cases)
            script = tmp_path / "runtime-interface-policy.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    . {flash}/common.sh
                    for case_name in {case_names}; do
                        IFCONFIG_CASE=$case_name
                        IFCONFIG_HINT=$(cat {shlex.quote(str(case_dir))}/"$IFCONFIG_CASE.hint")
                        export IFCONFIG_CASE
                        export IFCONFIG_HINT
                        status=0
                        cidr=$(get_runtime_iface_ipv4_cidr bridge0 "$IFCONFIG_HINT") || status=$?
                        printf '__TC_BEGIN__\\t%s\\t%s\\n' "$case_name" "$status"
                        if [ "$status" -eq 0 ]; then
                            printf '%s\\n' "$cidr"
                        fi
                        printf '__TC_END__\\t%s\\n' "$case_name"
                    done
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        sections = self.parse_named_shell_sections(proc.stdout)
        for name, (raw, hint) in cases.items():
            with self.subTest(case=name):
                expected_cidr = runtime_ipv4_cidr_from_ifconfig(raw, hint_ip=hint)
                status, stdout = sections[name]
                self.assertEqual(status, 0 if expected_cidr is not None else 1)
                self.assertEqual(stdout, f"{expected_cidr}\n" if expected_cidr is not None else "")

    def write_fake_acp(self, tmp_path: Path, raw: str | bytes, *, final_newline: bool = True) -> Path:
        acp = tmp_path / "acp"
        raw_text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        if final_newline:
            acp.write_text("#!/bin/sh\ncat <<'OUT'\n" + raw_text + "\nOUT\n")
        else:
            acp.write_text("#!/bin/sh\nprintf %s " + shlex.quote(raw_text) + "\n")
        acp.chmod(0o755)
        return acp

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
        selector = self.write_selectable_fixture_acp(tmp_path, fixtures)
        for fixture in fixtures:
            for volume in fixture.expected:
                Path(self.mapped_volume_root(volume, volumes_root)).mkdir(parents=True, exist_ok=True)
        override = "INTERNAL_SHARE_USE_DISK_ROOT=1" if internal_share_use_disk_root else ""
        names = " ".join(shlex.quote(fixture.name) for fixture in fixtures)
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
                    echo "$fixture_name" >{shlex.quote(str(selector))}
                    printf '__TC_BEGIN__\\t%s\\t0\\n' "$fixture_name"
                    tc_read_mast_volumes_to "$TC_VOLUMES_TSV" "$TC_MAST_RAW"
                    tc_build_share_state "$TC_VOLUMES_TSV"
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

    def test_ensure_volume_root_mounted_conn_uses_diskd_and_mount_hfs_fallback(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        with mock.patch("timecapsulesmb.device.storage.run_ssh", return_value=mock.Mock(returncode=0)) as run_ssh_mock:
            self.assertTrue(ensure_volume_root_mounted_conn(connection, "/Volumes/dk2", "/dev/dk2", wait_seconds=12))

        run_ssh_mock.assert_called_once()
        remote_command = run_ssh_mock.call_args.args[1]
        self.assertIn("/bin/df -k /Volumes/dk2", remote_command)
        self.assertIn("/usr/bin/tail -n +2", remote_command)
        self.assertIn("/usr/bin/acp rpc diskd.useVolume", remote_command)
        self.assertIn("/sbin/mount_hfs /dev/dk2 /Volumes/dk2", remote_command)
        self.assertNotIn("grep", remote_command)
        self.assertNotIn("awk", remote_command)
        self.assertNotIn("cut", remote_command)
        self.assertEqual(run_ssh_mock.call_args.kwargs["timeout"], 57)

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
            net_ipv4_hint="10.0.0.2",
        )

        self.assertIn("PAYLOAD_DIR_NAME=.samba4\n", rendered)
        self.assertIn("NET_IPV4_HINT=10.0.0.2\n", rendered)
        self.assertNotIn("PAYLOAD_VOLUME_HINT", rendered)
        self.assertNotIn("PAYLOAD_DEVICE_HINT", rendered)
        self.assertNotIn("PAYLOAD_INSTALL_ID", rendered)
        self.assertIn("INTERNAL_SHARE_USE_DISK_ROOT=1\n", rendered)
        self.assertIn("DISKD_USE_VOLUME_ATTEMPTS=2\n", rendered)
        self.assertIn("ATA_IDLE_SECONDS=300\n", rendered)
        self.assertIn("NBNS_ENABLED=1\n", rendered)
        self.assertIn("SMBD_DEBUG_LOGGING=1\n", rendered)
        self.assertNotIn("SMB_NETBIOS_NAME", rendered)
        self.assertNotIn("MDNS_INSTANCE_NAME", rendered)
        self.assertNotIn("MDNS_HOST_LABEL", rendered)
        self.assertNotIn("TC_SHARE_NAME", rendered)

    def test_deploy_net_ipv4_hint_uses_literal_host_on_interface(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_NET_IFACE": "bridge0"})

        hint = derive_net_ipv4_hint(config, ("169.254.44.9", "10.0.0.2"))

        self.assertEqual(hint, "10.0.0.2")

    def test_deploy_net_ipv4_hint_resolves_hostname_on_interface(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@timecapsule.local", "TC_NET_IFACE": "bridge0"})
        resolved = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.44.9", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.2", 0)),
        ]

        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=resolved):
            hint = derive_net_ipv4_hint(config, ("10.0.0.2",))

        self.assertEqual(hint, "10.0.0.2")

    def test_deploy_net_ipv4_hint_omits_unresolved_or_unmatched_host(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@timecapsule.local", "TC_NET_IFACE": "bridge0"})
        resolved = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.3", 0))]

        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=resolved):
            self.assertEqual(derive_net_ipv4_hint(config, ("10.0.0.2",)), "")
        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", side_effect=OSError):
            self.assertEqual(derive_net_ipv4_hint(config, ("10.0.0.2",)), "")

    def test_deploy_net_ipv4_hint_rejects_link_local_host(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@169.254.44.9", "TC_NET_IFACE": "bridge0"})

        hint = derive_net_ipv4_hint(config, ("169.254.44.9",))

        self.assertEqual(hint, "")

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
        self.assertIn("nbns_args=--name TimeCapsule --ipv4 192.168.1.2", proc.stdout)
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
            source.write_text("TC_CONFIG_VERSION=1\n")

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
            start_path = flash / "start-samba.sh"
            selector = self.write_selectable_fixture_acp(tmp_path, SHELL_MAST_FIXTURES)
            names = " ".join(shlex.quote(fixture.name) for fixture in SHELL_MAST_FIXTURES)
            script = tmp_path / "signature-fixtures.sh"
            script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    set -eu
                    for fixture_name in {names}; do
                        echo "$fixture_name" >{shlex.quote(str(selector))}
                        out={shlex.quote(str(tmp_path))}/"signature-$fixture_name.out"
                        err={shlex.quote(str(tmp_path))}/"signature-$fixture_name.err"
                        set +e
                        /bin/sh {shlex.quote(str(start_path))} --print-topology-signature >"$out" 2>"$err"
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
                expected_rc = 0 if fixture.expected else 1
                status, stdout = sections[fixture.name]
                self.assertEqual(status, expected_rc, proc.stderr)
                self.assertEqual(stdout, expected_stdout)
                self.assertEqual(self.parse_topology_tsv(stdout, volumes), parse_mast_plist(fixture.raw))

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
                        tc_init_runtime_identity() {{ echo identity; }}
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
                    "identity",
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
                    cat >"$TC_VOLUMES_TSV" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    tc_resolve_payload "$TC_VOLUMES_TSV" || status=$?
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
                    cat >"$TC_VOLUMES_TSV" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    tc_resolve_payload "$TC_VOLUMES_TSV" || status=$?
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

    def test_common_refresh_disk_state_sets_ata_idle_after_share_state(self) -> None:
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
                    tc_wait_for_mast_volumes_to() {{
                        cat >"$1" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                        : >"$2"
                        return 0
                    }}
                    tc_mount_mast_volumes_for_boot() {{ echo mount >>{events}; }}
                    tc_build_share_state() {{
                        echo share >>{events}
                        : >"$TC_SHARES_TSV"
                        echo "Data	{volumes}/dk2/ShareRoot	dk2	1	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" >>"$TC_SHARES_TSV"
                        : >"$TC_ADISK_TSV"
                        return 0
                    }}
                    tc_configure_ata_idle_for_mast_disks() {{ echo ata >>{events}; }}
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
        self.assertEqual(proc.stdout.splitlines(), ["mount", "share", "ata", "payload"])

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
                    tc_start_smbd_if_needed() {{ echo smbd; }}
                    runtime_process_present_by_ucomm() {{ echo "process $1"; return 0; }}
                    tc_all_managed_services_healthy() {{ echo healthy; return 0; }}
                    tc_watchdog_service_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "smbd\nprocess mdns-advertiser\nprocess nbns-advertiser\nhealthy\n")

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

                    tc_read_payload_state() {{ return 1; }}
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
            fake_sleep = tmp_path / "sleep"
            fake_sleep.write_text("#!/bin/sh\n/bin/sleep 0.05\n")
            fake_sleep.chmod(0o755)
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
                    PATH={shlex.quote(str(tmp_path))}:$PATH
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
                    wait "$TC_MDNS_CAPTURE_PID" || true
                    tc_launch_mdns_advertiser "mdns test" 1 0 0
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
                    wait "$TC_MDNS_CAPTURE_PID" || true
                    tc_launch_mdns_advertiser "mdns test" 1 0 0
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
        self.assertIn("mDNS snapshot capture did not produce trusted Apple snapshot; generating AirPort fallback", proc.stdout)
        self.assertIn("launching mdns-advertiser airport snapshot", proc.stdout)
        self.assertIn("--save-airport-snapshot", proc.stdout)
        self.assertIn("--load-snapshot", proc.stdout)

    def test_common_mdns_and_nbns_write_ram_logs_in_normal_mode(self) -> None:
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
        self.assertIn("/samba4/var/mdns.log", proc.stdout)
        self.assertIn("/samba4/var/nbns.log", proc.stdout)
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
                    cat >"$TC_VOLUMES_TSV" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    wd0	1	dk3	{volumes}/dk3	More	bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                    sd0	0	dk4	{volumes}/dk4	USB	cccccccc-cccc-cccc-cccc-cccccccccccc
                    sd1	1	dk5	{volumes}/dk5	SSD	dddddddd-dddd-dddd-dddd-dddddddddddd
                    EOF
                    tc_configure_ata_idle_for_mast_disks "$TC_VOLUMES_TSV"
                    cat {atactl_log}
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "wd0 setidle 300\n")
        self.assertIn("ATA idle tuning: set wd0 idle timer to 300s", log_text)
        self.assertIn("ATA idle tuning: skipping sd0 for /dev/dk4; MaSt marks disk as external", log_text)
        self.assertIn("ATA idle tuning: skipping sd1 for /dev/dk5; not a wd ATA disk", log_text)

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
                    cat >"$TC_VOLUMES_TSV" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    tc_configure_ata_idle_for_mast_disks "$TC_VOLUMES_TSV"
                    [ ! -f {atactl_log} ]
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ATA idle tuning disabled", log_text)

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
                    cat >"$TC_VOLUMES_TSV" <<'EOF'
                    wd0	1	dk2	{volumes}/dk2	Data	aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                    EOF
                    tc_configure_ata_idle_for_mast_disks "$TC_VOLUMES_TSV"
                    echo continued
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "continued\n")
        self.assertIn("ATA idle tuning: failed to set wd0 idle timer to 300s", log_text)

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
        self.assertIn("watchdog disk check: MaSt snapshot read failed or produced no valid HFS volumes", log_text)
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
                    tc_watchdog_wake_or_mount_volume() {{ echo "mount $1 $2"; return 0; }}
                    tc_start_smbd_if_needed() {{ echo smbd; }}
                    runtime_process_present_by_ucomm() {{ return 0; }}
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
                    tc_start_smbd_if_needed() {{ echo smbd; return 0; }}
                    runtime_process_present_by_ucomm() {{ return 0; }}
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
                    tc_write_payload_state {payload} {volumes}/dk2 /dev/dk2
                    sleep() {{ :; }}
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
                    tc_wait_for_bind_interfaces() {{ echo "127.0.0.1/8 192.168.1.2/24"; }}
                    tc_prepare_local_hostname_resolution() {{ echo hostname; }}
                    tc_init_runtime_identity() {{
                        echo identity
                        MDNS_INSTANCE_NAME=Live
                        MDNS_HOST_LABEL=live
                        SMB_NETBIOS_NAME=Live
                        SMB_SERVER_STRING=Live
                        TC_RUNTIME_IDENTITY_READY=1
                    }}
                    tc_generate_smb_conf() {{ echo "generate $1 $2"; }}
                    tc_reload_smbd_config() {{ echo reload; }}
                    tc_launch_mdns_advertiser() {{ echo "mdns $1 $2 $3 $4"; }}
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
                    f"generate {payload} 127.0.0.1/8 192.168.1.2/24",
                    "reload",
                    "mdns watchdog topology refresh 0 1 10",
                    "",
                )
            ),
        )
        self.assertIn("watchdog recovery: attempting live disk runtime refresh: MaSt topology changed", log_text)
        self.assertIn("watchdog recovery: live disk runtime refresh complete", log_text)
        self.assertNotIn("re-execing start-samba.sh", log_text)

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

    def test_common_watchdog_service_iteration_fails_when_smbd_recovery_payload_state_is_unavailable(self) -> None:
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
                    runtime_process_present_by_ucomm() {{ return 1; }}
                    tc_watchdog_refresh_runtime_identity_for_recovery() {{ :; }}
                    tc_watchdog_service_iteration
                    """
                )
            )
            script.chmod(0o755)

            proc = subprocess.run([str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            log_text = (memory / "samba4/var/test.log").read_text()

        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("watchdog recovery: smbd restart skipped; payload state is unavailable", log_text)
        self.assertIn("watchdog pass: smbd recovery did not complete", log_text)

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
