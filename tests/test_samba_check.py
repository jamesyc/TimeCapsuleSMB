"""tests/samba/check.py: which steps each tier runs, in what order, under which locks."""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from tests.samba import check, run
from tests.samba.check import (DRIVER_RAM, QUICK_DRIVERS, Outcome, Phase, Step, describe, driver_plan, plan,
                               run_drivers, run_phases, summary)

ENVS = {"6": "/env6", "4le": "/env4"}
OUT = Path("/out")


def names(steps):
    return [s.name for s in steps]


class PlanTest(unittest.TestCase):
    def test_quick_with_a_build(self) -> None:
        phases = plan("quick", ["6", "4le"], ENVS, OUT, True, None, set())
        self.assertEqual([p.name for p in phases], ["mac", "build", "install", "devices"])
        self.assertEqual(names(phases[0].steps), ["pytest", "host_regression"])
        build = phases[1].steps[0].argv
        self.assertIn("6,4le,4be", build)  # BE is built in the quick tier too
        self.assertNotIn("--install", build)
        self.assertEqual(names(phases[2].steps), ["swap_smbd", "swap_smbd"])
        self.assertIn("/out/binaries/smbd.4le", phases[2].steps[1].argv)
        self.assertEqual(names(phases[3].per_device["6"]),
                         ["drivers", "doctor", "dir_device", "growth_device", "smbtorture"])
        self.assertIn("--quick", phases[3].per_device["6"][2].argv)
        self.assertIn("--quick", phases[3].per_device["6"][3].argv)
        torture = phases[3].per_device["4le"][-1].argv
        self.assertEqual(torture[torture.index("--tier") + 1], "quick")
        self.assertEqual(torture[torture.index("--device") + 1], "4le")

    def test_full_deploys_netbsd6_first_and_runs_every_suite(self) -> None:
        phases = plan("full", ["4le", "6"], ENVS, OUT, True, None, set())
        self.assertIn("--install", phases[1].steps[0].argv)
        install = phases[2]
        self.assertEqual([s.device for s in install.steps], ["6", "4le"])
        self.assertFalse(install.parallel)
        self.assertEqual(names(phases[3].per_device["6"]),
                         ["drivers", "doctor", "dir_device", "growth_device", "growth_device_aio",
                          "durable_device", "links_device", "smbtorture"])
        self.assertNotIn("--quick", phases[3].per_device["6"][2].argv)

    def test_a_full_run_on_netbsd6_alone_holds_netbsd4_too(self) -> None:
        phases = plan("full", ["6"], ENVS, OUT, False, Path("/bins"), set())
        self.assertEqual(phases[-1].locks, ["NetBSD 6", "NetBSD 4"])
        quick = plan("quick", ["6"], ENVS, OUT, False, Path("/bins"), set())
        self.assertEqual(quick[-1].locks, ["NetBSD 6"])

    def test_without_new_binaries_nothing_is_installed_or_run_from_ram(self) -> None:
        phases = plan("quick", ["6"], ENVS, OUT, False, None, set())
        self.assertEqual([p.name for p in phases], ["mac", "devices"])
        self.assertNotIn("drivers", names(phases[-1].per_device["6"]))

    def test_skip(self) -> None:
        phases = plan("quick", ["6"], ENVS, OUT, True, None, {"host_regression", "smbtorture", "install"})
        self.assertEqual(names(phases[0].steps), ["pytest"])
        self.assertEqual([p.name for p in phases], ["mac", "build", "devices"])
        self.assertNotIn("smbtorture", names(phases[-1].per_device["6"]))


class DescribeTest(unittest.TestCase):
    def test_lists_every_step_with_its_command_and_locks(self) -> None:
        text = describe(plan("quick", ["6"], ENVS, OUT, True, None, set()))
        self.assertIn("mac (parallel)", text)
        self.assertIn("  mac:pytest  run_pytest()", text)
        self.assertIn("install (parallel; locks NetBSD 6)", text)
        self.assertIn("6:swap_smbd", text)
        self.assertIn("  6:drivers  run_drivers()", text)
        self.assertIn("tests.samba.dir_device --env /env6 --quick", text)


class FakeLocks:
    held: list = []

    def __init__(self, rows, holder, doing) -> None:
        self.rows = rows

    def __enter__(self):
        FakeLocks.held.append(("claim", tuple(self.rows)))

    def __exit__(self, *exc):
        FakeLocks.held.append(("release", tuple(self.rows)))
        return False


class RunTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeLocks.held = []
        self.ran: list[str] = []
        self.lock = threading.Lock()

    def runner(self, failing=()):
        def run(step: Step, out: Path) -> Outcome:
            with self.lock:
                self.ran.append(f"{step.device}:{step.name}")
            return Outcome("", step.name, step.device, step.name not in failing, 1.0)
        return run

    def phases(self):
        return [Phase("mac", [Step("pytest", None), Step("host_regression", None)]),
                Phase("install", [Step("deploy", "6"), Step("deploy", "4le")], parallel=False, locks=["NetBSD 6"]),
                Phase("devices", [], locks=["NetBSD 6", "NetBSD 4"],
                      per_device={"6": [Step("doctor", "6"), Step("smbtorture", "6")],
                                  "4le": [Step("doctor", "4le"), Step("smbtorture", "4le")]})]

    def test_everything_runs_and_locks_wrap_their_phases(self) -> None:
        outcomes = run_phases(self.phases(), OUT, True, "me", runner=self.runner(), locks_cls=FakeLocks)
        self.assertEqual(len(outcomes), 8)
        self.assertTrue(all(o.ok for o in outcomes))
        self.assertEqual({o.phase for o in outcomes}, {"mac", "install", "devices"})
        # Within a device its steps keep their order.
        for lane in ("6", "4le"):
            self.assertLess(self.ran.index(f"{lane}:doctor"), self.ran.index(f"{lane}:smbtorture"))
        self.assertEqual(FakeLocks.held, [("claim", ("NetBSD 6",)), ("release", ("NetBSD 6",)),
                                          ("claim", ("NetBSD 6", "NetBSD 4")), ("release", ("NetBSD 6", "NetBSD 4"))])

    def test_quick_stops_at_the_first_failed_phase(self) -> None:
        outcomes = run_phases(self.phases(), OUT, True, "me", runner=self.runner({"host_regression"}),
                              locks_cls=FakeLocks)
        self.assertEqual([o.phase for o in outcomes], ["mac", "mac"])
        self.assertEqual(FakeLocks.held, [])

    def test_a_failed_deploy_stops_the_sequential_install(self) -> None:
        outcomes = run_phases(self.phases(), OUT, True, "me", runner=self.runner({"deploy"}), locks_cls=FakeLocks)
        self.assertEqual([o.step for o in outcomes if o.phase == "install"], ["deploy"])
        self.assertNotIn("6:doctor", self.ran)

    def test_full_runs_on_after_a_failure(self) -> None:
        outcomes = run_phases(self.phases(), OUT, False, "me", runner=self.runner({"host_regression"}),
                              locks_cls=FakeLocks)
        self.assertEqual(len(outcomes), 8)
        self.assertEqual([o.step for o in outcomes if not o.ok], ["host_regression"])

    def test_summary(self) -> None:
        text = summary([Outcome("mac", "pytest", None, True, 120.0),
                        Outcome("devices", "doctor", "6", False, 60.0)], 600.0)
        self.assertIn("mac      mac    pytest             ok      2.0 min", text)
        self.assertIn("FAILED", text)
        self.assertTrue(text.endswith("1 ok, 1 failed, 10.0 min in all"))


class DriverPlanTest(unittest.TestCase):
    def test_full_runs_the_whole_device_plan_each_driver_together(self) -> None:
        drivers = driver_plan("full")
        self.assertEqual(list(drivers), list(run.TARGETS))
        self.assertEqual(drivers["tc_pthreadpool_sync_test"], [()])
        # Drivers with large fixtures run once with "all" on a device.
        self.assertEqual(drivers["tc_xattr_migrate_test"], [("all",)])
        self.assertEqual(drivers["tc_native_links_test"], [("all",)])
        self.assertEqual(drivers["tc_aio_fork_test"], [(case,) for case in run.AIO_CASES])
        self.assertEqual(sum(len(v) for v in drivers.values()), len(list(run.execution_cases(True))))

    def test_quick_runs_its_three_drivers(self) -> None:
        drivers = driver_plan("quick")
        self.assertEqual(list(drivers), list(QUICK_DRIVERS))
        self.assertEqual(drivers["tc_fork_repair_test"], [(case,) for case in run.FORK_REPAIR_CASES])
        self.assertEqual(drivers["tc_at_emulation_test"], [(case,) for case in run.AT_EMULATION_CASES])
        self.assertEqual(drivers["tc_file_growth_test"], [("all",)])


class FakeDevice:
    """A device with the drivers' RAM disk: mounting and unmounting it change
    what /sbin/mount lists, df reports free_kib free on it, each driver run's
    reply comes from reply(command), and every command and upload is
    recorded."""
    root = "/Volumes/dk2/ShareRoot"

    def __init__(self, free_kib="12200", reply=None, raise_on=None, mounted=False, umount_fails=False,
                 mount_fails=False) -> None:
        self.free_kib = free_kib
        self.reply = reply or (lambda command: "PASS\nrc=0\n")
        self.raise_on = raise_on
        self.mounted = mounted
        self.umount_fails = umount_fails
        self.mount_fails = mount_fails
        self.commands: list[str] = []
        self.uploads: list[tuple[str, int]] = []

    def sh(self, command: str, *, check: bool = True) -> str:
        self.commands.append(command)
        if command == "/sbin/mount":
            ram = f"tmpfs on {DRIVER_RAM} type tmpfs (local)\n" if self.mounted else ""
            return "/dev/md0a on / type ffs (local)\n" + ram
        if command.startswith("/bin/df"):
            return ("Filesystem 1K-blocks Used Avail %Cap Mounted on\n"
                    f"tmpfs 12288 0 {self.free_kib} 0% {DRIVER_RAM}\n")
        if "/sbin/mount_" in command:
            if self.mount_fails:
                raise RuntimeError("mount_mfs: Cannot allocate memory")
            self.mounted = True
            return ""
        if command.startswith("/sbin/umount"):
            if not self.umount_fails:
                self.mounted = False
            return ""
        if self.raise_on and self.raise_on in command:
            raise RuntimeError("ssh dropped")
        if "rc=$?" in command:
            return self.reply(command)
        return ""

    def put(self, path: str, data: bytes) -> None:
        self.uploads.append((path, len(data)))


TMP = "/Volumes/dk2/__tc_drivers__"


class RunDriversTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.binaries = self.dir / "binaries"
        self.binaries.mkdir()
        self.sizes = {"tc_at_emulation_test": 200_000, "tc_file_growth_test": 1_400_000,
                      "tc_fork_repair_test": 170_000, "tc_native_links_test": 9_700_000}
        for driver in run.TARGETS:
            for lane in ("6", "4le"):
                (self.binaries / f"{driver}.{lane}").write_bytes(b"x" * self.sizes.get(driver, 100_000))
        self.log = self.dir / "drivers.log"

    def drive(self, device: FakeDevice, tier: str = "quick", lane: str = "6") -> bool:
        with mock.patch("tests.samba.links_device.Device", return_value=device), \
             mock.patch("timecapsulesmb.core.config.parse_env_file", return_value={}):
            return run_drivers(tier, ".env.x", lane, self.binaries, self.log)

    def runs(self, device: FakeDevice) -> list[str]:
        return [c for c in device.commands if "rc=$?" in c]

    def test_every_driver_runs_from_the_ram_disk_which_is_removed_after(self) -> None:
        device = FakeDevice()
        self.assertTrue(self.drive(device))
        self.assertEqual(device.commands[:3], [f"rm -rf {TMP} && mkdir -p {TMP}", "/sbin/mount",
                                               f"mkdir -p {DRIVER_RAM} && /sbin/mount_tmpfs -s 12m tmpfs {DRIVER_RAM}"])
        self.assertEqual(device.uploads, [(f"{DRIVER_RAM}/{d}", self.sizes[d]) for d in QUICK_DRIVERS])
        runs = self.runs(device)
        self.assertEqual(len(runs), len(run.AT_EMULATION_CASES) + 1 + len(run.FORK_REPAIR_CASES))
        self.assertEqual(runs[0], f"cd {TMP} && TMPDIR={TMP} {DRIVER_RAM}/tc_at_emulation_test calls 2>&1; echo rc=$?")
        # Each driver is removed before the next is copied: one at a time.
        order = [c for c in device.commands if c.startswith(("rm -f ", "chmod"))]
        self.assertEqual(order, [x for d in QUICK_DRIVERS for x in (f"chmod 755 {DRIVER_RAM}/{d}",
                                                                     f"rm -f {DRIVER_RAM}/{d}")])
        self.assertEqual(device.commands[-4:], [f"/sbin/umount {DRIVER_RAM}", "/sbin/mount",
                                                f"rmdir {DRIVER_RAM}", f"rm -rf {TMP}"])
        self.assertFalse(device.mounted)
        self.assertTrue(self.log.read_text().endswith("0 failed: []\n"))

    def test_netbsd4_mounts_mfs_and_gives_the_migrator_its_scratch(self) -> None:
        device = FakeDevice()
        self.assertTrue(self.drive(device, tier="full", lane="4le"))
        self.assertIn(f"mkdir -p {DRIVER_RAM} && /sbin/mount_mfs -s 24576 swap {DRIVER_RAM}", device.commands)
        self.assertFalse(any("mount_tmpfs" in c for c in device.commands))
        # Every driver sees it; only the migrator driver reads it.
        self.assertTrue(all(f"TMPDIR={TMP} TC_MIGRATE_SCRATCH={DRIVER_RAM} {DRIVER_RAM}/" in c
                            for c in self.runs(device)))

    def test_netbsd6_migrator_scratch_stays_in_tmp(self) -> None:
        device = FakeDevice()
        self.assertTrue(self.drive(device, tier="full"))
        self.assertFalse(any("TC_MIGRATE_SCRATCH" in c for c in device.commands))

    def test_a_ram_disk_left_by_a_crashed_run_is_unmounted_first(self) -> None:
        device = FakeDevice(mounted=True)
        self.assertTrue(self.drive(device))
        first_mount = next(i for i, c in enumerate(device.commands) if "/sbin/mount_tmpfs" in c)
        self.assertIn(f"/sbin/umount {DRIVER_RAM}", device.commands[:first_mount])

    def test_the_full_tier_runs_every_driver_the_largest_included(self) -> None:
        device = FakeDevice()
        self.assertTrue(self.drive(device, tier="full", lane="4le"))
        self.assertEqual([path for path, _ in device.uploads], [f"{DRIVER_RAM}/{d}" for d in run.TARGETS])
        self.assertEqual(len(self.runs(device)), len(list(run.execution_cases(True))))
        self.assertTrue(any(c.endswith("tc_pthreadpool_sync_test  2>&1; echo rc=$?") for c in self.runs(device)))

    def test_a_driver_that_does_not_fit_fails_and_the_rest_still_run(self) -> None:
        device = FakeDevice(free_kib="9000")   # less than tc_native_links_test
        self.assertFalse(self.drive(device, tier="full"))
        self.assertNotIn("tc_native_links_test", " ".join(path for path, _ in device.uploads))
        self.assertIn(f"{DRIVER_RAM}/tc_fork_repair_test", dict(device.uploads))
        text = self.log.read_text()
        self.assertIn(f"== tc_native_links_test FAILED: 9700000 bytes do not fit {DRIVER_RAM} (9216000 free)", text)
        self.assertTrue(text.endswith("1 failed: ['tc_native_links_test']\n"))

    def test_unreadable_df_counts_as_no_room(self) -> None:
        device = FakeDevice(free_kib="-")
        self.assertFalse(self.drive(device))
        self.assertEqual(device.uploads, [])
        self.assertIn("3 failed", self.log.read_text())

    def test_a_failed_case_is_reported_and_the_rest_still_run(self) -> None:
        device = FakeDevice(reply=lambda c: "boom\nrc=1\n" if " times " in c else "PASS\nrc=0\n")
        self.assertFalse(self.drive(device))
        self.assertTrue(any("tc_fork_repair_test fallback" in c for c in self.runs(device)))
        self.assertIn("1 failed: ['tc_at_emulation_test times']", self.log.read_text())

    def test_output_ending_in_another_status_fails(self) -> None:
        # "rc=0" must be the final line, not just appear in the output.
        device = FakeDevice(reply=lambda c: "rc=0 seen\nrc=139\n" if "fallback" in c else "rc=0\n")
        self.assertFalse(self.drive(device))
        self.assertIn("['tc_fork_repair_test fallback']", self.log.read_text())

    def test_a_dropped_connection_still_removes_the_driver_and_the_ram_disk(self) -> None:
        device = FakeDevice(raise_on=" flags ")
        with self.assertRaises(RuntimeError):
            self.drive(device)
        self.assertEqual(device.commands[-6:], [f"rm -f {DRIVER_RAM}/tc_at_emulation_test", "/sbin/mount",
                                                f"/sbin/umount {DRIVER_RAM}", "/sbin/mount", f"rmdir {DRIVER_RAM}",
                                                f"rm -rf {TMP}"])
        self.assertFalse(device.mounted)

    def test_a_ram_disk_that_stays_mounted_fails_the_step(self) -> None:
        device = FakeDevice(umount_fails=True)
        self.assertFalse(self.drive(device))
        text = self.log.read_text()
        self.assertIn(f"FAILED: {DRIVER_RAM} is still mounted; unmount it by hand", text)
        self.assertNotIn(f"rmdir {DRIVER_RAM}", device.commands)
        self.assertTrue(text.endswith(f"1 failed: ['unmount {DRIVER_RAM}']\n"))

    def test_a_failed_mount_copies_nothing(self) -> None:
        device = FakeDevice(mount_fails=True)
        with self.assertRaisesRegex(RuntimeError, "Cannot allocate memory"):
            self.drive(device)
        self.assertEqual(device.uploads, [])
        self.assertEqual(device.commands[-1], f"rm -rf {TMP}")

if __name__ == "__main__":
    unittest.main()


class HostImageTest(unittest.TestCase):
    """The host regression's dependencies are installed once, into a local image."""

    def runner(self, inspect_rc, build_rc=0):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, inspect_rc if argv[:3] == ["docker", "image", "inspect"] else build_rc)
        return calls, run

    def test_an_existing_image_is_used_as_it_is(self) -> None:
        calls, run = self.runner(0)
        self.assertTrue(check.ensure_host_image(None, run))
        self.assertEqual([argv for argv, _ in calls], [["docker", "image", "inspect", check.HOST_IMAGE]])

    def test_a_missing_image_is_built_from_the_package_list(self) -> None:
        calls, run = self.runner(1)
        self.assertTrue(check.ensure_host_image(None, run))
        build, kwargs = calls[1]
        self.assertEqual(build, ["docker", "build", "-t", check.HOST_IMAGE, "-"])
        dockerfile = kwargs["input"].decode()
        self.assertTrue(dockerfile.startswith("FROM ubuntu:24.04\n"))
        for package in check.HOST_PACKAGES:
            self.assertIn(f" {package} ", dockerfile)

    def test_a_failed_build_fails_the_step_before_any_container_starts(self) -> None:
        calls, run = self.runner(1, build_rc=1)
        self.assertFalse(check.ensure_host_image(None, run))
        with mock.patch.object(check, "ensure_host_image", return_value=False), \
                mock.patch.object(check.subprocess, "run", side_effect=AssertionError("no container")), \
                tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(check.run_host_regression(Path(tmp) / "host.log"))
