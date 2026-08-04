tc_rsync_enabled() {
    [ "$RSYNC_ENABLED" = "1" ]
}

tc_find_payload_rsync() {
    payload_dir=$1

    if [ -x "$payload_dir/rsync" ]; then
        echo "$payload_dir/rsync"
        return 0
    fi
    tc_log "rsync binary not found in $payload_dir"
    return 1
}

tc_find_payload_rsync_config() {
    payload_dir=$1

    if [ -r "$payload_dir/rsyncd.conf" ]; then
        echo "$payload_dir/rsyncd.conf"
        return 0
    fi
    tc_log "rsync config not found in $payload_dir"
    return 1
}

tc_stage_runtime_rsync_config() {
    config_src=$1
    share_root=$2
    config_tmp="$TC_RSYNC_CONF.tmp.$$"

    rm -f "$config_tmp" >/dev/null 2>&1 || true
    # The persistent config records the deploy-time volume for inspection and
    # standalone use. Rewrite only the module path while staging so a payload
    # that is mounted under a different dkN path still serves its current disk.
    if /usr/bin/sed "s|^[[:space:]]*path[[:space:]]*=.*|path = $share_root|" "$config_src" >"$config_tmp"; then
        :
    else
        config_status=$?
        tc_log "rsync runtime staging failed: render config failed: $config_src -> $config_tmp status=$config_status"
        rm -f "$config_tmp" >/dev/null 2>&1 || true
        return "$config_status"
    fi
    staged_share_root=$(/usr/bin/sed -n 's/^[[:space:]]*path[[:space:]]*=[[:space:]]*//p' "$config_tmp" | /usr/bin/sed -n '1p')
    if [ "$staged_share_root" != "$share_root" ]; then
        tc_log "rsync runtime staging failed: rendered config does not contain current share root: $share_root"
        rm -f "$config_tmp" >/dev/null 2>&1 || true
        return 1
    fi
    if chmod 600 "$config_tmp" && mv "$config_tmp" "$TC_RSYNC_CONF"; then
        return 0
    fi
    config_status=$?
    tc_log "rsync runtime staging failed: install config failed: $config_tmp -> $TC_RSYNC_CONF status=$config_status"
    rm -f "$config_tmp" >/dev/null 2>&1 || true
    return "$config_status"
}

tc_stage_rsync_runtime() {
    rsync_src=$1
    config_src=$2
    share_root=$3

    tc_stage_runtime_executable "$rsync_src" "$TC_RSYNC_BIN" || return 1
    tc_stage_runtime_rsync_config "$config_src" "$share_root" || return 1
    tc_log "staged rsync runtime binary and config"
}

tc_rsync_bound_tcp_873() {
    tc_process_has_fstat_socket "$RSYNC_PROC_NAME" internet stream tcp 873 && return 0
    tc_process_has_fstat_socket "$RSYNC_PROC_NAME" internet6 stream tcp 873
}

tc_wait_for_rsync_ready() {
    max_attempts=${1:-10}
    attempt=0

    while [ "$attempt" -lt "$max_attempts" ]; do
        if runtime_process_present_by_ucomm "$RSYNC_PROC_NAME" && tc_rsync_bound_tcp_873; then
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    return 1
}

tc_stop_rsync_if_running() {
    if runtime_process_present_by_ucomm "$RSYNC_PROC_NAME"; then
        stop_runtime_process_by_ucomm "$RSYNC_PROC_NAME" "$RSYNC_PROC_NAME" || return 1
    fi
}

tc_manager_rsync_file_signature() {
    payload_dir=$1
    rsync_src=$2
    config_src=$3

    printf 'payload\t%s\n' "$payload_dir"
    tc_manager_file_metadata_signature "$rsync_src"
    tc_manager_file_metadata_signature "$config_src"
}

tc_manager_clear_rsync_runtime() {
    tc_stop_rsync_if_running || return 1
    rm -f "$TC_RSYNC_BIN" "$TC_RSYNC_CONF" >/dev/null 2>&1 || return 1
    TC_MANAGER_LAST_RSYNC_SIGNATURE=
}

tc_manager_reconcile_rsync() {
    if ! tc_rsync_enabled; then
        tc_manager_clear_rsync_runtime || return 1
        return 0
    fi

    if ! tc_manager_select_current_payload; then
        tc_log "manager rsync: payload state is unavailable"
        tc_manager_clear_rsync_runtime || return 1
        return 1
    fi
    if ! is_volume_root_mounted "$manager_payload_volume"; then
        tc_log "manager rsync: payload volume is not mounted: $manager_payload_volume"
        tc_manager_clear_rsync_runtime || return 1
        return 1
    fi

    manager_rsync_src=$(tc_find_payload_rsync "$manager_payload_dir") || return 1
    manager_rsync_config_src=$(tc_find_payload_rsync_config "$manager_payload_dir") || return 1
    manager_rsync_share_root="$manager_payload_volume/ShareRoot"
    mkdir -p "$manager_rsync_share_root" || return 1

    fresh_rsync_signature=$(tc_manager_rsync_file_signature "$manager_payload_dir" "$manager_rsync_src" "$manager_rsync_config_src") || return 1
    if [ "$fresh_rsync_signature" != "${TC_MANAGER_LAST_RSYNC_SIGNATURE:-}" ] ||
        [ ! -x "$TC_RSYNC_BIN" ] || [ ! -r "$TC_RSYNC_CONF" ]; then
        tc_stop_rsync_if_running || return 1
        tc_stage_rsync_runtime "$manager_rsync_src" "$manager_rsync_config_src" "$manager_rsync_share_root" || return 1
        TC_MANAGER_LAST_RSYNC_SIGNATURE=$fresh_rsync_signature
    fi

    rsync_log_size=$(tc_log_file_size "$TC_RSYNC_LOG_FILE")
    case "$rsync_log_size" in
        ""|*[!0123456789]*) rsync_log_size=0 ;;
    esac
    if [ "$rsync_log_size" -gt "$TC_RUNTIME_LOG_MAX_BYTES" ] &&
        runtime_process_present_by_ucomm "$RSYNC_PROC_NAME"; then
        # The shared trimmer replaces the path atomically. Stop rsync first so
        # it cannot keep writing to the old, unlinked RAM-disk inode.
        tc_log "manager rsync: restarting daemon to bound oversized log"
        tc_stop_rsync_if_running || return 1
    fi

    rsync_log_ready=0
    if tc_prepare_runtime_log_file "$TC_RSYNC_LOG_FILE"; then
        rsync_log_ready=1
    else
        tc_log "manager rsync: log unavailable at $TC_RSYNC_LOG_FILE"
    fi

    if runtime_process_present_by_ucomm "$RSYNC_PROC_NAME"; then
        if tc_rsync_bound_tcp_873; then
            return 0
        fi
        tc_log "manager rsync: process is running without TCP 873; restarting"
        tc_stop_rsync_if_running || return 1
    fi

    tc_log "manager rsync: starting daemon on TCP 873"
    if [ "$rsync_log_ready" = "1" ]; then
        "$TC_RSYNC_BIN" --daemon --no-detach --config="$TC_RSYNC_CONF" </dev/null >>"$TC_RSYNC_LOG_FILE" 2>&1 &
    else
        "$TC_RSYNC_BIN" --daemon --no-detach --config="$TC_RSYNC_CONF" </dev/null >/dev/null 2>&1 &
    fi
    if tc_wait_for_rsync_ready 10; then
        tc_log "manager rsync: daemon is ready on TCP 873"
        return 0
    fi

    tc_log "manager rsync: daemon failed to become ready on TCP 873"
    tc_stop_rsync_if_running || true
    return 1
}
