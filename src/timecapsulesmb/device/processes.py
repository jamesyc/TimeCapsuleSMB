from __future__ import annotations

import shlex


WATCHDOG_PATH = "/mnt/Flash/watchdog.sh"
WATCHDOG_KILL_PATTERN = "[w]atchdog.sh"
PS_TEMP_COMMAND = "ps axww -o stat= -o ucomm= -o command= >/tmp/tcapsule-ps.$$ 2>/dev/null"
PS_CAPTURE_COMMAND = "/bin/ps axww -o pid= -o ppid= -o stat= -o time= -o ucomm= -o command= 2>/dev/null || true"
WATCHDOG_PID_PS_COMMAND = "/bin/ps axww -o pid= -o stat= -o ucomm= -o command="


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


def render_process_present(pattern: str, *, full: bool) -> str:
    if not full:
        return render_process_present_by_ucomm(pattern)
    if pattern in {WATCHDOG_PATH, WATCHDOG_KILL_PATTERN}:
        return render_watchdog_process_present()
    raise ValueError(f"Unsupported full process match: {pattern!r}")


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


def render_pkill_wait_pkill9(pattern: str, *, full: bool, attempts: int = 5) -> str:
    if not full:
        return render_pkill_wait_pkill9_by_ucomm(pattern, attempts=attempts)
    if pattern in {WATCHDOG_PATH, WATCHDOG_KILL_PATTERN}:
        return render_pkill_wait_pkill9_watchdog(attempts=attempts)
    raise ValueError(f"Unsupported full process stop: {pattern!r}")


def render_direct_pkill9_by_ucomm(name: str) -> str:
    return f"/usr/bin/pkill -9 {shlex.quote(_ucomm_pkill_pattern(name))} >/dev/null 2>&1 || true"


def render_direct_pkill9_watchdog() -> str:
    return f"{render_watchdog_pid_helpers()}tc_kill_watchdog_pids KILL"


PROBE_PROCESS_HELPERS = (
    r'''
WATCHDOG_PATH=__WATCHDOG_PATH__

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
    process_by_ucomm_present "$1" mdns-advertiser
}

apple_mdns_present() {
    process_by_ucomm_present "$1" mDNSResponder
}

watchdog_process_present_for_volume() {
    ps_out=$1
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        set -- $line
        [ "$#" -ge 6 ] || continue
        case "$3" in
            Z*) continue ;;
        esac
        [ "$5" = "sh" ] || continue
        if [ "${6:-}" = "$WATCHDOG_PATH" ]; then
            return 0
        fi
        if [ "${6:-}" = "/bin/sh" ] || [ "${6:-}" = "sh" ]; then
            [ "${7:-}" = "$WATCHDOG_PATH" ] && return 0
        fi
    done <<EOF
$ps_out
EOF
    return 1
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
    .replace("__WATCHDOG_PATH__", shlex.quote(WATCHDOG_PATH))
    .replace("__PS_CAPTURE_COMMAND__", PS_CAPTURE_COMMAND)
)
