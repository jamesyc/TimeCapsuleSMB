from __future__ import annotations

import shlex
from dataclasses import dataclass


PS_TEMP_COMMAND = "ps axww -o stat= -o ucomm= -o command= >/tmp/tcapsule-ps.$$ 2>/dev/null"
PS_CAPTURE_COMMAND = "/bin/ps axww -o pid= -o ppid= -o stat= -o time= -o ucomm= -o command= 2>/dev/null || true"


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


# The manager gives each child TC_CHILD_GRACE_MS (build/native/common/process.h)
# to exit after SIGTERM, then sends SIGKILL, reaps it and only then exits itself.
# Stopping it takes up to that grace plus the kill and reap. v3.1.1 waited about
# 10 s and reported healthy managers as stuck; a rerun minutes later worked.
# Twice the grace also covers a slow NetBSD 4 process scan on each pass.
MANAGER_CHILD_GRACE_SECONDS = 10
SUPERVISOR_STOP_SECONDS = 2 * MANAGER_CHILD_GRACE_SECONDS
# NetBSD ps reports the kernel wait channel: a Samba child blocked on disk I/O
# is the usual reason a manager cannot finish stopping.
STUCK_PROCESS_PS_COMMAND = "/bin/ps axww -o pid= -o ppid= -o stat= -o wchan= -o ucomm="

# ps reads the process table through sysctl, not from the disk, so this still
# answers while processes are blocked on the data disk.
PROCESS_SNAPSHOT_COMMAND = "/bin/ps axww -o pid= -o ppid= -o pgid= -o stat= -o sl= -o wchan= -o ucomm= -o command="
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


def render_stop_service_runtime(*, attempts: int = 5, supervisor_seconds: int = SUPERVISOR_STOP_SECONDS) -> str:
    # Stop every launcher before its workers, including launchers forked during
    # shutdown. Apple's daemons and unrelated `service` processes are not ours.
    # Supervisors get supervisor_seconds of one-second passes; workers and the
    # SIGKILL escalation of launcher shells keep the shorter attempts budget.
    return r'''
set -f
managed_processes() {
    managed_ps=$(/bin/ps axww -o pid= -o stat= -o ucomm= -o command=) || return 1
    while read -r pid state comm command; do
        case "$state" in Z*|"") continue ;; esac
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
                echo "process $label did not stop; retry after its active work finishes" >&2
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
            if [ "$kind" = shell ] && [ "$attempt" -gt 0 ] && [ "$attempt" -ge __ATTEMPTS__ ]; then
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
'''.replace("__ATTEMPTS__", str(attempts)).replace(
        "__SUPERVISOR_SECONDS__", str(supervisor_seconds)
    ).replace("__STUCK_PS__", STUCK_PROCESS_PS_COMMAND).strip()


# Metadata and diagnostic jobs that outlive the SSH session that started them:
# the native migrator (ucomm truncated to 15 characters), shell migrations of
# older releases, and telemetry/debug jobs. Prints "pid ucomm" for each.
# device.migration_jobs matches the same migrators in Python; keep them in step.
IDLE_JOBS_FUNCTION = r'''
idle_jobs() {
    jobs_ps=$(/bin/ps axww -o pid= -o stat= -o ucomm= -o command=) || return 1
    while read -r pid state comm rest; do
        case "$state" in Z*|"") continue ;; esac
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
}
'''


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
        while read -r pid comm; do
            echo "job $comm (pid $pid) did not stop" >&2
        done <<EOF_BUSY
$busy
EOF_BUSY
        exit 1
    fi
    signal=TERM
    [ "$attempt" -lt __ATTEMPTS__ ] || signal=KILL
    while read -r pid comm; do
        /bin/kill -$signal "$pid" 2>/dev/null || true
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
    return (
        "found=1; "
        f"if {PS_TEMP_COMMAND}; then "
        "found=0; "
        "while IFS= read line; do "
        '[ -n "$line" ] || continue; '
        "set -- $line; "
        '[ "$#" -ge 2 ] || continue; '
        'case "$1" in Z*) continue ;; esac; '
        f'if [ "$2" = {shlex.quote(name)} ]; then found=1; break; fi; '
        "done </tmp/tcapsule-ps.$$; "
        "rm -f /tmp/tcapsule-ps.$$; "
        "fi; "
        '[ \"$found\" -eq 1 ]'
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


def _render_pkill_wait_pkill9(
    *,
    term_pattern: str,
    kill_pattern: str,
    full: bool,
    present_command: str,
    failure_label: str,
    attempts: int,
) -> str:
    term_flags = "-f " if full else ""
    kill_flags = "-9 -f " if full else "-9 "
    term_command = f"/usr/bin/pkill {term_flags}{shlex.quote(term_pattern)} >/dev/null 2>&1 || true"
    kill_command = f"/usr/bin/pkill {kill_flags}{shlex.quote(kill_pattern)} >/dev/null 2>&1 || true"
    wait_command = render_wait_for_process_absent(present_command, attempts=attempts)
    process_present = f"/bin/sh -c {shlex.quote(present_command)} >/dev/null 2>&1"
    failure_message = shlex.quote(f"process {failure_label} did not stop")
    return (
        f"{term_command}; "
        f"{wait_command}; "
        f"if {process_present}; then "
        f"{kill_command}; {wait_command}; "
        "fi; "
        f"if {process_present}; then echo {failure_message} >&2; exit 1; fi"
    )


def render_pkill_wait_pkill9_by_ucomm(name: str, *, attempts: int = 5) -> str:
    pattern = _ucomm_pkill_pattern(name)
    return _render_pkill_wait_pkill9(
        term_pattern=pattern,
        kill_pattern=pattern,
        full=False,
        present_command=render_process_present_by_ucomm(name),
        failure_label=name,
        attempts=attempts,
    )


PROBE_PROCESS_HELPERS = (
    r'''
capture_ps_out() {
    __PS_CAPTURE_COMMAND__
}

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

native_service_role_present() {
    role_ps=$1
    wanted_role=$2
    while IFS= read -r role_line; do
        set -- $role_line
        [ "$#" -ge 7 ] || continue
        case "$3" in Z*) continue ;; esac
        [ "$5" = service ] || continue
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
    .replace("__PS_CAPTURE_COMMAND__", PS_CAPTURE_COMMAND)
)
