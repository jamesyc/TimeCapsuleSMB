tc_cleanup_old_runtime() {
    cleanup_status=0

    tc_log "cleaning old managed runtime processes and RAM state"
    stop_manager_process || cleanup_status=1
    stop_runtime_process_by_ucomm "smbd" "smbd" || cleanup_status=1
    stop_runtime_process_by_ucomm "$DISCOVERY_PROC_NAME" "$DISCOVERY_PROC_NAME" || cleanup_status=1
    stop_discovery_conflicts || cleanup_status=1
    stop_runtime_process_by_ucomm "$RSYNC_PROC_NAME" "$RSYNC_PROC_NAME" || cleanup_status=1
    tc_prepare_telemetry_reset || cleanup_status=1
    # Apple's mDNSResponder is deliberately not on this list (F11).
    tc_stop_apple_afpserver || tc_log "Apple afpserver could not be stopped; AFP port 548 stays open"

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
            if /sbin/mount_tmpfs -s 4m tmpfs "$LOCKS_ROOT" >/dev/null 2>&1; then
                rm -rf "$LOCKS_ROOT"/* >/dev/null 2>&1 || true
                tc_log "mounted $LOCKS_ROOT tmpfs for Samba lock directory"
                return 0
            fi
            tc_log "failed to mount $LOCKS_ROOT tmpfs; using plain directory fallback"
            rm -rf "$LOCKS_ROOT"/* >/dev/null 2>&1 || true
            return 0
            ;;
        *)
            # mount_mfs sizes are 512-byte sectors; 8192 sectors is 4 MiB.
            if /sbin/mount_mfs -s 8192 swap "$LOCKS_ROOT" >/dev/null 2>&1; then
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

# v3.1.0: the registrant derives everything it advertises from the device
# plan (ACP + kernel interface table) and from the flash config, so the
# manager only tells it the payload state and the adisk rows. Apple's
# mDNSResponder is the wire; the registrant never touches port 5353.
tc_launch_discovery() {
    context=$1
    kill_prior=${2:-1}
    wait_attempts=${3:-0}
    diskless=${4:-0}
    discovery_debug_logging=${5:-${MDNS_DEBUG_LOGGING:-0}}
    discovery_share_rows=${6:-}

    if [ ! -x "$TC_DISCOVERY_BIN" ]; then
        # A skipped launch must not commit the manager's new argv signature.
        tc_log "$context: discovery skipped; missing $TC_DISCOVERY_BIN"
        return 1
    fi
    if [ "$kill_prior" = "1" ]; then
        if runtime_process_present_by_ucomm "$DISCOVERY_PROC_NAME"; then
            tc_log "$context: stopping prior $DISCOVERY_PROC_NAME"
            stop_runtime_process_by_ucomm "$DISCOVERY_PROC_NAME" "$DISCOVERY_PROC_NAME" || return 1
        fi
        stop_discovery_conflicts || return 1
    fi

    set -- "$TC_DISCOVERY_BIN"
    if [ "$diskless" = "1" ]; then
        set -- "$@" --diskless
        tc_log "$context: starting discovery controller in diskless mode"
    else
        tc_ensure_runtime_identity || return 1
        set -- "$@" --netbios-name "$SMB_NETBIOS_NAME"
        tc_log "$context: starting discovery controller for $SMB_NETBIOS_NAME"
    fi
    if [ "$diskless" != "1" ]; then
        # Use the same final share names as smb.conf. Separate quoted argv
        # values preserve spaces/UTF-8 without a file or command evaluation.
        while IFS="$TC_TAB" read -r mdns_share_name mdns_share_path mdns_disk_key mdns_builtin mdns_uuid ||
            [ -n "$mdns_share_name$mdns_share_path$mdns_disk_key$mdns_builtin$mdns_uuid" ]; do
            [ -n "$mdns_share_name" ] || continue
            set -- "$@" --adisk-share "$mdns_share_name" "$mdns_disk_key" "$mdns_uuid" "$TC_ADISK_DISK_ADVF"
        done <<EOF
$discovery_share_rows
EOF
    fi
    if [ "$discovery_debug_logging" = "1" ]; then
        set -- "$@" --debug-logging
    fi

    if tc_prepare_runtime_log_file "$TC_DISCOVERY_LOG_FILE"; then
        if tc_runtime_logs_unbounded; then
            tc_log "$context: debug logging enabled at $TC_DISCOVERY_LOG_FILE"
        else
            tc_log "$context: logging at $TC_DISCOVERY_LOG_FILE"
        fi
        printf '%s %s: launching discovery\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$TC_LOG_PREFIX" >>"$TC_DISCOVERY_LOG_FILE"
        "$@" </dev/null >>"$TC_DISCOVERY_LOG_FILE" 2>&1 &
    else
        tc_log "$context: log unavailable at $TC_DISCOVERY_LOG_FILE"
        "$@" </dev/null >/dev/null 2>&1 &
    fi
    TC_DISCOVERY_PID=$!
    tc_log "$context: launched background pid $TC_DISCOVERY_PID"
    if [ "$wait_attempts" -gt 0 ]; then
        if wait_for_process "$DISCOVERY_PROC_NAME" "$wait_attempts"; then
            tc_log "$context: discovery controller running"
        else
            tc_log "$context: discovery controller failed to stay running"
            return 1
        fi
    fi
    return 0
}
