wait_for_process() {
    proc_name=$1
    max_attempts=${2:-10}
    attempt=0
    while [ "$attempt" -lt "$max_attempts" ]; do
        if runtime_process_present_by_ucomm "$proc_name"; then
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    return 1
}

runtime_process_present_by_ucomm() {
    proc_name=$1
    if ps_out=$(/bin/ps axww -o pid= -o stat= -o ucomm= -o command= 2>/dev/null); then
        old_ifs=$IFS
        IFS='
'
        for line in $ps_out; do
            [ -n "$line" ] || continue
            line_ifs=$IFS
            IFS=' 	'
            set -- $line
            IFS=$line_ifs
            [ "$#" -ge 3 ] || continue
            case "$2" in
                Z*) continue ;;
            esac

            if [ "$3" = "$proc_name" ]; then
                IFS=$old_ifs
                return 0
            fi
        done
        IFS=$old_ifs
    fi

    return 1
}

tc_smbd_parent_pid() {
    pid_file="$RAM_VAR/smbd.pid"
    [ -f "$pid_file" ] || return 1

    smbd_pid=$(/bin/cat "$pid_file" 2>/dev/null | /usr/bin/sed -n '1p')
    case "$smbd_pid" in
        ""|*[!0123456789]*) return 1 ;;
    esac

    kill -0 "$smbd_pid" >/dev/null 2>&1 || return 1
    echo "$smbd_pid"
}

runtime_script_pids() {
    script_path=$1

    if ps_out=$(/bin/ps axww -o pid= -o stat= -o ucomm= -o command= 2>/dev/null); then
        old_ifs=$IFS
        IFS='
'
        for line in $ps_out; do
            [ -n "$line" ] || continue
            line_ifs=$IFS
            IFS=' 	'
            set -- $line
            IFS=$line_ifs
            [ "$#" -ge 4 ] || continue
            script_pid=$1
            script_stat=$2
            script_ucomm=$3
            shift 3
            case "$script_stat" in
                Z*) continue ;;
            esac
            [ "$script_ucomm" = "sh" ] || continue
            if [ "${1:-}" = "$script_path" ]; then
                printf '%s\n' "$script_pid"
                continue
            fi
            if [ "${1:-}" = "/bin/sh" ] || [ "${1:-}" = "sh" ]; then
                [ "${2:-}" = "$script_path" ] && printf '%s\n' "$script_pid"
            fi
        done
        IFS=$old_ifs
    fi
}

runtime_watchdog_pids() {
    runtime_script_pids "/mnt/Flash/watchdog.sh"
}

runtime_manager_pids() {
    runtime_script_pids "/mnt/Flash/manager.sh"
}

runtime_watchdog_present() {
    [ -n "$(runtime_watchdog_pids)" ]
}

runtime_manager_present() {
    [ -n "$(runtime_manager_pids)" ]
}

kill_runtime_script_pids() {
    script_signal=$1
    shift
    [ "$#" -gt 0 ] || return 0

    for script_pid do
        case "$script_signal" in
            KILL) /bin/kill -9 "$script_pid" >/dev/null 2>&1 || true ;;
            TERM|"") /bin/kill "$script_pid" >/dev/null 2>&1 || true ;;
            *) return 1 ;;
        esac
    done
}

kill_watchdog_pids() {
    watchdog_signal=$1
    kill_runtime_script_pids "$watchdog_signal" $(runtime_watchdog_pids)
}

kill_manager_pids() {
    manager_signal=$1
    kill_runtime_script_pids "$manager_signal" $(runtime_manager_pids)
}

wait_for_runtime_process_absent_by_ucomm() {
    proc_name=$1
    max_attempts=${2:-5}
    attempt=0

    while runtime_process_present_by_ucomm "$proc_name"; do
        if [ "$attempt" -ge "$max_attempts" ]; then
            return 1
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    return 0
}

wait_for_watchdog_absent() {
    max_attempts=${1:-5}
    attempt=0

    while runtime_watchdog_present; do
        if [ "$attempt" -ge "$max_attempts" ]; then
            return 1
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    return 0
}

wait_for_manager_absent() {
    max_attempts=${1:-5}
    attempt=0

    while runtime_manager_present; do
        if [ "$attempt" -ge "$max_attempts" ]; then
            return 1
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    return 0
}

stop_runtime_process_by_ucomm() {
    label=$1
    proc_name=$2
    case "$proc_name" in
        ""|*[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-]*)
            tc_log "refusing unsafe process name for $label: $proc_name"
            return 1
            ;;
    esac
    pkill_pattern="^$proc_name$"

    tc_log "stopping old $label"
    /usr/bin/pkill "$pkill_pattern" >/dev/null 2>&1 || true

    if wait_for_runtime_process_absent_by_ucomm "$proc_name" 5; then
        return 0
    fi

    tc_log "old $label still running after TERM; sending KILL"
    /usr/bin/pkill -9 "$pkill_pattern" >/dev/null 2>&1 || true

    if wait_for_runtime_process_absent_by_ucomm "$proc_name" 5; then
        return 0
    fi

    tc_log "old $label survived KILL"
    return 1
}

stop_manager_process() {
    tc_log "stopping old manager"
    kill_manager_pids TERM

    if wait_for_manager_absent 5; then
        return 0
    fi

    tc_log "old manager still running after TERM; sending KILL"
    kill_manager_pids KILL

    if wait_for_manager_absent 5; then
        return 0
    fi

    tc_log "old manager survived KILL"
    return 1
}

stop_watchdog_process() {
    tc_log "stopping old watchdog"
    kill_watchdog_pids TERM

    if wait_for_watchdog_absent 5; then
        return 0
    fi

    tc_log "old watchdog still running after TERM; sending KILL"
    kill_watchdog_pids KILL

    if wait_for_watchdog_absent 5; then
        return 0
    fi

    tc_log "old watchdog survived KILL"
    return 1
}

stop_apple_nbns_conflicts() {
    cleanup_status=0

    stop_runtime_process_by_ucomm "wcifsnd" "wcifsnd" || cleanup_status=1
    stop_runtime_process_by_ucomm "wcifsfs" "wcifsfs" || cleanup_status=1

    return "$cleanup_status"
}

stop_nbns_conflicts() {
    cleanup_status=0

    stop_apple_nbns_conflicts || cleanup_status=1
    stop_runtime_process_by_ucomm "$NBNS_PROC_NAME" "$NBNS_PROC_NAME" || cleanup_status=1

    return "$cleanup_status"
}
