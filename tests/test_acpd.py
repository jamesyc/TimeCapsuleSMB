"""Apple ACPd health read over SSH (timecapsulesmb.device.acpd). The process
listings are rows from real devices in telemetry, in PROCESS_SNAPSHOT_COMMAND's
layout (pid ppid pgid stat sl wchan time ucomm, then the command line)."""
from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from timecapsulesmb.device import acpd
from timecapsulesmb.device.probe import AirportAcpReading
from timecapsulesmb.integrations import acp
from tests.device_ps import FakePs, process
from timecapsulesmb.device.acpd import RestartMarker, RestartVerdict, acpd_stuck_children
from timecapsulesmb.device.errors import DeviceRestartingError
from timecapsulesmb.device.processes import PS_LISTING_END, parse_ps_rows
from timecapsulesmb.transport.errors import SshCommandTimeout, SshError
from timecapsulesmb.transport.ssh import SshConnection

# tests/conftest.py replaces these on the module for every test; keep the real ones.
REAL_ACPD_ANSWERS = acpd.acpd_answers
REAL_READ_PROCESSES = acpd.read_processes
REAL_MARK_RESTART = acpd.mark_restart
REAL_RESTART_VERDICT = acpd.restart_verdict
REAL_ACPD_ANSWERS_NETWORK = acpd.acpd_answers_network
REAL_NETWORK_SILENT_DETAILS = acpd.network_silent_details

CONNECTION = SshConnection("root@10.0.0.2", "pw", "")
# This computer's network ACP read: answered, rejected, unanswered, or not
# made for want of a password.
READ_ANSWERED = AirportAcpReading(password_matches=True)
READ_REJECTED = AirportAcpReading(password_matches=False)
READ_UNANSWERED = AirportAcpReading(password_matches=None, error="Could not connect to ACP on 10.0.0.2:5009: timed out")
READ_NO_PASSWORD = AirportAcpReading(password_matches=None)

# A v3.4.0 TimeCapsule8,119 the morning its deploy reboot left ACPd torn down:
# ACPd's fork 352 had slept since boot.
STUCK_BEFORE_REBOOT = "\n".join((
    "    1 0 1 Ss 10 wait 0:00.00 init",
    "  352 119 2 S 1 nanoslp 0:00.00 ACPd",
    "  467 119 2 S 0 select 0:00.00 sshd",
    "  943 119 2 Z 0 - 0:00.00 mDNSResponder",
    "  976 119 2 S 10 kqueue 0:00.00 diskd           /sbin/diskd -i  -d local. ",
    "19793 467 19793 Ss 0 select 0:00.00 sshd",
    "  119 1 2 Sl 0 kqueue 0:00.00 ACPd",
))
# The same device after the request: ACPd itself sleeps too, as ppid 1.
STUCK_AFTER_REQUEST = "\n".join((
    "    1 0 1 Is 127 wait 0:00.00 init",
    "  352 119 2 S 1 nanoslp 0:00.00 ACPd",
    "  467 119 2 S 0 select 0:00.00 sshd",
    "  119 1 2 Il 0 nanoslp 0:00.00 ACPd",
))
HEALTHY_NETBSD6 = "\n".join((
    "    1 0 1 Ss 7 wait 0:00.00 init",
    "  119 1 2 Sl 1 kqueue 0:00.00 ACPd",
    "  146 1 2 S 1 select 0:00.00 service       service: role=manager started=6 ",
    "  299 119 2 S 1 select 0:00.00 sshd",
    "  707 119 2 Sl 1 kqueue 0:00.00 mDNSResponder",
))
# NetBSD 4 runs ACPd -nofork; the ps | sed pipeline can list itself.
HEALTHY_NETBSD4 = "\n".join((
    "  190 1 15 Sa 8 kqread 0:00.00 ACPd",
    "  289 190 15 S 0 select 0:00.00 sshd",
    "28338 28660 28660 R 0 piperd 0:00.00 sh",
))

# A v3.6.0 TimeCapsule6,116 20 minutes after its deploy started Samba at
# uptime 24: ACPd is gone, its children have init as their parent, and the
# diskd, wcifsnd, wcifsfs and afpserver it ran are gone with it.
QUIT_NETBSD4 = "\n".join((
    "    1 0 1 Is 127 wait 0:00.03 init",
    "  107 1 15 S 0 select 0:00.32 sshd",
    "  174 1 15 Sa 2 sawait 0:00.24 mDNSResponder",
    "  300 1 571 S 0 select 0:04.81 service service: role=manager started=24",
    "  436 1 15 Ia 127 select 0:00.41 printd",
    PS_LISTING_END,
))
# The same listing cut short: it never says ACPd is gone.
QUIT_NETBSD4_CUT_SHORT = QUIT_NETBSD4.removesuffix(f"\n{PS_LISTING_END}")
# ACPd killed on a NetBSD 4 LE unit whose Samba had started: smbd is the
# manager's child, not ACPd's, so it kept running.
QUIT_NETBSD4_SERVING = QUIT_NETBSD4.replace(
    PS_LISTING_END, f"  320 300 320 Ss 1 select 0:01.20 smbd\n  341 320 320 S 0 select 0:00.40 smbd\n{PS_LISTING_END}",
)
# Only a zombie smbd left: Samba is not running.
QUIT_NETBSD4_ZOMBIE_SMBD = QUIT_NETBSD4.replace(PS_LISTING_END, f"  320 300 320 Z 0 - 0:00.00 smbd\n{PS_LISTING_END}")
HEALTHY_NETBSD4_FINISHED = f"{HEALTHY_NETBSD4}\n{PS_LISTING_END}"


def completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["ssh"], returncode, stdout=stdout, stderr="")


# A marker past the grace on a kernel whose ACPd tore itself down.
HUNG = RestartVerdict("hung", 75.0, {"restart_verdict": "hung", "restart_marker_age_sec": 75.0})


class AcpdStuckChildrenTests(unittest.TestCase):
    def test_a_sleeping_fork_of_acpd_is_found_before_and_after_the_request(self) -> None:
        for listing in (STUCK_BEFORE_REBOOT, STUCK_AFTER_REQUEST):
            with self.subTest(listing=listing.splitlines()[1]):
                self.assertEqual(acpd_stuck_children(listing), [352])

    def test_netbsd4_names_the_same_sleep_nanoslee(self) -> None:
        listing = "\n".join((
            "  190 1 15 Sa 8 kqread 0:00.00 ACPd",
            "  412 190 15 S 1 nanoslee 0:00.00 ACPd",
        ))
        self.assertEqual(acpd_stuck_children(listing), [412])

    def test_healthy_devices_have_none(self) -> None:
        for listing in (HEALTHY_NETBSD6, HEALTHY_NETBSD4, ""):
            with self.subTest(listing=listing[:40]):
                self.assertEqual(acpd_stuck_children(listing), [])

    def test_a_fork_on_its_way_to_exec_is_not_stuck(self) -> None:
        listing = HEALTHY_NETBSD6 + "\n  801   119     2 R      0 -       ACPd          /sbin/ACPd "
        self.assertEqual(acpd_stuck_children(listing), [])

    def test_only_acpd_s_own_forks_count(self) -> None:
        # sshd sleeps under ACPd, and another program may be named ACPd.
        listing = "\n".join((
            "  119 1 2 Sl 1 kqueue 0:00.00 ACPd",
            "  300 119 2 S 1 nanoslp 0:00.00 sshd",
            "  400 300 2 S 1 nanoslp 0:00.00 ACPd",
        ))
        self.assertEqual(acpd_stuck_children(listing), [])


class AcpdAnswersTests(unittest.TestCase):
    def answers(self, *outcomes) -> tuple[bool, mock.Mock]:
        run = mock.Mock(side_effect=list(outcomes))
        with mock.patch("timecapsulesmb.device.acpd.run_ssh", run):
            result = REAL_ACPD_ANSWERS(CONNECTION)
        for call in run.call_args_list:
            self.assertEqual(call, mock.call(CONNECTION, acpd.ACPD_SYUT_COMMAND, check=False, timeout=30))
        return result, run

    def run_on_this_host(self, fake_acp: str, deadline: int = acpd.ACPD_READ_DEADLINE_SECONDS) -> tuple[str, float]:
        """ACPD_SYUT_COMMAND in a local sh, with `fake_acp` as acp. A fake that
        answers keeps the real deadline: on a loaded Mac, starting a script
        written a moment ago can take most of a second, and a shorter
        deadline would kill it before it prints."""
        with tempfile.TemporaryDirectory() as tmp:
            acp_path = Path(tmp) / "acp"
            acp_path.write_text(f"#!/bin/sh\n{fake_acp}\n")
            acp_path.chmod(0o755)
            command = acpd.ACPD_SYUT_COMMAND.replace("/usr/bin/acp", str(acp_path)).replace(
                f"sleep {acpd.ACPD_READ_DEADLINE_SECONDS};", f"sleep {deadline};",
            )
            started = time.monotonic()
            proc = subprocess.run(["/bin/sh", "-c", command], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20)
            return proc.stdout, time.monotonic() - started

    def test_the_device_side_command_answers_fails_or_ends_a_hung_read(self) -> None:
        # The answer and acp's own failure text both come back, stderr included.
        self.assertEqual(self.run_on_this_host("echo 0x00015A4B")[0], "0x00015A4B\n")
        failed = "echo \"### get 'syUT' failed: <<UNKNOWN FORMAT CONVERSION CODE %m>>\" >&2"
        self.assertIn("### get 'syUT' failed", self.run_on_this_host(failed)[0])
        # A read ACPd never answers ends at the deadline with no output: no
        # job notice, and the watchdog does not hold the session open.
        # exec: acp is one process, so the kill closes its output.
        output, seconds = self.run_on_this_host("exec sleep 15", deadline=1)
        self.assertEqual(output, "")
        self.assertLess(seconds, 5)
        self.assertLess(acpd.ACPD_READ_DEADLINE_SECONDS, acpd.ACPD_SSH_TIMEOUT_SECONDS)

    def test_one_hex_line_is_an_answer_whatever_the_exit_status(self) -> None:
        for returncode in (0, 22):
            with self.subTest(returncode=returncode):
                self.assertTrue(self.answers(completed("0x00015A4B\n", returncode))[0])

    def test_a_failed_get_is_not_an_answer_and_is_not_retried(self) -> None:
        for stdout in (
            "### get 'syUT' failed: <<UNKNOWN FORMAT CONVERSION CODE %m>>\n",
            "0x00015A4B\n### get 'syUT' failed: <<UNKNOWN FORMAT CONVERSION CODE %m>>\n",
            "sh: /usr/bin/acp: not found\n",
        ):
            with self.subTest(stdout=stdout):
                result, run = self.answers(completed(stdout))
                self.assertFalse(result)
                self.assertEqual(run.call_count, 1)

    def test_one_read_the_device_ended_is_retried(self) -> None:
        # The deadline killed acp: the shell answered, with nothing to say.
        result, run = self.answers(completed(""), completed("0x00015A4B\n"))
        self.assertTrue(result)
        self.assertEqual(run.call_count, 2)

    def test_a_read_the_device_ended_twice_is_acpd_s_fault(self) -> None:
        result, run = self.answers(completed(""), completed("\n", returncode=143))
        self.assertFalse(result)
        self.assertEqual(run.call_count, 2)

    def test_an_ssh_failure_is_raised_not_blamed_on_acpd(self) -> None:
        # A session that hangs is the device or the network: the device's own
        # deadline would have answered first.
        for error in (SshError("Connection refused"), SshCommandTimeout("timed out")):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    self.answers(error)

    def test_an_ssh_failure_on_the_retry_is_raised_too(self) -> None:
        with self.assertRaises(SshCommandTimeout):
            self.answers(completed(""), SshCommandTimeout("timed out"))


# The LAN NetBSD 4 device's last listing before its kernel stopped in a real
# restart: ACPd's teardown has stopped mDNSResponder, left
# ifconfig as a zombie, and a dd of the raw disk waits in the kernel beside
# the kernel's own threads.
TEARDOWN_DONE_NETBSD4 = "\n".join((
    "    1 0 1 Is 0 wait 0:00.03 init",
    "   10 0 0 DK 0 stdflash 0:00.00 stdflash0",
    "  251 1 251 Sa 0 kqread 0:01.20 ACPd",
    "  569 251 251 S 0 select 0:00.10 sshd",
    " 2038 1 2038 DL 0 physio2 0:00.40 dd",
    " 3507 251 251 ZW 0 - 0:00.00 ifconfig",
))
# The same device 2 s earlier, its teardown not yet at mDNSResponder.
TEARDOWN_UNDER_WAY_NETBSD4 = TEARDOWN_DONE_NETBSD4 + "\n  552 251 251 Sa 0 kqread 0:00.30 mDNSResponder"
TICKS_AT_MARK = "user = 521, nice = 0, sys = 1537, intr = 275, idle = 40408"


def ticks_later(seconds: float) -> str:
    """TICKS_AT_MARK `seconds` later, all of them idle."""
    return f"user = 521, nice = 0, sys = 1537, intr = 275, idle = {40408 + round(seconds * 100)}"


def marker_output(
    marked: str, now: str | None, listing: str = TEARDOWN_UNDER_WAY_NETBSD4, *,
    written: str = "Nov  3 01:40:12 2026", clock: str = "Nov  3 01:40:20 2026",
) -> str:
    """_RESTART_MARKER_READ's output for a marker holding `marked`, written
    and read at those device-clock times."""
    lines = [f"marked: {marked}"] + ([] if now is None else [f"now: {now}"])
    lines += [f"written: -rw-r--r--  1 root  wheel  57 {written} /mnt/Memory/acp-reboot", f"clock: {clock}"]
    return "\n".join(lines) + "\n" + listing + "\n"


class RestartMarkerTests(unittest.TestCase):
    """The marker written before a restart request, with the kernel's tick
    count, and read by every command that judges the last restart."""

    # The LAN NetBSD 4 device's table, in PS_COLUMNS' layout.
    NETBSD4_ROWS = [
        process("1 Ss init", ppid="0"),
        process("190 S ACPd /sbin/ACPd -nofork", wchan="kqread"),
        process("289 S sshd /usr/sbin/sshd -D -e", ppid="190", wchan="select"),
    ]

    def test_the_device_side_commands_write_and_read_the_marker(self) -> None:
        writes: list[str] = []
        with mock.patch("timecapsulesmb.device.acpd.run_ssh", side_effect=lambda _c, command, **_k: writes.append(command) or completed("")):
            self.assertTrue(REAL_MARK_RESTART(CONNECTION))
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker"
            ticks = Path(tmp) / "ticks"
            sysctl = Path(tmp) / "sysctl"
            sysctl.write_text(f"#!/bin/sh\ncat {ticks}\n")
            sysctl.chmod(0o755)
            # The fake ps fails a listing that asks for command lines, whose
            # read can wait on a busy process.
            fake = FakePs(Path(tmp), self.NETBSD4_ROWS)

            def on_this_host(_connection: SshConnection, command: str, **_kwargs) -> subprocess.CompletedProcess[str]:
                command = command.replace(acpd.RESTART_MARKER, str(marker))
                command = command.replace(acpd.RESTART_TICKS_COMMAND, str(sysctl))
                # GNU ls reads -T as a tab width; `date -r FILE` prints a file's
                # time in the devices' `ls -lT` layout on macOS and Linux alike.
                command = command.replace(f"ls -lT {marker}", f"date -r {marker} '+%b %e %H:%M:%S %Y'")
                return subprocess.run(
                    ["/bin/sh", "-c", fake.install(command)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20,
                )

            with mock.patch("timecapsulesmb.device.acpd.run_ssh", side_effect=on_this_host):
                self.assertIsNone(acpd.read_restart_marker(CONNECTION))
                self.assertEqual(fake.calls, [])
                # A tick read that fails writes no marker.
                self.assertNotEqual(on_this_host(CONNECTION, writes[0]).returncode, 0)
                self.assertFalse(marker.exists())
                ticks.write_text(TICKS_AT_MARK + "\n")
                self.assertEqual(on_this_host(CONNECTION, writes[0]).returncode, 0)
                self.assertEqual(marker.read_text(), TICKS_AT_MARK + "\n")
                ticks.write_text(ticks_later(42.5) + "\n")
                found = acpd.read_restart_marker(CONNECTION)
                # The tick count unread now: the marker's time by the clock.
                ticks.write_text("")
                untimed = acpd.read_restart_marker(CONNECTION)
                # An older version's empty marker: the same.
                marker.write_text("")
                legacy = acpd.read_restart_marker(CONNECTION)
            self.assertEqual((found.age_seconds, found.age_clock), (42.5, "ticks"))
            for clocked in (untimed, legacy):
                self.assertEqual(clocked.age_clock, "device_clock")
                self.assertLess(clocked.age_seconds, 60)
            self.assertEqual([p.ucomm for p in parse_ps_rows(found.listing)], ["init", "ACPd", "sshd"])
            self.assertEqual(len(fake.calls), 3)
            self.assertEqual(fake.command_reads(), [])
        self.assertTrue(acpd.RESTART_MARKER.startswith("/mnt/Memory/"))

    def read(self, output: str) -> RestartMarker | None:
        with mock.patch("timecapsulesmb.device.acpd.run_ssh", return_value=completed(output)):
            return acpd.read_restart_marker(CONNECTION)

    def test_the_age_comes_from_the_kernel_s_ticks(self) -> None:
        self.assertEqual(self.read(marker_output(TICKS_AT_MARK, ticks_later(61))).age_seconds, 61.0)
        # Every counter counts, not idle alone.
        busy = "user = 621, nice = 0, sys = 1637, intr = 375, idle = 40408"
        self.assertEqual(self.read(marker_output(TICKS_AT_MARK, busy)).age_seconds, 3.0)

    def test_a_32_bit_counter_that_wrapped_still_counts(self) -> None:
        # NetBSD 4 prints a long counter past 2**31 as negative.
        marked = "user = 521, nice = 0, sys = 1537, intr = 275, idle = 2147483000"
        now = "user = 521, nice = 0, sys = 1537, intr = 275, idle = -2147483296"
        self.assertEqual(self.read(marker_output(marked, now)).age_seconds, 10.0)

    def test_the_device_clock_ages_a_marker_the_ticks_cannot(self) -> None:
        cases = {
            "written empty by an older version": marker_output("", ticks_later(5)),
            # Unknown, its age would be judged as an old marker's.
            "tick count unread now": marker_output(TICKS_AT_MARK, None),
            "tick count garbled now": marker_output(TICKS_AT_MARK, "sysctl: kern.cp_time: not found"),
        }
        for name, output in cases.items():
            with self.subTest(name):
                marker = self.read(output)
                self.assertEqual((marker.age_seconds, marker.age_clock), (8.0, "device_clock"))
        old = marker_output("", None, written="Oct 31 23:59:50 2026", clock="Nov  1 00:01:00 2026")
        self.assertEqual(self.read(old).age_seconds, 70.0)

    def test_a_marker_whose_age_cannot_be_told_is_still_a_marker(self) -> None:
        cases = {
            "written empty, clock unread": marker_output("", ticks_later(5), written="", clock=""),
            "written empty, clock stepped back": marker_output(
                "", ticks_later(5), written="Nov  3 01:40:12 2026", clock="Oct 12 09:00:00 2026",
            ),
            "garbage, clock unread": marker_output("x", ticks_later(5), written="", clock=""),
            "four counters, clock unread": marker_output(
                "user = 1, nice = 0, sys = 2, intr = 3", ticks_later(5), written="", clock="",
            ),
        }
        for name, output in cases.items():
            with self.subTest(name):
                marker = self.read(output)
                self.assertIsNotNone(marker)
                self.assertEqual((marker.age_seconds, marker.age_clock), (None, None))

    def test_no_marker(self) -> None:
        self.assertIsNone(self.read(""))

    def test_an_ssh_failure_reading_the_marker_is_raised(self) -> None:
        for error in (SshError("Connection refused"), SshCommandTimeout("timed out")):
            with self.subTest(error=type(error).__name__):
                with mock.patch("timecapsulesmb.device.acpd.run_ssh", side_effect=error):
                    with self.assertRaises(type(error)):
                        acpd.read_restart_marker(CONNECTION)

    def test_the_teardown_ran_once_mdnsresponder_is_gone(self) -> None:
        cases = {
            "under way": (TEARDOWN_UNDER_WAY_NETBSD4, False),
            "done": (TEARDOWN_DONE_NETBSD4, True),
            "only a zombie left": (TEARDOWN_DONE_NETBSD4 + "\n  552 251 251 Z 0 - 0:00.00 mDNSResponder", True),
            "ps failed": ("", None),
        }
        for name, (listing, done) in cases.items():
            with self.subTest(name):
                self.assertEqual(RestartMarker(70.0, listing).teardown_done, done)

    def test_the_evidence_names_acpd_and_what_waits_in_the_kernel(self) -> None:
        self.assertEqual(acpd.restart_evidence(RestartMarker(70.04, TEARDOWN_DONE_NETBSD4, "ticks")), {
            "restart_marker_age_sec": 70.0,
            "restart_marker_clock": "ticks",
            "restart_teardown_done": True,
            "restart_acpd_processes": ["251/1/kqread"],
            # The kernel's own flash thread is left out.
            "restart_waiting_processes": ["dd/physio2"],
        })

    def test_a_marker_that_could_not_be_written_says_so(self) -> None:
        for outcome in (completed("sh: cannot create /mnt/Memory/x: No space left on device\n", 2), SshError("Connection reset"), SshCommandTimeout("timed out")):
            with self.subTest(outcome=outcome):
                run = mock.Mock(side_effect=outcome) if isinstance(outcome, Exception) else mock.Mock(return_value=outcome)
                with mock.patch("timecapsulesmb.device.acpd.run_ssh", run):
                    self.assertFalse(REAL_MARK_RESTART(CONNECTION))


class RestartVerdictTests(unittest.TestCase):
    """What the marker says about the last restart asked for in this boot."""

    def verdict(self, marker: RestartMarker | None, *, answers: bool = True) -> RestartVerdict:
        with mock.patch("timecapsulesmb.device.acpd.read_restart_marker", return_value=marker), \
                mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=answers) as syut:
            verdict = REAL_RESTART_VERDICT(CONNECTION)
        self.syut = syut
        return verdict

    def test_the_verdicts(self) -> None:
        old = acpd.RESTART_HANG_SECONDS
        cases = (
            # name, marker, syUT answers, verdict, syUT read
            ("no marker", None, True, "none", False),
            ("young, teardown done", RestartMarker(old - 1, TEARDOWN_DONE_NETBSD4), False, "restarting", False),
            ("young, teardown under way", RestartMarker(5.0, TEARDOWN_UNDER_WAY_NETBSD4), False, "restarting", False),
            ("old, teardown done", RestartMarker(old, TEARDOWN_DONE_NETBSD4), True, "hung", False),
            ("old, teardown not done, syUT silent", RestartMarker(old, TEARDOWN_UNDER_WAY_NETBSD4), False, "hung", True),
            ("old, ACPd healthy", RestartMarker(old + 30, TEARDOWN_UNDER_WAY_NETBSD4), True, "not_taken", True),
            ("old, ps failed, syUT silent", RestartMarker(old, ""), False, "hung", True),
            ("old, ps failed, ACPd answers", RestartMarker(old, ""), True, "not_taken", True),
            ("age unknown, teardown done", RestartMarker(None, TEARDOWN_DONE_NETBSD4), True, "hung", False),
            ("age unknown, ACPd healthy", RestartMarker(None, TEARDOWN_UNDER_WAY_NETBSD4), True, "not_taken", True),
        )
        for name, marker, answers, state, read in cases:
            with self.subTest(name):
                verdict = self.verdict(marker, answers=answers)
                self.assertEqual(verdict.state, state)
                self.assertEqual(self.syut.called, read)
                self.assertEqual(verdict.acpd_answers, answers if read else None)
                if marker is None:
                    self.assertEqual(verdict.details, {})
                else:
                    self.assertEqual(verdict.details["restart_verdict"], state)
                    self.assertEqual(verdict.age_seconds, marker.age_seconds)

    def test_the_restart_message(self) -> None:
        self.assertEqual(
            acpd.restarting_message(12.4),
            "The device is restarting: TimeCapsuleSMB asked it to restart 12 seconds ago.",
        )
        self.assertEqual(
            acpd.restarting_message(0.6),
            "The device is restarting: TimeCapsuleSMB asked it to restart 1 second ago.",
        )


class ReadProcessesTests(unittest.TestCase):
    def test_one_listing_is_read(self) -> None:
        with mock.patch("timecapsulesmb.device.acpd.run_ssh", return_value=completed(STUCK_BEFORE_REBOOT)):
            self.assertEqual(REAL_READ_PROCESSES(CONNECTION), STUCK_BEFORE_REBOOT)

    def test_a_listing_that_does_not_answer_is_empty(self) -> None:
        with mock.patch("timecapsulesmb.device.acpd.run_ssh", side_effect=SshCommandTimeout("timed out")):
            self.assertEqual(REAL_READ_PROCESSES(CONNECTION), "")


class AcpdAnswersNetworkTests(unittest.TestCase):
    """ACPd's TCP listener, read through an SSH forward to the device's own
    127.0.0.1:5009, so the network between here and the device plays no part."""

    def read(self, outcome):
        get = mock.Mock(side_effect=outcome) if isinstance(outcome, BaseException) else mock.Mock(return_value=outcome)
        with mock.patch("timecapsulesmb.device.acpd.find_free_local_port", return_value=61234), \
                mock.patch("timecapsulesmb.device.acpd.ssh_local_forward", return_value=nullcontext()) as forward, \
                mock.patch("timecapsulesmb.device.acpd.acp.get_properties", get):
            answered = REAL_ACPD_ANSWERS_NETWORK(CONNECTION)
        return answered, forward, get

    def test_a_listener_that_answers_syut_answers(self) -> None:
        answered, forward, get = self.read({"syUT": b"\x00\x01\x3b\x2f"})
        self.assertTrue(answered)
        forward.assert_called_once_with(CONNECTION, local_port=61234, remote_host="127.0.0.1", remote_port=acp.ACP_PORT)
        # The local read's deadline: a busy ACPd answers within it.
        get.assert_called_once_with("127.0.0.1", "pw", ("syUT",), timeout=acpd.ACPD_READ_DEADLINE_SECONDS, port=61234)

    def test_a_rejected_password_or_a_malformed_reply_is_an_answer(self) -> None:
        for reply in (
            acp.ACPAuthError("ACP command failed with error_code -0x10"),
            acp.ACPProtocolError("ACP response body checksum mismatch: got 0x1, expected 0x2"),
        ):
            with self.subTest(reply=type(reply).__name__):
                answered, _forward, _get = self.read(reply)
                self.assertTrue(answered)

    def test_a_read_the_listener_leaves_unanswered_is_silence(self) -> None:
        for failure in (
            acp.ACPConnectionError("ACP receive failed: timed out"),
            # A port nothing listens on, through the forward (NetBSD 6 and 4).
            acp.ACPConnectionError("ACP receive failed: [Errno 54] Connection reset by peer"),
            acp.ACPConnectionError("ACP connection closed while reading 128 bytes"),
        ):
            with self.subTest(failure=str(failure)):
                answered, _forward, _get = self.read(failure)
                self.assertFalse(answered)

    def test_a_forward_that_fails_is_raised_not_blamed_on_acpd(self) -> None:
        failure = SshError("Timed out waiting for ssh tunnel to become ready")
        with mock.patch("timecapsulesmb.device.acpd.find_free_local_port", return_value=61234), \
                mock.patch("timecapsulesmb.device.acpd.ssh_local_forward", side_effect=failure), \
                mock.patch("timecapsulesmb.device.acpd.acp.get_properties") as get:
            with self.assertRaises(SshError) as raised:
                REAL_ACPD_ANSWERS_NETWORK(CONNECTION)
        self.assertIs(raised.exception, failure)
        get.assert_not_called()


class NetworkSilentDetailsTests(unittest.TestCase):
    """What tells a full file table, which also leaves this computer's
    connection queued unanswered, from ACPd's own silence."""

    LIMITS = "kern.sbmax = 262144\nkern.maxfiles = 1024\nkern.maxproc = 84\n"

    def test_the_limits_and_the_last_table_full_messages_are_kept(self) -> None:
        messages = "\n".join(f"file: table is full - increase kern.maxfiles or MAXFILES ({n})" for n in range(7))
        with mock.patch("timecapsulesmb.device.acpd.read_kern_limits_conn", return_value=self.LIMITS) as limits, \
                mock.patch("timecapsulesmb.device.acpd.run_ssh", return_value=completed(messages + "\n")) as run:
            details = REAL_NETWORK_SILENT_DETAILS(CONNECTION)
        limits.assert_called_once_with(CONNECTION)
        # /sbin is not on the device's SSH PATH.
        self.assertTrue(run.call_args.args[1].startswith("/sbin/dmesg | "))
        self.assertEqual(details["remote_kern_limits"], self.LIMITS.strip())
        self.assertEqual(
            details["remote_kernel_table_full"],
            [f"file: table is full - increase kern.maxfiles or MAXFILES ({n})" for n in range(2, 7)],
        )

    def test_a_kernel_that_never_filled_a_table_has_none(self) -> None:
        with mock.patch("timecapsulesmb.device.acpd.read_kern_limits_conn", return_value=self.LIMITS), \
                mock.patch("timecapsulesmb.device.acpd.run_ssh", return_value=completed("")):
            details = REAL_NETWORK_SILENT_DETAILS(CONNECTION)
        self.assertEqual(details["remote_kernel_table_full"], [])

    def test_an_ssh_failure_leaves_no_evidence_instead_of_hiding_the_silence(self) -> None:
        for failing in ("read_kern_limits_conn", "run_ssh"):
            with self.subTest(read=failing):
                reads = {
                    "read_kern_limits_conn": mock.Mock(return_value=self.LIMITS),
                    "run_ssh": mock.Mock(return_value=completed("")),
                    failing: mock.Mock(side_effect=SshCommandTimeout("Timed out waiting for ssh command to finish")),
                }
                with mock.patch.multiple("timecapsulesmb.device.acpd", **reads):
                    self.assertEqual(REAL_NETWORK_SILENT_DETAILS(CONNECTION), {})


class AcpdProblemsTests(unittest.TestCase):
    """The one list of ACPd problems that require_acpd_ready and doctor share."""

    def problems(
        self,
        *,
        restart: RestartVerdict = RestartVerdict("none"),
        answers: bool = True,
        listing: str = HEALTHY_NETBSD6,
        ps_output: str | None = None,
        network_reading: AirportAcpReading | None = None,
        network_answers: bool = True,
        listings: list[str] | None = None,
    ):
        """acpd_problems with these readings. `listings` are the listings read
        in turn (the recheck's too); otherwise every read gives `listing`."""
        reads = {"side_effect": list(listings)} if listings is not None else {"return_value": listing}
        with mock.patch("timecapsulesmb.device.acpd.restart_verdict", return_value=restart) as marker, \
                mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=answers) as syut, \
                mock.patch("timecapsulesmb.device.acpd.read_processes", **reads) as ps, \
                mock.patch("timecapsulesmb.device.acpd.pause") as pause, \
                mock.patch("timecapsulesmb.device.acpd.acpd_answers_network", return_value=network_answers) as network:
            problems = acpd.acpd_problems(CONNECTION, ps_output, network_reading=network_reading)
        marker.assert_called_once_with(CONNECTION)
        self.syut = syut
        self.network = network
        self.pause = pause
        return problems, ps

    def test_a_healthy_acpd_has_none(self) -> None:
        for listing in (HEALTHY_NETBSD6, HEALTHY_NETBSD4, ""):
            with self.subTest(listing=listing[:20]):
                self.assertEqual(self.problems(listing=listing)[0], [])

    def test_each_problem_is_named(self) -> None:
        problems, _ps = self.problems(restart=HUNG)
        self.assertEqual([problem.code for problem in problems], ["restart_never_happened"])
        self.assertIn("did not restart", problems[0].what)
        problems, _ps = self.problems(answers=False)
        self.assertEqual([problem.code for problem in problems], ["syut_silent"])
        problems, _ps = self.problems(listing=STUCK_BEFORE_REBOOT)
        self.assertEqual([problem.code for problem in problems], ["stuck_child"])
        self.assertEqual(problems[0].details, {"acpd_stuck_children": [352]})
        self.assertIn("pid 352", problems[0].what)

    def test_an_acpd_that_quit_is_reported_alone(self) -> None:
        # Its syUT and listener would only fail the same way, with the same fix.
        problems, ps = self.problems(answers=False, listing=QUIT_NETBSD4, network_reading=READ_UNANSWERED,
                                     network_answers=False)
        self.assertEqual([problem.code for problem in problems], ["quit"])
        self.assertEqual(problems[0].what, acpd.ACPD_QUIT_WHAT)
        self.assertEqual(problems[0].note, acpd.ACPD_QUIT_NOTE)
        self.assertTrue(problems[0].sharing_off)
        self.assertEqual(problems[0].details, {"acpd_quit_smbd_running": False})
        # Read, and read again a pause later: both listings lack ACPd.
        self.assertEqual(ps.call_args_list, [mock.call(CONNECTION), mock.call(CONNECTION)])
        self.pause.assert_called_once_with(acpd.ACPD_RECHECK_SECONDS)
        self.syut.assert_not_called()
        self.network.assert_not_called()
        self.assertEqual(
            acpd.ACPD_QUIT_MESSAGE,
            "Apple ACPd is broken: the AirPort service on this device has quit, a bug in Apple's firmware. "
            "File sharing stays off until the device restarts.",
        )

    def test_an_acpd_that_quit_says_whether_samba_still_serves(self) -> None:
        # Samba started before ACPd quit keeps serving: the second listing,
        # the one that confirms the quit, says which.
        cases = (
            ("smbd in both", [QUIT_NETBSD4_SERVING, QUIT_NETBSD4_SERVING], True),
            ("smbd only in the second", [QUIT_NETBSD4, QUIT_NETBSD4_SERVING], True),
            ("smbd only in the first", [QUIT_NETBSD4_SERVING, QUIT_NETBSD4], False),
            ("only a zombie smbd", [QUIT_NETBSD4, QUIT_NETBSD4_ZOMBIE_SMBD], False),
        )
        for name, listings, serving in cases:
            with self.subTest(name):
                problems, _ps = self.problems(listings=listings)
                self.assertEqual([problem.code for problem in problems], ["quit"])
                self.assertEqual(problems[0].sharing_off, not serving)
                self.assertEqual(problems[0].details, {"acpd_quit_smbd_running": serving})
                self.assertEqual(problems[0].note, acpd.ACPD_QUIT_SERVING_NOTE if serving else acpd.ACPD_QUIT_NOTE)
        self.assertEqual(
            acpd.acpd_broken_message(acpd.ACPD_QUIT_WHAT, note=acpd.ACPD_QUIT_SERVING_NOTE),
            "Apple ACPd is broken: the AirPort service on this device has quit, a bug in Apple's firmware. "
            "Sharing may work now, but until the device is unplugged it cannot be updated or restarted over the "
            "network, and AirPort Utility cannot reach it.",
        )

    def test_an_acpd_that_quit_is_found_in_a_listing_already_read(self) -> None:
        # Doctor's own listing is the first; only the recheck is read here.
        problems, ps = self.problems(ps_output=QUIT_NETBSD4, listing=QUIT_NETBSD4)
        ps.assert_called_once_with(CONNECTION)
        self.assertEqual([problem.code for problem in problems], ["quit"])

    def test_one_listing_without_acpd_is_not_a_quit(self) -> None:
        # The recheck shows ACPd, or cannot say: the other checks go on as usual.
        for name, second in (("ACPd back", HEALTHY_NETBSD4_FINISHED), ("cut short", QUIT_NETBSD4_CUT_SHORT), ("unread", "")):
            with self.subTest(second=name):
                problems, ps = self.problems(listings=[QUIT_NETBSD4, second])
                self.assertEqual(problems, [])
                self.assertEqual(ps.call_count, 2)
                self.pause.assert_called_once_with(acpd.ACPD_RECHECK_SECONDS)
                self.syut.assert_called_once_with(CONNECTION)

    def test_a_listing_with_acpd_is_not_read_again(self) -> None:
        problems, ps = self.problems(listing=HEALTHY_NETBSD4_FINISHED)
        self.assertEqual(problems, [])
        ps.assert_called_once_with(CONNECTION)
        self.pause.assert_not_called()

    def test_only_a_listing_that_finished_says_acpd_quit(self) -> None:
        for name, listing in (("cut short", QUIT_NETBSD4_CUT_SHORT), ("empty", ""), ("ACPd present", HEALTHY_NETBSD4_FINISHED)):
            with self.subTest(listing=name):
                problems, _ps = self.problems(listing=listing)
                self.assertEqual(problems, [])
                self.syut.assert_called_once_with(CONNECTION)

    def test_a_restart_that_never_happened_comes_before_an_acpd_that_quit(self) -> None:
        # The marker is the restart ACPd took and never ran: its own fix. Its
        # listing is read once, for Samba, and never rechecked for a quit.
        problems, ps = self.problems(restart=HUNG, listing=QUIT_NETBSD4)
        self.assertEqual([problem.code for problem in problems], ["restart_never_happened"])
        ps.assert_called_once_with(CONNECTION)
        self.pause.assert_not_called()

    def test_a_restart_that_never_happened_is_reported_alone(self) -> None:
        # ACPd tore itself down: its silent syUT and stuck fork are the same
        # problem, with the same fix, so they are not read or reported again.
        problems, ps = self.problems(restart=HUNG, answers=False, listing=STUCK_AFTER_REQUEST)
        self.assertEqual([problem.code for problem in problems], ["restart_never_happened"])
        self.syut.assert_not_called()
        ps.assert_called_once_with(CONNECTION)

    def test_a_restart_that_never_happened_says_whether_sharing_is_off(self) -> None:
        # Only a finished listing without smbd says Samba is off; one that
        # cannot say leaves it as a problem to report, not the whole verdict.
        cases = (
            ("no smbd", QUIT_NETBSD4, True),
            ("smbd serving", QUIT_NETBSD4_SERVING, False),
            ("listing cut short", QUIT_NETBSD4_CUT_SHORT, False),
            ("ps unread", "", False),
        )
        for name, listing, sharing_off in cases:
            with self.subTest(name):
                problems, _ps = self.problems(restart=HUNG, listing=listing)
                self.assertEqual(problems[0].sharing_off, sharing_off)
        # Doctor's listing is used as it is.
        problems, ps = self.problems(restart=HUNG, ps_output=QUIT_NETBSD4)
        ps.assert_not_called()
        self.assertTrue(problems[0].sharing_off)

    def test_only_a_gone_acpd_says_sharing_is_off(self) -> None:
        for name, kwargs in (("syUT silent", {"answers": False}), ("stuck fork", {"listing": STUCK_BEFORE_REBOOT})):
            with self.subTest(name):
                problems, _ps = self.problems(**kwargs)
                self.assertEqual([problem.sharing_off for problem in problems], [False])

    def test_the_other_problems_are_listed_together(self) -> None:
        problems, _ps = self.problems(answers=False, listing=STUCK_AFTER_REQUEST)
        self.assertEqual([problem.code for problem in problems], ["syut_silent", "stuck_child"])
        self.syut.assert_called_once_with(CONNECTION)

    def test_a_listing_already_read_is_not_read_again(self) -> None:
        problems, ps = self.problems(ps_output=STUCK_BEFORE_REBOOT)
        ps.assert_not_called()
        self.assertEqual([problem.code for problem in problems], ["stuck_child"])
        problems, ps = self.problems(ps_output="", listing=STUCK_BEFORE_REBOOT)
        ps.assert_not_called()
        self.assertEqual(problems, [])

    def test_an_ssh_failure_is_raised_not_blamed_on_acpd(self) -> None:
        for failing in ("restart_verdict", "acpd_answers", "acpd_answers_network"):
            with self.subTest(read=failing):
                reads = {
                    "restart_verdict": mock.Mock(return_value=RestartVerdict("none")),
                    "acpd_answers": mock.Mock(return_value=True),
                    failing: mock.Mock(side_effect=SshError("Connection reset")),
                }
                with mock.patch.multiple("timecapsulesmb.device.acpd", **reads):
                    with self.assertRaises(SshError):
                        acpd.acpd_problems(CONNECTION, HEALTHY_NETBSD6, network_reading=READ_UNANSWERED)

    def test_a_failed_marker_read_is_raised_before_any_listing_is_read(self) -> None:
        # A listing says nothing when SSH fails, after its own timeout: the
        # marker read, which raises, goes first.
        with mock.patch("timecapsulesmb.device.acpd.restart_verdict",
                        side_effect=SshCommandTimeout("timed out")), \
                mock.patch("timecapsulesmb.device.acpd.read_processes") as ps:
            with self.assertRaises(SshCommandTimeout):
                acpd.acpd_problems(CONNECTION)
        ps.assert_not_called()

    def test_a_restart_under_way_is_raised_before_anything_else_is_read(self) -> None:
        # Nothing is broken, and the device is about to go down.
        restarting = RestartVerdict("restarting", 7.0, {"restart_verdict": "restarting", "restart_marker_age_sec": 7.0})
        with mock.patch("timecapsulesmb.device.acpd.restart_verdict", return_value=restarting), \
                mock.patch("timecapsulesmb.device.acpd.acpd_answers") as syut, \
                mock.patch("timecapsulesmb.device.acpd.read_processes") as ps:
            with self.assertRaises(DeviceRestartingError) as raised:
                acpd.acpd_problems(CONNECTION)
        self.assertEqual(raised.exception.code, "device_restarting")
        self.assertEqual(raised.exception.message, acpd.restarting_message(7.0))
        self.assertEqual(raised.exception.details, restarting.details)
        syut.assert_not_called()
        ps.assert_not_called()

    def test_a_request_never_taken_is_no_problem_and_its_syut_answer_is_reused(self) -> None:
        not_taken = RestartVerdict("not_taken", 90.0, {"restart_verdict": "not_taken"}, acpd_answers=True)
        problems, ps = self.problems(restart=not_taken, listing=HEALTHY_NETBSD4_FINISHED)
        self.assertEqual(problems, [])
        self.syut.assert_not_called()
        ps.assert_called_once_with(CONNECTION)

    def test_a_restart_that_never_happened_carries_the_marker_s_evidence(self) -> None:
        problems, _ps = self.problems(restart=HUNG)
        self.assertEqual(problems[0].details, HUNG.details)

    def test_a_listener_silent_through_ssh_after_an_unanswered_network_read(self) -> None:
        # The device's own acp answered, this computer's read did not, and
        # SSH to the same address worked.
        evidence = {"remote_kern_limits": "kern.maxfiles = 1024", "remote_kernel_table_full": []}
        with mock.patch("timecapsulesmb.device.acpd.network_silent_details", return_value=evidence) as details:
            problems, _ps = self.problems(network_reading=READ_UNANSWERED, network_answers=False)
        self.assertEqual([problem.code for problem in problems], ["network_silent"])
        self.assertIn("stopped answering the network", problems[0].what)
        self.assertIn("while this lasts", problems[0].note)
        self.assertEqual(problems[0].details, evidence)
        self.network.assert_called_once_with(CONNECTION)
        details.assert_called_once_with(CONNECTION)

    def test_a_listener_that_answers_is_not_asked_for_evidence(self) -> None:
        with mock.patch("timecapsulesmb.device.acpd.network_silent_details") as details:
            problems, _ps = self.problems(network_reading=READ_UNANSWERED)
        self.assertEqual(problems, [])
        details.assert_not_called()

    def test_a_listener_that_answers_through_ssh_is_no_problem(self) -> None:
        # ACPd is fine; the path from this computer is what is blocked.
        problems, _ps = self.problems(network_reading=READ_UNANSWERED)
        self.assertEqual(problems, [])
        self.network.assert_called_once_with(CONNECTION)

    def test_the_listener_is_read_only_after_an_unanswered_network_read(self) -> None:
        for name, reading in (
            ("no read", None),
            ("answered", READ_ANSWERED),
            ("password rejected", READ_REJECTED),
            ("no password to read with", READ_NO_PASSWORD),
        ):
            with self.subTest(reading=name):
                problems, _ps = self.problems(network_reading=reading, network_answers=False)
                self.assertEqual(problems, [])
                self.network.assert_not_called()

    def test_an_acpd_already_silent_to_its_own_acp_is_not_read_through_ssh(self) -> None:
        problems, _ps = self.problems(answers=False, network_reading=READ_UNANSWERED, network_answers=False)
        self.assertEqual([problem.code for problem in problems], ["syut_silent"])
        self.network.assert_not_called()

    def test_a_restart_that_never_happened_is_not_read_through_ssh(self) -> None:
        problems, _ps = self.problems(restart=HUNG, network_reading=READ_UNANSWERED, network_answers=False)
        self.assertEqual([problem.code for problem in problems], ["restart_never_happened"])
        self.network.assert_not_called()

    def test_a_silent_listener_follows_a_stuck_fork(self) -> None:
        problems, _ps = self.problems(listing=STUCK_BEFORE_REBOOT, network_reading=READ_UNANSWERED, network_answers=False)
        self.assertEqual([problem.code for problem in problems], ["stuck_child", "network_silent"])


if __name__ == "__main__":
    unittest.main()
