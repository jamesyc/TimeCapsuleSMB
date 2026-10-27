"""Wipe Internal Disk: the device helpers (geometry, offsets, the wipe script,
the read-back, diskd's jobs, `gpt show`, volume names) and the flow, against
a fake device whose MaSt, process list and diskd answers each step changes."""
from __future__ import annotations

from dataclasses import replace
import shlex
import subprocess
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

from timecapsulesmb.app.confirmations import build_confirmation
from timecapsulesmb.app.ops import OPERATION_PARAMS
from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.device import wipe
from timecapsulesmb.device.storage import (
    DiskdRpcReply,
    MaStDiskSnapshot,
    MaStDiscoveryResult,
    MaStPartitionSnapshot,
    MaStReadResult,
    disk_size_misreported,
)
from timecapsulesmb.services import wipe_disk
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.deploy import DeployDeviceError, mast_no_volume_error
from timecapsulesmb.services.maintenance import SharingRestart, reboot_and_restart_sharing
from timecapsulesmb.services.reboot import RebootFlowError
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection


CONNECTION = SshConnection("root@10.0.0.2", "pw", "")
DISK_UUID = "8320bedd-bee1-56fc-92fb-cc47aa532c1f"
OLD_VOLUME_UUID = "105b5aa0-ebe6-5928-84d0-9b3f959fe903"
NEW_VOLUME_UUID = "31a84d4e-6e16-5f54-9692-5a6afb4ea3d9"
USB_UUID = "5c418e94-6028-5b5b-92be-ec4e27902b91"
TWO_TB = wipe.DiskGeometry(512, 3907029168, "0x200", "0xe8e088b0")


def drvctl_output(sector_size: str, sectors: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0">\n<dict>\n\t<key>disk-info</key>\n\t<dict>\n'
        "\t\t<key>geometry</key>\n\t\t<dict>\n"
        f"\t\t\t<key>sector-size</key>\n\t\t\t<integer>{sector_size}</integer>\n"
        f"\t\t\t<key>sectors-per-unit</key>\n\t\t\t<integer>{sectors}</integer>\n"
        "\t\t</dict>\n\t</dict>\n</dict>\n</plist>\n"
    )


def disk(**changes: object) -> MaStDiskSnapshot:
    base = MaStDiskSnapshot(
        "sd0", "", 1907729, True,
        (MaStPartitionSnapshot("dk2", "Data", "hfs", 1905681, 8977, 1896704, users=1, uuid=OLD_VOLUME_UUID),),
        label="Disk 1", model="WDC WD20EARS-42S0XB0", product="WD-WCC1H1064561", uuid=DISK_UUID,
    )
    return replace(base, **changes)


class DiskSizeRuleTests(unittest.TestCase):
    def test_the_internal_disk_reporting_4_gb_with_no_partitions_is_misreported(self) -> None:
        # The TC8,119 record from telemetry: Apple's Seagate, its "APPLE HDD"
        # prefix gone, at 3950 MiB.
        self.assertTrue(disk_size_misreported(disk(size=3950, partitions=(), model="TS0300MD00 1")))

    def test_real_disks_are_not(self) -> None:
        cases = {
            "blank USB stick under 15 GB": disk(builtin=False, size=7600, partitions=()),
            "blank full-size retail drive": disk(size=1907729, partitions=(), model="TS0200MD00-8U21B20"),
            "small internal disk with a partition": disk(size=3950),
            "internal disk at the limit": disk(size=wipe_disk_limit(), partitions=()),
            "unknown size": disk(size=None, partitions=()),
        }
        for name, case in cases.items():
            with self.subTest(name):
                self.assertFalse(disk_size_misreported(case))


def wipe_disk_limit() -> int:
    from timecapsulesmb.device.storage import MISREPORTED_DISK_SIZE_LIMIT_MIB
    return MISREPORTED_DISK_SIZE_LIMIT_MIB


class DataVolumeTests(unittest.TestCase):
    def test_only_the_data_volume_marks_the_erase_done(self) -> None:
        # Apple's erase makes APconfig and APswap HFS partitions too.
        apconfig = MaStPartitionSnapshot("dk0", "APconfig", "hfs", 1024, uuid="11111111-1111-1111-1111-111111111111")
        data = MaStPartitionSnapshot("dk2", "Data", "hfs", 1903000, uuid=NEW_VOLUME_UUID)
        self.assertEqual(wipe_disk.data_volume(disk(partitions=(apconfig, data))), data)
        self.assertIsNone(wipe_disk.data_volume(disk(partitions=())))

    def test_the_dry_run_names_the_table_in_words(self) -> None:
        self.assertEqual(wipe_disk.partition_table_text(wipe.GptVerdict("apple_hfs", ("apple_hfs",))), "Apple HFS partitions.")
        self.assertEqual(wipe_disk.partition_table_text(wipe.GptVerdict("bogus_map")),
                         "AirPort Utility cannot replace this disk's partition table.")
        self.assertEqual(wipe_disk.partition_table_text(wipe.GptVerdict("unreadable")), "could not be read.")

    def test_the_disk_is_described_without_its_volumes(self) -> None:
        described = wipe_disk.describe_disk(disk(label="Disk: one"))
        self.assertEqual(described, "Disk: one (sd0, internal, 2.0 TB, WDC WD20EARS-42S0XB0)")


class DecimalSizeTests(unittest.TestCase):
    def test_sizes_read_the_way_disks_are_sold(self) -> None:
        self.assertEqual([wipe.decimal_size(mib) for mib in (3950, 1907729, 2861588, 8, 0)],
                         ["4 GB", "2.0 TB", "3.0 TB", "8 MB", "0 MB"])


class GeometryAndOffsetTests(unittest.TestCase):
    def test_drvctl_geometry_is_read_with_its_exact_text(self) -> None:
        geometry = wipe.parse_drvctl_geometry(drvctl_output("0x200", "0x1d1bfb800"))
        self.assertEqual(geometry, wipe.DiskGeometry(512, 7813969920, "0x200", "0x1d1bfb800"))

    def test_drvctl_without_a_geometry_is_none(self) -> None:
        for text in ("", "drvctl: DRVGETEVENT: Device not configured", drvctl_output("0x0", "0x10")):
            with self.subTest(text=text[:20]):
                self.assertIsNone(wipe.parse_drvctl_geometry(text))

    def test_offsets_cover_10_mib_at_each_end_in_blocks_that_divide_the_disk(self) -> None:
        cases = {
            # 2 TB WD20EARS: 3907029168 = 2^4 x odd sectors, so 8 KiB blocks.
            "2 TB": (wipe.DiskGeometry(512, 3907029168, "", ""), 8192),
            # The stock 3 TB Seagate: past 2^32 sectors.
            "3 TB": (wipe.DiskGeometry(512, 5860533168, "", ""), 8192),
            # The 4 TB USB disk on the LAN NetBSD 6 unit: a whole number of MiB.
            "4 TB": (wipe.DiskGeometry(512, 7813969920, "", ""), 1024 * 1024),
            "4096-byte sectors": (wipe.DiskGeometry(4096, 976754646, "", ""), 4096 * 2),
        }
        for name, (geometry, block) in cases.items():
            with self.subTest(name):
                offsets = wipe.wipe_offsets(geometry)
                self.assertEqual(offsets.block_size, block)
                self.assertEqual(offsets.head_count * block, wipe.WIPE_BYTES_PER_END)
                self.assertEqual(offsets.tail_count * block, wipe.WIPE_BYTES_PER_END)
                self.assertEqual((offsets.tail_seek + offsets.tail_count) * block, geometry.size_bytes)
                self.assertEqual(block % geometry.sector_size, 0)

    def test_other_sector_sizes_and_tiny_disks_are_refused(self) -> None:
        for geometry in (wipe.DiskGeometry(520, 1 << 30, "", ""), wipe.DiskGeometry(512, 1000, "", "")):
            with self.subTest(geometry=geometry):
                with self.assertRaises(ValueError):
                    wipe.wipe_offsets(geometry)


class WipeScriptTests(unittest.TestCase):
    """The script runs in sh with fakes for drvctl and dd, so the target
    check, the record counts and the report are the real ones."""

    def run_script(self, *, drvctl: str, dd_records: str | None = None, dd_exit: int = 0) -> str:
        offsets = wipe.wipe_offsets(TWO_TB)
        script = wipe.render_wipe_script("sd0", 2, TWO_TB, offsets)
        with tempfile.TemporaryDirectory() as tmp:
            fake_drvctl = Path(tmp) / "drvctl"
            fake_drvctl.write_text(f"#!/bin/sh\ncat <<'EOF'\n{drvctl}EOF\n")
            fake_dd = Path(tmp) / "dd"
            log = Path(tmp) / "dd.log"
            records = dd_records or '"$count"'
            fake_dd.write_text(
                "#!/bin/sh\n"
                f'echo "$*" >> {log}\n'
                'for arg; do case "$arg" in count=*) count=${arg#count=} ;; esac; done\n'
                f'echo "{records}+0 records in" >&2; echo "{records}+0 records out" >&2\n'
                f"exit {dd_exit}\n"
            )
            for fake in (fake_drvctl, fake_dd):
                fake.chmod(0o755)
            script = script.replace("/sbin/drvctl", str(fake_drvctl)).replace("/bin/dd", str(fake_dd))
            proc = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True)
            self.dd_calls = log.read_text().splitlines() if log.exists() else []
            return proc.stdout

    def test_both_ends_are_zeroed_on_the_raw_whole_disk_device(self) -> None:
        output = self.run_script(drvctl=drvctl_output("0x200", "0xe8e088b0"))
        offsets = wipe.wipe_offsets(TWO_TB)
        self.assertTrue(wipe.check_wipe_output(output, offsets).complete)
        self.assertEqual(self.dd_calls, [
            f"if=/dev/zero of=/dev/rsd0c bs=8192 count={offsets.head_count}",
            f"if=/dev/zero of=/dev/rsd0c bs=8192 seek={offsets.tail_seek} count={offsets.tail_count}",
        ])

    def test_a_different_disk_under_the_name_stops_before_any_write(self) -> None:
        output = self.run_script(drvctl=drvctl_output("0x200", "0x1d1bfb800"))
        self.assertEqual(wipe.check_wipe_output(output, wipe.wipe_offsets(TWO_TB)).reason, "disk_changed")
        self.assertEqual(self.dd_calls, [])

    def test_an_unreadable_geometry_counts_as_a_changed_disk(self) -> None:
        output = self.run_script(drvctl="")
        self.assertEqual(wipe.check_wipe_output(output, wipe.wipe_offsets(TWO_TB)).reason, "disk_changed")
        self.assertEqual(self.dd_calls, [])

    def test_dd_that_writes_fewer_records_with_exit_0_is_a_short_write(self) -> None:
        # NetBSD 6's dd writes nothing past the end and still exits 0.
        output = self.run_script(drvctl=drvctl_output("0x200", "0xe8e088b0"), dd_records="0")
        self.assertEqual(wipe.check_wipe_output(output, wipe.wipe_offsets(TWO_TB)).reason, "short_write")

    def test_a_failing_dd_stops_after_the_first_end(self) -> None:
        output = self.run_script(drvctl=drvctl_output("0x200", "0xe8e088b0"), dd_exit=1)
        self.assertEqual(wipe.check_wipe_output(output, wipe.wipe_offsets(TWO_TB)).reason, "write_failed")
        self.assertEqual(len(self.dd_calls), 1)

    def test_no_report_at_all_is_no_response(self) -> None:
        self.assertEqual(wipe.check_wipe_output("", wipe.wipe_offsets(TWO_TB)).reason, "no_response")


class ReadbackTests(unittest.TestCase):
    def test_only_three_full_sectors_of_zeros_pass(self) -> None:
        sector = TWO_TB.sector_size
        cases = {
            "zeros": (bytes(3 * sector), True),
            "nothing (dd past the end)": (b"", False),
            "two sectors": (bytes(2 * sector), False),
            "GPT signature left": (bytes(sector) + b"EFI PART" + bytes(2 * sector - 8), False),
            "MBR signature left": (bytes(510) + b"\x55\xaa" + bytes(2 * sector), False),
        }
        for name, (data, blank) in cases.items():
            with self.subTest(name):
                self.assertEqual(wipe.readback_is_blank(data, TWO_TB), blank)

    def test_the_read_back_reads_sectors_0_1_and_the_last(self) -> None:
        # Run against a fake dd that logs its arguments and answers with a
        # marker per read: the Mac gets both reads, in order, as one stream.
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "log"
            fake_dd = Path(tmp) / "dd"
            fake_dd.write_text(
                f'#!/bin/sh\necho "$*" >> {shlex.quote(str(log))}\n'
                'case "$*" in *skip=*) printf L ;; *) printf HH ;; esac\n'
            )
            fake_dd.chmod(0o755)
            command = wipe.readback_command("sd0", 2, TWO_TB).replace("/bin/dd", str(fake_dd))
            proc = subprocess.run(["/bin/sh", "-c", command], capture_output=True)
            calls = log.read_text().splitlines()
        self.assertEqual(proc.stdout, b"HHL")
        self.assertEqual(calls, ["if=/dev/rsd0c bs=512 count=2", "if=/dev/rsd0c bs=512 skip=3907029167 count=1"])


class DiskJobTests(unittest.TestCase):
    PS = """\
    1     0 Is   init
  339     1 Ss   diskd
  400   339 S    sh
  401   400 D    newfs_hfs
  402   339 S    umount
  500   777 S    sh
  501   500 D    gpt
  600   339 Z    fsck_hfs
  777     1 Ss   sshd
"""

    def test_only_diskds_children_and_grandchildren_count(self) -> None:
        # Our own `gpt show` (child of our sh) never looks like an erase, and a
        # zombie is not running.
        self.assertEqual(wipe.disk_jobs_in(self.PS), ("newfs_hfs", "umount"))

    def test_an_idle_device_has_no_jobs(self) -> None:
        self.assertEqual(wipe.disk_jobs_in("  339     1 Ss   diskd\n  777     1 Ss   sshd\n"), ())


class GptShowTests(unittest.TestCase):
    NOISE = "unable to open device 'rsd0c': Undefined error: 0\n"

    def test_tables_are_named_by_what_they_hold(self) -> None:
        cases = {
            "Apple layout": (
                "   start    size  index  contents\n       0       1         PMBR\n"
                "      40 2097152      1  GPT part - Apple HFS\n" + self.NOISE, "apple_hfs", ("apple_hfs",)),
            "bogus map": ("gpt: error: bogus map\n" + self.NOISE, "bogus_map", ()),
            "MBR beside a GPT": ("gpt: sd0: error: no room for the backup header\n", "bogus_map", ()),
            "APFS": ("   40  409600      1  GPT part - c12a7328-f81f-11d2-ba4b-00a0c93ec93b\n"
                     "  409640 9000000  2  GPT part - 7c3457ef-0000-11aa-aa11-00306543ecac\n", "foreign", ("efi", "apfs")),
            "Linux": ("    2048  126976      1  GPT part - 0fc63daf-8483-4772-8e79-3d69d8477de4\n", "foreign", ("linux",)),
            "Windows basic data": ("      34  262144      1  GPT part - Linux/Windows\n", "foreign", ("basic_data",)),
            "MBR only, FAT32": ("       0       1         MBR\n      63  999999      1  MBR part 12\n", "foreign", ("fat32",)),
            "blank": ("   start    size  index  contents\n       0  131072         \ngpt: : error: device doesn't contain a GPT\n",
                      "blank", ()),
            "nothing": ("", "unreadable", ()),
        }
        for name, (output, kind, formats) in cases.items():
            with self.subTest(name):
                self.assertEqual(wipe.classify_gpt_show(output), wipe.GptVerdict(kind, formats))

    def test_the_description_names_formats_users_know(self) -> None:
        self.assertEqual(wipe.gpt_verdict_description(wipe.GptVerdict("foreign", ("efi", "apfs"))),
                         "This disk is formatted as APFS.")
        self.assertEqual(wipe.gpt_verdict_description(wipe.GptVerdict("bogus_map")),
                         "AirPort Utility cannot replace this disk's partition table.")
        self.assertIsNone(wipe.gpt_verdict_description(wipe.GptVerdict("unreadable")))

    def test_a_hung_gpt_show_is_killed_on_the_device(self) -> None:
        command = wipe.gpt_show_command("wd0")
        self.assertIn(f"sleep {wipe.GPT_SHOW_KILL_SECONDS}; kill $gpt_pid", command)
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "gpt"
            fake.write_text("#!/bin/sh\necho \"$@\"\n")
            fake.chmod(0o755)
            proc = subprocess.run(["/bin/sh", "-c", command.replace("/sbin/gpt", str(fake))],
                                  capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.stdout.strip(), "show wd0")


class VolumeNameTests(unittest.TestCase):
    def test_names_newfs_hfs_can_take(self) -> None:
        for name in ("Data", "AirMacディスク", "Time Machine SV", "x" * 255):
            with self.subTest(name=name):
                self.assertTrue(wipe.volume_name_allowed(name))
        for name in ('Say "hi"', "a`b", "$HOME", "back\\slash", "a:b", "line\nbreak", "", " lead", "trail ", "x" * 256):
            with self.subTest(name=name):
                self.assertFalse(wipe.volume_name_allowed(name))

    def test_the_old_name_is_kept_composed_else_data(self) -> None:
        decomposed = unicodedata.normalize("NFD", "AirMacディスク")
        self.assertEqual(wipe.erase_volume_name((decomposed,)), "AirMacディスク")
        self.assertEqual(wipe.erase_volume_name(('Say "hi"', "Backups")), "Backups")
        self.assertEqual(wipe.erase_volume_name(("a:b",)), "Data")
        self.assertEqual(wipe.erase_volume_name(()), "Data")


class PayloadHomeProbeTests(unittest.TestCase):
    def test_only_mounted_volumes_are_looked_at(self) -> None:
        command = wipe.payload_home_probe_command(("/Volumes/dk2", "/Volumes/dk4"), ".samba4")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dk2" / ".samba4").mkdir(parents=True)
            fake_mount = root / "mount"
            fake_mount.write_text(f"#!/bin/sh\necho '/dev/dk2 on {root}/dk2 type hfs (local)'\n")
            fake_mount.chmod(0o755)
            script = command.replace("/sbin/mount", str(fake_mount)).replace("/Volumes", str(root))
            proc = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True)
        self.assertEqual(wipe.parse_payload_home_probe(proc.stdout), {f"{root}/dk2": "yes", f"{root}/dk4": "unmounted"})
        # Nothing claims or mounts a volume.
        self.assertNotIn("useVolume", command)
        self.assertNotIn("mount_hfs", command)


class FakeDevice:
    """MaSt, diskd's jobs and RPC answers, the wipe script and the read-back,
    as one device the flow talks to. Each list is consumed in order; the last
    entry repeats."""

    def __init__(self) -> None:
        self.before = (disk(),)
        self.after = (disk(partitions=(MaStPartitionSnapshot("dk2", "Data", "hfs", uuid=NEW_VOLUME_UUID),)),)
        self.disks_reads: list[tuple[MaStDiskSnapshot, ...]] = []
        self.jobs: list[tuple[str, ...] | None] = [()]
        self.wait_jobs: list[tuple[str, ...] | None] = [()]
        self.probe = DiskdRpcReply(0, -6705, None)
        self.erase = DiskdRpcReply(0, 0, None)
        self.wipe_output: str | None = None
        self.readback = bytes(3 * TWO_TB.sector_size)
        self.mast_after_erase: list[object] = []
        self.calls: list[str] = []
        self.erased = False
        self.now = 0.0
        # disks_with_apple_labels' answer per check (the last repeats), and
        # what it was asked.
        self.labelled: list[dict[str, str]] = [{}]
        self.label_checks: list[tuple[tuple[str, ...], str, int]] = []

    def plan(self, **changes: object) -> wipe_disk.WipePlan:
        offsets = wipe.wipe_offsets(TWO_TB)
        base = wipe_disk.WipePlan(disk(), TWO_TB, offsets, 2, "Data", wipe.GptVerdict("apple_hfs", ("apple_hfs",)), False)
        return replace(base, **changes)

    def _next(self, values: list):
        return values.pop(0) if len(values) > 1 else values[0]

    def apple_labels(self, _connection, disks, target, raw_partition):
        self.calls.append("labels")
        self.label_checks.append((tuple(item.device for item in disks), target.device, raw_partition))
        others = tuple(item.device for item in disks if item.device != target.device)
        return wipe_disk.OtherDisks(others, self._next(self.labelled))

    def read_disks(self, _connection):
        self.calls.append("read_disks")
        return self.after if self.erased else (self._next(self.disks_reads) if self.disks_reads else self.before)

    def disk_jobs(self, _connection):
        self.calls.append("jobs")
        return self._next(self.wait_jobs if self.erased else self.jobs)

    def rpc(self, _connection, name, *arguments, timeout=30):
        method = arguments[-1]
        self.calls.append(f"rpc {name} {method}")
        if method == "method:i:9":
            return self.probe
        reply = self.erase
        if isinstance(reply, BaseException):
            self.erased = getattr(self, "erased_on_timeout", False)
            raise reply
        self.erased = reply.status == 0
        return reply

    def run_ssh(self, _connection, command, **_kwargs):
        script = shlex.split(command)[2] if command.startswith("/bin/sh -c ") else command
        if "if=/dev/zero" in script:
            self.calls.append("wipe")
            if self.wipe_output is None:
                offsets = wipe.wipe_offsets(TWO_TB)
                self.wipe_output = "".join(
                    f"{wipe.WIPE_LINE_PREFIX}{end}: {count}+0 records out\n{wipe.WIPE_LINE_PREFIX}{end} exit 0\n"
                    for end, count in (("head", offsets.head_count), ("tail", offsets.tail_count))
                ) + wipe.WIPE_DONE_LINE + "\n"
            if isinstance(self.wipe_output, BaseException):
                raise self.wipe_output
            return subprocess.CompletedProcess(command, 0, self.wipe_output, "")
        if script.startswith("/usr/bin/acp MaSt"):
            self.calls.append("mast")
            if self.mast_after_erase:
                answer = self._next(self.mast_after_erase)
                if isinstance(answer, BaseException):
                    raise answer
                return subprocess.CompletedProcess(command, 0, answer, "")
            return subprocess.CompletedProcess(command, 0, mast_xml(self.after), "")
        raise AssertionError(f"unexpected command {command}")

    def capture(self, _connection, command, **_kwargs):
        self.calls.append("readback")
        return self.readback

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def reboot(self, connection, *, wait, netbsd4_autostart, callbacks, after_reboot=None):
        self.calls.append(f"reboot autostart={netbsd4_autostart}")
        if after_reboot is not None:
            after_reboot(connection)
        return SharingRestart(connection)

    def patches(self):
        return (
            mock.patch.object(wipe_disk, "read_disks_conn", side_effect=self.read_disks),
            mock.patch.object(wipe_disk, "disk_jobs_conn", side_effect=self.disk_jobs),
            mock.patch.object(wipe_disk, "diskd_rpc_conn", side_effect=self.rpc),
            mock.patch.object(wipe_disk, "run_ssh", side_effect=self.run_ssh),
            mock.patch.object(wipe_disk, "run_ssh_capture_bytes", side_effect=self.capture),
            mock.patch.object(wipe_disk, "reboot_and_restart_sharing", side_effect=self.reboot),
            mock.patch.object(wipe_disk, "disks_with_apple_labels", side_effect=self.apple_labels),
        )

    def run(self, plan: wipe_disk.WipePlan | None = None, *, autostart: bool | None = False,
            callbacks: OperationCallbacks | None = None) -> wipe_disk.WipeOutcome:
        patches = self.patches()
        for patch in patches:
            patch.start()
        try:
            return wipe_disk.run_wipe(
                CONNECTION, plan or self.plan(), netbsd4_autostart=autostart, callbacks=callbacks or OperationCallbacks(),
                clock=self.clock, sleep=self.sleep,
            )
        finally:
            for patch in patches:
                patch.stop()


def mast_xml(disks: tuple[MaStDiskSnapshot, ...]) -> str:
    import plistlib
    import uuid as uuidlib
    return plistlib.dumps([{
        "deviceName": item.device, "builtin": item.builtin, "size": item.size_mib or 0, "blockSize": 512,
        "uuid": uuidlib.UUID(item.uuid).bytes,
        "partitions": [{"deviceName": p.device, "name": p.name, "format": p.format, "uuid": uuidlib.UUID(p.uuid).bytes}
                       for p in item.partitions],
        **({"softDisconnected": item.soft_disconnected} if item.soft_disconnected is not None else {}),
    } for item in disks]).decode()


class RunWipeTests(unittest.TestCase):
    def test_a_wipe_probes_after_the_identity_check_then_wipes_reads_back_erases_and_reboots(self) -> None:
        device = FakeDevice()
        outcome = device.run()
        self.assertEqual(outcome.new_volume_uuid, NEW_VOLUME_UUID)
        order = [call for call in device.calls if call in (
            "read_disks", "rpc eraseDisk method:i:9", "wipe", "readback", "rpc eraseDisk method:i:0")
            or call.startswith("reboot")]
        self.assertEqual(order[:5], ["read_disks", "rpc eraseDisk method:i:9", "wipe", "readback", "rpc eraseDisk method:i:0"])
        # The other disks are checked once after the confirmation, before
        # anything is written, and not again (a later refusal would leave the
        # internal disk cleared).
        self.assertEqual(len(device.label_checks), 1)
        self.assertLess(device.calls.index("labels"), device.calls.index("wipe"))
        # The disk held no Samba files and another one does: sharing restarts
        # as fsck restarts it, and the new volume is checked after the reboot.
        self.assertIn("reboot autostart=False", device.calls)
        self.assertEqual(device.calls[-1], "read_disks")

    def test_after_wiping_the_samba_disk_nothing_is_started_and_install_comes_next(self) -> None:
        device = FakeDevice()
        outcome = device.run(device.plan(offer_install=True), autostart=False)
        self.assertTrue(outcome.offer_install)
        self.assertIn("reboot autostart=None", device.calls)

    def test_a_disk_with_apple_labels_plugged_in_since_the_confirmation_refuses_the_wipe(self) -> None:
        device = FakeDevice()
        device.before = (disk(), disk(device="sd1", builtin=False, uuid=USB_UUID))
        device.labelled = [{"sd1": "apple_labels"}]
        with self.assertRaises(wipe_disk.WipeDiskError) as raised:
            device.run()
        self.assertEqual((raised.exception.code, raised.exception.reason), (ErrorCode.WIPE_DISK_OTHER_DISK, "other_disk"))
        self.assertEqual(raised.exception.detail, "sd1: apple_labels")
        self.assertEqual(device.label_checks, [(("sd0", "sd1"), "sd0", 2)])
        self.assertNotIn("rpc eraseDisk method:i:9", device.calls)
        self.assertNotIn("wipe", device.calls)

    def test_a_usb_disk_without_apple_labels_does_not_stop_the_wipe(self) -> None:
        # Its wedges carry other names, so the erase's own wedges are created
        # under Apple's names and the kernel returns the internal disk's.
        device = FakeDevice()
        device.before = (disk(), disk(device="sd1", builtin=False, uuid=USB_UUID))
        outcome = device.run()
        self.assertEqual(outcome.new_volume_uuid, NEW_VOLUME_UUID)
        # Checked after the confirmation, before anything is asked of diskd.
        self.assertLess(device.calls.index("labels"), device.calls.index("rpc eraseDisk method:i:9"))

    def test_a_disk_that_changed_since_the_confirmation_is_left_alone(self) -> None:
        device = FakeDevice()
        device.before = (disk(uuid="00000000-0000-0000-0000-000000000009"),)
        with self.assertRaises(wipe_disk.WipeDiskError) as raised:
            device.run()
        self.assertEqual((raised.exception.code, raised.exception.reason), (ErrorCode.WIPE_DISK_CHANGED, "identity_changed"))
        self.assertNotIn("rpc eraseDisk method:i:9", device.calls)

    def test_a_running_erase_stops_before_anything_is_stopped(self) -> None:
        # diskd's own jobs (on any disk: they name none), or diskd saying it
        # is busy (a secure erase from AirPort Utility, which can take hours,
        # has its own code).
        cases = (
            (("newfs_hfs",), DiskdRpcReply(0, -6705, None), ErrorCode.WIPE_DISK_JOB_RUNNING),
            ((), DiskdRpcReply(0, -6721, None), ErrorCode.WIPE_DISK_BUSY),
        )
        for jobs, probe, code in cases:
            with self.subTest(jobs=jobs, status=probe.status):
                device = FakeDevice()
                device.jobs, device.probe = [jobs], probe
                with self.assertRaises(wipe_disk.WipeDiskError) as raised:
                    device.run()
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn("wipe", device.calls)

    def test_a_probe_that_started_an_erase_is_reported_as_not_accepted(self) -> None:
        # A diskd that skipped the method check would answer 0; nothing of
        # ours has been stopped or written, and the guidance offers AirPort
        # Utility's own erase as the way out.
        from timecapsulesmb.app.recovery import recovery_for
        device = FakeDevice()
        device.probe = DiskdRpcReply(0, 0, None)
        with self.assertRaises(wipe_disk.WipeDiskError) as raised:
            device.run()
        self.assertEqual((raised.exception.code, raised.exception.reason),
                         (ErrorCode.WIPE_DISK_RESTART_NEEDED, "probe_unexpected"))
        self.assertNotIn("wipe", device.calls)
        steps = recovery_for("wipe-disk", ErrorCode.WIPE_DISK_RESTART_NEEDED)["actions"]
        self.assertEqual(steps[-1], "If it still does not start, try “Erase Disk” in AirPort Utility instead.")

    def test_an_unanswered_erase_with_acpd_silent_asks_for_an_unplug_and_requests_no_reboot(self) -> None:
        from timecapsulesmb.app.recovery import recovery_for
        from timecapsulesmb.device.storage import StorageDeviceError
        for jobs, still in (((), False), (("newfs_hfs",), True)):
            with self.subTest(jobs=jobs):
                device = FakeDevice()
                device.erase = SshCommandTimeout("acp rpc")
                device.wait_jobs = [jobs]
                silent = StorageDeviceError("Apple ACPd is broken. Its disks cannot be read.", code=ErrorCode.ACPD_BROKEN)
                with mock.patch.object(wipe_disk, "raise_if_acpd_silent", side_effect=silent):
                    device.erased_on_timeout = bool(jobs)
                    with self.assertRaises(wipe_disk.WipeDiskError) as raised:
                        device.run()
                self.assertEqual((raised.exception.code, raised.exception.reason), (ErrorCode.ACPD_BROKEN, "acpd_silent"))
                self.assertIn("partition table was already cleared", str(raised.exception))
                self.assertEqual("still formatting" in str(raised.exception), still)
                self.assertFalse(any(call.startswith("reboot") for call in device.calls))
        recovery = recovery_for("wipe-disk", ErrorCode.ACPD_BROKEN, stage="erase_disk")
        self.assertEqual(recovery["actions"][0], "If the device is still formatting the disk, wait until it finishes.")

    def test_an_unanswered_erase_with_acpd_answering_waits_for_diskd(self) -> None:
        device = FakeDevice()
        device.erase = SshCommandTimeout("acp rpc")
        device.erased_on_timeout = True
        with mock.patch.object(wipe_disk, "raise_if_acpd_silent", return_value=None):
            outcome = device.run()
        self.assertEqual(outcome.new_volume_uuid, NEW_VOLUME_UUID)

    def test_an_unregistered_erase_rpc_needs_a_restart_and_changes_nothing(self) -> None:
        device = FakeDevice()
        device.probe = DiskdRpcReply(22, None, "-6727")
        with self.assertRaises(wipe_disk.WipeDiskError) as raised:
            device.run()
        self.assertEqual((raised.exception.code, raised.exception.reason), (ErrorCode.WIPE_DISK_RESTART_NEEDED, "rpc_missing"))
        self.assertNotIn("wipe", device.calls)

    def test_a_disk_that_does_not_take_the_zeros_is_failing_and_is_not_erased(self) -> None:
        from timecapsulesmb.app.recovery import recovery_for
        cases = {
            "short write": ("tcapsule-wipe: head: 0+0 records out\ntcapsule-wipe: head exit 0\n", None, "short_write"),
            "timeout": (SshCommandTimeout("dd"), None, "no_response"),
            "read-back": (None, bytes(512) + b"EFI PART" + bytes(1016), "readback"),
        }
        for name, (output, readback, reason) in cases.items():
            with self.subTest(name):
                device = FakeDevice()
                device.wipe_output = output
                if readback is not None:
                    device.readback = readback
                stages: list[str] = []
                with self.assertRaises(wipe_disk.WipeDiskError) as raised:
                    device.run(callbacks=OperationCallbacks(set_stage=stages.append))
                self.assertEqual((raised.exception.code, raised.exception.reason), (ErrorCode.DISK_FAILING, reason))
                self.assertNotIn("rpc eraseDisk method:i:0", device.calls)
                # Nothing restarts the device here: the error's guidance does.
                # The guidance for the stage it failed in says the partition
                # table may be gone and file sharing is off until a restart,
                # not the general failing-disk card's "copy off your files".
                self.assertFalse(any(call.startswith("reboot") for call in device.calls))
                card = recovery_for("wipe-disk", raised.exception.code, stage=stages[-1])
                self.assertEqual(card["localization_key"], "wipe-disk.disk_failing.during_wipe")
                self.assertEqual(card["actions"][0], "Restart the device.")
                self.assertFalse(card["retryable"])

    def test_a_stop_that_failed_wrote_nothing_and_needs_a_restart(self) -> None:
        device = FakeDevice()
        device.wipe_output = "process smbd did not stop\n"
        with self.assertRaises(wipe_disk.WipeDiskError) as raised:
            device.run()
        self.assertEqual((raised.exception.code, raised.exception.reason), (ErrorCode.WIPE_DISK_RESTART_NEEDED, "stop_failed"))

    def test_an_erase_of_a_disk_diskd_lost_reboots_then_reports_it(self) -> None:
        device = FakeDevice()
        device.erase = DiskdRpcReply(0, -6727, None)
        with self.assertRaises(wipe_disk.WipeDiskError) as raised:
            device.run()
        self.assertEqual((raised.exception.code, raised.exception.reason), (ErrorCode.WIPE_DISK_ERASE_FAILED, "unknown_disk"))
        # afpserver is stopped, which only a reboot may follow; nothing starts.
        self.assertEqual(device.calls[-1], "reboot autostart=None")

    def test_the_wait_keeps_going_while_diskd_works_even_when_mast_does_not_answer(self) -> None:
        device = FakeDevice()
        # A slow teardown unmount, then the format, for well over the idle
        # minute, while MaSt reads time out; then the new volume appears.
        device.wait_jobs = [("umount",)] * 30 + [("newfs_hfs",)] * 30 + [()]
        device.mast_after_erase = [SshCommandTimeout("acp MaSt")] * 60 + [mast_xml(device.after)]
        outcome = device.run()
        self.assertEqual(outcome.new_volume_uuid, NEW_VOLUME_UUID)
        self.assertGreater(device.now, wipe_disk.ERASE_IDLE_SECONDS)

    def test_an_old_volume_uuid_under_a_reused_name_does_not_end_the_wait(self) -> None:
        device = FakeDevice()
        device.mast_after_erase = [mast_xml(device.before)] * 5 + [mast_xml(device.after)]
        device.wait_jobs = [("gpt",)] * 5 + [()]
        self.assertEqual(device.run().new_volume_uuid, NEW_VOLUME_UUID)

    def test_an_erase_that_stops_working_without_a_volume_fails_after_a_minute_and_reboots(self) -> None:
        device = FakeDevice()
        device.mast_after_erase = [mast_xml(device.before)]
        with self.assertRaises(wipe_disk.WipeDiskError) as raised:
            device.run()
        self.assertEqual((raised.exception.code, raised.exception.reason), (ErrorCode.WIPE_DISK_ERASE_FAILED, "erase_idle"))
        self.assertGreaterEqual(device.now, wipe_disk.ERASE_IDLE_SECONDS)
        self.assertLess(device.now, wipe_disk.ERASE_IDLE_SECONDS + 2 * wipe_disk.ERASE_POLL_SECONDS)
        self.assertEqual(device.calls[-1], "reboot autostart=None")

    def test_a_format_still_running_at_the_cap_is_never_rebooted(self) -> None:
        device = FakeDevice()
        device.mast_after_erase = [mast_xml(device.before)]
        device.wait_jobs = [("newfs_hfs",)]
        with self.assertRaises(wipe_disk.WipeDiskError) as raised:
            device.run()
        self.assertEqual((raised.exception.code, raised.exception.reason), (ErrorCode.WIPE_DISK_ERASE_RUNNING, "still_formatting"))
        self.assertGreaterEqual(device.now, wipe_disk.ERASE_CAP_SECONDS)
        self.assertFalse(any(call.startswith("reboot") for call in device.calls))

    def test_a_reboot_that_fails_after_a_failed_erase_keeps_why_the_erase_failed(self) -> None:
        device = FakeDevice()
        device.mast_after_erase = [mast_xml(device.before)]

        def reboot(connection, **kwargs):
            device.calls.append("reboot")
            raise RebootFlowError("ACP reboot request failed: timed out", code=ErrorCode.REBOOT_NOT_STARTED)

        device.reboot = reboot
        fields: dict[str, object] = {}
        with self.assertRaises(RebootFlowError) as raised:
            device.run(callbacks=OperationCallbacks(update_fields=fields.update))
        self.assertEqual(device.calls[-1], "reboot")
        # The reboot's error is what is raised; telemetry still says why the
        # wipe needed it, and the text keeps the wipe's own message.
        self.assertEqual(raised.exception.code, ErrorCode.REBOOT_NOT_STARTED)
        self.assertEqual(fields["wipe_reason"], "erase_idle")
        self.assertIn(wipe_disk.ERASE_FAILED_MESSAGE, str(raised.exception))

    def test_a_volume_gone_after_the_reboot_did_not_persist(self) -> None:
        device = FakeDevice()

        def reboot(connection, **kwargs):
            device.after = (disk(partitions=()),)
            return FakeDevice.reboot(device, connection, **kwargs)

        patches = device.patches()
        for patch in patches:
            patch.start()
        try:
            with mock.patch.object(wipe_disk, "reboot_and_restart_sharing", side_effect=reboot):
                with self.assertRaises(wipe_disk.WipeDiskError) as raised:
                    wipe_disk.run_wipe(CONNECTION, device.plan(), netbsd4_autostart=None, callbacks=OperationCallbacks(),
                                       clock=device.clock, sleep=device.sleep)
        finally:
            for patch in patches:
                patch.stop()
        self.assertEqual((raised.exception.code, raised.exception.reason), (ErrorCode.WIPE_DISK_ERASE_FAILED, "not_persisted"))


class PrepareWipeTests(unittest.TestCase):
    def prepare(self, target: MaStDiskSnapshot, *, drvctl: str | None = None, payload: str = "", jobs=(), others=(),
                labelled=None):
        commands: list[str] = []
        # Kept even when prepare_wipe raises, to show what it ran first.
        self.commands = commands
        raw = mast_xml((target, *others))

        def run_ssh(_connection, command, **_kwargs):
            commands.append(command)
            if "drvctl" in command:
                return subprocess.CompletedProcess(command, 0, drvctl if drvctl is not None else drvctl_output("0x200", "0xe8e088b0"), "")
            if "kern.rawpartition" in command:
                return subprocess.CompletedProcess(command, 0, "2\n", "")
            if "mounts=$(/sbin/mount)" in command:
                return subprocess.CompletedProcess(command, 0, payload, "")
            raise AssertionError(command)

        with (
            mock.patch.object(wipe_disk, "read_mast_volumes_with_output_conn", return_value=MaStReadResult((), raw)),
            mock.patch.object(wipe_disk, "disk_jobs_conn", return_value=jobs),
            mock.patch.object(wipe_disk, "run_ssh", side_effect=run_ssh),
            mock.patch.object(wipe_disk, "read_gpt_verdict_conn", return_value=wipe.GptVerdict("apple_hfs", ("apple_hfs",))),
            mock.patch.object(wipe_disk, "disks_with_apple_labels",
                              return_value=wipe_disk.OtherDisks(tuple(item.device for item in others), labelled or {}))
            as label_check,
        ):
            self.label_check = label_check
            self.callbacks = mock.MagicMock(wraps=OperationCallbacks())
            plan = wipe_disk.prepare_wipe(CONNECTION, callbacks=self.callbacks)
        self.commands = commands
        return plan

    def test_the_preflight_reads_only_and_claims_nothing(self) -> None:
        plan = self.prepare(disk(), payload="/Volumes/dk2 no\n")
        self.assertEqual((plan.geometry, plan.raw_partition, plan.volume_name), (TWO_TB, 2, "Data"))
        for command in self.commands:
            with self.subTest(command=command[:40]):
                self.assertNotIn("useVolume", command)
                self.assertNotIn("eraseDisk", command)
                self.assertNotIn("/bin/dd", command)

    def test_install_comes_next_unless_samba_lives_on_another_disk(self) -> None:
        self.assertTrue(self.prepare(disk(), payload="/Volumes/dk2 yes\n").offer_install)
        self.assertTrue(self.prepare(disk(), payload="/Volumes/dk2 unmounted\n").offer_install)
        # Nothing holds it at all: Install too.
        self.assertTrue(self.prepare(disk(), payload="/Volumes/dk2 no\n").offer_install)

    def test_refused_disks(self) -> None:
        cases = {
            "wrong size": (disk(size=3950, partitions=()), ErrorCode.DISK_SIZE_MISREPORTED),
            "soft-disconnected": (disk(soft_disconnected=True), ErrorCode.DISK_FAILING),
        }
        from timecapsulesmb.app.recovery import recovery_for
        for name, (target, code) in cases.items():
            with self.subTest(name):
                with self.assertRaises(wipe_disk.WipeDiskError) as raised:
                    self.prepare(target)
                self.assertEqual(raised.exception.code, code)
                # Refused before anything was written: the general card.
                stage = self.callbacks.stage.call_args.args[0]
                self.assertEqual(recovery_for("wipe-disk", code, stage=stage)["localization_key"], code.value)

    def test_only_the_internal_disk_is_wiped(self) -> None:
        # Listed after a USB disk, it is still the one chosen.
        usb = disk(device="sd1", builtin=False, label="Disk 2", uuid=USB_UUID)
        plan = self.prepare(usb, others=(disk(),))
        self.assertEqual(plan.disk.device, "sd0")
        # An AirPort Extreme has no internal disk to wipe.
        with self.assertRaises(wipe_disk.WipeDiskError) as raised:
            self.prepare(usb)
        self.assertEqual((raised.exception.code, raised.exception.reason), (ErrorCode.VALIDATION_FAILED, "no_internal_disk"))
        self.assertEqual(self.commands, [])

    def test_another_disk_with_apple_labels_refuses_the_wipe(self) -> None:
        # A USB disk with APconfig/APswap/APdata labels can hold those wedge
        # names and dk0 to dk2; the erase would then format it.
        usb = disk(device="sd1", builtin=False, label="Disk 2", uuid=USB_UUID)
        with self.assertRaises(wipe_disk.WipeDiskError) as raised:
            self.prepare(disk(), others=(usb,), labelled={"sd1": "apple_labels"})
        error = raised.exception
        self.assertEqual((error.code, error.reason, error.detail),
                         (ErrorCode.WIPE_DISK_OTHER_DISK, "other_disk", "sd1: apple_labels"))
        disks, target, raw_partition = self.label_check.call_args.args[1:]
        self.assertEqual(([item.device for item in disks], target.device, raw_partition), (["sd0", "sd1"], "sd0", 2))
        # Telemetry keeps the verdicts and how many other disks were read.
        self.callbacks.update.assert_any_call(wipe_other_disks={"sd1": "apple_labels"}, wipe_other_disks_checked=1)
        # The internal disk's own problem says more than the other disk does,
        # and nothing else is read for it.
        with self.assertRaises(wipe_disk.WipeDiskError) as raised:
            self.prepare(disk(size=3950, partitions=()), others=(usb,), labelled={"sd1": "apple_labels"})
        self.assertEqual(raised.exception.code, ErrorCode.DISK_SIZE_MISREPORTED)
        self.label_check.assert_not_called()

    def test_an_unreadable_geometry_needs_a_restart(self) -> None:
        with self.assertRaises(wipe_disk.WipeDiskError) as raised:
            self.prepare(disk(), drvctl="")
        self.assertEqual((raised.exception.code, raised.exception.reason),
                         (ErrorCode.WIPE_DISK_RESTART_NEEDED, "geometry_unreadable"))

    def test_a_running_disk_job_refuses_the_wipe(self) -> None:
        # A job, or a process list that could not be read. diskd's jobs name
        # no disk, so the message claims none: a check of another disk's
        # volume after a boot refuses too.
        from timecapsulesmb.app.recovery import recovery_for
        for jobs in (("fsck_hfs",), None):
            with self.subTest(jobs=jobs):
                with self.assertRaises(wipe_disk.WipeDiskError) as raised:
                    self.prepare(disk(), jobs=jobs)
                error = raised.exception
                self.assertEqual((error.code, error.reason), (ErrorCode.WIPE_DISK_JOB_RUNNING, "disk_job_running"))
                self.assertTrue(str(error).startswith("The device is formatting a disk, or checking or mounting a volume."))
                self.assertEqual(error.detail, "running: fsck_hfs" if jobs else "the process list could not be read")
                recovery = recovery_for("wipe-disk", error.code)
                self.assertNotIn("this disk", recovery["message"])
                self.assertEqual(recovery["actions"], ["Wait until it finishes.", "Then run “Wipe Internal Disk” again."])


class ConfirmationTests(unittest.TestCase):
    def test_the_confirmation_id_ignores_what_changes_between_the_two_calls(self) -> None:
        device = FakeDevice()
        first = device.plan()
        # A backup wrote meanwhile, and gpt show printed something else.
        used = (MaStPartitionSnapshot("dk2", "Data", "hfs", 1905681, 9999, 1895682, users=3, uuid=OLD_VOLUME_UUID),)
        second = replace(first, disk=disk(partitions=used), gpt_verdict=wipe.GptVerdict("unreadable"))
        third = replace(first, disk=disk(uuid="00000000-0000-0000-0000-000000000009"))

        def confirmation_id(plan):
            return build_confirmation(
                operation="wipe-disk", params={}, title="", message="", action_title="", risk="destructive",
                summary="", context=plan.confirmation_context(), presentation_id="wipe-disk.confirm",
            ).confirmation_id

        self.assertEqual(confirmation_id(first), confirmation_id(second))
        # Another disk under the same name needs its own confirmation.
        self.assertNotEqual(confirmation_id(first), confirmation_id(third))

    def test_the_app_operation_takes_only_its_own_params(self) -> None:
        params = OPERATION_PARAMS["wipe-disk"]
        # Nothing to choose (the internal disk is the only target) and no
        # Plan step (the CLI's --dry-run prints one).
        self.assertFalse({"disk", "list_disks", "dry_run", "no_reboot", "no_wait", "mount_wait"} & params)


class DeployVerdictTests(unittest.TestCase):
    def error(self, disks: tuple[MaStDiskSnapshot, ...], verdict: wipe.GptVerdict) -> DeployDeviceError:
        with mock.patch("timecapsulesmb.services.deploy.read_gpt_verdict_conn", return_value=verdict):
            return mast_no_volume_error(MaStDiscoveryResult((), 20, mast_xml(disks)), connection=CONNECTION)

    def test_the_verdict_picks_the_code_whose_guidance_fits(self) -> None:
        blank = disk(partitions=())
        misreported = self.error((disk(size=3950, partitions=()),), wipe.GptVerdict("blank"))
        self.assertEqual(misreported.code, ErrorCode.DISK_SIZE_MISREPORTED)
        self.assertIn("only 4 GB", str(misreported))
        # An Apple HFS partition on disk only has not mounted: no Wipe button.
        self.assertEqual(self.error((blank,), wipe.GptVerdict("apple_hfs", ("apple_hfs",))).code, ErrorCode.DEPLOY_DISK_NOT_MOUNTED)
        foreign = self.error((blank,), wipe.GptVerdict("foreign", ("apfs",)))
        self.assertEqual(foreign.code, ErrorCode.DEPLOY_NO_HFS_PARTITION)
        self.assertIn("This disk is formatted as APFS.", str(foreign))
        self.assertEqual(self.error((blank,), wipe.GptVerdict("bogus_map")).code, ErrorCode.DEPLOY_NO_HFS_PARTITION)

    def test_without_ssh_the_old_code_stays(self) -> None:
        error = mast_no_volume_error(MaStDiscoveryResult((), 20, mast_xml((disk(partitions=()),))))
        self.assertEqual(error.code, ErrorCode.DEPLOY_NO_HFS_PARTITION)


class SharedMaintenanceTests(unittest.TestCase):
    def test_the_reboot_tail_checks_after_the_wait_and_before_the_start(self) -> None:
        events: list[str] = []
        with (
            mock.patch("timecapsulesmb.services.maintenance.reboot_device", side_effect=lambda *a, **k: events.append("reboot")),
            mock.patch("timecapsulesmb.services.maintenance.start_netbsd4_runtime_after_reboot",
                       side_effect=lambda *a, **k: events.append("start")),
        ):
            restart = reboot_and_restart_sharing(
                CONNECTION, wait=True, netbsd4_autostart=False, callbacks=OperationCallbacks(),
                after_reboot=lambda _connection: events.append("check"),
            )
        self.assertEqual(events, ["reboot", "check", "start"])
        self.assertIsNone(restart.error)

    def test_nothing_is_started_where_nothing_needs_starting(self) -> None:
        events: list[str] = []
        with (
            mock.patch("timecapsulesmb.services.maintenance.reboot_device", side_effect=lambda *a, **k: events.append("reboot")),
            mock.patch("timecapsulesmb.services.maintenance.start_netbsd4_runtime_after_reboot",
                       side_effect=lambda *a, **k: events.append("start")),
        ):
            reboot_and_restart_sharing(CONNECTION, wait=True, netbsd4_autostart=None, callbacks=OperationCallbacks())
            reboot_and_restart_sharing(CONNECTION, wait=False, netbsd4_autostart=False, callbacks=OperationCallbacks(),
                                       after_reboot=lambda _connection: events.append("check"))
        self.assertEqual(events, ["reboot", "reboot"])


class NoClaimTests(unittest.TestCase):
    def test_a_whole_wipe_never_claims_a_volume(self) -> None:
        commands: list[str] = []
        device = FakeDevice()
        real = device.run_ssh

        def recording(connection, command, **kwargs):
            commands.append(command)
            return real(connection, command, **kwargs)

        device.run_ssh = recording
        device.run()
        self.assertTrue(commands)
        self.assertFalse(any("useVolume" in command for command in commands))



class KernelDiskLineTests(unittest.TestCase):
    def test_the_kernel_filter_keeps_the_wedge_lines(self) -> None:
        from timecapsulesmb.device.probe import DISK_EVIDENCE_COMMANDS
        dmesg = (
            "NetBSD 6.0 (TC8)\n"
            "wd0: <APPLE HDD ST3000DM001, AQ52>\n"
            "dk2 at wd0: APdata\n"
            "dk0 at sd0 (primary) deleted\n"
            "ath0: beacon miss\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "dmesg"
            fake.write_text(f"#!/bin/sh\ncat <<'EOF'\n{dmesg}EOF\n")
            fake.chmod(0o755)
            command = DISK_EVIDENCE_COMMANDS["remote_kernel_disk_messages"].replace("/sbin/dmesg", str(fake))
            proc = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True)
        self.assertEqual(proc.stdout.splitlines(), [
            "NetBSD 6.0 (TC8)", "wd0: <APPLE HDD ST3000DM001, AQ52>", "dk2 at wd0: APdata", "dk0 at sd0 (primary) deleted",
        ])


class DoctorDiskVerdictTests(unittest.TestCase):
    def results(self, disks, verdict):
        from types import SimpleNamespace
        from timecapsulesmb.checks import doctor_steps
        added = []
        sink = SimpleNamespace(add=added.append)
        with mock.patch.object(doctor_steps, "read_gpt_verdict_conn", return_value=verdict):
            doctor_steps._doctor_check_disks_without_hfs(SimpleNamespace(connection=CONNECTION), disks, sink)
        return [(result.status, result.details["code"], result.message) for result in added]

    def test_the_wrong_size_disk_and_a_foreign_disk_fail_with_their_codes(self) -> None:
        from timecapsulesmb.checks.doctor_steps import DOCTOR_CODE_DISK_NO_HFS_PARTITION, DOCTOR_CODE_DISK_SIZE_MISREPORTED
        [(status, code, message)] = self.results((disk(size=3950, partitions=()),), wipe.GptVerdict("blank"))
        self.assertEqual((status, code), ("FAIL", DOCTOR_CODE_DISK_SIZE_MISREPORTED))
        self.assertIn("reports only 4 GB", message)
        [(status, code, message)] = self.results((disk(partitions=()),), wipe.GptVerdict("foreign", ("apfs",)))
        self.assertEqual((status, code), ("FAIL", DOCTOR_CODE_DISK_NO_HFS_PARTITION))
        self.assertIn("This disk is formatted as APFS.", message)
        self.assertIn("Wipe Internal Disk (`tcapsule wipe-internal-disk`) formats it", message)
        # A USB disk fails the same way, but the wipe never takes it.
        usb = disk(device="sd1", builtin=False, label="Disk 2", uuid=USB_UUID, partitions=())
        [(status, code, message)] = self.results((usb,), wipe.GptVerdict("foreign", ("apfs",)))
        self.assertEqual((status, code), ("FAIL", DOCTOR_CODE_DISK_NO_HFS_PARTITION))
        self.assertNotIn("Wipe Internal Disk", message)

    def test_an_apple_hfs_partition_that_has_not_mounted_is_not_offered_a_wipe(self) -> None:
        self.assertEqual(self.results((disk(partitions=()),), wipe.GptVerdict("apple_hfs", ("apple_hfs",))), [])


class WipeDiskOperationTests(unittest.TestCase):
    """The app's wipe-disk operation: Find Disks reads MaSt only, the first
    call stops at the confirmation, and the confirmed call wipes."""

    def call(self, params, *, evidence=None, error=None, plan_obj=None):
        from timecapsulesmb.app.ops import maintenance
        from timecapsulesmb.services.app import AppOperationError
        from timecapsulesmb.app.confirmations import AppConfirmationRequired
        context = mock.MagicMock()
        context.to_operation_callbacks.return_value = OperationCallbacks()
        target_plan = plan_obj or FakeDevice().plan(offer_install=True)
        with (
            mock.patch.object(maintenance, "load_request_config"),
            mock.patch.object(maintenance, "resolve_env_connection", return_value=CONNECTION),
            mock.patch.object(maintenance, "require_request_acpd_ready") as acpd_ready,
            mock.patch.object(maintenance, "prepare_wipe", return_value=target_plan) as prepare,
            mock.patch.object(maintenance, "wipe_failing_evidence", return_value=evidence) as failing_evidence,
            mock.patch.object(maintenance, "run_wipe", side_effect=error,
                              return_value=wipe_disk.WipeOutcome(NEW_VOLUME_UUID, "Data", True)) as run_wipe,
        ):
            try:
                outcome = maintenance.wipe_disk_operation(params, context)
            except (AppConfirmationRequired, AppOperationError) as exc:
                outcome = exc
        self.mocks = {"acpd_ready": acpd_ready, "prepare": prepare, "run_wipe": run_wipe, "context": context,
                      "failing_evidence": failing_evidence}
        return outcome

    def test_the_first_call_stops_at_a_destructive_confirmation_then_the_confirmed_call_wipes(self) -> None:
        from timecapsulesmb.app.confirmations import AppConfirmationRequired
        first = self.call({})
        self.assertIsInstance(first, AppConfirmationRequired)
        request = first.confirmation
        self.assertEqual((request.presentation_id, request.risk), ("wipe-disk.confirm", "destructive"))
        self.assertEqual(set(request.context), {"disk", "disk_uuid", "volume_name"})
        # Data for the app to word and format itself, never English text or
        # raw format codes in a sentence.
        values = request.presentation_values
        self.assertEqual(set(values), {"disk", "size_mib", "volumes", "gpt_verdict", "formats"})
        self.assertEqual((values["disk"], values["size_mib"]), ("Disk 1", 1907729))
        self.assertEqual(values["volumes"], [{"name": "Data", "used_mib": 8977}])
        self.assertEqual((values["gpt_verdict"], values["formats"]), ("apple_hfs", ["apple_hfs"]))
        self.mocks["run_wipe"].assert_not_called()
        second = self.call({"confirmation_id": request.confirmation_id})
        self.assertTrue(second.ok)
        self.assertEqual((second.payload["offer_install"], second.payload["summary_key"]), (True, "wipe_disk_completed"))
        self.assertEqual((second.payload["disk"], second.payload["disk_label"]), ("sd0", "Disk 1"))
        self.mocks["run_wipe"].assert_called_once()

    def test_a_failing_disk_asks_with_its_own_warning(self) -> None:
        from timecapsulesmb.device.disk_errors import kernel_evidence
        request = self.call({}, evidence=kernel_evidence(("sd0c: error reading fsbn 64",))).confirmation
        self.assertEqual((request.presentation_id, request.action_title), ("wipe-disk.disk_failing", "Wipe Anyway"))
        # The warning follows what the wipe deletes, and is about this disk.
        self.assertTrue(request.message.startswith("Wipe Internal Disk deletes everything on this disk"))
        self.assertIn("This disk reported errors it could not recover from (sd0c: error reading fsbn 64)", request.message)
        self.assertIn("Wiping it does not fix that", request.message)
        self.mocks["failing_evidence"].assert_called_with(CONNECTION, self.mocks["prepare"].return_value)

    def test_a_wipe_error_keeps_its_code_and_reason(self) -> None:
        error = wipe_disk.WipeDiskError("x", code=ErrorCode.WIPE_DISK_CHANGED, reason="target_changed")
        first = self.call({})
        raised = self.call({"confirmation_id": first.confirmation.confirmation_id}, error=error)
        self.assertEqual(raised.code, ErrorCode.WIPE_DISK_CHANGED)
        self.mocks["context"].update_fields.assert_any_call(wipe_reason="target_changed")


class FailingEvidenceTests(unittest.TestCase):
    """Wipe Internal Disk's failing-disk warning is about the disk it wipes."""

    def evidence(self, plan, *, errors=(), problems=()):
        from timecapsulesmb.device import disk_errors
        with (
            mock.patch.object(disk_errors, "read_disk_errors_conn", return_value=errors),
            mock.patch.object(disk_errors, "manager_payload_problems", return_value=problems) as payload_problems,
        ):
            evidence = wipe_disk.wipe_failing_evidence(CONNECTION, plan)
        self.payload_problems = payload_problems
        return evidence

    def test_only_the_internal_disks_kernel_lines_count(self) -> None:
        internal4 = FakeDevice().plan(disk=disk(device="sd0"))
        internal6 = FakeDevice().plan(disk=disk(device="wd0"))
        wd0 = "wd0c: error reading fsbn 64 of 64-71 (wd0 bn 64; cn 0 tn 1 sn 1)"
        sd0 = "sd0a: error writing fsbn 128 of 128-135 (sd0 bn 128; cn 0 tn 2 sn 2)"
        sd1 = "sd1a: error writing fsbn 8 of 8-15 (sd1 bn 8; cn 0 tn 0 sn 8)"
        marvell = "NetBSD IAL (ERROR) [0 0] device error occured"
        # A USB disk's errors are no reason to warn about the internal disk.
        self.assertIsNone(self.evidence(internal4, errors=(sd1,)))
        self.assertIsNone(self.evidence(internal6, errors=(sd0,)))
        # Its own, named in the warning; the Marvell driver's are always the
        # internal disk's.
        self.assertIn(sd0, self.evidence(internal4, errors=(sd1, sd0)).text)
        self.assertIn(wd0, self.evidence(internal6, errors=(wd0,)).text)
        self.assertIn(marvell, self.evidence(internal4, errors=(marvell,)).text)
        # A name that only starts the same is another disk.
        self.assertIsNone(self.evidence(FakeDevice().plan(disk=disk(device="wd1")), errors=("wd10a: error reading fsbn 8",)))

    def test_unreadable_samba_files_count_only_when_they_may_be_on_this_disk(self) -> None:
        from timecapsulesmb.device.disk_errors import UNREADABLE_EVIDENCE
        from timecapsulesmb.device.probe import DISK_UNREADABLE_PROBLEMS
        problem = next(iter(DISK_UNREADABLE_PROBLEMS))
        self.assertEqual(self.evidence(FakeDevice().plan(offer_install=True), problems=(problem,)), UNREADABLE_EVIDENCE)
        self.assertIsNone(self.evidence(FakeDevice().plan(offer_install=False), problems=(problem,)))
        self.payload_problems.assert_not_called()

    def test_the_warning_names_this_disk(self) -> None:
        from timecapsulesmb.device.disk_errors import disk_failing_warning, kernel_evidence
        evidence = kernel_evidence(("sd0a: error writing fsbn 128",))
        self.assertTrue(disk_failing_warning(evidence, "wipe").startswith("This disk reported errors"))
        # The other operations keep their wording.
        self.assertTrue(disk_failing_warning(evidence, "repair").startswith("The device's disk reported errors"))


class WipeRecoveryTests(unittest.TestCase):
    def test_wipe_disks_own_errors_offer_no_wipe_disk_button_but_deploys_does(self) -> None:
        # Wipe Internal Disk's screen is the one showing its own errors.
        from timecapsulesmb.app.recovery import recovery_for
        own = [
            recovery_for("wipe-disk", code)
            for code in (ErrorCode.WIPE_DISK_RESTART_NEEDED, ErrorCode.WIPE_DISK_ERASE_RUNNING,
                         ErrorCode.WIPE_DISK_JOB_RUNNING, ErrorCode.WIPE_DISK_BUSY, ErrorCode.WIPE_DISK_CHANGED,
                         ErrorCode.WIPE_DISK_ERASE_FAILED)
        ] + [
            recovery_for("wipe-disk", ErrorCode.ACPD_BROKEN, stage="erase_disk"),
            recovery_for("wipe-disk", ErrorCode.DISK_FAILING, stage="check_wipe"),
        ]
        for recovery in own:
            with self.subTest(recovery["title"]):
                self.assertEqual(recovery["action_ids"], [])
        self.assertEqual(recovery_for("deploy", ErrorCode.DEPLOY_NO_HFS_PARTITION)["action_ids"], ["wipe_disk"])



def gpt_tables(geometry: wipe.DiskGeometry, names: tuple[str, ...], *, primary: bool = True, backup: bool = True,
               primary_entries_lba: int = 2) -> bytes:
    """What gpt_tables_command reads from a disk whose GPT has these
    partition names (both copies, or only one)."""
    import uuid as uuidlib
    size = geometry.sector_size
    table_sectors = wipe.GPT_TABLE_BYTES // size
    table = bytearray(table_sectors * size)
    for index, name in enumerate(names):
        entry = bytearray(128)
        entry[0:16] = uuidlib.UUID("48465300-0000-11aa-aa11-00306543ecac").bytes_le
        entry[16:32] = uuidlib.uuid4().bytes_le
        encoded = name.encode("utf-16-le")[:72]
        entry[56:56 + len(encoded)] = encoded
        table[index * 128:(index + 1) * 128] = entry

    def header(entries_lba: int) -> bytes:
        block = bytearray(size)
        block[:8] = b"EFI PART"
        block[72:80] = entries_lba.to_bytes(8, "little")
        block[80:84] = (128).to_bytes(4, "little")
        block[84:88] = (128).to_bytes(4, "little")
        return bytes(block)

    empty_table = bytes(len(table))
    head = (header(primary_entries_lba) + bytes(table)) if primary else bytes(size) + empty_table
    tail = (bytes(table) + header(geometry.sectors - 1 - table_sectors)) if backup else empty_table + bytes(size)
    return head + tail


FOUR_K = wipe.DiskGeometry(4096, 976754646, "0x1000", "0x3a3812d6")
APPLE = ("APconfig", "APswap", "APdata")


class GptLabelTests(unittest.TestCase):
    def test_names_from_either_table(self) -> None:
        for geometry in (TWO_TB, FOUR_K):
            with self.subTest(sector_size=geometry.sector_size):
                self.assertEqual(wipe.gpt_partition_names(gpt_tables(geometry, APPLE), geometry), APPLE + APPLE)
                # Only the backup copy left (its head was zeroed): still read.
                self.assertEqual(wipe.gpt_partition_names(gpt_tables(geometry, APPLE, primary=False), geometry), APPLE)
                self.assertEqual(
                    wipe.gpt_partition_names(gpt_tables(geometry, ("EFI System Partition", "Backup"), backup=False), geometry),
                    ("EFI System Partition", "Backup"),
                )

    def test_no_gpt_has_no_names_and_an_unreadable_one_is_none(self) -> None:
        self.assertEqual(wipe.gpt_partition_names(gpt_tables(TWO_TB, (), primary=False, backup=False), TWO_TB), ())
        full = gpt_tables(TWO_TB, APPLE)
        self.assertIsNone(wipe.gpt_partition_names(full[:-1], TWO_TB))
        self.assertIsNone(wipe.gpt_partition_names(b"", TWO_TB))
        # A valid header whose table lies outside what was read.
        self.assertIsNone(wipe.gpt_partition_names(gpt_tables(TWO_TB, APPLE, primary_entries_lba=4096), TWO_TB))

    def test_the_command_reads_both_ends_and_writes_nothing(self) -> None:
        for geometry, primary, backup in ((TWO_TB, "skip=1 count=33", "skip=3907029135 count=33"),
                                          (FOUR_K, "skip=1 count=5", "skip=976754641 count=5")):
            with self.subTest(sector_size=geometry.sector_size), tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp) / "log"
                fake_dd = Path(tmp) / "dd"
                fake_dd.write_text(f'#!/bin/sh\necho "$*" >> {shlex.quote(str(log))}\n')
                fake_dd.chmod(0o755)
                command = wipe.gpt_tables_command("sd1", 2, geometry).replace("/bin/dd", str(fake_dd))
                subprocess.run(["/bin/sh", "-c", command], check=True)
                size = geometry.sector_size
                self.assertEqual(log.read_text().splitlines(), [
                    f"if=/dev/rsd1c bs={size} {primary}", f"if=/dev/rsd1c bs={size} {backup}",
                ])


class AppleLabelsElsewhereTests(unittest.TestCase):
    def check(self, others: dict[str, object], *, kernel: str | None = None, mast: tuple[str, ...] | None = None):
        """others: device -> GPT bytes, b"" for an unreadable geometry, or an
        exception the read raises. `mast` (default: all of them) are the disks
        MaSt lists; `kernel` is hw.disknames (default: the internal disk
        only)."""
        internal = disk()
        names = others if mast is None else mast
        disks = (internal, *(disk(device=name, builtin=False, uuid=USB_UUID) for name in names))
        commands: list[str] = []

        def read_text(_connection, command, *, timeout):
            commands.append(command)
            if command == wipe_disk.KERNEL_DISKS_COMMAND:
                return "md0 sd0 dk0 dk1 dk2\n" if kernel is None else kernel
            device = command.split()[-1]
            return "" if others.get(device) == b"" else drvctl_output("0x200", "0xe8e088b0")

        def capture(_connection, command, *, timeout):
            commands.append(command)
            answer = next(value for name, value in others.items() if f"/dev/r{name}c" in command)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        with (
            mock.patch.object(wipe_disk, "_read_text", side_effect=read_text),
            mock.patch.object(wipe_disk, "run_ssh_capture_bytes", side_effect=capture),
        ):
            found = wipe_disk.disks_with_apple_labels(CONNECTION, disks, internal, 2)
        self.checked = found.checked
        return found.verdicts, commands

    def test_each_other_disk_gets_a_verdict(self) -> None:
        from timecapsulesmb.transport.errors import TransportError
        found, commands = self.check({
            "sd1": gpt_tables(TWO_TB, APPLE),
            "sd2": gpt_tables(TWO_TB, ("EFI System Partition", "Untitled")),
            "sd3": gpt_tables(TWO_TB, ()),
            "sd4": b"",
            "sd5": TransportError("timed out"),
            "sd6": gpt_tables(TWO_TB, ("Data", "APdata"), primary=False),
        })
        self.assertEqual(found, {"sd1": "apple_labels", "sd4": "unreadable", "sd5": "unreadable", "sd6": "apple_labels"})
        # Only the other disks are read, and only read.
        self.assertFalse(any("sd0" in command for command in commands if command != wipe_disk.KERNEL_DISKS_COMMAND))
        self.assertFalse(any("of=" in command for command in commands))

    def test_a_disk_only_the_kernel_lists_is_checked_too(self) -> None:
        # Just plugged in, or diskd slow: its wedges exist all the same.
        # Flash, the RAM disk and wedges are not disks with tables; a disk in
        # both lists is read once.
        found, commands = self.check(
            {"sd1": gpt_tables(TWO_TB, APPLE), "wd1": gpt_tables(TWO_TB, ("Backup",))},
            kernel="flash0 md0 sd0 dk0 dk1 dk2 sd1 dk3 wd1\n", mast=("wd1",),
        )
        self.assertEqual(found, {"sd1": "apple_labels"})
        self.assertEqual(self.checked, ("wd1", "sd1"))
        self.assertEqual(sum("/dev/rwd1c" in command for command in commands), 1)
        self.assertFalse(any(name in command for command in commands for name in ("flash0", "md0", "rdk")))

    def test_no_other_disk_reads_nothing_but_the_lists(self) -> None:
        found, commands = self.check({}, kernel="md0 sd0 dk0\n")
        self.assertEqual((found, commands, self.checked), ({}, [wipe_disk.KERNEL_DISKS_COMMAND], ()))

    def test_a_kernel_list_without_the_internal_disk_was_not_read_and_refuses(self) -> None:
        # An unreadable list (timeout, nothing printed) or one missing the
        # target would otherwise pass with no disk checked; MaSt's disks are
        # still read.
        for kernel in ("", "md0 flash0\n"):
            with self.subTest(kernel=kernel):
                found, _commands = self.check({"sd1": gpt_tables(TWO_TB, ("Backup",))}, kernel=kernel)
                self.assertEqual(found, {wipe_disk.KERNEL_DISKS_KEY: "unreadable"})
                self.assertEqual(self.checked, ("sd1",))
        # Both lists empty: still a refusal, not a pass.
        found, _commands = self.check({}, kernel="")
        self.assertEqual(found, {wipe_disk.KERNEL_DISKS_KEY: "unreadable"})

    def test_each_check_keeps_its_own_telemetry(self) -> None:
        callbacks = mock.MagicMock()
        answer = wipe_disk.OtherDisks(("sd1", "sd2"), {"sd2": "unreadable"})
        with mock.patch.object(wipe_disk, "disks_with_apple_labels", return_value=answer):
            with self.assertRaises(wipe_disk.WipeDiskError) as raised:
                wipe_disk.require_no_apple_labels_elsewhere(CONNECTION, (disk(),), disk(), 2, callbacks=callbacks,
                                                            field="wipe_other_disks_confirmed")
        callbacks.update.assert_called_once_with(wipe_other_disks_confirmed={"sd2": "unreadable"},
                                                 wipe_other_disks_confirmed_checked=2)
        self.assertEqual(raised.exception.detail, "sd2: unreadable")

if __name__ == "__main__":
    unittest.main()
