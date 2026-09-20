from __future__ import annotations

import shlex


WATCHDOG_PATH = "/mnt/Flash/watchdog.sh"
WATCHDOG_KILL_PATTERN = "[w]atchdog.sh"
MANAGER_PATH = "/mnt/Flash/manager.sh"
MANAGER_KILL_PATTERN = "[m]anager.sh"
PS_TEMP_COMMAND = "ps axww -o stat= -o ucomm= -o command= >/tmp/tcapsule-ps.$$ 2>/dev/null"
PS_CAPTURE_COMMAND = "/bin/ps axww -o pid= -o ppid= -o stat= -o time= -o ucomm= -o command= 2>/dev/null || true"
WATCHDOG_PID_PS_COMMAND = "/bin/ps axww -o pid= -o stat= -o ucomm= -o command="


def service_role_lines(ps_output: str, role: str) -> list[str]:
    """Match the live role title, or argv before the native title is installed."""
    rows = []
    legacy = {"discovery": "discoveryd", "telemetry": "telemetry"}.get(role)
    for line in ps_output.splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[2].startswith("Z"):
            continue
        arguments = fields[5:]
        diagnostic = any(word in arguments for word in ("--print-link-plan", "--print-mast", "--version", "--print-payload", "--once", "--cleanup"))
        if legacy and fields[4] == legacy and not diagnostic:
            rows.append(line)
        elif fields[4] == "service" and (
            (fields[5] == "service:" and fields[6:7] == [f"role={role}"])
            or (fields[5] == "/mnt/Flash/service" and fields[6:7] == [role] and not diagnostic)
        ):
            rows.append(line)
    return rows


def render_stop_service_runtime(*, attempts: int = 5) -> str:
    # Stop every launcher before its workers, including launchers forked during
    # shutdown. Apple's daemons and unrelated `service` processes are not ours.
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
            label=service
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
                    kind=worker ;;
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
for scope in supervisor worker; do
    attempt=0
    limit=__ATTEMPTS__
    [ "$scope" != supervisor ] || limit=$((limit * 2))
    while :; do
        processes=$(managed_processes) || exit 1
        [ -n "$processes" ] || break
        if [ "$attempt" -gt "$limit" ]; then
            while read -r pid kind label; do
                echo "process $label did not stop; retry after its active work finishes" >&2
            done <<EOF_BUSY
$processes
EOF_BUSY
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
'''.replace("__ATTEMPTS__", str(attempts)).strip()


def render_wait_for_idle_jobs(*, attempts: int = 5) -> str:
    # An interrupted SSH session may leave a migration or diagnostic child.
    # Never unlink its executable or kill it halfway through a metadata write.
    return r'''
attempt=0
while :; do
    jobs_ps=$(/bin/ps axww -o stat= -o ucomm= -o command=) || exit 1
    busy=0
    while read -r state comm rest; do
        case "$state" in Z*|"") continue ;; esac
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
    done <<EOF
$jobs_ps
EOF
    [ "$busy" = 1 ] || exit 0
    if [ "$attempt" -ge __ATTEMPTS__ ]; then
        echo 'migration or diagnostic work is still active; retry after it finishes' >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    sleep 1
done
'''.replace("__ATTEMPTS__", str(attempts)).strip()


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


def render_watchdog_process_present() -> str:
    watchdog_path = shlex.quote(WATCHDOG_PATH)
    return (
        "found=1; "
        f"if {PS_TEMP_COMMAND}; then "
        "found=0; "
        "while IFS= read line; do "
        '[ -n "$line" ] || continue; '
        "set -- $line; "
        '[ "$#" -ge 3 ] || continue; '
        'case "$1" in Z*) continue ;; esac; '
        '[ "$2" = sh ] || continue; '
        f'if [ "${{3:-}}" = {watchdog_path} ]; then found=1; break; fi; '
        'if [ "${3:-}" = /bin/sh ] || [ "${3:-}" = sh ]; then '
        f'if [ "${{4:-}}" = {watchdog_path} ]; then found=1; break; fi; '
        "fi; "
        "done </tmp/tcapsule-ps.$$; "
        "rm -f /tmp/tcapsule-ps.$$; "
        "fi; "
        '[ \"$found\" -eq 1 ]'
    )


def render_manager_process_present() -> str:
    manager_path = shlex.quote(MANAGER_PATH)
    return (
        "found=1; "
        f"if {PS_TEMP_COMMAND}; then "
        "found=0; "
        "while IFS= read line; do "
        '[ -n "$line" ] || continue; '
        "set -- $line; "
        '[ "$#" -ge 3 ] || continue; '
        'case "$1" in Z*) continue ;; esac; '
        '[ "$2" = sh ] || continue; '
        f'if [ "${{3:-}}" = {manager_path} ]; then found=1; break; fi; '
        'if [ "${3:-}" = /bin/sh ] || [ "${3:-}" = sh ]; then '
        f'if [ "${{4:-}}" = {manager_path} ]; then found=1; break; fi; '
        "fi; "
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


def render_watchdog_pid_helpers() -> str:
    watchdog_path = shlex.quote(WATCHDOG_PATH)
    return (
        "tc_watchdog_pids() { "
        "tc_watchdog_ps=/tmp/tcapsule-watchdog-ps.$$; "
        f"if {WATCHDOG_PID_PS_COMMAND} >\"$tc_watchdog_ps\" 2>/dev/null; then "
        "while IFS= read line; do "
        '[ -n "$line" ] || continue; '
        "set -- $line; "
        '[ "$#" -ge 4 ] || continue; '
        "tc_watchdog_pid=$1; "
        "tc_watchdog_stat=$2; "
        "tc_watchdog_ucomm=$3; "
        "shift 3; "
        'case "$tc_watchdog_stat" in Z*) continue ;; esac; '
        '[ "$tc_watchdog_ucomm" = sh ] || continue; '
        f'if [ "${{1:-}}" = {watchdog_path} ]; then printf "%s\\n" "$tc_watchdog_pid"; continue; fi; '
        'if [ "${1:-}" = /bin/sh ] || [ "${1:-}" = sh ]; then '
        f'[ "${{2:-}}" = {watchdog_path} ] && printf "%s\\n" "$tc_watchdog_pid"; '
        "fi; "
        "done <\"$tc_watchdog_ps\"; "
        "fi; "
        "rm -f \"$tc_watchdog_ps\"; "
        "}; "
        "tc_kill_watchdog_pids() { "
        "tc_watchdog_signal=$1; "
        "for tc_watchdog_pid in $(tc_watchdog_pids); do "
        'case "$tc_watchdog_signal" in '
        'KILL) /bin/kill -9 "$tc_watchdog_pid" >/dev/null 2>&1 || true ;; '
        'TERM|"") /bin/kill "$tc_watchdog_pid" >/dev/null 2>&1 || true ;; '
        "*) return 1 ;; "
        "esac; "
        "done; "
        "}; "
    )


def render_manager_pid_helpers() -> str:
    manager_path = shlex.quote(MANAGER_PATH)
    return (
        "tc_manager_pids() { "
        "tc_manager_ps=/tmp/tcapsule-manager-ps.$$; "
        f"if {WATCHDOG_PID_PS_COMMAND} >\"$tc_manager_ps\" 2>/dev/null; then "
        "while IFS= read line; do "
        '[ -n "$line" ] || continue; '
        "set -- $line; "
        '[ "$#" -ge 4 ] || continue; '
        "tc_manager_pid=$1; "
        "tc_manager_stat=$2; "
        "tc_manager_ucomm=$3; "
        "shift 3; "
        'case "$tc_manager_stat" in Z*) continue ;; esac; '
        '[ "$tc_manager_ucomm" = sh ] || continue; '
        f'if [ "${{1:-}}" = {manager_path} ]; then printf "%s\\n" "$tc_manager_pid"; continue; fi; '
        'if [ "${1:-}" = /bin/sh ] || [ "${1:-}" = sh ]; then '
        f'[ "${{2:-}}" = {manager_path} ] && printf "%s\\n" "$tc_manager_pid"; '
        "fi; "
        "done <\"$tc_manager_ps\"; "
        "fi; "
        "rm -f \"$tc_manager_ps\"; "
        "}; "
        "tc_kill_manager_pids() { "
        "tc_manager_signal=$1; "
        "for tc_manager_pid in $(tc_manager_pids); do "
        'case "$tc_manager_signal" in '
        'KILL) /bin/kill -9 "$tc_manager_pid" >/dev/null 2>&1 || true ;; '
        'TERM|"") /bin/kill "$tc_manager_pid" >/dev/null 2>&1 || true ;; '
        "*) return 1 ;; "
        "esac; "
        "done; "
        "}; "
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


def render_pkill_wait_pkill9_watchdog(*, attempts: int = 5) -> str:
    present_command = render_watchdog_process_present()
    wait_command = render_wait_for_process_absent(present_command, attempts=attempts)
    process_present = f"/bin/sh -c {shlex.quote(present_command)} >/dev/null 2>&1"
    failure_message = shlex.quote("process watchdog did not stop")
    return (
        f"{render_watchdog_pid_helpers()}"
        "tc_kill_watchdog_pids TERM; "
        f"{wait_command}; "
        f"if {process_present}; then "
        f"tc_kill_watchdog_pids KILL; {wait_command}; "
        "fi; "
        f"if {process_present}; then echo {failure_message} >&2; exit 1; fi"
    )


def render_pkill_wait_pkill9_manager(*, attempts: int = 5) -> str:
    present_command = render_manager_process_present()
    wait_command = render_wait_for_process_absent(present_command, attempts=attempts)
    process_present = f"/bin/sh -c {shlex.quote(present_command)} >/dev/null 2>&1"
    failure_message = shlex.quote("process manager did not stop")
    return (
        f"{render_manager_pid_helpers()}"
        "tc_kill_manager_pids TERM; "
        f"{wait_command}; "
        f"if {process_present}; then "
        f"tc_kill_manager_pids KILL; {wait_command}; "
        "fi; "
        f"if {process_present}; then echo {failure_message} >&2; exit 1; fi"
    )


def render_direct_pkill9_by_ucomm(name: str) -> str:
    return f"/usr/bin/pkill -9 {shlex.quote(_ucomm_pkill_pattern(name))} >/dev/null 2>&1 || true"


def render_direct_pkill9_watchdog() -> str:
    return f"{render_watchdog_pid_helpers()}tc_kill_watchdog_pids KILL"


def render_direct_pkill9_manager() -> str:
    return f"{render_manager_pid_helpers()}tc_kill_manager_pids KILL"


PROBE_PROCESS_HELPERS = (
    r'''
MANAGER_PATH=__MANAGER_PATH__

capture_ps_out() {
    __PS_CAPTURE_COMMAND__
}

process_by_ucomm_present() {
    ps_out=$1
    ucomm=$2
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        set -- $line
        [ "$#" -ge 5 ] || continue
        case "$3" in
            Z*) continue ;;
        esac
        [ "$5" = "$ucomm" ] && return 0
    done <<EOF
$ps_out
EOF
    return 1
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

mdns_process_present() {
    native_service_role_present "$1" discovery || process_by_ucomm_present "$1" discoveryd
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

apple_mdns_present() {
    process_by_ucomm_present "$1" mDNSResponder
}

runtime_script_process_present() {
    ps_out=$1
    script_path=$2
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        set -- $line
        [ "$#" -ge 6 ] || continue
        case "$3" in
            Z*) continue ;;
        esac
        [ "$5" = "sh" ] || continue
        if [ "${6:-}" = "$script_path" ]; then
            return 0
        fi
        if [ "${6:-}" = "/bin/sh" ] || [ "${6:-}" = "sh" ]; then
            [ "${7:-}" = "$script_path" ] && return 0
        fi
    done <<EOF
$ps_out
EOF
    return 1
}

manager_process_present_for_volume() {
    native_service_role_present "$1" manager || runtime_script_process_present "$1" "$MANAGER_PATH"
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
    .replace("__MANAGER_PATH__", shlex.quote(MANAGER_PATH))
    .replace("__PS_CAPTURE_COMMAND__", PS_CAPTURE_COMMAND)
)
