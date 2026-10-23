from __future__ import annotations

import re
import shlex
from dataclasses import dataclass


# Reading another process's command line (ps's command column, the kernel's
# KERN_PROC_ARGS) copies it out of that process's memory under its map lock.
# A process asleep in fork() or msync() holds that lock while the disk keeps it
# waiting, and a `ps ax -o command=` listing then waits for it too, however
# many other processes it lists. The process table itself comes from kernel
# structures (KERN_PROC2) and never waits. So, like the manager
# (build/native/service/proctable.h), every listing here reads the table alone
# and reads a command line only for the processes whose role depends on it.
#
# ps_rows prints this in place of a command line it did not read: the process
# was in an uninterruptible sleep (state D) on two looks a second apart, or ps
# could not read it. Callers that would act on the role treat the process as
# possibly theirs and never as absent. The second look and the read are
# separate commands, so this narrows the stall rather than closing it.
UNREADABLE_COMMAND = "(unreadable)"
# Every listing's columns, in this order; ps_rows adds the command line after
# them. ps prints "-" for a process with no wait channel, so every row has all
# eight. A ucomm with a space would spill into the command line, but every
# name a caller selects has none.
PS_COLUMNS = "-o pid= -o ppid= -o pgid= -o stat= -o sl= -o wchan= -o time= -o ucomm="
PS_ROWS_FUNCTION = r'''
ps_rows() (
    # One row per process: pid ppid pgid stat sl wchan time ucomm, then the
    # command line for the processes whose ucomm matches one of the case
    # patterns given as arguments. Split at blanks whatever the caller's IFS,
    # and never expand a command line's glob characters.
    unset IFS
    set -f
    ps_select=$*
    ps_list=$(/bin/ps ax __PS_COLUMNS__ 2>/dev/null) || exit 1
    ps_waited=
    while IFS= read -r ps_line; do
        set -- $ps_line
        if [ "$#" -lt 8 ]; then
            [ -z "$ps_line" ] || printf '%s\n' "$ps_line"
            continue
        fi
        ps_pid=$1
        ps_state=$4
        ps_comm=$8
        ps_selected=
        for ps_pattern in $ps_select; do
            case "$ps_comm" in $ps_pattern) ps_selected=1 ;; esac
        done
        if [ -z "$ps_selected" ]; then
            printf '%s\n' "$ps_line"
            continue
        fi
        case "$ps_state" in
            Z*) printf '%s\n' "$ps_line"; continue ;;
            D*)
                # Most uninterruptible sleeps end within milliseconds; one
                # that lasts may hold the map lock, so look once more later.
                if [ -z "$ps_waited" ]; then
                    sleep 1
                    ps_waited=1
                fi
                ps_state=$(/bin/ps -o stat= -p "$ps_pid" 2>/dev/null)
                case "$ps_state" in
                    "")
                        /bin/kill -0 "$ps_pid" 2>/dev/null || continue
                        printf '%s %s\n' "$ps_line" '__UNREADABLE__'
                        continue ;;
                    *D*)
                        printf '%s %s\n' "$ps_line" '__UNREADABLE__'
                        continue ;;
                esac ;;
        esac
        # NetBSD 4's ps -p takes one PID. A process gone since the listing
        # is left out; one that ran another program since is not this row's.
        ps_read=$(/bin/ps -ww -o ucomm= -o command= -p "$ps_pid" 2>/dev/null)
        ps_read=${ps_read#"${ps_read%%[! ]*}"}
        ps_command='__UNREADABLE__'
        if [ -z "$ps_read" ]; then
            /bin/kill -0 "$ps_pid" 2>/dev/null || continue
        else
            case "$ps_read" in
                "$ps_comm "*)
                    ps_read=${ps_read#"$ps_comm"}
                    ps_read=${ps_read#"${ps_read%%[! ]*}"}
                    # ps shows "(ucomm)" when it cannot read the command line.
                    case "$ps_read" in
                        ""|"($ps_comm)") ;;
                        *) ps_command=$ps_read ;;
                    esac ;;
            esac
        fi
        printf '%s %s\n' "$ps_line" "$ps_command"
    done <<EOF_PS_ROWS
$ps_list
EOF_PS_ROWS
)
'''.replace("__PS_COLUMNS__", PS_COLUMNS).replace("__UNREADABLE__", UNREADABLE_COMMAND).strip() + "\n"


# The line a listing ends with once ps_rows has printed every row. ACPd is
# among the last rows ps prints, so a listing cut short reads as one without
# ACPd: only a listing with this line says a process is gone (listing_complete).
PS_LISTING_END = "tcapsule-ps: end"


def ps_rows_command(*select: str) -> str:
    """An SSH command printing ps_rows' rows, reading the command lines of the
    processes whose ucomm matches a pattern in select; it fails when ps does.
    A listing that finished ends with PS_LISTING_END."""
    call = " ".join(["ps_rows", *(shlex.quote(pattern) for pattern in select)])
    call = f"{call} || exit 1; echo '{PS_LISTING_END}'"
    return f"/bin/sh -c {shlex.quote(PS_ROWS_FUNCTION + call)}"


@dataclass(frozen=True)
class Process:
    """One ps_rows row."""

    pid: int
    ppid: int
    pgid: int
    stat: str
    # Seconds in the current sleep, capped at PS_SLEEP_SECONDS_CAP.
    sl: int
    wchan: str
    time: str
    ucomm: str
    # The command line's words; empty when it was not asked for.
    argv: tuple[str, ...] = ()

    @property
    def zombie(self) -> bool:
        return self.stat.startswith("Z")

    @property
    def unreadable(self) -> bool:
        return self.argv == (UNREADABLE_COMMAND,)


def parse_ps_rows(output: str) -> list[Process]:
    """ps_rows' rows (or a listing of PS_COLUMNS alone). A line that is not
    one, such as an error message, is left out."""
    rows = []
    for fields in (line.split() for line in output.splitlines()):
        if len(fields) < 8 or not all(field.isdigit() for field in (*fields[:3], fields[4])):
            continue
        rows.append(Process(int(fields[0]), int(fields[1]), int(fields[2]), fields[3], int(fields[4]),
                            fields[5], fields[6], fields[7], tuple(fields[8:])))
    return rows


# The listing doctor's Samba, mDNS, startup and hostname checks read: the
# manager's and discovery's titles, and diskd's interface.
PS_CAPTURE_FUNCTION = PS_ROWS_FUNCTION + "capture_ps_out() { ps_rows service diskd; }\n"
PS_CAPTURE_COMMAND = PS_CAPTURE_FUNCTION + "capture_ps_out || true"


def listing_complete(output: str) -> bool:
    """Whether a listing ends with PS_LISTING_END: every row is in it."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return bool(lines) and lines[-1] == PS_LISTING_END


def _running(output: str, ucomm: str) -> bool | None:
    """Whether a live process named ucomm runs, in a listing that ends with
    PS_LISTING_END; None for any other listing, which cannot say a process is
    gone. The sshd serving this listing is always in it, so a listing without
    it, whose rows did not parse as such (another layout, an error line),
    cannot say a process is gone either."""
    if not listing_complete(output):
        return None
    rows = [p for p in parse_ps_rows(output) if not p.zombie]
    if any(p.ucomm == ucomm for p in rows):
        return True
    return False if any(p.ucomm == "sshd" for p in rows) else None


# A real restart runs /sbin/shutdown (ACPd's reboot path), which stops ACPd
# before sshd, its last step.
SHUTDOWN_UCOMMS = frozenset({"shutdown", "reboot"})


def acpd_running(output: str) -> bool | None:
    """Whether Apple's ACPd runs (_running). sshd and the other daemons are
    ACPd's children, so a device that answers SSH started ACPd. NetBSD 4
    does not start it again when it exits. A listing taken while the device
    shuts down (SHUTDOWN_UCOMMS) cannot say ACPd quit either."""
    running = _running(output, "ACPd")
    if running is False and any(p.ucomm in SHUTDOWN_UCOMMS for p in parse_ps_rows(output)):
        return None
    return running


def smbd_running(output: str) -> bool | None:
    """Whether smbd runs (_running). Samba is the manager's child, not
    ACPd's, so an ACPd that quit after Samba started leaves it serving."""
    return _running(output, "smbd")


def service_role_processes(ps_output: str, role: str) -> list[Process]:
    """The live processes holding role: by the role title, or by argv before
    the native title is installed."""
    rows = []
    for process in parse_ps_rows(ps_output):
        argv = process.argv
        if process.zombie or process.ucomm != "service":
            continue
        if argv[:2] == ("service:", f"role={role}"):
            rows.append(process)
        elif argv[:2] == ("/mnt/Flash/service", role):
            if role == "discovery" and any(word in argv[2:] for word in ("--diskless", "--netbios-name")):
                rows.append(process)
            elif role == "telemetry" and "--daemon" in argv[2:]:
                rows.append(process)
    return rows


def service_unreadable(ps_output: str) -> bool:
    """A live service process whose command line ps_rows did not read: it may
    hold any role."""
    return any(p.ucomm == "service" and p.unreadable and not p.zombie for p in parse_ps_rows(ps_output))


# The manager gives each child TC_CHILD_GRACE_MS (build/native/common/process.h)
# to exit after SIGTERM, then sends SIGKILL, reaps it and only then exits itself.
# Stopping it takes up to that grace plus the kill and reap. v3.1.1 waited about
# 10 s and reported healthy managers as stuck; a rerun minutes later worked.
# Twice the grace also covers a slow NetBSD 4 process scan on each pass. The
# budget counts passes, not seconds: a pass also waits a second whenever a
# shell or service process is asleep in the kernel (ps_rows' second look),
# so a stop can take twice as long, still well inside the deploy action's
# 120 s SSH timeout (REMOTE_ACTION_TIMEOUT_SECONDS).
MANAGER_CHILD_GRACE_SECONDS = 10
SUPERVISOR_STOP_SECONDS = 2 * MANAGER_CHILD_GRACE_SECONDS
# The table alone, which never waits, in ps_rows' layout without command
# lines: what a failed stop and a timed-out listing report. ps_rows' own
# listing is this same command, so a test that fakes one by replacing this
# text replaces both. NetBSD ps reports
# the kernel wait channel: a Samba child blocked on disk I/O is the usual
# reason a manager cannot finish stopping.
STUCK_PROCESS_PS_COMMAND = f"/bin/ps ax {PS_COLUMNS}"
# How the stop script names a service process it waited for in vain without
# reading its command line: asleep in the kernel throughout, like a manager
# that does not stop, and nearly always on a failing disk.
UNREADABLE_SERVICE_STOP_MESSAGE = "process service did not show its command line"


def describe_uninterruptible(stuck_listing: str) -> str:
    """The processes in an uninterruptible sleep (state D, kernel threads
    aside) in STUCK_PROCESS_PS_COMMAND output, for a listing that timed out:
    a ps among them waits for a map lock (see UNREADABLE_COMMAND)."""
    return ", ".join(
        f"{p.ucomm} (pid {p.pid}) on {p.wchan}"
        for p in parse_ps_rows(stuck_listing) if p.stat.startswith("D") and "K" not in p.stat
    )


# The same listing as one SSH command, ending with PS_LISTING_END. Read before
# any check touches the data disk. Only the manager's title and diskd's
# interface come from command lines, so a process held on the disk cannot hold
# up the listing (see UNREADABLE_COMMAND).
PROCESS_SNAPSHOT_COMMAND = ps_rows_command("service", "diskd")
# ps shows D for an uninterruptible sleep. The kernel counts a sleeping
# thread's seconds asleep (l_slptime) and resets the count at every wake-up,
# so a process doing slow disk I/O never reaches this; one that does has
# waited in a single sleep that long. A wait that wakes on a timeout to retry,
# such as NetBSD 6's needbuf, resets the count; the manager follows processes
# across samples and names those in its process title (stuck.c).
STUCK_SLEEP_SECONDS = 120
# ps prints the sleep time capped at 127.
PS_SLEEP_SECONDS_CAP = 127


@dataclass(frozen=True)
class StuckProcess:
    pid: int
    name: str
    wchan: str
    sleep_seconds: int
    # ps's sleep time stops at 127; the manager's count does not.
    capped: bool = False

    def describe(self) -> str:
        seconds = f"{self.sleep_seconds}+" if self.capped else str(self.sleep_seconds)
        return f"{self.name} (pid {self.pid}) waiting on {self.wchan} for {seconds} s"


def _manager_title_stuck(title: tuple[str, ...]) -> tuple[list[StuckProcess], int]:
    """Entries of the manager's "stuck=PID:COMM:WAIT:SECONDS,...[,+N]" title
    word, and N, how many more it counts than it names."""
    stuck = []
    unnamed = 0
    for word in title:
        if not word.startswith("stuck="):
            continue
        for item in word.removeprefix("stuck=").split(","):
            parts = item.split(":")
            try:
                if len(parts) == 1 and item.startswith("+"):
                    unnamed = int(item[1:])
                elif len(parts) == 4:
                    stuck.append(StuckProcess(int(parts[0]), parts[1], parts[2], int(parts[3])))
            except ValueError:
                continue
    return stuck, unnamed


def _is_manager_title(process: Process) -> bool:
    return process.ucomm == "service" and "role=manager" in process.argv


def manager_unnamed_stuck_count(ps_output: str) -> int:
    """How many stuck processes the manager's title counts beyond the ones it names."""
    manager = next((p for p in parse_ps_rows(ps_output) if _is_manager_title(p)), None)
    return _manager_title_stuck(manager.argv)[1] if manager is not None else 0


def stuck_processes(ps_output: str) -> list[StuckProcess]:
    """Stuck processes in PROCESS_SNAPSHOT_COMMAND output.

    A row asleep uninterruptibly for STUCK_SLEEP_SECONDS, other than a kernel
    thread (state K, which always sleeps this way), and whatever the manager's
    title names, which takes precedence for the same PID.
    """
    by_pid: dict[int, StuckProcess] = {}
    for process in parse_ps_rows(ps_output):
        if _is_manager_title(process):
            for stuck in _manager_title_stuck(process.argv)[0]:
                by_pid[stuck.pid] = stuck
            continue
        if not process.stat.startswith("D") or "K" in process.stat:
            continue
        if process.sl >= STUCK_SLEEP_SECONDS and process.pid not in by_pid:
            by_pid[process.pid] = StuckProcess(
                process.pid, process.ucomm, process.wchan, process.sl, capped=process.sl >= PS_SLEEP_SECONDS_CAP
            )
    return list(by_pid.values())


# ps's wait channel for a process waiting for a lock another holds: an
# fcntl() byte-range lock (a TDB's record locks) or a flock() on a whole file
# (smbd's native HFS xattr writes) both sleep here.
LOCKF_WCHAN = "lockf"


def smbd_lock_waits(ps_output: str, min_seconds: int = 0) -> list[int]:
    """PIDs of the smbds asleep waiting for a lock for at least min_seconds,
    in PROCESS_SNAPSHOT_COMMAND output.

    The wait is interruptible (state I), so stuck_processes() never reports it,
    however long it lasts.
    """
    return [p.pid for p in parse_ps_rows(ps_output)
            if p.ucomm == "smbd" and p.wchan == LOCKF_WCHAN and p.sl >= min_seconds]


# Doctor reads a lock waiter's registers only once it has slept this long. A
# TDB record lock is normally held for well under a second, so a longer wait
# is worth a look at kernel memory, and a waiter about to wake is not read.
LOCK_WAIT_READ_SECONDS = 10

# What a process asleep in "lockf" asked for, from `service
# --print-syscall-registers` (r0-r3 are a system call's arguments on ARM).
# Two calls sleep there: flock(fd, how) and fcntl(fd, F_SETLKW, lock). Apple's
# xattr calls have no libc wrapper and go through syscall(2) (libc's
# SYS_syscall stub on both lanes), which takes the call's number in r0 and
# shifts its arguments one register up; one of them here would mean the
# kernel sleeps in "lockf" inside it.
ARM_MODE_MASK = 0x1F
ARM_USER_MODE = 0x10
FCNTL_F_SETLKW = 9
FLOCK_OPERATIONS = {1: "LOCK_SH", 2: "LOCK_EX"}
APPLE_FD_XATTR_SYSCALLS = {377: "fsetxattr", 380: "fgetxattr", 383: "flistxattr", 386: "fremovexattr"}
_REGISTERS_LINE = re.compile(
    r"^(\d+) wchan=(\S+) spsr=([0-9a-f]{8}) r0=([0-9a-f]{8}) r1=([0-9a-f]{8}) r2=([0-9a-f]{8})"
    r" r3=([0-9a-f]{8}) pc=([0-9a-f]{8})$"
)
_FSTAT_HEADER = ["USER", "CMD", "PID", "FD"]


def _names_file(row: str | None) -> bool:
    return row is not None and row.split()[4].startswith("/")


def read_lock_waits(output: str) -> tuple[list[dict[str, object]], str]:
    """Each lock waiter's call and the file its descriptor names, from
    lock_waiters_command output: the service's register lines (and its
    per-process errors), then fstat's rows (USER CMD PID FD MOUNT INUM MODE
    SIZE R/W) for the same processes.

    Returns one reading per register line, and the evidence: the service's
    lines, then fstat's header and only the rows for the descriptors the
    readings name, so a long descriptor table cannot push the registers out
    of a size-limited debug field. A call is read only from a process the
    service saw in "lockf" before and after reading its frame, whose spsr
    says the frame was saved on entry from a program; otherwise the reading
    says which of those failed. When the registers fit both an Apple xattr
    call (number in r0, descriptor in r1) and a lock call (descriptor in r0,
    operation in r1), the one whose descriptor fstat lists as a file wins;
    with both or neither, the reading is "ambiguous" and names both. A file
    is fstat's row past the descriptor: mount point, inode, mode, size, R/W.
    """
    header: str | None = None
    rows: dict[tuple[int, int], str] = {}
    lines: list[str] = []
    registers: list[tuple[int, str, list[int]]] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _REGISTERS_LINE.match(line)
        if match:
            registers.append((int(match.group(1)), match.group(2), [int(value, 16) for value in match.groups()[2:]]))
            lines.append(line)
            continue
        fields = line.split()
        if fields[:4] == _FSTAT_HEADER:
            header = header or line
        elif len(fields) >= 5 and fields[2].isdigit():
            # A descriptor row; the cwd and root rows (wd, root) name none.
            if fields[3].rstrip("*").isdigit():
                rows.setdefault((int(fields[2]), int(fields[3].rstrip("*"))), line)
        else:
            lines.append(line)
    calls: list[dict[str, object]] = []
    named: set[tuple[int, int]] = set()
    for pid, wchan, (spsr, r0, r1, r2, _r3, _pc) in registers:
        fd: int | None = None
        if wchan != LOCKF_WCHAN:
            call = f"not waiting in lockf ({wchan})"
        elif spsr & ARM_MODE_MASK != ARM_USER_MODE:
            call = "not a user-mode frame"
        else:
            readings: list[tuple[str, int]] = []
            if r1 == FCNTL_F_SETLKW:
                readings.append(("fcntl F_SETLKW", r0))
            elif r1 in FLOCK_OPERATIONS:
                readings.append((f"flock {FLOCK_OPERATIONS[r1]}", r0))
            if r0 in APPLE_FD_XATTR_SYSCALLS:
                readings.append((APPLE_FD_XATTR_SYSCALLS[r0], r1))
            if len(readings) == 2:
                named.update((pid, descriptor) for _, descriptor in readings)
                files = [reading for reading in readings if _names_file(rows.get((pid, reading[1])))]
                if len(files) == 1:
                    readings = files
            if len(readings) == 1:
                call, fd = readings[0]
            elif readings:
                call = "ambiguous: " + " or ".join(f"{name} on fd {descriptor}" for name, descriptor in readings)
            else:
                call = f"unrecognized (r0={r0:#x} r1={r1:#x} r2={r2:#x})"
        row = rows.get((pid, fd)) if fd is not None else None
        if fd is not None:
            named.add((pid, fd))
        calls.append({"pid": pid, "call": call, "fd": fd, "file": " ".join(row.split()[4:]) if row else None})
    kept = [rows[key] for key in sorted(named) if key in rows]
    if kept:
        lines += [header, *kept] if header else kept
    return calls, "\n".join(lines)


def render_stop_service_runtime(*, attempts: int = 5, supervisor_seconds: int = SUPERVISOR_STOP_SECONDS) -> str:
    # Stop every launcher before its workers, including launchers forked during
    # shutdown. Apple's daemons and unrelated `service` processes are not ours.
    # Supervisors get supervisor_seconds of one-second passes; workers and the
    # SIGKILL escalation of launcher shells keep the shorter attempts budget.
    return (PS_ROWS_FUNCTION + r'''
set -f
managed_processes() {
    managed_ps=$(ps_rows sh service) || return 1
    while read -r pid _ppid _pgid state _sl _wchan _time comm command; do
        case "$state" in Z*|"") continue ;; esac
        if [ "$command" = '__UNREADABLE__' ]; then
            # It may be one of ours: wait for it in both scopes, but never
            # signal a process that may be someone else's.
            printf '%s unknown %s\n' "$pid" "$comm"
            continue
        fi
        if [ "$comm" = sh ]; then
            # Match argv, not text embedded in the SSH `sh -c` wrapper.
            set -- $command
            case "${1:-}" in /bin/sh|sh) shift ;; esac
            case "${1:-}" in
                /mnt/Flash/rc.local|/mnt/Flash/boot.sh|/mnt/Flash/start-samba.sh|\
                /mnt/Flash/manager.sh|/mnt/Flash/watchdog.sh)
                    kind=shell
                    label=${1##*/}
                    label=${label%.sh} ;;
                *) continue ;;
            esac
        elif [ "$comm" = service ]; then
            # Deploy classifies "process manager did not stop" as a stuck
            # supervisor; workers keep the generic label.
            label=manager
            case "$command" in
                '/mnt/Flash/service run'|'/mnt/Flash/service run '*|\
                '/mnt/Flash/service manager'|'/mnt/Flash/service manager '*|\
                'service: role=manager'|'service: role=manager '*|\
                '/mnt/Memory/samba4/sbin/service run'|'/mnt/Memory/samba4/sbin/service run '*)
                    kind=supervisor ;;
                '/mnt/Flash/service'|'/mnt/Flash/service '*|\
                '/mnt/Memory/samba4/sbin/service'|'/mnt/Memory/samba4/sbin/service '*|\
                'service: role=mdns '*|'service: role=netbios '*|'service: role=telemetry '*|\
                'service: role=discovery '*|'service: role=job '*)
                    kind=worker
                    label=service ;;
                *) continue ;;
            esac
        else
            continue
        fi
        case "$scope:$kind" in
            supervisor:shell|supervisor:supervisor|worker:worker)
                printf '%s %s %s\n' "$pid" "$kind" "$label" ;;
        esac
    done <<EOF_PS
$managed_ps
EOF_PS
    return 0
}
report_stuck() {
    # Show what each process that did not stop, and each of its children, is
    # waiting on, so a failed stop says whether the disk was the cause.
    stuck=" $1 "
    __STUCK_PS__ 2>/dev/null | while read -r pid ppid _pgid state _sl wchan _time comm _rest; do
        case "$stuck" in
            *" $pid "*|*" $ppid "*)
                echo "still running: pid=$pid ppid=$ppid stat=$state wchan=$wchan comm=$comm" >&2 ;;
        esac
    done
}
for scope in supervisor worker; do
    attempt=0
    limit=__ATTEMPTS__
    [ "$scope" != supervisor ] || limit=__SUPERVISOR_SECONDS__
    while :; do
        processes=$(managed_processes) || exit 1
        [ -n "$processes" ] || break
        if [ "$attempt" -gt "$limit" ]; then
            pids=
            while read -r pid kind label; do
                if [ "$kind" = unknown ]; then
                    echo "process $label did not show its command line (pid $pid); retry after its active work finishes" >&2
                else
                    echo "process $label did not stop; retry after its active work finishes" >&2
                fi
                pids="$pids $pid"
            done <<EOF_BUSY
$processes
EOF_BUSY
            report_stuck "$pids"
            exit 1
        fi
        # Signal the whole snapshot before waiting. Rescan each pass: a boot
        # script may have forked a manager after the preceding snapshot.
        while read -r pid kind label; do
            if [ "$kind" = unknown ]; then
                :
            elif [ "$kind" = shell ] && [ "$attempt" -gt 0 ] && [ "$attempt" -ge __ATTEMPTS__ ]; then
                /bin/kill -9 "$pid" 2>/dev/null || true
            else
                # Native service/telemetry owners may be draining a debug
                # child. Preserve their graceful-only shutdown policy.
                /bin/kill -TERM "$pid" 2>/dev/null || true
            fi
        done <<EOF_STOP
$processes
EOF_STOP
        [ "$attempt" -ge "$limit" ] || sleep 1
        attempt=$((attempt + 1))
    done
done
''').replace("__ATTEMPTS__", str(attempts)).replace(
        "__SUPERVISOR_SECONDS__", str(supervisor_seconds)
    ).replace("__STUCK_PS__", STUCK_PROCESS_PS_COMMAND).replace("__UNREADABLE__", UNREADABLE_COMMAND).strip()


# Metadata and diagnostic jobs that outlive the SSH session that started them:
# the native migrator (ucomm truncated to 15 characters), shell migrations of
# older releases, and telemetry/debug jobs. Prints "pid ucomm" for each, and
# "pid ucomm unreadable" for a shell or service process whose command line was
# not read: it may be one, so it is waited for but never signalled.
# device.migration_jobs matches the same migrators in Python; keep them in step.
IDLE_JOBS_FUNCTION = PS_ROWS_FUNCTION + r'''
idle_jobs() (
    # A shell's arguments may hold glob patterns; never expand them.
    set -f
    jobs_ps=$(ps_rows sh service) || exit 1
    while read -r pid _ppid _pgid state _sl _wchan _time comm rest; do
        case "$state" in Z*|"") continue ;; esac
        case "$comm:$rest" in
            'sh:__UNREADABLE__'|'service:__UNREADABLE__')
                echo "$pid $comm unreadable"
                continue ;;
        esac
        busy=
        case "$comm" in
            telemetry|debug|heartbeat|tc-xattr-hfs-mi*|xattr-hfs-migra*) busy=1 ;;
        esac
        if [ "$comm" = service ]; then
            case "$rest" in
                'service: role=telemetry '*|'service: role=job '*|\
                '/mnt/Flash/service telemetry '*|'/mnt/Flash/service --once '*) busy=1 ;;
            esac
        fi
        if [ "$comm" = sh ]; then
            set -- $rest
            case "${1:-}" in /bin/sh|sh) shift ;; esac
            case "${1:-}" in
                /mnt/Flash/migrate.sh|/mnt/Flash/xattr-migrate-wrapper.sh) busy=1 ;;
            esac
        fi
        [ -z "$busy" ] || echo "$pid $comm"
    done <<EOF
$jobs_ps
EOF
)
'''.replace("__UNREADABLE__", UNREADABLE_COMMAND)


def render_wait_for_idle_jobs(*, attempts: int = 5) -> str:
    # An interrupted SSH session may leave a migration or diagnostic child.
    # Never unlink its executable or kill it halfway through a metadata write.
    # Deploy waits for a running migration before this (wait_for_previous_migration),
    # so in practice this waits out short telemetry and diagnostic jobs.
    return (IDLE_JOBS_FUNCTION + r'''
attempt=0
while :; do
    busy=$(idle_jobs) || exit 1
    [ -n "$busy" ] || exit 0
    if [ "$attempt" -ge __ATTEMPTS__ ]; then
        echo 'migration or diagnostic work is still active; retry after it finishes' >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    sleep 1
done
''').replace("__ATTEMPTS__", str(attempts)).strip()


def render_stop_idle_jobs(*, attempts: int = 5) -> str:
    # Uninstall removes everything these jobs work on, so it stops them instead
    # of waiting: SIGTERM on each pass, SIGKILL once the attempts are spent.
    return (IDLE_JOBS_FUNCTION + r'''
attempt=0
while :; do
    busy=$(idle_jobs) || exit 1
    [ -n "$busy" ] || exit 0
    if [ "$attempt" -gt __ATTEMPTS__ ]; then
        while read -r pid comm unreadable; do
            if [ -n "$unreadable" ]; then
                echo "process $comm did not show its command line (pid $pid)" >&2
            else
                echo "job $comm (pid $pid) did not stop" >&2
            fi
        done <<EOF_BUSY
$busy
EOF_BUSY
        exit 1
    fi
    signal=TERM
    [ "$attempt" -lt __ATTEMPTS__ ] || signal=KILL
    while read -r pid comm unreadable; do
        [ -n "$unreadable" ] || /bin/kill -$signal "$pid" 2>/dev/null || true
    done <<EOF_STOP
$busy
EOF_STOP
    attempt=$((attempt + 1))
    sleep 1
done
''').replace("__ATTEMPTS__", str(attempts)).strip()


def _ucomm_pkill_pattern(name: str) -> str:
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
    if not name or any(char not in allowed for char in name):
        raise ValueError(f"Unsafe process name: {name!r}")
    return f"^{name}$"


def render_process_present_by_ucomm(name: str) -> str:
    """Exits 0 while a live process has ucomm NAME, and when ps fails."""
    return (
        "ps_out=$(/bin/ps ax -o stat= -o ucomm= 2>/dev/null) || exit 0; "
        "printf '%s\\n' \"$ps_out\" | { "
        "while read -r state comm; do "
        'case "$state" in Z*) continue ;; esac; '
        f'[ "$comm" != {shlex.quote(name)} ] || exit 0; '
        "done; exit 1; }"
    )


def render_wait_for_process_absent(present_command: str, *, attempts: int) -> str:
    return (
        "attempt=0; "
        f"while /bin/sh -c {shlex.quote(present_command)} >/dev/null 2>&1; do "
        f'if [ "$attempt" -ge {attempts} ]; then break; fi; '
        "attempt=$((attempt + 1)); "
        "sleep 1; "
        "done"
    )


def render_pkill_wait_pkill9_by_ucomm(name: str, *, attempts: int = 5) -> str:
    # pkill matches ucomm only: with -f it would read every process's
    # command line (see UNREADABLE_COMMAND).
    pattern = shlex.quote(_ucomm_pkill_pattern(name))
    present_command = render_process_present_by_ucomm(name)
    wait_command = render_wait_for_process_absent(present_command, attempts=attempts)
    process_present = f"/bin/sh -c {shlex.quote(present_command)} >/dev/null 2>&1"
    failure_message = shlex.quote(f"process {name} did not stop")
    return (
        f"/usr/bin/pkill {pattern} >/dev/null 2>&1 || true; "
        f"{wait_command}; "
        f"if {process_present}; then "
        f"/usr/bin/pkill -9 {pattern} >/dev/null 2>&1 || true; {wait_command}; "
        "fi; "
        f"if {process_present}; then echo {failure_message} >&2; exit 1; fi"
    )


# Shell readers of capture_ps_out's rows (ps_rows' layout: pid ppid pgid stat
# sl wchan time ucomm, then the command line).
PROBE_PROCESS_HELPERS = (
    PS_CAPTURE_FUNCTION
    + r'''
smbd_parent_process_present() {
    ps_out=$1
    smbd_pids=""
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        set -- $line
        [ "$#" -ge 8 ] || continue
        case "$4" in
            Z*) continue ;;
        esac
        if [ "$8" = "smbd" ]; then
            smbd_pids="$smbd_pids $1"
        fi
    done <<EOF
$ps_out
EOF

    while IFS= read -r line; do
        [ -n "$line" ] || continue
        set -- $line
        [ "$#" -ge 8 ] || continue
        case "$4" in
            Z*) continue ;;
        esac
        if [ "$8" = "smbd" ]; then
            case " $smbd_pids " in
                *" $2 "*) ;;
                *) return 0 ;;
            esac
        fi
    done <<EOF
$ps_out
EOF
    return 1
}

# Sets role_unreadable to the PIDs of the service processes whose command
# lines capture_ps_out did not read: any of them may hold the role.
native_service_role_present() {
    role_ps=$1
    wanted_role=$2
    role_unreadable=
    while IFS= read -r role_line; do
        set -- $role_line
        [ "$#" -ge 9 ] || continue
        case "$4" in Z*) continue ;; esac
        [ "$8" = service ] || continue
        role_pid=$1
        shift 8
        if [ "$#" -eq 1 ] && [ "$1" = '__UNREADABLE__' ]; then
            role_unreadable="$role_unreadable $role_pid"
            continue
        fi
        [ "$#" -ge 2 ] || continue
        if [ "$1" = 'service:' ] && [ "$2" = "role=$wanted_role" ]; then return 0; fi
        if [ "$1" = /mnt/Flash/service ] && [ "$2" = "$wanted_role" ]; then return 0; fi
    done <<EOF_ROLE
$role_ps
EOF_ROLE
    return 1
}

manager_process_present_for_volume() {
    native_service_role_present "$1" manager
}

capture_fstat_for_ucomm() {
    ps_out=$1
    ucomm=$2
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        set -- $line
        [ "$#" -ge 8 ] || continue
        [ "$8" = "$ucomm" ] || continue
        case "$4" in
            Z*) continue ;;
        esac
        # NetBSD4 has fstat but not netstat/sockstat/lsof. Scope fstat to
        # candidate PIDs so activation checks do not scan every open file on a
        # busy Time Capsule.
        /usr/bin/fstat -p "$1" 2>/dev/null || true
    done <<EOF
$ps_out
EOF
}
'''.replace("__UNREADABLE__", UNREADABLE_COMMAND)
)
