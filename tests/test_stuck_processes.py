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


class ReadLockWaitsTests(unittest.TestCase):
    """read_lock_waits reads lock_waiters_command output: the service's
    register lines for each smbd in lockf, then fstat's rows for them."""

    HEADER = "USER     CMD          PID   FD MOUNT       INUM MODE         SZ|DV R/W"
    FSTAT = [
        HEADER,
        "root     smbd        7652   wd /             2 drwxr-xr-x     512 r",
        "root     smbd        7652    9 /mnt/Locks     7 -rw-r--r--    8192 rw",
        "root     smbd        7652   12 /Volumes/dk2 48811 -rw-rw-rw-  921344 r",
        "root     smbd        7652   13* /mnt/Locks     5 -rw-r--r--   12288 rw",
        "root     smbd        7652   14* unix stream c2859340 <-> c280f500",
        HEADER,
        "root     smbd        1894   12 /mnt/Locks     9 -rw-r--r--    8192 rw",
        "root     smbd        1894  377 /Volumes/dk2 50001 -rw-rw-rw-    4096 rw",
    ]

    def read(self, *register_lines: str):
        from timecapsulesmb.device.processes import read_lock_waits

        return read_lock_waits("\n".join([*register_lines, *self.FSTAT]))

    def test_a_flock_on_a_data_file_names_the_descriptor_and_its_file(self) -> None:
        line = "7652 wchan=lockf spsr=60000010 r0=0000000c r1=00000002 r2=beef0000 r3=00000011 pc=0012abcd"
        waits = self.read(line)

        self.assertEqual(waits.calls, [{
            "pid": 7652, "call": "flock LOCK_EX", "fd": 12,
            "file": "/Volumes/dk2 48811 -rw-rw-rw- 921344 r", "pc": "0x0012abcd",
        }])
        # Only the descriptor the call names: not the cwd, the TDBs or the socket.
        self.assertEqual(waits.evidence.splitlines(), [line, self.HEADER, self.FSTAT[3]])

    def test_a_tdb_record_lock_wait_is_fcntl_f_setlkw(self) -> None:
        # fstat marks a close-on-exec descriptor with *; the same number in
        # another process is another file.
        waits = self.read(
            "7652 wchan=lockf spsr=20000010 r0=0000000d r1=00000009 r2=7fffd000 r3=00000000 pc=00034560",
            "1894 wchan=lockf spsr=20000010 r0=0000000c r1=00000009 r2=7fffd000 r3=00000000 pc=00034560",
        )

        self.assertEqual([(c["pid"], c["call"], c["fd"], c["file"]) for c in waits.calls], [
            (7652, "fcntl F_SETLKW", 13, "/mnt/Locks 5 -rw-r--r-- 12288 rw"),
            (1894, "fcntl F_SETLKW", 12, "/mnt/Locks 9 -rw-r--r-- 8192 rw"),
        ])
        self.assertEqual(waits.evidence.splitlines()[2:], [self.HEADER, self.FSTAT[7], self.FSTAT[4]])

    def test_a_shared_flock_is_named_as_one(self) -> None:
        waits = self.read("7652 wchan=lockf spsr=00000010 r0=0000000c r1=00000001 r2=00000000 r3=00000000 pc=00001000")

        self.assertEqual((waits.calls[0]["call"], waits.calls[0]["fd"]), ("flock LOCK_SH", 12))

    def test_asleep_inside_apples_xattr_call_takes_the_descriptor_from_r1(self) -> None:
        # syscall(377, fd, name, value): the kernel sleeps in lockf inside it.
        waits = self.read("7652 wchan=lockf spsr=00000010 r0=00000179 r1=0000000c r2=00012000 r3=00013000 pc=00001000")

        self.assertEqual((waits.calls[0]["call"], waits.calls[0]["fd"], waits.calls[0]["file"]),
                         ("fsetxattr", 12, "/Volumes/dk2 48811 -rw-rw-rw- 921344 r"))

    def test_registers_that_fit_both_readings_take_the_one_whose_descriptor_is_a_file(self) -> None:
        # r0=377 r1=9: fsetxattr on fd 9 or fcntl(377, F_SETLKW). 7652 has no
        # fd 377, so its reading is the xattr call on its TDB at fd 9.
        waits = self.read("7652 wchan=lockf spsr=00000010 r0=00000179 r1=00000009 r2=00012000 r3=00000000 pc=00001000")

        self.assertEqual((waits.calls[0]["call"], waits.calls[0]["fd"]), ("fsetxattr", 9))
        # The other way round: 1894 has a file at fd 377 and none at fd 2.
        waits = self.read("1894 wchan=lockf spsr=00000010 r0=00000179 r1=00000002 r2=00000000 r3=00000000 pc=00001000")

        self.assertEqual((waits.calls[0]["call"], waits.calls[0]["fd"], waits.calls[0]["file"]),
                         ("flock LOCK_EX", 377, "/Volumes/dk2 50001 -rw-rw-rw- 4096 rw"))

    def test_registers_that_fit_both_readings_with_files_for_both_or_neither_are_ambiguous(self) -> None:
        from timecapsulesmb.device.processes import read_lock_waits

        line = "1894 wchan=lockf spsr=00000010 r0=00000179 r1=00000009 r2=00000000 r3=00000000 pc=00001000"
        both = [
            self.HEADER,
            "root     smbd        1894    9 /mnt/Locks     7 -rw-r--r--    8192 rw",
            "root     smbd        1894  377 /Volumes/dk2 50001 -rw-rw-rw-    4096 rw",
        ]
        waits = read_lock_waits("\n".join([line, *both]))

        self.assertEqual(waits.calls[0]["call"], "ambiguous: fcntl F_SETLKW on fd 377 or fsetxattr on fd 9")
        self.assertIsNone(waits.calls[0]["fd"])
        self.assertIsNone(waits.calls[0]["file"])
        # Both candidates' rows stay as evidence.
        self.assertEqual(waits.evidence.splitlines(), [line, *both])
        # Neither descriptor open: still both readings, and no rows.
        waits = read_lock_waits(line)

        self.assertEqual(waits.calls[0]["call"], "ambiguous: fcntl F_SETLKW on fd 377 or fsetxattr on fd 9")
        self.assertEqual(waits.evidence, line)

    def test_a_process_no_longer_in_lockf_is_not_read_as_a_call(self) -> None:
        # Asleep in poll(fds, 2, ...) when read: r1 = 2 is not LOCK_EX here.
        waits = self.read("7652 wchan=poll spsr=00000010 r0=00020000 r1=00000002 r2=000003e8 r3=00000000 pc=00001000")

        self.assertEqual((waits.calls[0]["call"], waits.calls[0]["fd"], waits.calls[0]["file"]),
                         ("not waiting in lockf (poll)", None, None))
        self.assertEqual(waits.evidence.splitlines(), [
            "7652 wchan=poll spsr=00000010 r0=00020000 r1=00000002 r2=000003e8 r3=00000000 pc=00001000",
        ])

    def test_registers_not_saved_from_user_mode_are_not_read_as_a_call(self) -> None:
        # What a frame read with the other lane's layout gives: the pad word as spsr.
        waits = self.read("7652 wchan=lockf spsr=f111f111 r0=0000000c r1=00000002 r2=00000000 r3=00000000 pc=00001000")

        self.assertEqual((waits.calls[0]["call"], waits.calls[0]["fd"], waits.calls[0]["file"]),
                         ("not a user-mode frame", None, None))

    def test_an_unknown_call_keeps_its_arguments_and_names_no_file(self) -> None:
        waits = self.read("7652 wchan=lockf spsr=00000010 r0=0000000c r1=00000008 r2=7fffd000 r3=00000000 pc=00001000")

        self.assertEqual(waits.calls[0]["call"], "unrecognized (r0=0xc r1=0x8 r2=0x7fffd000)")
        self.assertIsNone(waits.calls[0]["fd"])
        self.assertIsNone(waits.calls[0]["file"])

    def test_a_descriptor_fstat_does_not_list_has_no_file(self) -> None:
        waits = self.read("7652 wchan=lockf spsr=00000010 r0=00000063 r1=00000002 r2=00000000 r3=00000000 pc=00001000")

        self.assertEqual((waits.calls[0]["fd"], waits.calls[0]["file"]), (99, None))
        # No row to keep, so no fstat header either.
        self.assertEqual(len(waits.evidence.splitlines()), 1)

    def test_the_services_per_process_messages_are_kept_and_give_no_call(self) -> None:
        waits = self.read("/dev/kmem: Permission denied", "9999: No such process", "1894: changed during the read")

        self.assertEqual(waits.calls, [])
        self.assertEqual(waits.evidence.splitlines(), [
            "/dev/kmem: Permission denied", "9999: No such process", "1894: changed during the read",
        ])

    def test_reads_apples_fstat_rows_with_the_file_name_appended(self) -> None:
        # Verbatim from a NetBSD 4 device: a test program blocked in
        # flock(3, LOCK_EX) on an HFS file another process held.
        from timecapsulesmb.device.processes import read_lock_waits

        output = "\n".join([
            "4532 wchan=lockf spsr=20000010 r0=00000003 r1=00000002 r2=00000018 r3=00025e38 pc=00008908",
            "USER     CMD          PID   FD MOUNT       INUM MODE         SZ|DV R/W",
            "root     tc-locktest  4532   wd /              2 drwxr-xr-x     512 r ",
            "root     tc-locktest  4532    0 /            951 crw-rw-rw-    null r ",
            "root     tc-locktest  4532    1 /Volumes/dk2  930138 -rw-r--r--      28 w   tc-lt.out",
            "root     tc-locktest  4532    3 /Volumes/dk2  930139 -rw-r--r--       0 rw  tc-locktest-file",
            "root     tc-locktest  4532    4* pipe 0xc90525e0 <- 0xc90524b4 r",
        ])

        waits = read_lock_waits(output)

        self.assertEqual(waits.calls, [{
            "pid": 4532, "call": "flock LOCK_EX", "fd": 3,
            "file": "/Volumes/dk2 930139 -rw-r--r-- 0 rw tc-locktest-file", "pc": "0x00008908",
        }])

    def test_registers_without_fstat_rows_still_give_the_call(self) -> None:
        # Doctor leaves fstat out while a process is stuck in the kernel.
        from timecapsulesmb.device.processes import read_lock_waits

        line = "7652 wchan=lockf spsr=00000010 r0=0000000c r1=00000002 r2=00000000 r3=00000000 pc=00001000"
        waits = read_lock_waits(line + "\n")

        self.assertEqual((waits.calls[0]["call"], waits.calls[0]["fd"], waits.calls[0]["file"]), ("flock LOCK_EX", 12, None))
        self.assertEqual(waits.evidence, line)
