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

runtime_manager_pids() {
    runtime_script_pids "/mnt/Flash/manager.sh"
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

# Apple's mDNSResponder is never killed (v3.1.0): it is the only responder
# on the device, ACPd does not respawn it, and a hand-started daemon lacks
# _airport. Our registrations go through its IPC instead.

# Reports how Apple's diskd is running: `loopback` (our relaunch, `-i lo0`),
# `acpd` (ACPd's `-i ""` start, registering _smb/_adisk/_afpovertcp on the
# LAN) or `absent`. Zombies count as absent. `acpd` wins over `loopback`
# when both exist: an ACPd diskd next to ours still advertises on the LAN
# and is what the manager's retry has to remove. TC_APPLE_DISKD_STRAY_PIDS
# lists those non-loopback PIDs so they can be killed without touching ours.
tc_apple_diskd_probe() {
    diskd_state=absent
    TC_APPLE_DISKD_STRAY_PIDS=
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
            [ "$3" = "diskd" ] || continue
            case "$line" in
                *"diskd -i lo0"*) [ "$diskd_state" = "acpd" ] || diskd_state=loopback ;;
                *)
                    diskd_state=acpd
                    TC_APPLE_DISKD_STRAY_PIDS="$TC_APPLE_DISKD_STRAY_PIDS $1"
                    ;;
            esac
        done
        IFS=$old_ifs
    fi
    TC_APPLE_DISKD_STATE=$diskd_state
}

# Printing form for callers that only need the state word. The probe sets
# the globals in this shell; a $(...) call could not.
tc_apple_diskd_state() {
    tc_apple_diskd_probe
    printf '%s\n' "$TC_APPLE_DISKD_STATE"
}

# Kill only the diskd processes that are not ours (`-i lo0`). pkill by name
# would take the loopback one down with them.
tc_signal_pid() {
    /bin/kill -"$1" "$2" 2>/dev/null
}

tc_kill_stray_apple_diskd() {
    tc_apple_diskd_probe
    for stray_pid in $TC_APPLE_DISKD_STRAY_PIDS; do
        tc_log "stopping diskd pid $stray_pid (not on loopback)"
        tc_signal_pid TERM "$stray_pid" || true
    done
    diskd_wait=0
    while :; do
        tc_apple_diskd_probe
        [ -n "$TC_APPLE_DISKD_STRAY_PIDS" ] || return 0
        if [ "$diskd_wait" -ge 10 ]; then
            return 1
        fi
        diskd_wait=$((diskd_wait + 1))
        sleep 1
    done
}

# Bounded (R2): a MaSt read that never returns counts as unavailable, so the
# 30 s relaunch wait and the manager's disk pass really are bounded.
tc_acp_mast_available() {
    mast_probe=$(tc_read_mast) || return 1
    [ -n "$mast_probe" ]
}

# Guide B.8 step 2. diskd is load-bearing (MaSt, diskd.useVolume, spin-down)
# but registers Apple's _smb/_adisk/_afpovertcp unconditionally; relaunched
# with `-i lo0` those registrations never leave loopback while everything
# else keeps working (F7/F8). This is best effort, not a guarantee: a
# failure is logged and returns 1 so the caller can retry (the manager does,
# every service pass with a backoff) and doctor's "diskd runs on loopback"
# check is the gate. Nothing else withdraws a registration diskd owns.
tc_relaunch_diskd_loopback() {
    diskd_state=$(tc_apple_diskd_state)
    case "$diskd_state" in
        loopback)
            tc_log "diskd already running on loopback"
            return 0
            ;;
        absent)
            tc_log "diskd not running; launching it on loopback"
            ;;
        *)
            tc_log "stopping ACPd's diskd so Apple's SMB/AFP names stay off the LAN"
            if ! tc_kill_stray_apple_diskd; then
                tc_log "diskd relaunch failed; Apple SMB/AFP names may be visible (old diskd still running)"
                return 1
            fi
            if [ "$(tc_apple_diskd_state)" = "loopback" ]; then
                tc_log "ACPd's diskd stopped; ours is already running on loopback"
                return 0
            fi
            ;;
    esac
    /sbin/diskd -i lo0 -d local. </dev/null >/dev/null 2>&1 &
    # Elapsed-time bound, not an iteration count: each MaSt probe is itself
    # bounded (R2) and may take its whole allowance while ACPd is wedged.
    diskd_started=$(tc_now_seconds)
    while ! tc_acp_mast_available; do
        diskd_wait=$(tc_elapsed_seconds_since "$diskd_started")
        if [ "$diskd_wait" -ge 30 ]; then
            tc_log "diskd relaunch failed; Apple SMB/AFP names may be visible (MaSt not served after ${diskd_wait}s)"
            return 1
        fi
        sleep 2
    done
    diskd_wait=$(tc_elapsed_seconds_since "$diskd_started")
    tc_log "diskd relaunched on loopback; MaSt available after ${diskd_wait}s"
    return 0
}

# afpserver serves nothing ("No HFS+ volumes") but listens on 548 on every
# interface. macOS 26.x/27 treats an AFP-advertising Time Capsule as
# SMB1-only and hides it, so unless the user opted in it dies at boot.
tc_stop_apple_afpserver() {
    if [ "${MDNS_ADVERTISE_AFP:-0}" = "1" ]; then
        tc_log "leaving Apple afpserver running because MDNS_ADVERTISE_AFP=1"
        return 0
    fi
    if runtime_process_present_by_ucomm afpserver; then
        stop_runtime_process_by_ucomm "Apple afpserver" afpserver || return 1
    fi
    return 0
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

stop_discovery_conflicts() {
    cleanup_status=0

    stop_runtime_process_by_ucomm "wcifsfs" "wcifsfs" || cleanup_status=1
    stop_runtime_process_by_ucomm "wcifsnd" "wcifsnd" || cleanup_status=1
    stop_runtime_process_by_ucomm "legacy mdns advertiser" "mdns-advertiser" || cleanup_status=1
    stop_runtime_process_by_ucomm "legacy nbns advertiser" "nbns-advertiser" || cleanup_status=1

    return "$cleanup_status"
}
