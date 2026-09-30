"""tests/samba/check.py: which steps each tier runs, in what order, under which locks."""
from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from tests.samba import run
from tests.samba.check import (QUICK_DRIVERS, RAM, Outcome, Phase, Step, describe, driver_plan, plan,
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
    """A device whose /mnt/Memory has free_kib free; each command's reply comes
    from reply(command), and every command and upload is recorded."""
    root = "/Volumes/dk2/ShareRoot"

    def __init__(self, free_kib="4000", reply=None, raise_on=None) -> None:
        self.free_kib = free_kib
        self.reply = reply or (lambda command: "PASS\nrc=0\n")
        self.raise_on = raise_on
        self.commands: list[str] = []
        self.uploads: list[tuple[str, int]] = []

    def sh(self, command: str, *, check: bool = True) -> str:
        self.commands.append(command)
        if command.startswith("/bin/df"):
            return ("Filesystem 1K-blocks Used Avail %Cap Mounted on\n"
                    f"/dev/md0 15000 11000 {self.free_kib} 70% /mnt/Memory\n")
        if self.raise_on and self.raise_on in command:
            raise RuntimeError("ssh dropped")
        if "rc=$?" in command:
            return self.reply(command)
        return ""

    def put(self, path: str, data: bytes) -> None:
        self.uploads.append((path, len(data)))


class RunDriversTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.binaries = self.dir / "binaries"
        self.binaries.mkdir()
        self.sizes = {"tc_at_emulation_test": 200_000, "tc_file_growth_test": 1_400_000,
                      "tc_fork_repair_test": 170_000}
        for driver in run.TARGETS:
            for lane in ("6", "4le"):
                size = self.sizes.get(driver, 100_000)
                if driver == "tc_native_links_test":
                    size = 9_700_000
                (self.binaries / f"{driver}.{lane}").write_bytes(b"x" * size)
        self.log = self.dir / "drivers.log"

    def drive(self, device: FakeDevice, tier: str = "quick", lane: str = "6") -> bool:
        with mock.patch("tests.samba.links_device.Device", return_value=device), \
             mock.patch("timecapsulesmb.core.config.parse_env_file", return_value={}):
            return run_drivers(tier, ".env.x", lane, self.binaries, self.log)

    def test_each_driver_goes_to_ram_once_and_every_case_runs(self) -> None:
        device = FakeDevice()
        self.assertTrue(self.drive(device))
        self.assertEqual(device.uploads, [(f"{RAM}/{d}", self.sizes[d]) for d in QUICK_DRIVERS])
        runs = [c for c in device.commands if "rc=$?" in c]
        self.assertEqual(len(runs), len(run.AT_EMULATION_CASES) + 1 + len(run.FORK_REPAIR_CASES))
        scratch = "/Volumes/dk2/__tc_drivers__"
        self.assertEqual(runs[0], f"cd {scratch}/tmp && TMPDIR={scratch}/tmp {RAM}/tc_at_emulation_test calls 2>&1; "
                                  "echo rc=$?")
        # Each copy is removed after its cases, and the scratch directory at the end.
        removals = [c for c in device.commands if c.startswith("rm -f ")]
        self.assertEqual(removals, [f"rm -f {RAM}/{d}" for d in QUICK_DRIVERS])
        self.assertEqual(device.commands[-1], f"rm -rf {scratch}")
        self.assertIn("0 failed: []", self.log.read_text())

    def test_a_failed_case_is_reported_and_the_rest_still_run(self) -> None:
        device = FakeDevice(reply=lambda c: "boom\nrc=1\n" if " times " in c else "PASS\nrc=0\n")
        self.assertFalse(self.drive(device))
        runs = [c for c in device.commands if "rc=$?" in c]
        self.assertTrue(any("tc_fork_repair_test fallback" in c for c in runs))
        self.assertIn("1 failed: ['tc_at_emulation_test times']", self.log.read_text())

    def test_output_ending_in_another_status_fails(self) -> None:
        # "rc=0" must be the final line, not just appear in the output.
        device = FakeDevice(reply=lambda c: "rc=0 seen\nrc=139\n" if "fallback" in c else "rc=0\n")
        self.assertFalse(self.drive(device))
        self.assertIn("['tc_fork_repair_test fallback']", self.log.read_text())

    def test_full_tier_runs_every_driver(self) -> None:
        device = FakeDevice(free_kib="40000")
        self.assertTrue(self.drive(device, tier="full"))
        self.assertEqual([path for path, _ in device.uploads], [f"{RAM}/{d}" for d in run.TARGETS])
        runs = [c for c in device.commands if "rc=$?" in c]
        self.assertEqual(len(runs), len(list(run.execution_cases(True))))
        self.assertTrue(any(c.endswith("tc_pthreadpool_sync_test  2>&1; echo rc=$?") for c in runs))

    def test_too_large_for_ram_runs_from_the_data_disk_on_netbsd6(self) -> None:
        device = FakeDevice(free_kib="5000")
        self.assertTrue(self.drive(device, tier="full"))
        homes = dict(device.uploads)
        self.assertIn("/Volumes/dk2/__tc_drivers__/bin/tc_native_links_test", homes)
        self.assertIn(f"{RAM}/tc_native_metadata_test", homes)
        self.assertIn("(from /Volumes/dk2/__tc_drivers__/bin)", self.log.read_text())

    def test_too_large_for_ram_is_skipped_on_netbsd4(self) -> None:
        device = FakeDevice(free_kib="5000")
        self.assertTrue(self.drive(device, tier="full", lane="4le"))
        self.assertNotIn("tc_native_links_test", " ".join(path for path, _ in device.uploads))
        self.assertFalse(any("tc_native_links_test" in c for c in device.commands if "rc=$?" in c))
        text = self.log.read_text()
        self.assertIn("== tc_native_links_test SKIPPED: 9700000 bytes do not fit /mnt/Memory", text)
        self.assertIn("1 skipped (too large for /mnt/Memory on NetBSD 4): ['tc_native_links_test']", text)

    def test_unreadable_df_counts_as_no_room(self) -> None:
        device = FakeDevice(free_kib="-")
        self.assertTrue(self.drive(device, lane="4le"))
        self.assertEqual(device.uploads, [])
        self.assertIn("3 skipped", self.log.read_text())

    def test_copies_are_removed_when_the_connection_fails(self) -> None:
        device = FakeDevice(raise_on=" flags ")
        with self.assertRaises(RuntimeError):
            self.drive(device)
        self.assertEqual(device.commands[-2:], [f"rm -f {RAM}/tc_at_emulation_test", "rm -rf /Volumes/dk2/__tc_drivers__"])


if __name__ == "__main__":
    unittest.main()
