"""Apple ACPd health read over SSH (timecapsulesmb.device.acpd and
device.processes.acpd_stuck_children). The process listings are rows from
real devices in telemetry (PROCESS_SNAPSHOT_COMMAND columns: pid ppid pgid
stat sl wchan ucomm command)."""
from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from timecapsulesmb.device import acpd
from timecapsulesmb.device.processes import acpd_stuck_children
from timecapsulesmb.transport.errors import SshCommandTimeout, SshError
from timecapsulesmb.transport.ssh import SshConnection

# tests/conftest.py replaces these on the module for every test; keep the real ones.
REAL_ACPD_ANSWERS = acpd.acpd_answers
REAL_READ_RESTART_EVIDENCE = acpd.read_restart_evidence
REAL_READ_ACPD_STUCK_CHILDREN = acpd.read_acpd_stuck_children

CONNECTION = SshConnection("root@10.0.0.2", "pw", "")

# A v3.4.0 TimeCapsule8,119 the morning its deploy reboot left ACPd torn down:
# ACPd's fork 352 had slept since boot.
STUCK_BEFORE_REBOOT = "\n".join((
    "    1     0     1 Ss    10 wait    init            init ",
    "  352   119     2 S      1 nanoslp ACPd            /sbin/ACPd ",
    "  467   119     2 S      0 select  sshd            /usr/sbin/sshd -D -e ",
    "  943   119     2 Z      0 -       mDNSResponder   (mDNSResponder)",
    "  976   119     2 S     10 kqueue  diskd           /sbin/diskd -i  -d local. ",
    "19793   467 19793 Ss     0 select  sshd            sshd: root@notty ",
    "  119     1     2 Sl     0 kqueue  ACPd            /sbin/ACPd ",
))
# The same device after the request: ACPd itself sleeps too, as ppid 1.
STUCK_AFTER_REQUEST = "\n".join((
    "    1     0     1 Is   127 wait    init      init ",
    "  352   119     2 S      1 nanoslp ACPd      /sbin/ACPd ",
    "  467   119     2 S      0 select  sshd      /usr/sbin/sshd -D -e ",
    "  119     1     2 Il     0 nanoslp ACPd      /sbin/ACPd ",
))
HEALTHY_NETBSD6 = "\n".join((
    "    1     0     1 Ss     7 wait    init          init ",
    "  119     1     2 Sl     1 kqueue  ACPd          /sbin/ACPd ",
    "  146     1     2 S      1 select  service       service: role=manager started=6 ",
    "  299   119     2 S      1 select  sshd          /usr/sbin/sshd -D -e ",
    "  707   119     2 Sl     1 kqueue  mDNSResponder /sbin/mDNSResponder -d ",
))
# NetBSD 4 runs ACPd -nofork; the ps | sed pipeline can list itself.
HEALTHY_NETBSD4 = "\n".join((
    "  190     1    15 Sa     8 kqread   ACPd            /sbin/ACPd -nofork ",
    "  289   190    15 S      0 select   sshd            /usr/sbin/sshd -D -e ",
    "28338 28660 28660 R      0 piperd   sh              sed -n /ACPd/p (sh)",
))


def completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["ssh"], returncode, stdout=stdout, stderr="")


class AcpdStuckChildrenTests(unittest.TestCase):
    def test_a_sleeping_fork_of_acpd_is_found_before_and_after_the_request(self) -> None:
        for listing in (STUCK_BEFORE_REBOOT, STUCK_AFTER_REQUEST):
            with self.subTest(listing=listing.splitlines()[1]):
                self.assertEqual(acpd_stuck_children(listing), [352])

    def test_netbsd4_names_the_same_sleep_nanoslee(self) -> None:
        listing = "\n".join((
            "  190     1    15 Sa     8 kqread   ACPd            /sbin/ACPd -nofork ",
            "  412   190    15 S      1 nanoslee ACPd            /sbin/ACPd -nofork ",
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
            "  119     1     2 Sl     1 kqueue  ACPd          /sbin/ACPd ",
            "  300   119     2 S      1 nanoslp sshd          /usr/sbin/sshd -D -e ",
            "  400   300     2 S      1 nanoslp ACPd          /sbin/ACPd ",
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

    def run_on_this_host(self, fake_acp: str) -> tuple[str, float]:
        """ACPD_SYUT_COMMAND in a local sh, with `fake_acp` as acp and a 1 s deadline."""
        with tempfile.TemporaryDirectory() as tmp:
            acp_path = Path(tmp) / "acp"
            acp_path.write_text(f"#!/bin/sh\n{fake_acp}\n")
            acp_path.chmod(0o755)
            command = acpd.ACPD_SYUT_COMMAND.replace("/usr/bin/acp", str(acp_path)).replace(
                f"sleep {acpd.ACPD_READ_DEADLINE_SECONDS};", "sleep 1;",
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
        output, seconds = self.run_on_this_host("exec sleep 15")
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


class RestartEvidenceTests(unittest.TestCase):
    # sysctl -n kern.cp_time, then ps -o ucomm=, from the LAN devices on 2026-10-11.
    NETBSD6 = "user = 11971, nice = 0, sys = 23734, intr = 17393, idle = 1380088\ninit\nACPd\nsshd\n"
    NETBSD4 = "user = 2788, nice = 0, sys = 11307, intr = 7549, idle = 1378180\nACPd\nsshd\n"

    def test_the_tick_count_is_the_sum_of_the_five_counters(self) -> None:
        self.assertEqual(acpd.parse_restart_evidence(self.NETBSD6), acpd.RestartEvidence(1433186, False))
        self.assertEqual(acpd.parse_restart_evidence(self.NETBSD4), acpd.RestartEvidence(1399824, False))

    def test_a_listed_shutdown_or_reboot_is_a_restart_under_way(self) -> None:
        for name in ("shutdown", "reboot", "  shutdown  "):
            with self.subTest(name=name):
                evidence = acpd.parse_restart_evidence(self.NETBSD4 + name + "\n")
                self.assertTrue(evidence.shutting_down)
        self.assertFalse(acpd.parse_restart_evidence(self.NETBSD4 + "shutdownd\n").shutting_down)

    def test_anything_but_five_counters_is_unreadable(self) -> None:
        for output in ("", "sysctl: unknown oid 'kern.cp_time'\n", "user = 1, nice = 0, sys = 2\nACPd\n"):
            with self.subTest(output=output):
                self.assertIsNone(acpd.parse_restart_evidence(output))

    def read(self, outcome) -> acpd.RestartEvidence | None:
        run = mock.Mock(side_effect=outcome) if isinstance(outcome, Exception) else mock.Mock(return_value=outcome)
        with mock.patch("timecapsulesmb.device.acpd.run_ssh", run):
            return REAL_READ_RESTART_EVIDENCE(CONNECTION)

    def test_one_read_gives_the_evidence(self) -> None:
        self.assertEqual(self.read(completed(self.NETBSD6)), acpd.RestartEvidence(1433186, False))

    def test_a_failed_read_or_ssh_gives_none(self) -> None:
        for outcome in (completed(self.NETBSD6, 1), SshError("Connection refused"), SshCommandTimeout("timed out")):
            with self.subTest(outcome=outcome):
                self.assertIsNone(self.read(outcome))


class ReadAcpdStuckChildrenTests(unittest.TestCase):
    def test_one_listing_is_read(self) -> None:
        with mock.patch("timecapsulesmb.device.acpd.run_ssh", return_value=completed(STUCK_BEFORE_REBOOT)):
            self.assertEqual(REAL_READ_ACPD_STUCK_CHILDREN(CONNECTION), [352])

    def test_a_listing_that_does_not_answer_finds_nothing(self) -> None:
        with mock.patch("timecapsulesmb.device.acpd.run_ssh", side_effect=SshCommandTimeout("timed out")):
            self.assertEqual(REAL_READ_ACPD_STUCK_CHILDREN(CONNECTION), [])


if __name__ == "__main__":
    unittest.main()
