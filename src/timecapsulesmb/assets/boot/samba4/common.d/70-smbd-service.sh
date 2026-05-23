tc_fstat_line_matches_socket() {
    family=$1
    sock_type=$2
    proto=$3
    port=$4
    line=$5

    case "$line" in
        *" $family $sock_type $proto "*":$port"*) return 0 ;;
        *) return 1 ;;
    esac
}

tc_runtime_process_table() {
    /bin/ps axww -o pid= -o stat= -o ucomm= -o command= 2>/dev/null
}

tc_runtime_fstat_pid() {
    /usr/bin/fstat -p "$1" 2>/dev/null
}

tc_fstat_line_is_ipv4_tcp_445() {
    tc_fstat_line_matches_socket internet stream tcp 445 "$1"
}

tc_fstat_line_is_ipv6_tcp_445() {
    tc_fstat_line_matches_socket internet6 stream tcp 445 "$1"
}

tc_bind_interfaces_have_ipv6() {
    set -- ${TC_SMB_BIND_INTERFACES:-}
    for token in "$@"; do
        case "$token" in
            ::1/128) ;;
            *:*) return 0 ;;
        esac
    done
    return 1
}

tc_smbd_bound_ipv4_445() {
    tc_process_has_fstat_socket smbd internet stream tcp 445
}

tc_smbd_bound_ipv6_445() {
    tc_process_has_fstat_socket smbd internet6 stream tcp 445
}

tc_process_has_fstat_socket() {
    proc_name=$1
    family=$2
    sock_type=$3
    proto=$4
    port=$5

    if ps_out=$(tc_runtime_process_table); then
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
            [ "$3" = "$proc_name" ] || continue

            if fstat_out=$(tc_runtime_fstat_pid "$1"); then
                fstat_ifs=$IFS
                IFS='
'
                for fstat_line in $fstat_out; do
                    if tc_fstat_line_matches_socket "$family" "$sock_type" "$proto" "$port" "$fstat_line"; then
                        IFS=$old_ifs
                        return 0
                    fi
                done
                IFS=$fstat_ifs
            fi
        done
        IFS=$old_ifs
    fi

    return 1
}

tc_smbd_bound_tcp_445() {
    if tc_bind_interfaces_have_nonloopback_ipv4; then
        tc_smbd_bound_ipv4_445 || return 1
    fi
    if tc_bind_interfaces_have_ipv6; then
        tc_smbd_bound_ipv6_445 || return 1
    fi
    if ! tc_bind_interfaces_have_nonloopback_ipv4 && ! tc_bind_interfaces_have_ipv6; then
        tc_smbd_bound_ipv4_445 || return 1
    fi
    return 0
}

tc_fstat_line_is_ipv4_udp_port() {
    tc_fstat_line_matches_socket internet dgram udp "$1" "$2"
}

tc_fstat_line_is_ipv6_udp_port() {
    tc_fstat_line_matches_socket internet6 dgram udp "$1" "$2"
}

tc_process_bound_ipv4_udp_port() {
    proc_name=$1
    port=$2

    tc_process_has_fstat_socket "$proc_name" internet dgram udp "$port"
}

tc_process_bound_ipv6_udp_port() {
    proc_name=$1
    port=$2

    tc_process_has_fstat_socket "$proc_name" internet6 dgram udp "$port"
}

tc_bind_interfaces_have_nonloopback_ipv4() {
    set -- ${TC_SMB_BIND_INTERFACES:-}
    for token in "$@"; do
        case "$token" in
            127.*) ;;
            *.*/*) return 0 ;;
        esac
    done
    return 1
}

tc_nbns_bound_ipv4_udp_137() {
    tc_process_bound_ipv4_udp_port "$NBNS_PROC_NAME" 137
}

tc_mdns_bound_ipv4_udp_5353() {
    tc_process_bound_ipv4_udp_port "$MDNS_PROC_NAME" 5353
}

tc_mdns_bound_ipv6_udp_5353() {
    tc_process_bound_ipv6_udp_port "$MDNS_PROC_NAME" 5353
}

tc_mdns_health_socket_family() {
    families=$(tc_probe_mdns_socket_families) || return $?

    set -- $families
    for family in "$@"; do
        if [ "$family" = "ipv4" ]; then
            printf '%s\n' ipv4
            return 0
        fi
    done
    for family in "$@"; do
        if [ "$family" = "ipv6" ]; then
            printf '%s\n' ipv6
            return 0
        fi
    done
    return 1
}

tc_mdns_bound_udp_5353() {
    family=$(tc_mdns_health_socket_family) || return $?
    case "$family" in
        ipv4) tc_mdns_bound_ipv4_udp_5353 ;;
        ipv6) tc_mdns_bound_ipv6_udp_5353 ;;
        *) return 1 ;;
    esac
}

tc_wait_for_smbd_ipv4_445() {
    max_attempts=${1:-10}
    attempt=0
    while [ "$attempt" -lt "$max_attempts" ]; do
        if tc_smbd_bound_tcp_445; then
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    return 1
}

tc_log_smbd_socket_diagnostics() {
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
            [ "$3" = "smbd" ] || continue

            if fstat_out=$(/usr/bin/fstat -p "$1" 2>/dev/null); then
                fstat_ifs=$IFS
                IFS='
'
                for fstat_line in $fstat_out; do
                    case "$fstat_line" in
                        *":445"*) tc_log "smbd socket diagnostic: $fstat_line" ;;
                    esac
                done
                IFS=$fstat_ifs
            fi
        done
        IFS=$old_ifs
    fi
}

tc_start_smbd() {
    tc_log "starting smbd from $TC_SMBD_BIN with config $TC_SMBD_CONF"
    "$TC_SMBD_BIN" -D -s "$TC_SMBD_CONF"
    if wait_for_process smbd 15 && tc_wait_for_smbd_ipv4_445 15; then
        return 0
    fi
    tc_log "smbd TCP 445 listener was not observed after launch"
    tc_log_smbd_socket_diagnostics
    stop_runtime_process_by_ucomm "smbd" smbd || true
    return 1
}

tc_prepare_smbd_recovery_disk_runtime() {
    recovery_status=0
    recovery_share_count=0

    if ! tc_load_payload_state; then
        tc_log "watchdog recovery: smbd restart skipped; payload state is unavailable"
        return 1
    fi

    tc_log "watchdog recovery: ensuring payload volume is mounted before smbd restart: device=$TC_PAYLOAD_DEVICE root=$TC_PAYLOAD_VOLUME"
    if ! tc_watchdog_wake_or_mount_volume "$TC_PAYLOAD_DEVICE" "$TC_PAYLOAD_VOLUME"; then
        tc_log "watchdog recovery: payload volume unavailable before smbd restart: device=$TC_PAYLOAD_DEVICE root=$TC_PAYLOAD_VOLUME"
        return 1
    fi

    if ! tc_verify_payload_dir "$TC_PAYLOAD_DIR"; then
        tc_log "watchdog recovery: payload directory is invalid before smbd restart: $TC_PAYLOAD_DIR"
        return 1
    fi

    recovery_share_rows=$(tc_runtime_share_rows || true)
    if [ -z "$recovery_share_rows" ]; then
        tc_log "watchdog recovery: active share state missing; smbd restart will use existing config"
        return 0
    fi

    while IFS="$TC_TAB" read -r share_name share_path part_device builtin part_uuid ||
        [ -n "$share_name$share_path$part_device$builtin$part_uuid" ]; do
        [ -n "$part_device" ] || continue
        recovery_share_count=$((recovery_share_count + 1))
        tc_log "watchdog recovery: ensuring active share volume is mounted before smbd restart: share=$share_name device=/dev/$part_device root=/Volumes/$part_device"
        if tc_watchdog_wake_or_mount_volume "/dev/$part_device" "/Volumes/$part_device"; then
            :
        else
            recovery_status=1
        fi
    done <<EOF
$recovery_share_rows
EOF

    if [ "$recovery_share_count" -eq 0 ]; then
        tc_log "watchdog recovery: active share state has no valid rows; smbd restart will use existing config"
        return 0
    fi

    if [ "$recovery_status" -ne 0 ]; then
        tc_log "watchdog recovery: one or more active share volumes are unavailable before smbd restart"
    fi
    return "$recovery_status"
}

tc_start_smbd_if_needed() {
    if runtime_process_present_by_ucomm smbd; then
        if tc_smbd_bound_tcp_445; then
            return 0
        fi
        tc_log "watchdog recovery: smbd is running without required TCP 445 listeners; restarting"
        tc_log_smbd_socket_diagnostics
        stop_runtime_process_by_ucomm "smbd" smbd || return 1
    fi

    if [ ! -x "$TC_SMBD_BIN" ] || [ ! -f "$TC_SMBD_CONF" ]; then
        tc_log "watchdog recovery: smbd is not running, but runtime is not staged yet"
        return 0
    fi

    tc_watchdog_refresh_runtime_identity_for_recovery
    if ! tc_prepare_smbd_recovery_disk_runtime; then
        return 1
    fi
    rm -rf "$LOCKS_ROOT"/* >/dev/null 2>&1 || true
    "$TC_SMBD_BIN" -D -s "$TC_SMBD_CONF" >/dev/null 2>&1 || true
    tc_log "watchdog recovery: smbd restart requested"
    if wait_for_process smbd 15 && tc_wait_for_smbd_ipv4_445 15; then
        return 0
    fi
    tc_log "watchdog recovery: smbd restart failed to bind required TCP 445 listeners"
    tc_log_smbd_socket_diagnostics
    stop_runtime_process_by_ucomm "smbd" smbd || true
    return 1
}

tc_restart_smbd_for_bind_change() {
    restart_reason=$1
    tc_log "watchdog recovery: restarting smbd after bind interface change: $restart_reason"
    stop_runtime_process_by_ucomm "smbd" smbd || return 1
    tc_start_smbd_if_needed
}

tc_reload_smbd_config() {
    smbd_pid=$(tc_smbd_parent_pid || true)
    if [ -z "$smbd_pid" ]; then
        tc_log "watchdog recovery: smbd config reload skipped; missing valid $RAM_VAR/smbd.pid"
        return 1
    fi

    # Samba's parent process reloads services on SIGHUP. Signal only the
    # parent pid file instead of pkilling every smbd child, so active SMB
    # sessions can keep running while new share definitions are loaded.
    if kill -HUP "$smbd_pid" >/dev/null 2>&1; then
        tc_log "watchdog recovery: smbd config reload requested with SIGHUP pid $smbd_pid"
        return 0
    fi

    tc_log "watchdog recovery: smbd config reload failed for pid $smbd_pid"
    return 1
}

tc_start_watchdog() {
    if runtime_watchdog_present; then
        tc_log "watchdog already running"
        return 0
    fi

    tc_log "starting watchdog"
    TC_SMB_BIND_INTERFACES="$TC_SMB_BIND_INTERFACES" /mnt/Flash/watchdog.sh </dev/null >/dev/null 2>&1 &
    watchdog_pid=$!
    tc_log "watchdog launched as pid $watchdog_pid"
}

tc_current_topology_signature() {
    [ -f "$TC_TOPOLOGY_SIGNATURE" ] || return 1
    /bin/cat "$TC_TOPOLOGY_SIGNATURE"
}

tc_topology_changed_from_file() {
    fresh_file=$1
    if [ ! -f "$fresh_file" ]; then
        tc_log "watchdog recovery: MaSt topology check failed; fresh topology snapshot is missing"
        return 1
    fi
    fresh=$(/bin/cat "$fresh_file" 2>/dev/null || true)
    if [ ! -f "$TC_TOPOLOGY_SIGNATURE" ]; then
        tc_log "watchdog recovery: MaSt topology check changed; current topology signature is missing"
        return 0
    fi
    current=$(tc_current_topology_signature || true)
    [ "$current" != "$fresh" ]
}

tc_watchdog_capture_mast_state() {
    capture_volumes_file=$1
    capture_raw_file=$2

    rm -f "$capture_volumes_file" "$capture_raw_file"
    if [ ! -x /usr/bin/acp ]; then
        tc_log "watchdog disk check: MaSt snapshot skipped; /usr/bin/acp is unavailable"
        : >"$capture_volumes_file"
        : >"$capture_raw_file"
        return 1
    fi

    if tc_read_mast_volumes_to "$capture_volumes_file" "$capture_raw_file"; then
        return 0
    fi

    tc_log "watchdog disk check: MaSt snapshot read failed"
    return 1
}

tc_replace_watchdog_mast_snapshot() {
    replace_volumes_file=$1
    replace_raw_file=$2
    replace_new_volumes_file=$3
    replace_new_raw_file=$4

    if [ -f "$replace_new_volumes_file" ]; then
        mv -f "$replace_new_volumes_file" "$replace_volumes_file"
    fi
    if [ -f "$replace_new_raw_file" ]; then
        mv -f "$replace_new_raw_file" "$replace_raw_file"
    fi
}

tc_cleanup_watchdog_mast_temp_files() {
    [ -d "$TC_STATE_DIR" ] || return 0
    rm -f "$TC_STATE_DIR"/watchdog-volumes.tsv.* "$TC_STATE_DIR"/watchdog-mast.raw.*
}

tc_topology_changed_debounced_from_snapshot() {
    snapshot_volumes_file=$1
    snapshot_raw_file=$2

    if ! tc_topology_changed_from_file "$snapshot_volumes_file"; then
        return 1
    fi

    topology_debounce_seconds=$(tc_sanitize_unsigned_integer "$WATCHDOG_TOPOLOGY_DEBOUNCE_SECONDS" 5)
    if [ "$topology_debounce_seconds" != "$WATCHDOG_TOPOLOGY_DEBOUNCE_SECONDS" ]; then
        tc_log "watchdog recovery: invalid WATCHDOG_TOPOLOGY_DEBOUNCE_SECONDS=$WATCHDOG_TOPOLOGY_DEBOUNCE_SECONDS; using 5s"
    fi

    tc_log "watchdog recovery: MaSt topology changed; debouncing ${topology_debounce_seconds}s"
    if [ "$topology_debounce_seconds" -gt 0 ]; then
        sleep "$topology_debounce_seconds"
    fi

    debounce_volumes_file="$snapshot_volumes_file.debounce"
    debounce_raw_file="$snapshot_raw_file.debounce"
    debounce_status=0
    tc_watchdog_capture_mast_state "$debounce_volumes_file" "$debounce_raw_file" || debounce_status=$?
    if [ "$debounce_status" -eq 0 ]; then
        if tc_topology_changed_from_file "$debounce_volumes_file"; then
            tc_replace_watchdog_mast_snapshot "$snapshot_volumes_file" "$snapshot_raw_file" "$debounce_volumes_file" "$debounce_raw_file"
            return 0
        fi

        tc_replace_watchdog_mast_snapshot "$snapshot_volumes_file" "$snapshot_raw_file" "$debounce_volumes_file" "$debounce_raw_file"
        tc_log "watchdog recovery: MaSt topology change cleared after debounce"
        return 1
    fi

    rm -f "$debounce_volumes_file" "$debounce_raw_file"
    tc_log "watchdog recovery: MaSt topology change could not be confirmed after debounce"
    return 1
}

tc_exec_start_samba() {
    reason=$1
    tc_log "watchdog recovery: re-execing start-samba.sh: $reason"
    exec /mnt/Flash/start-samba.sh --reload-disk-runtime
}
