"""tests/samba/check.py: which steps each tier runs, in what order, under which locks."""
from __future__ import annotations

from pathlib import Path
import threading
import unittest

from tests.samba.check import Outcome, Phase, Step, describe, plan, run_phases, summary

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


if __name__ == "__main__":
    unittest.main()
