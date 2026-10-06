from __future__ import annotations

import unittest

from timecapsulesmb.device.processes import StuckProcess, stuck_processes

# PROCESS_SNAPSHOT_COMMAND rows (pid ppid pgid stat sl wchan ucomm command),
# shaped like the NetBSD 6 and NetBSD 4 devices print them.
SMBD_PARENT = "  457   146   457 I       20 select   smbd     /mnt/Memory/samba4/sbin/smbd -F --no-process-group -s /mnt/Memory/samba4/etc/smb.conf"
NETBSD6_KERNEL = "    0     0     0 DKl      0 uvm      system   [system]"
NETBSD4_KERNEL_THREAD = "    5     0     0 DK     127 amc6821c amc6821c_thread [amc6821c_thread]"
ZOMBIE = "   97   119     2 Z        0 -        wcifsnd  (wcifsnd)"
RUNNING = " 3166   457   457 R        0 -        smbd     /mnt/Memory/samba4/sbin/smbd -F --no-process-group -s /mnt/Memory/samba4/etc/smb.conf"


def row(pid: int, state: str, sleep: str, wchan: str, name: str) -> str:
    return f"{pid:5d}   457   457 {state:<6} {sleep:>4} {wchan:<8} {name:<8} /mnt/Memory/samba4/sbin/{name} -F"


class StuckProcessesTests(unittest.TestCase):
    def test_reports_process_asleep_uninterruptibly_for_the_threshold(self) -> None:
        output = "\n".join([SMBD_PARENT, row(3166, "D", "60", "biowait", "smbd"), RUNNING])

        self.assertEqual(stuck_processes(output), [StuckProcess(3166, "smbd", "biowait", 60)])

    def test_ignores_uninterruptible_sleep_shorter_than_threshold(self) -> None:
        # A process doing slow disk I/O wakes between requests, and every wake
        # resets the kernel's sleep time.
        self.assertEqual(stuck_processes(row(3166, "D", "59", "biowait", "smbd")), [])

    def test_ignores_interruptible_sleeps_however_long(self) -> None:
        output = "\n".join([row(10, "I", "127", "select", "smbd"), row(11, "S", "127", "kqueue", "sshd")])

        self.assertEqual(stuck_processes(output), [])

    def test_ignores_kernel_threads_which_always_sleep_uninterruptibly(self) -> None:
        self.assertEqual(stuck_processes("\n".join([NETBSD6_KERNEL, NETBSD4_KERNEL_THREAD])), [])

    def test_ignores_running_and_zombie_processes(self) -> None:
        self.assertEqual(stuck_processes("\n".join([RUNNING, ZOMBIE])), [])

    def test_keeps_state_modifiers_after_d(self) -> None:
        # Session leaders and foreground processes add s or + after the state.
        output = "\n".join([row(20, "Ds", "90", "vnlock", "sh"), row(21, "D+", "127", "tstile", "tail")])

        self.assertEqual(
            stuck_processes(output),
            [StuckProcess(20, "sh", "vnlock", 90), StuckProcess(21, "tail", "tstile", 127)],
        )

    def test_reports_every_stuck_process_in_listing_order(self) -> None:
        output = "\n".join(
            [
                row(30, "D", "127", "biowait", "smbd"),
                SMBD_PARENT,
                row(31, "D", "75", "needbuf", "service"),
            ]
        )

        self.assertEqual(
            [process.pid for process in stuck_processes(output)],
            [30, 31],
        )

    def test_skips_short_and_malformed_rows(self) -> None:
        output = "\n".join(["", "garbage", "  x   1   1 D 127 biowait smbd smbd", "  40 1 1 D many biowait smbd smbd"])

        self.assertEqual(stuck_processes(output), [])

    def test_describe_marks_sleep_at_ps_display_cap(self) -> None:
        self.assertEqual(
            StuckProcess(3166, "smbd", "biowait", 127).describe(),
            "smbd (pid 3166) waiting on biowait for 127+ s",
        )
        self.assertEqual(
            StuckProcess(3166, "smbd", "biowait", 75).describe(),
            "smbd (pid 3166) waiting on biowait for 75 s",
        )


if __name__ == "__main__":
    unittest.main()
