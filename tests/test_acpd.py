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
from tests.device_ps import FakePs, process, ps_row
from timecapsulesmb.device.acpd import acpd_stuck_children
from timecapsulesmb.device.processes import PS_LISTING_END, parse_ps_rows
from timecapsulesmb.transport.errors import SshCommandTimeout, SshError
from timecapsulesmb.transport.ssh import SshConnection

# tests/conftest.py replaces these on the module for every test; keep the real ones.
REAL_ACPD_ANSWERS = acpd.acpd_answers
REAL_READ_PROCESSES = acpd.read_processes
REAL_MARK_RESTART = acpd.mark_restart
REAL_RESTART_NEVER_HAPPENED = acpd.restart_never_happened
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


class RestartMarkerTests(unittest.TestCase):
    """The marker written before a restart request, and read while the wait
    and later commands look for a restart that never happened."""

    # The LAN NetBSD 4 device's table, in PS_COLUMNS' layout.
    NETBSD4_ROWS = [
        process("1 Ss init", ppid="0"),
        process("190 S ACPd /sbin/ACPd -nofork", wchan="kqread"),
        process("289 S sshd /usr/sbin/sshd -D -e", ppid="190", wchan="select"),
    ]

    def rows(self, *extra: str) -> str:
        """NETBSD4_ROWS as STUCK_PROCESS_PS_COMMAND prints them, plus processes named extra."""
        names = [(int(p["pid"]), str(p["ucomm"])) for p in self.NETBSD4_ROWS]
        names += [(900 + index, name) for index, name in enumerate(extra)]
        return "".join(ps_row(pid, name) + "\n" for pid, name in names)

    def test_the_device_side_commands_write_and_find_the_marker(self) -> None:
        # The device has no touch: the write is the shell's own redirection.
        writes: list[str] = []
        with mock.patch("timecapsulesmb.device.acpd.run_ssh", side_effect=lambda _c, command, **_k: writes.append(command) or completed("")):
            self.assertTrue(REAL_MARK_RESTART(CONNECTION))
        reads: list[str] = []
        with mock.patch("timecapsulesmb.device.acpd.run_ssh", side_effect=lambda _c, command, **_k: reads.append(command) or completed("", 1)):
            REAL_RESTART_NEVER_HAPPENED(CONNECTION)
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker"
            # The fake ps fails a listing that asks for command lines, whose
            # read can wait on a busy process.
            fake = FakePs(Path(tmp), self.NETBSD4_ROWS)

            def on_this_host(command: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["/bin/sh", "-c", fake.install(command.replace(acpd.RESTART_MARKER, str(marker)))],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20,
                )

            absent = on_this_host(reads[0])
            self.assertNotEqual(absent.returncode, 0)
            self.assertEqual((absent.stdout, fake.calls), ("", []))
            self.assertEqual(on_this_host(writes[0]).returncode, 0)
            self.assertTrue(marker.exists())
            present = on_this_host(reads[0])
            self.assertEqual(present.returncode, 0, present.stdout)
            self.assertEqual([p.ucomm for p in parse_ps_rows(present.stdout)], ["init", "ACPd", "sshd"])
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(fake.command_reads(), [])
        self.assertTrue(acpd.RESTART_MARKER.startswith("/mnt/Memory/"))

    def never_happened(self, outcome) -> bool:
        run = mock.Mock(side_effect=outcome) if isinstance(outcome, Exception) else mock.Mock(return_value=outcome)
        with mock.patch("timecapsulesmb.device.acpd.run_ssh", run):
            return REAL_RESTART_NEVER_HAPPENED(CONNECTION)

    def test_a_marker_that_outlived_its_request_with_no_shutdown_running_is_a_restart_that_never_happened(self) -> None:
        self.assertTrue(self.never_happened(completed(self.rows())))
        self.assertTrue(self.never_happened(completed(self.rows("shutdownd"))))

    def test_no_marker_is_a_restart_that_happened(self) -> None:
        self.assertFalse(self.never_happened(completed("", 1)))

    def test_a_slow_shutdown_still_running_is_a_restart_under_way(self) -> None:
        for name in ("shutdown", "reboot"):
            with self.subTest(name=name):
                self.assertFalse(self.never_happened(completed(self.rows(name))))

    def test_an_ssh_failure_reading_the_marker_is_raised(self) -> None:
        for error in (SshError("Connection refused"), SshCommandTimeout("timed out")):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    self.never_happened(error)

    def test_a_marker_that_could_not_be_written_says_so(self) -> None:
        for outcome in (completed("sh: cannot create /mnt/Memory/x: No space left on device\n", 2), SshError("Connection reset"), SshCommandTimeout("timed out")):
            with self.subTest(outcome=outcome):
                run = mock.Mock(side_effect=outcome) if isinstance(outcome, Exception) else mock.Mock(return_value=outcome)
                with mock.patch("timecapsulesmb.device.acpd.run_ssh", run):
                    self.assertFalse(REAL_MARK_RESTART(CONNECTION))


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
        never_happened: bool = False,
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
        with mock.patch("timecapsulesmb.device.acpd.restart_never_happened", return_value=never_happened) as marker, \
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
        problems, _ps = self.problems(never_happened=True)
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
        problems, ps = self.problems(never_happened=True, listing=QUIT_NETBSD4)
        self.assertEqual([problem.code for problem in problems], ["restart_never_happened"])
        ps.assert_called_once_with(CONNECTION)
        self.pause.assert_not_called()

    def test_a_restart_that_never_happened_is_reported_alone(self) -> None:
        # ACPd tore itself down: its silent syUT and stuck fork are the same
        # problem, with the same fix, so they are not read or reported again.
        problems, ps = self.problems(never_happened=True, answers=False, listing=STUCK_AFTER_REQUEST)
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
                problems, _ps = self.problems(never_happened=True, listing=listing)
                self.assertEqual(problems[0].sharing_off, sharing_off)
        # Doctor's listing is used as it is.
        problems, ps = self.problems(never_happened=True, ps_output=QUIT_NETBSD4)
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
        for failing in ("restart_never_happened", "acpd_answers", "acpd_answers_network"):
            with self.subTest(read=failing):
                reads = {
                    "restart_never_happened": mock.Mock(return_value=False),
                    "acpd_answers": mock.Mock(return_value=True),
                    failing: mock.Mock(side_effect=SshError("Connection reset")),
                }
                with mock.patch.multiple("timecapsulesmb.device.acpd", **reads):
                    with self.assertRaises(SshError):
                        acpd.acpd_problems(CONNECTION, HEALTHY_NETBSD6, network_reading=READ_UNANSWERED)

    def test_a_failed_marker_read_is_raised_before_any_listing_is_read(self) -> None:
        # A listing says nothing when SSH fails, after its own timeout: the
        # marker read, which raises, goes first.
        with mock.patch("timecapsulesmb.device.acpd.restart_never_happened",
                        side_effect=SshCommandTimeout("timed out")), \
                mock.patch("timecapsulesmb.device.acpd.read_processes") as ps:
            with self.assertRaises(SshCommandTimeout):
                acpd.acpd_problems(CONNECTION)
        ps.assert_not_called()

    def test_a_listing_taken_while_the_device_shuts_down_is_no_quit(self) -> None:
        # A real restart's shutdown stops ACPd before sshd: that is no quit,
        # and the other checks go on as usual.
        shutting_down = QUIT_NETBSD4.replace(PS_LISTING_END, f"  901 1 901 S 0 wait 0:00.01 shutdown\n{PS_LISTING_END}")
        problems, ps = self.problems(listing=shutting_down)
        self.assertEqual(problems, [])
        ps.assert_called_once_with(CONNECTION)
        self.pause.assert_not_called()

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
        problems, _ps = self.problems(never_happened=True, network_reading=READ_UNANSWERED, network_answers=False)
        self.assertEqual([problem.code for problem in problems], ["restart_never_happened"])
        self.network.assert_not_called()

    def test_a_silent_listener_follows_a_stuck_fork(self) -> None:
        problems, _ps = self.problems(listing=STUCK_BEFORE_REBOOT, network_reading=READ_UNANSWERED, network_answers=False)
        self.assertEqual([problem.code for problem in problems], ["stuck_child", "network_silent"])


if __name__ == "__main__":
    unittest.main()
