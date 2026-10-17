"""Apple ACPd health, read over SSH, and the marker that shows a restart
ACPd took but never ran (RESTART_MARKER).

ACPd serves both network ACP (port 5009) and the device's own /usr/bin/acp.
It can hang (DETAIL.md, "Apple ACPd can hang during its own restart"), and
acp's failure text proves nothing on its own: a failed get prints
`### get 'X' failed: <<UNKNOWN FORMAT CONVERSION CODE %m>>` to stderr and
exits 0, for an unknown property too. syUT always exists, so a syUT read that
does not answer is what blames ACPd.

The device's acp reaches ACPd over a Unix socket (/var/run/ACPIPC on
NetBSD 6, /tmp/ACPIPC on NetBSD 4), never TCP 5009, so its answer says
nothing about the listener this computer uses. That listener is read through
SSH (acpd_answers_network).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from timecapsulesmb.device.probe import AirportAcpReading, read_kern_limits_conn
from timecapsulesmb.device.processes import PROCESS_SNAPSHOT_COMMAND, STUCK_PROCESS_PS_COMMAND, parse_ps_rows
from timecapsulesmb.integrations import acp
from timecapsulesmb.integrations.acp import ACP_PORT, DEVICE_ACP_PATH
from timecapsulesmb.transport.errors import TransportError
from timecapsulesmb.transport.local import find_free_local_port
from timecapsulesmb.transport.ssh import SshConnection, run_ssh, ssh_local_forward

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
_HEX_LINE = re.compile(r"0x[0-9A-Fa-f]+")
# Written over SSH just before every ACP restart request that SSH can see. A
# real restart empties Apple's RAM disk, so a marker that is still there proves
# the kernel never restarted: ACPd took the request and stopped without
# running shutdown. Nothing on the device reads it.
RESTART_MARKER = "/mnt/Memory/acp-reboot"
RESTART_MARKER_TIMEOUT_SECONDS = 15
# A real restart runs /sbin/shutdown, whose last step stops sshd, so SSH can
# still see the marker while a slow shutdown runs. The process table is read
# without any command line, so it never waits (see UNREADABLE_COMMAND).
_RESTART_MARKER_READ = f"[ -e {RESTART_MARKER} ] && {STUCK_PROCESS_PS_COMMAND}"
# ps's wait channel for nanosleep: NetBSD 6 names it nanoslp, and NetBSD 4's
# ps cuts nanosleep to eight characters.
NANOSLEEP_WCHANS = frozenset({"nanoslp", "nanoslee"})


@dataclass(frozen=True)
class AcpdProblem:
    """One reason ACPd cannot take a restart or serve ACP."""

    code: str
    # Words for acpd_broken_message.
    what: str
    note: str = ""
    details: dict[str, object] = field(default_factory=dict)


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


def acpd_answers_network(connection: SshConnection) -> bool:
    """True when ACPd's TCP listener answers a syUT read through SSH.

    The read goes to the device's own 127.0.0.1:5009, so the network between
    this computer and the device plays no part. Any reply is an answer, a
    rejected password or a malformed one too. It gets the local read's
    deadline: a busy ACPd answers within it, and a listener that stopped
    answering stays silent for hours. Raises TransportError when the forward
    cannot be set up.
    """
    port = find_free_local_port()
    with ssh_local_forward(connection, local_port=port, remote_host="127.0.0.1", remote_port=ACP_PORT):
        try:
            acp.get_properties("127.0.0.1", connection.password, ("syUT",),
                               timeout=ACPD_READ_DEADLINE_SECONDS, port=port)
        except acp.ACPConnectionError:
            # No reply in time, or the device refused the connection and the forward reset it.
            return False
        except acp.ACPError:
            pass  # A rejected password or a malformed reply is still an answer.
    return True


# Both kernels' accept() takes a file before it takes a connection off the
# queue (sys_accept on NetBSD 4, do_sys_accept on NetBSD 6), so a full file
# table leaves this computer's connection queued and unanswered, as a stalled
# ACPd does. smbd fills that table when several Macs connect, which is why
# the manager raises kern.maxfiles.
TABLE_FULL_COMMAND = "/sbin/dmesg | sed -n '/table is full/p'"
TABLE_FULL_LINES = 5


def network_silent_details(connection: SshConnection) -> dict[str, object]:
    """The kernel's limits and its last "table is full" messages, which tell
    a full file table from ACPd's own silence. Empty when SSH fails: the
    silence is reported either way."""
    try:
        limits = read_kern_limits_conn(connection)
        full = run_ssh(connection, TABLE_FULL_COMMAND, check=False, timeout=ACPD_SSH_TIMEOUT_SECONDS).stdout or ""
    except TransportError:
        return {}
    return {
        "remote_kern_limits": limits.strip() or "(empty)",
        "remote_kernel_table_full": full.splitlines()[-TABLE_FULL_LINES:],
    }


def mark_restart(connection: SshConnection) -> bool:
    """Write RESTART_MARKER; False when it could not be written."""
    try:
        proc = run_ssh(connection, f": > {RESTART_MARKER}", check=False, timeout=RESTART_MARKER_TIMEOUT_SECONDS)
    except TransportError:
        return False
    return proc.returncode == 0


def restart_never_happened(connection: SshConnection) -> bool:
    """True when RESTART_MARKER outlived its restart request and no shutdown runs.

    False also when the marker is there but ps fails: that can only delay a
    hang being reported, never report one that did not happen. Raises
    TransportError.
    """
    proc = run_ssh(connection, _RESTART_MARKER_READ, check=False, timeout=RESTART_MARKER_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        return False
    return not {p.ucomm for p in parse_ps_rows(proc.stdout or "")} & {"shutdown", "reboot"}


def read_processes(connection: SshConnection) -> str:
    """PROCESS_SNAPSHOT_COMMAND output; empty when ps does not answer."""
    try:
        return run_ssh(connection, PROCESS_SNAPSHOT_COMMAND, check=False, timeout=ACPD_SSH_TIMEOUT_SECONDS).stdout or ""
    except TransportError:
        return ""


def acpd_stuck_children(ps_output: str) -> list[int]:
    """PIDs of ACPd's own forks asleep in nanosleep, in PROCESS_SNAPSHOT_COMMAND output.

    A healthy device runs one ACPd. A second one, ACPd's child that never went
    on to exec, predicted every v3.4.0 reboot that tore ACPd down without
    restarting the kernel. A fork on its way to exec is never in nanosleep.
    Matched on ucomm: sshd and the other daemons are ACPd's children too.
    """
    rows = [p for p in parse_ps_rows(ps_output) if p.ucomm == "ACPd"]
    acpd = {p.pid for p in rows}
    return [p.pid for p in rows if p.ppid in acpd and p.wchan in NANOSLEEP_WCHANS]


def acpd_problems(
    connection: SshConnection,
    ps_output: str | None = None,
    *,
    network_reading: AirportAcpReading | None = None,
) -> list[AcpdProblem]:
    """Every reason ACPd cannot take a restart now.

    A restart that never happened is the whole story: ACPd tore itself down,
    so it is reported alone, with one fix, and nothing more is read.

    `ps_output` is a PROCESS_SNAPSHOT_COMMAND listing already read; it is read
    here when None. `network_reading` is this computer's network ACP read of
    the device at this connection's address: when ACPd left it unanswered,
    ACPd's listener is read through SSH as well. The marker, syUT and
    forwarded reads raise TransportError: an SSH failure is the device's or
    the network's, not ACPd's.
    """
    if restart_never_happened(connection):
        return [AcpdProblem(
            "restart_never_happened",
            "the device did not restart when TimeCapsuleSMB last asked it to",
            note="Restarting it again would fail the same way. ",
        )]
    problems = []
    answers = acpd_answers(connection)
    if not answers:
        # The device's own acp gets no uptime (syUT) from ACPd.
        problems.append(AcpdProblem("syut_silent", "the AirPort service on this device is stuck"))
    stuck = acpd_stuck_children(read_processes(connection) if ps_output is None else ps_output)
    if stuck:
        problems.append(AcpdProblem(
            "stuck_child",
            f"its helper process (pid {', '.join(map(str, stuck))}) is stuck",
            note="Sharing may work now, but the next restart will leave the device offline until it is unplugged. ",
            details={"acpd_stuck_children": stuck},
        ))
    # A read without a password has no error: it never asked the device.
    unanswered = bool(network_reading and network_reading.password_matches is None and network_reading.error)
    if answers and unanswered and not acpd_answers_network(connection):
        problems.append(AcpdProblem(
            "network_silent",
            "the AirPort service stopped answering the network",
            note="Sharing may work now, but the device cannot be updated or restarted over the network while this lasts. ",
            details=network_silent_details(connection),
        ))
    return problems
