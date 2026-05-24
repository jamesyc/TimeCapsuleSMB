tc_cleanup_old_runtime() {
    cleanup_status=0

    tc_log "cleaning old managed runtime processes and RAM state"
    stop_manager_process || cleanup_status=1
    stop_watchdog_process || cleanup_status=1
    stop_runtime_process_by_ucomm "smbd" "smbd" || cleanup_status=1
    stop_runtime_process_by_ucomm "$MDNS_PROC_NAME" "$MDNS_PROC_NAME" || cleanup_status=1
    stop_runtime_process_by_ucomm "$NBNS_PROC_NAME" "$NBNS_PROC_NAME" || cleanup_status=1

    if [ "$cleanup_status" -ne 0 ]; then
        tc_log "old managed runtime cleanup failed; refusing to delete /mnt/Memory/samba4"
        return 1
    fi

    rm -rf /mnt/Memory/samba4
    tc_log "old managed runtime cleanup complete"
}

tc_locks_root_is_mounted() {
    df_line=$(/bin/df -k "$LOCKS_ROOT" 2>/dev/null | /usr/bin/tail -n +2 || true)
    case "$df_line" in
        *" $LOCKS_ROOT") return 0 ;;
    esac
    return 1
}

tc_prepare_locks_ramdisk() {
    mkdir -p "$LOCKS_ROOT"

    if tc_locks_root_is_mounted; then
        rm -rf "$LOCKS_ROOT"/* >/dev/null 2>&1 || true
        tc_log "cleared existing $LOCKS_ROOT mount contents"
        return 0
    fi

    kernel_release=$(/usr/bin/uname -r 2>/dev/null || true)
    case "$kernel_release" in
        6.*)
            if /sbin/mount_tmpfs -s 9m tmpfs "$LOCKS_ROOT" >/dev/null 2>&1; then
                rm -rf "$LOCKS_ROOT"/* >/dev/null 2>&1 || true
                tc_log "mounted $LOCKS_ROOT tmpfs for Samba lock directory"
                return 0
            fi
            tc_log "failed to mount $LOCKS_ROOT tmpfs; using plain directory fallback"
            rm -rf "$LOCKS_ROOT"/* >/dev/null 2>&1 || true
            return 0
            ;;
        *)
            if /sbin/mount_mfs -s 18432 swap "$LOCKS_ROOT" >/dev/null 2>&1; then
                rm -rf "$LOCKS_ROOT"/* >/dev/null 2>&1 || true
                tc_log "mounted $LOCKS_ROOT mfs for Samba lock directory"
                return 0
            fi
            tc_log "failed to mount $LOCKS_ROOT mfs; refusing rootfs fallback"
            return 1
            ;;
    esac
}

tc_prepare_legacy_prefix() {
    mkdir -p /root
    for legacy_prefix in \
        "$LEGACY_PREFIX_NETBSD7" \
        "$LEGACY_PREFIX_NETBSD4" \
        "$LEGACY_PREFIX_NETBSD4LE" \
        "$LEGACY_PREFIX_NETBSD4BE"
    do
        rm -rf "$legacy_prefix"
        ln -s "$RAM_ROOT" "$legacy_prefix"
    done
}

tc_prepare_ram_root() {
    mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_VAR" "$RAM_ROOT/locks" "$RAM_PRIVATE"
    mkdir -p "$RAM_VAR/run/ncalrpc" "$RAM_VAR/cores"
    chmod 755 "$RAM_ROOT" "$RAM_SBIN" "$RAM_ETC" "$RAM_VAR" "$RAM_ROOT/locks" "$RAM_PRIVATE"
    chmod 755 "$RAM_VAR/run" "$RAM_VAR/run/ncalrpc"
    chmod 700 "$RAM_VAR/cores"
}

tc_tune_kernel_memory() {
    current_bufcache=$(/sbin/sysctl -n vm.bufcache 2>/dev/null || true)
    if [ -z "$current_bufcache" ]; then
        tc_log "kernel memory tuning skipped; vm.bufcache unavailable"
        return 0
    fi

    if [ "$current_bufcache" = "$TC_SAMBA_VM_BUFCACHE" ]; then
        tc_log "kernel memory tuning: vm.bufcache already $TC_SAMBA_VM_BUFCACHE"
        return 0
    fi

    if /sbin/sysctl -w "vm.bufcache=$TC_SAMBA_VM_BUFCACHE" >/dev/null 2>&1; then
        new_bufcache=$(/sbin/sysctl -n vm.bufcache 2>/dev/null || echo "$TC_SAMBA_VM_BUFCACHE")
        tc_log "kernel memory tuning: vm.bufcache $current_bufcache -> $new_bufcache"
    else
        tc_log "kernel memory tuning failed to set vm.bufcache=$TC_SAMBA_VM_BUFCACHE; continuing"
    fi
}

tc_hosts_has_hostname() {
    hosts_target=$1
    hosts_target_local="${hosts_target}.local"

    [ -r /etc/hosts ] || return 1

    while read hosts_addr hosts_names || [ -n "$hosts_addr$hosts_names" ]; do
        case "$hosts_addr" in
            ""|\#*) continue ;;
        esac

        for hosts_name in $hosts_names; do
            case "$hosts_name" in
                \#*) break ;;
            esac
            if [ "$hosts_name" = "$hosts_target" ] || [ "$hosts_name" = "$hosts_target_local" ]; then
                return 0
            fi
        done
    done </etc/hosts

    return 1
}

tc_prepare_local_hostname_resolution() {
    device_hostname=$(/bin/hostname 2>/dev/null || true)
    if [ -z "$device_hostname" ]; then
        tc_log "local hostname resolution skipped; hostname unavailable"
        return 0
    fi

    if tc_hosts_has_hostname "$device_hostname"; then
        tc_smbd_debug_log "local hostname resolution already present for $device_hostname"
    elif printf '127.0.0.1\t%s %s.local\n' "$device_hostname" "$device_hostname" >>/etc/hosts; then
        tc_log "local hostname resolution prepared for $device_hostname"
    else
        tc_log "local hostname resolution could not update /etc/hosts"
    fi
}

derive_airport_fields() {
    iface_mac=$1

    AIRPORT_INSTANCE_NAME=$(get_airport_system_name || true)
    AIRPORT_HOST_LABEL=$(get_airport_host_label || true)
    AIRPORT_WAMA=
    AIRPORT_RAMA=$(get_radio_mac bwl0 || true)
    AIRPORT_RAM2=$(get_radio_mac bwl1 || true)
    AIRPORT_RAST=$(get_airport_rast || true)
    AIRPORT_RANA=$(get_airport_rana || true)
    AIRPORT_SYFL=$(get_airport_syfl || true)
    AIRPORT_SRCV=$(get_airport_acp_value srcv || get_airport_srcv || true)
    AIRPORT_SYVS=$(get_airport_syvs "$AIRPORT_SRCV" || true)
    AIRPORT_BJSD=$(get_airport_bjsd || true)
    AIRPORT_WAMA=$iface_mac
    AIRPORT_SYAP=$(get_airport_syap || true)
    MDNS_DEVICE_MODEL=$(get_airport_mdns_model "$AIRPORT_SYAP" || true)
    [ -n "$MDNS_DEVICE_MODEL" ] || MDNS_DEVICE_MODEL=TimeCapsule

    if [ -n "$AIRPORT_WAMA" ] || [ -n "$AIRPORT_RAMA" ] || [ -n "$AIRPORT_RAM2" ] || [ -n "$AIRPORT_SRCV" ] || [ -n "$AIRPORT_SYVS" ]; then
        return 0
    fi
    return 1
}

tc_log_mdns_snapshot_age() {
    snapshot_path=$1
    if [ ! -f "$snapshot_path" ]; then
        tc_log "trusted Apple mDNS snapshot missing at $snapshot_path"
        return 1
    fi

    tc_log "trusted Apple mDNS snapshot present: $snapshot_path"
    return 0
}

tc_prepare_mdns_identity() {
    iface_mac=${1:-}
    context=$2

    if [ ! -x "$TC_MDNS_BIN" ]; then
        tc_log "$context: mdns skipped; missing $TC_MDNS_BIN"
        return 1
    fi

    if [ "${TC_AIRPORT_FIELDS_READY:-0}" = "1" ]; then
        tc_log "$context: using cached airport fields mac=${TC_AIRPORT_FIELDS_ADVERTISE_MAC:-missing}"
        return 0
    fi

    [ -n "$iface_mac" ] || iface_mac=$(tc_select_advertise_mac "" || true)
    TC_AIRPORT_FIELDS_ADVERTISE_MAC=$iface_mac
    tc_log "$context: advertise auto-ip mac=${iface_mac:-missing}"
    if [ -z "$iface_mac" ]; then
        tc_log "$context: mdns skipped; missing advertise MAC address"
        return 1
    fi

    if derive_airport_fields "$iface_mac"; then
        tc_log "$context: derived airport fields instance=${AIRPORT_INSTANCE_NAME:-missing} host=${AIRPORT_HOST_LABEL:-missing} model=${MDNS_DEVICE_MODEL:-missing} wama=${AIRPORT_WAMA:-missing} rama=${AIRPORT_RAMA:-missing} ram2=${AIRPORT_RAM2:-missing} rast=${AIRPORT_RAST:-missing} rana=${AIRPORT_RANA:-missing} syfl=${AIRPORT_SYFL:-missing} syap=${AIRPORT_SYAP:-missing} syvs=${AIRPORT_SYVS:-missing} srcv=${AIRPORT_SRCV:-missing} bjsd=${AIRPORT_BJSD:-missing}"
    else
        tc_log "$context: airport clone fields incomplete; skipping _airport._tcp advertisement"
    fi
    TC_AIRPORT_FIELDS_READY=1
    return 0
}

tc_run_mdns_snapshot_command() {
    log_context=$1
    shift

    if tc_prepare_runtime_log_file "$TC_MDNS_LOG_FILE"; then
        if tc_runtime_logs_unbounded; then
            tc_log "$log_context: debug logging enabled at $TC_MDNS_LOG_FILE"
        else
            tc_log "$log_context: logging at $TC_MDNS_LOG_FILE"
        fi
        printf '%s %s: launching mdns-advertiser %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$TC_LOG_PREFIX" "$log_context" >>"$TC_MDNS_LOG_FILE"
        if "$@" >>"$TC_MDNS_LOG_FILE" 2>&1; then
            return 0
        fi
    else
        tc_log "$log_context: log unavailable at $TC_MDNS_LOG_FILE"
        if "$@" >/dev/null 2>&1; then
            return 0
        fi
    fi
    return 1
}

tc_mdns_auto_ip_available() {
    tc_probe_mdns_socket_families >/dev/null 2>&1
}

tc_nbns_auto_ip_available() {
    tc_probe_nbns_socket_families >/dev/null 2>&1
}

tc_mark_mdns_deferred_no_ip() {
    TC_WATCHDOG_MDNS_DEFERRED_NO_IP=1
    if [ "$TC_MDNS_AUTO_IP_WAIT_LOGGED" != "1" ]; then
        tc_log "mDNS startup deferred; no usable address has appeared yet"
        TC_MDNS_AUTO_IP_WAIT_LOGGED=1
    fi
}

tc_ensure_mdns_auto_ip_seen() {
    if [ "$TC_MDNS_AUTO_IP_SEEN" = "1" ]; then
        return 0
    fi

    if [ ! -x "$TC_MDNS_BIN" ]; then
        TC_WATCHDOG_MDNS_UNAVAILABLE=1
        tc_log "mDNS auto-ip check failed; missing $TC_MDNS_BIN"
        return 1
    fi

    tc_log "mDNS auto-ip check: running $TC_MDNS_BIN --print-mdns-socket-families"
    if tc_mdns_auto_ip_available; then
        TC_MDNS_AUTO_IP_SEEN=1
        tc_log "mDNS auto-ip check: usable address is available"
        tc_log "mDNS auto-ip is available; starting capture and advertiser"
        return 0
    else
        mdns_auto_ip_status=$?
    fi

    if tc_auto_ip_unavailable_status "$mdns_auto_ip_status"; then
        tc_log "mDNS auto-ip check: no usable address yet"
        tc_mark_mdns_deferred_no_ip
    else
        TC_WATCHDOG_MDNS_UNAVAILABLE=1
        tc_log "mDNS auto-ip check failed with exit code $mdns_auto_ip_status"
    fi
    return 1
}

tc_run_mdns_capture() {
    skip_fresh_snapshot=$1

    if ! tc_prepare_mdns_identity "" "mdns capture"; then
        return 1
    fi

    if [ "$skip_fresh_snapshot" = "1" ]; then
        tc_log "starting mDNS snapshot capture"
    else
        tc_log "starting mDNS snapshot capture without freshness skip"
    fi
    set -- "$TC_MDNS_BIN" \
        --save-all-snapshot "$ALL_MDNS_SNAPSHOT" \
        --save-snapshot "$APPLE_MDNS_SNAPSHOT"
    if [ "$skip_fresh_snapshot" = "1" ]; then
        set -- "$@" --skip-capture-if-snapshot-newer-than-boot "$APPLE_MDNS_SNAPSHOT"
    fi
    set -- "$@" --auto-ip
    if [ -n "${AIRPORT_WAMA:-}" ] || [ -n "${AIRPORT_RAMA:-}" ] || [ -n "${AIRPORT_RAM2:-}" ] || [ -n "${AIRPORT_RAST:-}" ] || [ -n "${AIRPORT_RANA:-}" ] || [ -n "${AIRPORT_SYFL:-}" ] || [ -n "${AIRPORT_SYAP:-}" ] || [ -n "${AIRPORT_SYVS:-}" ] || [ -n "${AIRPORT_SRCV:-}" ] || [ -n "${AIRPORT_BJSD:-}" ]; then
        set -- "$@" \
            --airport-wama "$AIRPORT_WAMA" \
            --airport-rama "$AIRPORT_RAMA" \
            --airport-ram2 "$AIRPORT_RAM2" \
            --airport-rast "$AIRPORT_RAST" \
            --airport-rana "$AIRPORT_RANA" \
            --airport-syfl "$AIRPORT_SYFL" \
            --airport-syap "$AIRPORT_SYAP" \
            --airport-syvs "$AIRPORT_SYVS" \
            --airport-srcv "$AIRPORT_SRCV" \
            --airport-bjsd "$AIRPORT_BJSD"
    fi

    if tc_prepare_runtime_log_file "$TC_MDNS_LOG_FILE"; then
        if tc_runtime_logs_unbounded; then
            tc_log "mdns capture: debug logging enabled at $TC_MDNS_LOG_FILE"
        else
            tc_log "mdns capture: logging at $TC_MDNS_LOG_FILE"
        fi
        printf '%s %s: launching mdns-advertiser capture\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$TC_LOG_PREFIX" >>"$TC_MDNS_LOG_FILE"
        if "$@" >>"$TC_MDNS_LOG_FILE" 2>&1; then
            if [ -s "$APPLE_MDNS_SNAPSHOT" ]; then
                tc_log "mDNS snapshot capture finished"
                return 0
            fi
            tc_log "mDNS snapshot capture completed without trusted Apple snapshot"
        else
            tc_log "mDNS snapshot capture exited with failure; final advertiser will use generated records if needed"
        fi
    else
        tc_log "mdns capture: log unavailable at $TC_MDNS_LOG_FILE"
        if "$@" >/dev/null 2>&1; then
            if [ -s "$APPLE_MDNS_SNAPSHOT" ]; then
                tc_log "mDNS snapshot capture finished"
                return 0
            fi
            tc_log "mDNS snapshot capture completed without trusted Apple snapshot"
        else
            tc_log "mDNS snapshot capture exited with failure; final advertiser will use generated records if needed"
        fi
    fi
    return 1
}

tc_start_mdns_capture() {
    tc_run_mdns_capture 1 || true
}

tc_capture_mdns_snapshot_for_manager() {
    tc_run_mdns_capture 0
}

tc_mdnsresponder_alive() {
    runtime_process_present_by_ucomm mDNSResponder
}

tc_mdns_snapshot_newer_than_boot() {
    if [ ! -x "$TC_MDNS_BIN" ]; then
        tc_log "mDNS snapshot freshness check skipped; missing $TC_MDNS_BIN"
        return 1
    fi

    if tc_run_mdns_snapshot_command "snapshot freshness" "$TC_MDNS_BIN" --snapshot-newer-than-boot "$APPLE_MDNS_SNAPSHOT"; then
        tc_log "trusted Apple mDNS snapshot is newer than current boot: $APPLE_MDNS_SNAPSHOT"
        return 0
    fi

    tc_log "trusted Apple mDNS snapshot is missing, stale, or freshness check failed: $APPLE_MDNS_SNAPSHOT"
    return 1
}

tc_generate_mdns() {
    if ! tc_prepare_mdns_identity "" "mdns generation"; then
        return 0
    fi

    tc_log "generating AirPort mDNS snapshot"
    set -- "$TC_MDNS_BIN" \
        --save-airport-snapshot "$APPLE_MDNS_SNAPSHOT" \
        --instance "$AIRPORT_INSTANCE_NAME" \
        --host "$AIRPORT_HOST_LABEL"
    if [ -n "${AIRPORT_WAMA:-}" ] || [ -n "${AIRPORT_RAMA:-}" ] || [ -n "${AIRPORT_RAM2:-}" ] || [ -n "${AIRPORT_RAST:-}" ] || [ -n "${AIRPORT_RANA:-}" ] || [ -n "${AIRPORT_SYFL:-}" ] || [ -n "${AIRPORT_SYAP:-}" ] || [ -n "${AIRPORT_SYVS:-}" ] || [ -n "${AIRPORT_SRCV:-}" ] || [ -n "${AIRPORT_BJSD:-}" ]; then
        set -- "$@" \
            --airport-wama "$AIRPORT_WAMA" \
            --airport-rama "$AIRPORT_RAMA" \
            --airport-ram2 "$AIRPORT_RAM2" \
            --airport-rast "$AIRPORT_RAST" \
            --airport-rana "$AIRPORT_RANA" \
            --airport-syfl "$AIRPORT_SYFL" \
            --airport-syap "$AIRPORT_SYAP" \
            --airport-syvs "$AIRPORT_SYVS" \
            --airport-srcv "$AIRPORT_SRCV" \
            --airport-bjsd "$AIRPORT_BJSD"
    fi

    if tc_run_mdns_snapshot_command "airport snapshot" "$@"; then
        tc_log "mDNS AirPort snapshot generated"
        return 0
    fi

    tc_log "mDNS AirPort snapshot generation failed; final advertiser will use generated records if needed"
}

tc_finalize_mdns_snapshot_after_capture() {
    if [ -s "$APPLE_MDNS_SNAPSHOT" ]; then
        tc_log_mdns_snapshot_age "$APPLE_MDNS_SNAPSHOT" || true
        return 0
    fi

    tc_log "mDNS snapshot capture did not produce trusted Apple snapshot; generating AirPort fallback"
    tc_generate_mdns
    if [ -s "$APPLE_MDNS_SNAPSHOT" ]; then
        tc_log_mdns_snapshot_age "$APPLE_MDNS_SNAPSHOT" || true
    else
        tc_log "mdns advertiser will fall back to generated records"
    fi
}

tc_launch_mdns_advertiser() {
    context=$1
    kill_prior=$2
    wait_attempts=$3
    diskless=${4:-0}
    tc_ensure_runtime_identity

    if ! tc_prepare_mdns_identity "" "$context"; then
        return 0
    fi
    iface_mac=$TC_AIRPORT_FIELDS_ADVERTISE_MAC

    if [ "$kill_prior" = "1" ]; then
        tc_log "$context: killing prior $MDNS_PROC_NAME processes"
        stop_runtime_process_by_ucomm "$MDNS_PROC_NAME" "$MDNS_PROC_NAME" || true
    fi

    if [ "$diskless" = "1" ]; then
        tc_log "$context: starting mdns advertiser in diskless auto-ip mode"
    else
        tc_log "$context: starting mdns advertiser in auto-ip mode"
    fi
    set -- "$TC_MDNS_BIN" \
        --load-snapshot "$APPLE_MDNS_SNAPSHOT" \
        --instance "$MDNS_INSTANCE_NAME" \
        --host "$MDNS_HOST_LABEL" \
        --device-model "${MDNS_DEVICE_MODEL:-TimeCapsule}"
    if [ "$diskless" = "1" ]; then
        set -- "$@" --diskless
    fi
    if [ -n "${AIRPORT_WAMA:-}" ] || [ -n "${AIRPORT_RAMA:-}" ] || [ -n "${AIRPORT_RAM2:-}" ] || [ -n "${AIRPORT_RAST:-}" ] || [ -n "${AIRPORT_RANA:-}" ] || [ -n "${AIRPORT_SYFL:-}" ] || [ -n "${AIRPORT_SYAP:-}" ] || [ -n "${AIRPORT_SYVS:-}" ] || [ -n "${AIRPORT_SRCV:-}" ] || [ -n "${AIRPORT_BJSD:-}" ]; then
        set -- "$@" \
            --airport-wama "$AIRPORT_WAMA" \
            --airport-rama "$AIRPORT_RAMA" \
            --airport-ram2 "$AIRPORT_RAM2" \
            --airport-rast "$AIRPORT_RAST" \
            --airport-rana "$AIRPORT_RANA" \
            --airport-syfl "$AIRPORT_SYFL" \
            --airport-syap "$AIRPORT_SYAP" \
            --airport-syvs "$AIRPORT_SYVS" \
            --airport-srcv "$AIRPORT_SRCV" \
            --airport-bjsd "$AIRPORT_BJSD"
    fi
    if [ "$diskless" != "1" ] && [ -s "$TC_ADISK_TSV" ]; then
        set -- "$@" \
            --adisk-shares-file "$TC_ADISK_TSV" \
            --adisk-sys-wama "$iface_mac"
    fi
    set -- "$@" --auto-ip

    if tc_prepare_runtime_log_file "$TC_MDNS_LOG_FILE"; then
        if tc_runtime_logs_unbounded; then
            tc_log "$context: debug logging enabled at $TC_MDNS_LOG_FILE"
        else
            tc_log "$context: logging at $TC_MDNS_LOG_FILE"
        fi
        printf '%s %s: launching mdns-advertiser\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$TC_LOG_PREFIX" >>"$TC_MDNS_LOG_FILE"
        "$@" >>"$TC_MDNS_LOG_FILE" 2>&1 &
    else
        tc_log "$context: log unavailable at $TC_MDNS_LOG_FILE"
        "$@" >/dev/null 2>&1 &
    fi
    mdns_launch_pid=$!
    tc_log "$context: launched background pid $mdns_launch_pid"
    if [ "$wait_attempts" -gt 0 ]; then
        if wait_for_process "$MDNS_PROC_NAME" "$wait_attempts"; then
            tc_log "$context: mdns advertiser launch requested"
        else
            tc_log "$context: mdns advertiser failed to stay running"
        fi
    fi
}

tc_start_mdns_advertiser() {
    tc_finalize_mdns_snapshot_after_capture
    if tc_load_payload_state; then
        tc_launch_mdns_advertiser "mdns startup" 1 100 0
    else
        tc_launch_mdns_advertiser "mdns startup" 1 100 1
    fi
}

tc_restart_mdns() {
    if tc_load_payload_state; then
        tc_launch_mdns_advertiser "watchdog recovery" 1 0 0
    else
        tc_launch_mdns_advertiser "watchdog recovery" 1 0 1
    fi
}

tc_launch_nbns() {
    context=$1
    wait_attempts=$2
    tc_ensure_runtime_identity

    if [ "$NBNS_ENABLED" != "1" ]; then
        tc_log "$context: nbns responder skipped; disabled in $TC_CONFIG_FILE"
        return 0
    fi

    if [ ! -x "$TC_NBNS_BIN" ]; then
        tc_log "$context: nbns responder launch skipped; missing runtime binary"
        return 0
    fi

    if [ "$context" = "watchdog recovery" ]; then
        stop_apple_nbns_conflicts || {
            tc_log "$context: nbns responder launch skipped; conflicting Apple CIFS/NBNS processes still running"
            return 0
        }
    else
        stop_nbns_conflicts || {
            tc_log "$context: nbns responder launch skipped; conflicting Apple CIFS/NBNS processes still running"
            return 0
        }
    fi

    tc_log "$context: starting nbns responder for $SMB_NETBIOS_NAME in auto-ip mode"
    set -- "$TC_NBNS_BIN" \
        --name "$SMB_NETBIOS_NAME" \
        --auto-ip
    if tc_prepare_runtime_log_file "$TC_NBNS_LOG_FILE"; then
        if tc_runtime_logs_unbounded; then
            tc_log "$context: nbns debug logging enabled at $TC_NBNS_LOG_FILE"
        else
            tc_log "$context: nbns logging at $TC_NBNS_LOG_FILE"
        fi
        printf '%s %s: launching nbns-advertiser\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$TC_LOG_PREFIX" >>"$TC_NBNS_LOG_FILE"
        "$@" >>"$TC_NBNS_LOG_FILE" 2>&1 &
    else
        tc_log "$context: nbns log unavailable at $TC_NBNS_LOG_FILE"
        "$@" >/dev/null 2>&1 &
    fi
    if [ "$wait_attempts" -gt 0 ]; then
        if wait_for_process "$NBNS_PROC_NAME" "$wait_attempts"; then
            tc_log "$context: nbns responder launch requested"
        else
            tc_log "$context: nbns responder failed to stay running"
        fi
    else
        tc_log "$context: nbns restart requested"
    fi
}

tc_restart_nbns() {
    tc_launch_nbns "watchdog recovery" 0
}
