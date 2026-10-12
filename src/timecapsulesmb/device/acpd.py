"""Apple ACPd health, read over SSH.

ACPd serves both network ACP (port 5009) and the device's own /usr/bin/acp.
It can hang (DETAIL.md, "Apple ACPd can hang during its own restart"), and
acp's failure text proves nothing on its own: a failed get prints
`### get 'X' failed: <<UNKNOWN FORMAT CONVERSION CODE %m>>` to stderr and
exits 0, for an unknown property too. syUT always exists, so a syUT read that
does not answer is what blames ACPd.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from timecapsulesmb.device.processes import PROCESS_SNAPSHOT_COMMAND, acpd_stuck_children
from timecapsulesmb.integrations.acp import DEVICE_ACP_PATH
from timecapsulesmb.transport.errors import TransportError
from timecapsulesmb.transport.ssh import SshConnection, run_ssh

# A healthy device answers in under 1 s; a hung ACPd does not answer for
# minutes, so one more try costs only a loaded NetBSD 4 device's slow moment.
ACPD_READ_DEADLINE_SECONDS = 15
ACPD_READ_ATTEMPTS = 2
# The device ends a read that ACPd leaves waiting, so a hung acp neither
# piles up on the device nor looks like a slow device: the shell answers with
# no output. Only an SSH session that itself hangs reaches the SSH timeout.
# acp's own stderr (its failed-get text) joins its stdout; the outer 2>/dev/null
# drops the shell's "Terminated" job notice, and the watchdog's sleep gets
# /dev/null, or it would hold the session open (checked on NetBSD 6 and 4 LE,
# 2026-10-12: answer 0.1-0.7 s, a 40 s sleep in acp's place ended at 15.1 s
# with no output and nothing left running).
ACPD_SYUT_COMMAND = (
    f"({DEVICE_ACP_PATH} -q syUT 2>&1 & p=$!; "
    f"(sleep {ACPD_READ_DEADLINE_SECONDS}; kill $p) </dev/null >/dev/null 2>&1 & w=$!; "
    "wait $p; kill $w) 2>/dev/null"
)
ACPD_SSH_TIMEOUT_SECONDS = ACPD_READ_DEADLINE_SECONDS + 15
# The kernel's statistics clock ticks since boot: it only grows while the
# kernel runs and starts again from zero at every boot, and it never reads
# the wall clock, which NTP steps by weeks just after boot (measured
# 2026-10-11: 100 ticks a second on both families, a constant offset from
# syUT). The process names show a shutdown under way.
RESTART_EVIDENCE_COMMAND = "/sbin/sysctl -n kern.cp_time && /bin/ps axww -o ucomm="
RESTART_EVIDENCE_TIMEOUT_SECONDS = 15
_HEX_LINE = re.compile(r"0x[0-9A-Fa-f]+")
_CP_TIME_FIELD = re.compile(r"\w+ = (\d+)")
_CP_TIME_FIELDS = 5


@dataclass(frozen=True)
class RestartEvidence:
    """What the running kernel shows about a restart, from one SSH read."""

    # kern.cp_time summed: statistics clock ticks since this kernel booted.
    cpu_ticks: int
    # A shutdown or reboot process is running: a restart is under way.
    shutting_down: bool


def acpd_broken_message(what: str, *, note: str = "", then: str = ", then try again") -> str:
    """Every caller's words for a broken ACPd: what failed, then the one fix."""
    return (
        f"Apple ACPd is broken: {what}, a bug in Apple's firmware. {note}"
        f"Unplug the device, wait 10 seconds, plug it back in and wait 2 minutes{then}."
    )


def acpd_answers(connection: SshConnection) -> bool:
    """True when the device's own acp reads syUT: one hex line, whatever the exit status.

    A read the device ended at its deadline, twice, is ACPd's fault. An SSH
    failure, a session that hangs included, is the device's or the network's,
    and is raised.
    """
    for _attempt in range(ACPD_READ_ATTEMPTS):
        output = (run_ssh(connection, ACPD_SYUT_COMMAND, check=False, timeout=ACPD_SSH_TIMEOUT_SECONDS).stdout or "").strip()
        if output:
            # The uptime, or acp's failed-get text: either way ACPd's answer.
            return _HEX_LINE.fullmatch(output) is not None
    return False


def parse_restart_evidence(output: str) -> RestartEvidence | None:
    """RESTART_EVIDENCE_COMMAND output; None unless cp_time has its five counters."""
    lines = output.splitlines()
    if not lines:
        return None
    counters = _CP_TIME_FIELD.findall(lines[0])
    if len(counters) != _CP_TIME_FIELDS:
        return None
    names = {line.strip() for line in lines[1:]}
    return RestartEvidence(sum(int(value) for value in counters), bool(names & {"shutdown", "reboot"}))


def read_restart_evidence(connection: SshConnection) -> RestartEvidence | None:
    """parse_restart_evidence of one read; None when SSH or the read fails."""
    try:
        proc = run_ssh(connection, RESTART_EVIDENCE_COMMAND, check=False, timeout=RESTART_EVIDENCE_TIMEOUT_SECONDS)
    except TransportError:
        return None
    return parse_restart_evidence(proc.stdout or "") if proc.returncode == 0 else None


def read_acpd_stuck_children(connection: SshConnection) -> list[int]:
    """acpd_stuck_children of one process listing; empty when ps does not answer."""
    try:
        proc = run_ssh(connection, PROCESS_SNAPSHOT_COMMAND, check=False, timeout=ACPD_SSH_TIMEOUT_SECONDS)
    except TransportError:
        return []
    return acpd_stuck_children(proc.stdout or "")
