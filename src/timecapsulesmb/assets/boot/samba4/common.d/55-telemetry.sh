tc_stop_telemetry_schedulers() {
    # Never escalate to KILL: a telemetry process may still own a debug child.
    # Match live processes rather than trusting a PID cached by an old manager.
    /usr/bin/pkill '^telemetry$' >/dev/null 2>&1 || true
}

tc_legacy_telemetry_users_present() {
    legacy_ps=$(/bin/ps ax -o stat= -o ucomm= 2>/dev/null) || return 2
    while read -r legacy_stat legacy_command legacy_rest; do
        case "$legacy_stat" in Z*|"") continue ;; esac
        case "$legacy_command" in telemetry|debug|heartbeat) return 0 ;; esac
    done <<EOF
$legacy_ps
EOF
    return 1
}

tc_cleanup_legacy_telemetry() {
    legacy_root="/mnt/Memory/tc-telemetry"
    [ -e "$legacy_root" ] || [ -L "$legacy_root" ] || return 0
    if [ -L "$legacy_root" ] || [ ! -d "$legacy_root" ]; then
        echo "telemetry: refusing unexpected legacy workspace type: $legacy_root" >&2
        return 1
    fi
    # Version 2 did not pass its lock to debug. An unlocked directory alone
    # cannot prove that a legacy job is finished, so check live users first.
    legacy_attempt=0
    while :; do
        if tc_legacy_telemetry_users_present; then
            legacy_status=0
        else
            legacy_status=$?
        fi
        case "$legacy_status" in
            1) break ;;
            2) echo "telemetry: cannot inspect legacy workspace users" >&2; return 1 ;;
        esac
        if [ "$legacy_attempt" -ge 5 ]; then
            echo "telemetry: legacy debug work is still active; cleanup deferred" >&2
            return 75
        fi
        legacy_attempt=$((legacy_attempt + 1))
        sleep 1
    done
    legacy_mounts=$(/sbin/mount 2>/dev/null) || {
        echo "telemetry: cannot inspect legacy workspace mounts" >&2
        return 1
    }
    while IFS= read -r legacy_mount; do
        case "$legacy_mount" in
            *" on $legacy_root "*|*" on $legacy_root/"*)
                echo "telemetry: refusing cleanup of a mounted legacy workspace" >&2
                return 1
                ;;
        esac
    done <<EOF
$legacy_mounts
EOF
    # One-time migration only. rm does not follow symlinks within this verified
    # application tree. New telemetry never creates a job directory.
    /bin/rm -rf "$legacy_root" || {
        echo "telemetry: could not remove legacy workspace" >&2
        return 1
    }
}

tc_prepare_telemetry_reset() {
    tc_stop_telemetry_schedulers
    tc_cleanup_legacy_telemetry
}

tc_cleanup_telemetry_for_uninstall() {
    tc_prepare_telemetry_reset || return $?
    cleanup_bin="${TC_TELEMETRY_BIN:-/mnt/Memory/samba4/sbin/telemetry}"
    cleanup_version=
    if [ -x "$cleanup_bin" ]; then
        cleanup_version=$("$cleanup_bin" --version 2>/dev/null) || cleanup_version=
    fi
    case "$cleanup_version" in ""|*[!0-9]*) cleanup_version=0 ;; esac
    if [ "$cleanup_version" -ge 3 ]; then
        cleanup_attempt=0
        while :; do
            if "$cleanup_bin" --cleanup; then return 0; else cleanup_status=$?; fi
            [ "$cleanup_status" -eq 75 ] || return "$cleanup_status"
            if [ "$cleanup_attempt" -ge 5 ]; then
                echo "telemetry: active work prevents uninstall; retry after debug finishes" >&2
                return 75
            fi
            cleanup_attempt=$((cleanup_attempt + 1))
            sleep 1
        done
    fi
    # An older installation has no fixed-file cleanup API. Do not remove these
    # names without kernel ownership if its binary is absent or incompatible.
    for cleanup_path in "/mnt/Memory/debug" "/mnt/Memory/debug.sig"; do
        if [ -e "$cleanup_path" ] || [ -L "$cleanup_path" ]; then
            echo "telemetry: fixed debug files remain but the cleanup helper is unavailable" >&2
            return 1
        fi
    done
}
