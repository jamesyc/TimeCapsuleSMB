tc_manager_stop_samba_lane_without_payload() {
    if runtime_process_present_by_ucomm smbd; then
        tc_log "manager no_payload: stopping smbd because payload state is unavailable"
        stop_runtime_process_by_ucomm "smbd" smbd || return 1
    fi
    if runtime_process_present_by_ucomm "$RSYNC_PROC_NAME"; then
        tc_log "manager no_payload: stopping rsync because payload state is unavailable"
        stop_runtime_process_by_ucomm "$RSYNC_PROC_NAME" "$RSYNC_PROC_NAME" || return 1
    fi
    rm -f "$TC_RSYNC_BIN" "$TC_RSYNC_CONF" >/dev/null 2>&1 || return 1
    TC_MANAGER_LAST_RSYNC_SIGNATURE=
    return 0
}
