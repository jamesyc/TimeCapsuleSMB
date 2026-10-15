from __future__ import annotations

import unittest

from tests.device_ps import ps_row
from timecapsulesmb.device.processes import StuckProcess, manager_unnamed_stuck_count, stuck_processes

# PROCESS_SNAPSHOT_COMMAND rows: only service and diskd rows carry a command
# line.
SMBD_PARENT = ps_row(457, "smbd", ppid=146, stat="I", sl=20, wchan="select")
NETBSD6_KERNEL = ps_row(0, "system", ppid=0, stat="DKl", wchan="uvm")
NETBSD4_KERNEL_THREAD = ps_row(5, "amc6821c_thread", ppid=0, pgid=0, stat="DK", sl=127, wchan="amc6821c")
ZOMBIE = ps_row(97, "wcifsnd", ppid=119, pgid=2, stat="Z")
RUNNING = ps_row(3166, "smbd", ppid=457, pgid=457, stat="R")


def manager_row(words: str) -> str:
    return ps_row(146, "service", f"service: role=manager {words}".rstrip(), pgid=2, sl=1, wchan="select")


def row(pid: int, state: str, sleep: str, wchan: str, name: str) -> str:
    return ps_row(pid, name, ppid=457, pgid=457, stat=state, sl=int(sleep), wchan=wchan)


class StuckProcessesTests(unittest.TestCase):
    def test_reports_process_asleep_uninterruptibly_for_the_threshold(self) -> None:
        output = "\n".join([SMBD_PARENT, row(3166, "D", "120", "biowait", "smbd"), RUNNING])

        self.assertEqual(stuck_processes(output), [StuckProcess(3166, "smbd", "biowait", 120)])

    def test_ignores_uninterruptible_sleep_shorter_than_threshold(self) -> None:
        # A process doing slow disk I/O wakes between requests, and every wake
        # resets the kernel's sleep time.
        self.assertEqual(stuck_processes(row(3166, "D", "119", "biowait", "smbd")), [])

    def test_ignores_interruptible_sleeps_however_long(self) -> None:
        output = "\n".join([row(10, "I", "127", "select", "smbd"), row(11, "S", "127", "kqueue", "sshd")])

        self.assertEqual(stuck_processes(output), [])

    def test_ignores_kernel_threads_which_always_sleep_uninterruptibly(self) -> None:
        self.assertEqual(stuck_processes("\n".join([NETBSD6_KERNEL, NETBSD4_KERNEL_THREAD])), [])

    def test_ignores_running_and_zombie_processes(self) -> None:
        self.assertEqual(stuck_processes("\n".join([RUNNING, ZOMBIE])), [])

    def test_keeps_state_modifiers_after_d(self) -> None:
        # Session leaders and foreground processes add s or + after the state.
        output = "\n".join([row(20, "Ds", "121", "vnlock", "sh"), row(21, "D+", "127", "tstile", "tail")])

        self.assertEqual(
            stuck_processes(output),
            [StuckProcess(20, "sh", "vnlock", 121), StuckProcess(21, "tail", "tstile", 127, capped=True)],
        )

    def test_reports_every_stuck_process_in_listing_order(self) -> None:
        output = "\n".join(
            [
                row(30, "D", "127", "biowait", "smbd"),
                SMBD_PARENT,
                row(31, "D", "125", "needbuf", "service"),
            ]
        )

        self.assertEqual(
            [process.pid for process in stuck_processes(output)],
            [30, 31],
        )

    def test_skips_short_and_malformed_rows(self) -> None:
        output = "\n".join(["", "garbage", "  x 1 1 D 127 biowait 0:00.00 smbd", "  40 1 1 D many biowait 0:00.00 smbd",
                            "  41 1 1 D 127 biowait 0:00.00"])

        self.assertEqual(stuck_processes(output), [])

    def test_marks_sleep_at_ps_display_cap(self) -> None:
        stuck = stuck_processes(row(3166, "D", "127", "biowait", "smbd"))

        self.assertEqual(stuck, [StuckProcess(3166, "smbd", "biowait", 127, capped=True)])
        self.assertEqual(stuck[0].describe(), "smbd (pid 3166) waiting on biowait for 127+ s")
        self.assertEqual(
            StuckProcess(3166, "smbd", "biowait", 75).describe(),
            "smbd (pid 3166) waiting on biowait for 75 s",
        )

    def test_reads_waits_that_keep_waking_from_the_manager_title(self) -> None:
        # needbuf wakes four times a second, so ps shows a sleep time near 0.
        output = "\n".join(
            [
                manager_row("stuck=3166:smbd:needbuf:600,359:mDNSResponder:tstile:75"),
                row(3166, "D", "0", "needbuf", "smbd"),
            ]
        )

        stuck = stuck_processes(output)

        self.assertEqual(
            stuck,
            [StuckProcess(3166, "smbd", "needbuf", 600), StuckProcess(359, "mDNSResponder", "tstile", 75)],
        )
        self.assertEqual(stuck[0].describe(), "smbd (pid 3166) waiting on needbuf for 600 s")

    def test_manager_title_takes_precedence_over_ps_for_the_same_pid(self) -> None:
        output = "\n".join([row(3166, "D", "127", "biowait", "smbd"), manager_row("stuck=3166:smbd:biowait:900")])

        self.assertEqual(stuck_processes(output), [StuckProcess(3166, "smbd", "biowait", 900)])

    def test_manager_title_with_hostname_wait_and_more_than_it_names(self) -> None:
        output = manager_row("waiting=hostname stuck=40:smbd:biowait:121,41:smbd:biowait:121,+3")

        self.assertEqual([process.pid for process in stuck_processes(output)], [40, 41])
        self.assertEqual(manager_unnamed_stuck_count(output), 3)

    def test_unnamed_count_is_zero_without_a_manager_title_or_overflow(self) -> None:
        self.assertEqual(manager_unnamed_stuck_count(row(3166, "D", "127", "biowait", "smbd")), 0)
        self.assertEqual(manager_unnamed_stuck_count(manager_row("stuck=40:smbd:biowait:121")), 0)
        self.assertEqual(manager_unnamed_stuck_count(manager_row("")), 0)
        self.assertEqual(manager_unnamed_stuck_count(manager_row("stuck=40:smbd:biowait:121,+x")), 0)

    def test_ignores_manager_title_without_stuck_and_malformed_entries(self) -> None:
        output = "\n".join(
            [
                manager_row(""),
                manager_row("stuck=x:smbd:biowait:61,42:smbd:biowait,43:smbd:biowait:soon"),
            ]
        )

        self.assertEqual(stuck_processes(output), [])

    def test_only_the_manager_title_is_read(self) -> None:
        discovery = ps_row(911, "service", "service: role=discovery stuck=1:x:y:99", ppid=146, sl=1, wchan="select")

        self.assertEqual(stuck_processes(discovery), [])


if __name__ == "__main__":
    unittest.main()


class ListedWithoutCommandLinesTests(unittest.TestCase):
    """The snapshot reads command lines only for service and diskd processes."""

    def snapshot(self, processes: list[dict[str, object]]) -> tuple[str, list[str]]:
        import subprocess
        import tempfile
        from pathlib import Path

        from tests.device_ps import FakePs
        from timecapsulesmb.device.processes import PROCESS_SNAPSHOT_COMMAND

        with tempfile.TemporaryDirectory() as tmp:
            fake = FakePs(Path(tmp), processes)
            result = subprocess.run(
                fake.install(PROCESS_SNAPSHOT_COMMAND).replace("sleep 1", ":"), shell=True, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout, fake.command_reads()

    def test_finds_stuck_processes_and_the_manager_title_without_reading_other_command_lines(self) -> None:
        from tests.device_ps import process
        from timecapsulesmb.device.acpd import acpd_stuck_children
        from timecapsulesmb.device.processes import smbd_lock_waits

        output, reads = self.snapshot([
            process("146 S service service: role=manager started=3 stuck=900:smbd:needbuf:300,+2", ppid="1", pgid="2"),
            process("3166 D smbd /mnt/Memory/samba4/sbin/smbd -F", ppid="457", sl="127", wchan="biowait"),
            process("3167 I smbd /mnt/Memory/samba4/sbin/smbd -F", ppid="457", sl="40", wchan="lockf"),
            process("200 S ACPd /usr/sbin/ACPd", ppid="1"),
            process("201 S ACPd /usr/sbin/ACPd", ppid="200", wchan="nanoslp"),
            process("559 S diskd /sbin/diskd -i lo0 -d local.", ppid="1"),
        ])

        self.assertEqual(reads, ["146", "559"])
        self.assertEqual(stuck_processes(output), [
            StuckProcess(900, "smbd", "needbuf", 300),
            StuckProcess(3166, "smbd", "biowait", 127, capped=True),
        ])
        self.assertEqual(manager_unnamed_stuck_count(output), 2)
        self.assertEqual(smbd_lock_waits(output, 30), [3167])
        self.assertEqual(acpd_stuck_children(output), [201])

    def test_a_manager_asleep_in_the_kernel_is_kept_unread_and_reported_when_stuck(self) -> None:
        from tests.device_ps import process

        output, reads = self.snapshot([
            process("146 D service service: role=manager", ppid="1", sl="127", wchan="vmmapbsy", stat_later="D"),
        ])

        self.assertEqual(reads, [])
        self.assertEqual(stuck_processes(output), [StuckProcess(146, "service", "vmmapbsy", 127, capped=True)])
        self.assertEqual(manager_unnamed_stuck_count(output), 0)


class UnreadableServiceTests(unittest.TestCase):
    def test_lists_live_service_processes_whose_command_lines_were_not_read(self) -> None:
        from timecapsulesmb.device.processes import UNREADABLE_COMMAND, service_role_processes, service_unreadable

        rows = "\n".join([
            ps_row(10, "service", UNREADABLE_COMMAND, stat="D"),
            ps_row(11, "service", UNREADABLE_COMMAND, stat="Z"),
            # diskd's command line is read too, and can be unreadable.
            ps_row(12, "diskd", UNREADABLE_COMMAND, stat="D"),
            ps_row(13, "service", "service: role=manager"),
            ps_row(14, "service", "(service)"),
            ps_row(15, "service"),
        ])

        self.assertTrue(service_unreadable(rows))
        # A zombie, another program, or a command line that was read: none.
        self.assertFalse(service_unreadable("\n".join(rows.splitlines()[1:])))
        # An unread row holds no role.
        self.assertEqual([process.pid for process in service_role_processes(rows, "manager")], [13])
