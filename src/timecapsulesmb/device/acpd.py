"""Apple ACPd health, read over SSH, and the marker that tells a restart under
way from one ACPd took but never ran (RESTART_MARKER, restart_verdict).

ACPd serves both network ACP (port 5009) and the device's own /usr/bin/acp.
It can hang (build/native/README.md, "Apple ACPd"), and
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
import time
from dataclasses import dataclass, field
from datetime import datetime

from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.device.errors import DeviceRestartingError
from timecapsulesmb.device.probe import AirportAcpReading, read_kern_limits_conn
from timecapsulesmb.device.processes import (
    PROCESS_SNAPSHOT_COMMAND,
    STUCK_PROCESS_PS_COMMAND,
    acpd_running,
    parse_ps_rows,
    smbd_running,
)
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
# the kernel has not restarted yet. It holds the kernel's tick count
# (RESTART_TICKS_COMMAND), so any process tells from one read how long ago the
# restart was asked for, without trusting the device's clock, which NTP steps
# by weeks just after boot. Nothing on the device reads it.
RESTART_MARKER = "/mnt/Memory/acp-reboot"
# A Wi-Fi link that loses packets for seconds can hold even this short read
# that long (probe.PROCESS_SNAPSHOT_TIMEOUT_SECONDS).
RESTART_MARKER_TIMEOUT_SECONDS = 30
# The statistics clock's ticks since this kernel booted, as five counters
# (user, nice, sys, intr, idle) whose sum grows 100 a second on these
# single-CPU devices. ACPd plays no part in it.
RESTART_TICKS_COMMAND = "/sbin/sysctl -n kern.cp_time"
RESTART_TICKS_PER_SECOND = 100
# NetBSD 4's counters are 32-bit longs on these CPUs: the idle one wraps,
# printed negative, after about 248 days of uptime.
_TICK_COUNTER_WRAP = 2**32
_TICK_COUNTER = re.compile(r"=\s*(-?\d+)")
# A real restart stops the old kernel within about 10 s of the request on both
# families, loaded or not (build/native/README.md, "Apple ACPd"). A marker
# older than this on a kernel that still runs is a restart that hung, and
# unplugging the device is its fix, harmless even if the restart was only slow.
RESTART_HANG_SECONDS = 60
# A tick read that fails writes no marker, so the restart counts as unmarked,
# never as one whose age is unknown.
_RESTART_MARK = f't=$({RESTART_TICKS_COMMAND}) && [ -n "$t" ] && echo "$t" > {RESTART_MARKER}'
# The marker, the tick count now, the marker's time and the device's clock
# (for a marker without a tick count to compare, _clock_age_seconds) and the
# process table, read without any command line so it never waits (see
# UNREADABLE_COMMAND). No output without the marker.
_RESTART_MARKER_READ = (
    f'[ -e {RESTART_MARKER} ] || exit 1; echo "marked: $(cat {RESTART_MARKER})"; '
    f'echo "now: $({RESTART_TICKS_COMMAND})"; echo "written: $(ls -lT {RESTART_MARKER})"; '
    f'echo "clock: $(date \'+%b %e %H:%M:%S %Y\')"; {STUCK_PROCESS_PS_COMMAND}'
)
# A time as `ls -lT` and the date format above print it: "Nov  3 01:40:12 2026".
_DEVICE_TIME = re.compile(r"([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}:\d{2}:\d{2})\s+(\d{4})")
# The last daemon ACPd stops before the kernel goes down, about 0.3 s before
# on both families. Nothing else stops it and ACPd never starts it again, so
# a listing without it shows ACPd's teardown ran.
TEARDOWN_LAST_DAEMON = "mDNSResponder"
# Processes waiting in the kernel that a restart's telemetry lists, at most.
RESTART_WAITING_PROCESSES_MAX = 10
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
    # The whole sentence, in place of acpd_broken_message's, for a problem
    # that does not prove ACPd broken (network_silent: a slow link looks the
    # same).
    sentence: str = ""
    details: dict[str, object] = field(default_factory=dict)
    # The error code a command that refuses for it reports.
    error_code: ErrorCode = ErrorCode.ACPD_BROKEN
    # ACPd is gone: it quit, or a restart tore it down without restarting the
    # kernel. Only unplugging the device brings it back.
    gone: bool = False
    # ACPd is gone and a finished listing shows no smbd: nothing works until
    # the device restarts.
    sharing_off: bool = False


# The one fix for a broken ACPd. An error leaves it to the code's next steps
# (app.recovery); Checkup's result says it.
ACPD_FIX = "Unplug the device, wait 10 seconds, plug it back in and wait 2 minutes"

# Checkup's code for every ACPd problem; its `acpd_problem` names which.
APPLE_ACPD_BROKEN_CODE = "apple_acpd_broken"

# The problems a process stuck on the disk causes (processes.disk_io_stalls):
# ACPd, waiting on diskd, which waits for the disk, answers neither its own acp
# nor the network. A quit, a restart that never happened or a stuck fork of
# ACPd is ACPd's own.
DISK_STALL_PROBLEMS = frozenset({"syut_silent", "network_silent"})


def disk_stall_could_explain(problems: list[AcpdProblem]) -> bool:
    """Whether a problem found is one a stuck disk causes, so the process
    list is worth reading again before ACPd is blamed for it: ACPd's reads
    take up to 45 s, long enough for a disk process that stopped answering
    at boot to pass processes.STUCK_SLEEP_SECONDS meanwhile. Another problem
    beside it, such as a stuck fork of ACPd, does not explain the silence."""
    return any(problem.code in DISK_STALL_PROBLEMS for problem in problems)


def acpd_broken_message(what: str, *, note: str = "") -> str:
    """Every caller's words for a broken ACPd: what failed, and why it matters."""
    return f"Apple ACPd is broken: {what}, a bug in Apple's firmware. {note}".rstrip()


def problem_message(problem: AcpdProblem, *, note: str = "") -> str:
    """The words for one problem: its own sentence when it has one, else
    acpd_broken_message's."""
    if problem.sentence:
        return f"{problem.sentence} {note}".rstrip()
    return acpd_broken_message(problem.what, note=note)


# Neither this computer's ACP read nor one through SSH answered, and each of
# three causes looks the same: the SSH read is tunnelled over the same link,
# so a link that loses packets for seconds stalls it too; a full kernel file
# table leaves both connections queued (TABLE_FULL_COMMAND); or ACPd's network
# listener stopped. dmesg keeps its "table is full" lines from the whole boot,
# so they cannot say which cause this is: the sentence names all three.
NETWORK_SILENT_SENTENCE = (
    "No answer from Apple ACPd over the network: either the connection to the device is too slow, the device has "
    "too many files open (several Macs connected at once can cause this), or the AirPort service no longer answers "
    "the network (a bug in Apple's firmware)."
)


# ACPd gone from the process list (processes.acpd_running). Its children,
# sshd among them, keep running with init as their parent; diskd, wcifsnd,
# wcifsfs and afpserver go with it. NetBSD 4 does not start it again.
ACPD_QUIT_WHAT = "the AirPort service on this device has quit"
ACPD_QUIT_NOTE = "File sharing stays off until the device restarts. "
ACPD_QUIT_MESSAGE = acpd_broken_message(ACPD_QUIT_WHAT, note=ACPD_QUIT_NOTE)
# Samba started before ACPd quit keeps serving (processes.smbd_running).
ACPD_QUIT_SERVING_NOTE = (
    "Sharing may work now, but until the device is unplugged it cannot be updated or restarted over the "
    "network, and AirPort Utility cannot reach it. "
)
# A listing without ACPd is read again this long after, about one attempt of
# the runtime wait apart (probe.ACPD_GONE_LISTINGS): NetBSD 4 never starts
# ACPd again, but on NetBSD 6 that is not known, so one listing could fall
# between two ACPds. Only a device whose ACPd is already gone waits for it.
ACPD_RECHECK_SECONDS = 5.0


def pause(seconds: float) -> None:
    time.sleep(seconds)


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

    The read goes to the device's own 127.0.0.1:5009, so no address, route or
    filter between this computer and the device plays a part. Its bytes still
    travel inside the SSH connection, though, so a link that stalls for
    longer than the deadline makes it fail as a silent listener would. Any
    reply is an answer, a rejected password or a malformed one too. It gets
    the local read's deadline: a busy ACPd answers within it, and a listener
    that stopped answering stays silent for hours. Raises TransportError when
    the forward cannot be set up (SshCommandTimeout when it was not ready in
    time).
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
        proc = run_ssh(connection, _RESTART_MARK, check=False, timeout=RESTART_MARKER_TIMEOUT_SECONDS)
    except TransportError:
        return False
    return proc.returncode == 0


def _tick_counters(text: str) -> tuple[int, ...] | None:
    counters = tuple(int(value) for value in _TICK_COUNTER.findall(text))
    return counters if len(counters) == 5 else None


def _device_time(text: str) -> datetime | None:
    match = _DEVICE_TIME.search(text)
    if match is None:
        return None
    try:
        return datetime.strptime(" ".join(match.groups()), "%b %d %H:%M:%S %Y")
    except ValueError:
        return None


def _clock_age_seconds(written: str, clock: str) -> float | None:
    """Seconds between the marker's time and the device's clock, both in the
    device's local time; None when either cannot be read or the clock went
    back. Only when the ticks cannot tell: a marker an older version wrote
    empty, or a tick count that could not be read now. NTP steps the clock by
    weeks just after boot, which can make such a marker look older than it
    is, as an empty one always counted."""
    marked, now = _device_time(written), _device_time(clock)
    if marked is None or now is None or now < marked:
        return None
    return (now - marked).total_seconds()


def _elapsed_seconds(marked: tuple[int, ...] | None, now: tuple[int, ...] | None) -> float | None:
    """Seconds between two RESTART_TICKS_COMMAND readings of one kernel; None
    when either is missing or they do not add up."""
    if marked is None or now is None:
        return None
    ticks = 0
    for before, after in zip(marked, now):
        delta = after - before
        if delta < 0:
            delta += _TICK_COUNTER_WRAP
        if delta < 0:
            return None
        ticks += delta
    return ticks / RESTART_TICKS_PER_SECOND


@dataclass(frozen=True)
class RestartMarker:
    """RESTART_MARKER as read_restart_marker found it."""

    # Seconds since it was written; None when neither clock tells.
    age_seconds: float | None
    # The process table read with it; no rows when ps failed.
    listing: str
    # What age_seconds came from: "ticks", the kernel's (RESTART_TICKS_COMMAND),
    # or "device_clock" when they cannot tell (_clock_age_seconds).
    age_clock: str | None = None

    @property
    def teardown_done(self) -> bool | None:
        """Whether ACPd's teardown ran: TEARDOWN_LAST_DAEMON is gone from a
        listing that shows sshd. None when the listing cannot say."""
        rows = [p for p in parse_ps_rows(self.listing) if not p.zombie]
        if not any(p.ucomm == "sshd" for p in rows):
            return None
        return not any(p.ucomm == TEARDOWN_LAST_DAEMON for p in rows)


def read_restart_marker(connection: SshConnection) -> RestartMarker | None:
    """RESTART_MARKER, its age and the process table; None without it.
    Raises TransportError."""
    output = run_ssh(connection, _RESTART_MARKER_READ, check=False, timeout=RESTART_MARKER_TIMEOUT_SECONDS).stdout or ""
    values: dict[str, str] = {}
    for line in output.splitlines():
        label, _, value = line.partition(": ")
        if label in ("marked", "now", "written", "clock"):
            values.setdefault(label, value)
    if "marked" not in values:
        return None
    age = _elapsed_seconds(_tick_counters(values["marked"]), _tick_counters(values.get("now", "")))
    if age is not None:
        return RestartMarker(age, output, "ticks")
    age = _clock_age_seconds(values.get("written", ""), values.get("clock", ""))
    return RestartMarker(age, output, None if age is None else "device_clock")


def restart_evidence(marker: RestartMarker) -> dict[str, object]:
    """The marker's debug fields: its age, whether ACPd's teardown ran, ACPd's
    own processes and every process waiting in the kernel (state D, kernel
    threads aside), as a disk flush that slows a restart would. They show
    what a restart that outlives RESTART_HANG_SECONDS was doing."""
    rows = parse_ps_rows(marker.listing)
    waiting = [p for p in rows if p.stat.startswith("D") and "K" not in p.stat]
    return {
        "restart_marker_age_sec": None if marker.age_seconds is None else round(marker.age_seconds, 1),
        "restart_marker_clock": marker.age_clock,
        "restart_teardown_done": marker.teardown_done,
        "restart_acpd_processes": [f"{p.pid}/{p.ppid}/{p.wchan}" for p in rows if p.ucomm == "ACPd"],
        "restart_waiting_processes": [f"{p.ucomm}/{p.wchan}" for p in waiting][:RESTART_WAITING_PROCESSES_MAX],
    }


@dataclass(frozen=True)
class RestartVerdict:
    """restart_verdict's answer."""

    # "none", "restarting", "hung" or "not_taken" (restart_verdict).
    state: str
    age_seconds: float | None = None
    # restart_evidence's fields and the verdict, when there is a marker.
    details: dict[str, object] = field(default_factory=dict)
    # acpd_answers' answer, when the verdict needed it.
    acpd_answers: bool | None = None


def restart_verdict(connection: SshConnection) -> RestartVerdict:
    """What RESTART_MARKER says about the last restart asked for in this boot.

    none: no marker. restarting: written less than RESTART_HANG_SECONDS ago.
    Older, or of unknown age: hung when ACPd's teardown ran or the device's
    own acp gets no syUT from ACPd, as ACPd tore itself down without
    restarting the kernel; not_taken when ACPd answers and its daemons run, as
    the request never started a restart, and the marker waits harmlessly for
    the next one. Raises TransportError. A read in a real restart's last
    seconds outlives the old kernel and fails that way, never as a hang.
    """
    marker = read_restart_marker(connection)
    if marker is None:
        return RestartVerdict("none")
    details = restart_evidence(marker)

    def verdict(state: str, answers: bool | None = None) -> RestartVerdict:
        details["restart_verdict"] = state
        return RestartVerdict(state, marker.age_seconds, details, answers)

    if marker.age_seconds is not None and marker.age_seconds < RESTART_HANG_SECONDS:
        return verdict("restarting")
    if marker.teardown_done:
        return verdict("hung")
    answers = acpd_answers(connection)
    return verdict("not_taken" if answers else "hung", answers)


def restarting_message(age_seconds: float | None) -> str:
    """Every caller's words for a restart under way (restart_verdict)."""
    seconds = round(age_seconds or 0)
    return (
        f"The device is restarting: TimeCapsuleSMB asked it to restart {seconds} "
        f"second{'' if seconds == 1 else 's'} ago."
    )


def read_processes(connection: SshConnection) -> str:
    """PROCESS_SNAPSHOT_COMMAND output; empty when ps does not answer."""
    try:
        return run_ssh(connection, PROCESS_SNAPSHOT_COMMAND, check=False, timeout=ACPD_SSH_TIMEOUT_SECONDS).stdout or ""
    except TransportError:
        return ""


def acpd_quit(connection: SshConnection, ps_output: str) -> AcpdProblem | None:
    """The "quit" problem when ACPd is gone from `ps_output`, a
    PROCESS_SNAPSHOT_COMMAND listing, and from another one read
    ACPD_RECHECK_SECONDS later; None otherwise. A listing that cannot say
    (processes.acpd_running's None) is no quit. Its note says whether smbd
    still runs in the second listing."""
    if acpd_running(ps_output) is not False:
        return None
    pause(ACPD_RECHECK_SECONDS)
    listing = read_processes(connection)
    if acpd_running(listing) is not False:
        return None
    # A listing that says ACPd is gone also says whether smbd runs.
    serving = smbd_running(listing) is True
    return AcpdProblem(
        "quit",
        ACPD_QUIT_WHAT,
        note=ACPD_QUIT_SERVING_NOTE if serving else ACPD_QUIT_NOTE,
        details={"acpd_quit_smbd_running": serving},
        error_code=ErrorCode.ACPD_QUIT,
        gone=True,
        sharing_off=not serving,
    )


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

    A restart under way raises DeviceRestartingError before anything else is
    read: nothing is broken, and the device is about to go down. A restart
    that never happened is the whole story: ACPd tore itself down, so it is
    reported alone, with one fix, and nothing more is read.

    An ACPd that quit is the whole story too: every other read would only
    fail the same way. Either one says whether file sharing is off
    (AcpdProblem.sharing_off).

    `ps_output` is a PROCESS_SNAPSHOT_COMMAND listing already read; it is read
    here when None. `network_reading` is this computer's network ACP read of
    the device at this connection's address: when ACPd left it unanswered,
    ACPd's listener is read through SSH as well. The marker, syUT and
    forwarded reads raise TransportError: an SSH failure is the device's or
    the network's, not ACPd's.
    """
    # The marker first: an SSH failure reading it is raised before a listing,
    # which says nothing when SSH fails, waits out its own timeout.
    restart = restart_verdict(connection)
    if restart.state == "restarting":
        raise DeviceRestartingError(restarting_message(restart.age_seconds), details=restart.details)
    if restart.state == "hung":
        if ps_output is None:
            ps_output = read_processes(connection)
        return [AcpdProblem(
            "restart_never_happened",
            "the device did not restart when TimeCapsuleSMB last asked it to",
            note="Restarting it again would fail the same way. ",
            details=restart.details,
            gone=True,
            sharing_off=smbd_running(ps_output) is False,
        )]
    if ps_output is None:
        ps_output = read_processes(connection)
    gone = acpd_quit(connection, ps_output)
    if gone is not None:
        return [gone]
    problems = []
    answers = acpd_answers(connection) if restart.acpd_answers is None else restart.acpd_answers
    if not answers:
        # The device's own acp gets no uptime (syUT) from ACPd.
        problems.append(AcpdProblem("syut_silent", "the AirPort service on this device is stuck"))
    stuck = acpd_stuck_children(ps_output)
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
            sentence=NETWORK_SILENT_SENTENCE,
            error_code=ErrorCode.ACPD_NETWORK_SILENT,
            details=network_silent_details(connection),
        ))
    return problems
