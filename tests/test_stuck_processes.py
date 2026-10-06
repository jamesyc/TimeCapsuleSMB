from __future__ import annotations

import unittest

from timecapsulesmb.device.processes import StuckProcess, manager_unnamed_stuck_count, stuck_processes

# PROCESS_SNAPSHOT_COMMAND rows (pid ppid pgid stat sl wchan ucomm command),
# shaped like the NetBSD 6 and NetBSD 4 devices print them.
SMBD_PARENT = "  457   146   457 I       20 select   smbd     /mnt/Memory/samba4/sbin/smbd -F --no-process-group -s /mnt/Memory/samba4/etc/smb.conf"
NETBSD6_KERNEL = "    0     0     0 DKl      0 uvm      system   [system]"
NETBSD4_KERNEL_THREAD = "    5     0     0 DK     127 amc6821c amc6821c_thread [amc6821c_thread]"
ZOMBIE = "   97   119     2 Z        0 -        wcifsnd  (wcifsnd)"
RUNNING = " 3166   457   457 R        0 -        smbd     /mnt/Memory/samba4/sbin/smbd -F --no-process-group -s /mnt/Memory/samba4/etc/smb.conf"


def manager_row(words: str) -> str:
    return f"  146     1     2 S        1 select   service  service: role=manager {words}".rstrip()


def row(pid: int, state: str, sleep: str, wchan: str, name: str) -> str:
    return f"{pid:5d}   457   457 {state:<6} {sleep:>4} {wchan:<8} {name:<8} /mnt/Memory/samba4/sbin/{name} -F"


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
        output = "\n".join(["", "garbage", "  x   1   1 D 127 biowait smbd smbd", "  40 1 1 D many biowait smbd smbd"])

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
        discovery = "  911   146   911 S        1 select   service  service: role=discovery stuck=1:x:y:99"

        self.assertEqual(stuck_processes(discovery), [])


if __name__ == "__main__":
    unittest.main()
