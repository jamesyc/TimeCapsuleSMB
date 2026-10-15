from __future__ import annotations

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
_PS_ROWS_FUNCTION = r'''
__NAME__() (
    # One row per process: __COLUMNS__ ucomm, then the command line for the
    # ucomms __SELECT__. Split at blanks whatever the caller's IFS, and
    # never expand a command line's glob characters.
    unset IFS
    set -f
    ps_list=$(/bin/ps ax __LISTING__ -o ucomm= 2>/dev/null) || exit 1
    ps_waited=
    while IFS= read -r ps_line; do
        set -- $ps_line
        if [ "$#" -le __STATE__ ]; then
            [ -z "$ps_line" ] || printf '%s\n' "$ps_line"
            continue
        fi
        ps_pid=$1
        eval "ps_state=\${__STATE__} ps_comm=\${$#}"
        case "$ps_comm" in
            __SELECT__) ;;
            *) printf '%s\n' "$ps_line"; continue ;;
        esac
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
'''


def render_ps_rows(name: str, columns: tuple[str, ...], select: tuple[str, ...]) -> str:
    """A shell function NAME that prints the process table, one row per
    process: COLUMNS (pid first, stat among them), ucomm, then the command
    line for the processes whose ucomm matches a SELECT case pattern.

    It runs in a subshell, so its set -f and variables stay its own, and fails
    only when the listing itself fails.
    """
    return (
        _PS_ROWS_FUNCTION.replace("__NAME__", name)
        .replace("__COLUMNS__", " ".join(columns))
        .replace("__LISTING__", " ".join(f"-o {column}=" for column in columns))
        .replace("__STATE__", str(columns.index("stat") + 1))
        .replace("__SELECT__", "|".join(select))
        .replace("__UNREADABLE__", UNREADABLE_COMMAND)
        .strip()
    )


def ps_rows_command(name: str, columns: tuple[str, ...], select: tuple[str, ...]) -> str:
    """An SSH command printing render_ps_rows' rows; it fails when ps does."""
    return f"/bin/sh -c {shlex.quote(render_ps_rows(name, columns, select) + chr(10) + name)}"


def command_unreadable(argv: list[str]) -> bool:
    """A ps_rows command line it did not read."""
    return argv == [UNREADABLE_COMMAND]


PS_CAPTURE_FUNCTION = render_ps_rows("capture_ps_out", ("pid", "ppid", "stat", "time"), ("service", "diskd"))
PS_CAPTURE_COMMAND = PS_CAPTURE_FUNCTION + "\ncapture_ps_out || true"
# The same listing as one SSH command. A listing that fails prints nothing.
PS_CAPTURE_SSH_COMMAND = f"/bin/sh -c {shlex.quote(PS_CAPTURE_COMMAND)}"
# The launchers and service processes the stop script and idle_jobs tell
# apart by their command lines.
MANAGED_PS_ROWS_FUNCTION = render_ps_rows("managed_ps_rows", ("pid", "stat"), ("sh", "service")) + "\n"


def service_role_lines(ps_output: str, role: str) -> list[str]:
    """Match the live role title, or argv before the native title is installed."""
    rows = []
    for line in ps_output.splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[2].startswith("Z"):
            continue
        if fields[4] == "service" and fields[5] == "service:" and fields[6:7] == [f"role={role}"]:
            rows.append(line)
        elif fields[4] == "service" and fields[5] == "/mnt/Flash/service" and fields[6:7] == [role]:
            role_arguments = fields[7:]
            if role == "discovery" and any(word in role_arguments for word in ("--diskless", "--netbios-name")):
                rows.append(line)
            elif role == "telemetry" and "--daemon" in role_arguments:
                rows.append(line)
    return rows


def service_unreadable(ps_output: str) -> bool:
    """A live service process whose command line ps_rows did not read, in the
    layout service_role_lines reads: it may hold any role."""
    for fields in (line.split() for line in ps_output.splitlines()):
        if len(fields) == 6 and not fields[2].startswith("Z") and fields[4] == "service" \
                and command_unreadable(fields[5:]):
            return True
    return False


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
# NetBSD ps reports the kernel wait channel: a Samba child blocked on disk I/O
# is the usual reason a manager cannot finish stopping.
STUCK_PROCESS_PS_COMMAND = "/bin/ps axww -o pid= -o ppid= -o stat= -o wchan= -o ucomm="
# How the stop script names a service process it waited for in vain without
# reading its command line: asleep in the kernel throughout, like a manager
# that does not stop, and nearly always on a failing disk.
UNREADABLE_SERVICE_STOP_MESSAGE = "process service did not show its command line"


def describe_uninterruptible(stuck_listing: str) -> str:
    """The processes in an uninterruptible sleep (state D, kernel threads
    aside) in STUCK_PROCESS_PS_COMMAND output, for a listing that timed out:
    a ps among them waits for a map lock (see UNREADABLE_COMMAND)."""
    found = []
    for fields in (line.split(None, 4) for line in stuck_listing.splitlines()):
        if len(fields) == 5 and fields[2].startswith("D") and "K" not in fields[2]:
            found.append(f"{fields[4]} (pid {fields[0]}) on {fields[3]}")
    return ", ".join(found)


# Read before any check touches the data disk. Only the manager's title and
# diskd's interface come from command lines, so a process held on the disk
# cannot hold up the listing (see UNREADABLE_COMMAND).
PROCESS_SNAPSHOT_COMMAND = ps_rows_command(
    "process_snapshot", ("pid", "ppid", "pgid", "stat", "sl", "wchan"), ("service", "diskd")
)
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


def _manager_title_stuck(command: str) -> tuple[list[StuckProcess], int]:
    """Entries of the manager's "stuck=PID:COMM:WAIT:SECONDS,...[,+N]" title
    word, and N, how many more it counts than it names."""
    stuck = []
    unnamed = 0
    for word in command.split():
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


def _manager_title_command(ps_output: str) -> str | None:
    for line in ps_output.splitlines():
        fields = line.split(None, 7)
        if len(fields) == 8 and fields[6] == "service" and "role=manager" in fields[7].split():
            return fields[7]
    return None


def manager_unnamed_stuck_count(ps_output: str) -> int:
    """How many stuck processes the manager's title counts beyond the ones it names."""
    command = _manager_title_command(ps_output)
    return _manager_title_stuck(command)[1] if command is not None else 0


def stuck_processes(ps_output: str) -> list[StuckProcess]:
    """Stuck processes in PROCESS_SNAPSHOT_COMMAND output.

    A row asleep uninterruptibly for STUCK_SLEEP_SECONDS, other than a kernel
    thread (state K, which always sleeps this way), and whatever the manager's
    title names, which takes precedence for the same PID.
    """
    by_pid: dict[int, StuckProcess] = {}
    for line in ps_output.splitlines():
        fields = line.split(None, 7)
        if len(fields) < 7:
            continue
        pid, _ppid, _pgid, state, sleep, wchan, name = fields[:7]
        if name == "service" and len(fields) == 8 and "role=manager" in fields[7].split():
            for process in _manager_title_stuck(fields[7])[0]:
                by_pid[process.pid] = process
            continue
        if not state.startswith("D") or "K" in state:
            continue
        try:
            sleep_seconds = int(sleep)
            stuck_pid = int(pid)
        except ValueError:
            continue
        if sleep_seconds >= STUCK_SLEEP_SECONDS and stuck_pid not in by_pid:
            by_pid[stuck_pid] = StuckProcess(
                stuck_pid, name, wchan, sleep_seconds, capped=sleep_seconds >= PS_SLEEP_SECONDS_CAP
            )
    return list(by_pid.values())


# ps's wait channel for a process waiting for another's fcntl() byte-range
# lock: an smbd waiting for a TDB record lock that another process holds.
LOCKF_WCHAN = "lockf"


def smbd_lock_waits(ps_output: str, min_seconds: int = 0) -> list[int]:
    """PIDs of the smbds asleep waiting for an fcntl() lock for at least
    min_seconds, in PROCESS_SNAPSHOT_COMMAND output.

    The wait is interruptible (state I), so stuck_processes() never reports it,
    however long it lasts.
    """
    pids = []
    for fields in (line.split(None, 7) for line in ps_output.splitlines()):
        if fields[5:7] != [LOCKF_WCHAN, "smbd"]:
            continue
        try:
            if int(fields[4]) >= min_seconds:
                pids.append(int(fields[0]))
        except ValueError:
            continue
    return pids


# ps's wait channel for nanosleep: NetBSD 6 names it nanoslp, and NetBSD 4's
# ps cuts nanosleep to eight characters.
NANOSLEEP_WCHANS = frozenset({"nanoslp", "nanoslee"})


def acpd_stuck_children(ps_output: str) -> list[int]:
    """PIDs of ACPd's own forks asleep in nanosleep, in PROCESS_SNAPSHOT_COMMAND output.

    A healthy device runs one ACPd. A second one, ACPd's child that never went
    on to exec, predicted every v3.4.0 reboot that tore ACPd down without
    restarting the kernel. A fork on its way to exec is never in nanosleep.
    Matched on ucomm: sshd and the other daemons are ACPd's children too.
    """
    rows = [line.split(None, 7) for line in ps_output.splitlines()]
    rows = [fields for fields in rows if len(fields) >= 7 and fields[6] == "ACPd"]
    acpd = {fields[0] for fields in rows}
    return [int(fields[0]) for fields in rows if fields[1] in acpd and fields[5] in NANOSLEEP_WCHANS]


def render_stop_service_runtime(*, attempts: int = 5, supervisor_seconds: int = SUPERVISOR_STOP_SECONDS) -> str:
    # Stop every launcher before its workers, including launchers forked during
    # shutdown. Apple's daemons and unrelated `service` processes are not ours.
    # Supervisors get supervisor_seconds of one-second passes; workers and the
    # SIGKILL escalation of launcher shells keep the shorter attempts budget.
    return (MANAGED_PS_ROWS_FUNCTION + r'''
set -f
managed_processes() {
    managed_ps=$(managed_ps_rows) || return 1
    while read -r pid state comm command; do
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
    __STUCK_PS__ 2>/dev/null | while read -r pid ppid state wchan comm; do
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
IDLE_JOBS_FUNCTION = MANAGED_PS_ROWS_FUNCTION + r'''
idle_jobs() (
    # A shell's arguments may hold glob patterns; never expand them.
    set -f
    jobs_ps=$(managed_ps_rows) || exit 1
    while read -r pid state comm rest; do
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


PROBE_PROCESS_HELPERS = (
    r'''
__PS_CAPTURE_FUNCTION__

smbd_parent_process_present() {
    ps_out=$1
    smbd_pids=""
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        set -- $line
        [ "$#" -ge 5 ] || continue
        case "$3" in
            Z*) continue ;;
        esac
        if [ "$5" = "smbd" ]; then
            smbd_pids="$smbd_pids $1"
        fi
    done <<EOF
$ps_out
EOF

    while IFS= read -r line; do
        [ -n "$line" ] || continue
        set -- $line
        [ "$#" -ge 5 ] || continue
        case "$3" in
            Z*) continue ;;
        esac
        if [ "$5" = "smbd" ]; then
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
        [ "$#" -ge 6 ] || continue
        case "$3" in Z*) continue ;; esac
        [ "$5" = service ] || continue
        if [ "$#" -eq 6 ] && [ "$6" = '__UNREADABLE__' ]; then
            role_unreadable="$role_unreadable $1"
            continue
        fi
        [ "$#" -ge 7 ] || continue
        if [ "$6" = 'service:' ] && [ "$7" = "role=$wanted_role" ]; then return 0; fi
        if [ "$6" = /mnt/Flash/service ] && [ "$7" = "$wanted_role" ]; then return 0; fi
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
        [ "$#" -ge 5 ] || continue
        [ "$5" = "$ucomm" ] || continue
        case "$3" in
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
'''
    .replace("__PS_CAPTURE_FUNCTION__", PS_CAPTURE_FUNCTION)
    .replace("__UNREADABLE__", UNREADABLE_COMMAND)
)
