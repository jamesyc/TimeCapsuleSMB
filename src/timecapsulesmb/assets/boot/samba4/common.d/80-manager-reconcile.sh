tc_nbns_enabled() {
    [ "$NBNS_ENABLED" = "1" ]
}

tc_mark_nbns_deferred_no_ip() {
    TC_MANAGER_NBNS_DEFERRED_NO_IP=1
    if [ "${TC_NBNS_AUTO_IP_WAIT_LOGGED:-0}" != "1" ]; then
        tc_log "NBNS startup deferred; no usable address has appeared yet"
        TC_NBNS_AUTO_IP_WAIT_LOGGED=1
    fi
}

tc_manager_stop_samba_lane_without_payload() {
    if runtime_process_present_by_ucomm smbd; then
        tc_log "manager no_payload: stopping smbd because payload state is unavailable"
        stop_runtime_process_by_ucomm "smbd" smbd || return 1
    fi
    if tc_nbns_enabled && runtime_process_present_by_ucomm "$NBNS_PROC_NAME"; then
        tc_log "manager no_payload: stopping nbns responder because payload state is unavailable"
        stop_runtime_process_by_ucomm "$NBNS_PROC_NAME" "$NBNS_PROC_NAME" || return 1
    fi
    return 0
}

tc_manager_refresh_runtime_identity_for_recovery() {
    if [ "${TC_MANAGER_RECOVERY_IDENTITY_REFRESHED:-0}" != "1" ]; then
        tc_init_runtime_identity
        TC_MANAGER_RECOVERY_IDENTITY_REFRESHED=1
    fi
}

tc_manager_identity_signature() {
    printf '%s\n%s\n%s\n%s\n' \
        "${MDNS_INSTANCE_NAME:-}" \
        "${MDNS_HOST_LABEL:-}" \
        "${SMB_NETBIOS_NAME:-}" \
        "${SMB_SERVER_STRING:-}"
}

tc_manager_write_identity_signature() {
    TC_MANAGER_LAST_IDENTITY_SIGNATURE=$(tc_manager_identity_signature)
    TC_MANAGER_IDENTITY_SIGNATURE_READY=1
}

tc_manager_identity_signature_changed() {
    identity_signature=$(tc_manager_identity_signature)

    if [ "${TC_MANAGER_IDENTITY_SIGNATURE_READY:-0}" != "1" ]; then
        TC_MANAGER_LAST_IDENTITY_SIGNATURE=$identity_signature
        TC_MANAGER_IDENTITY_SIGNATURE_READY=1
        return 1
    fi

    if [ "$identity_signature" != "$TC_MANAGER_LAST_IDENTITY_SIGNATURE" ]; then
        return 0
    fi
    return 1
}

tc_manager_reset_pass_state() {
    TC_MANAGER_SMB_DEFERRED_NO_IP=0
    TC_MANAGER_MDNS_DEFERRED_NO_IP=0
    TC_MANAGER_MDNS_UNAVAILABLE=0
    TC_MANAGER_NBNS_DEFERRED_NO_IP=0
    TC_AIRPORT_FIELDS_READY=0
    TC_AIRPORT_FIELDS_ADVERTISE_MAC=
}

tc_manager_reconcile_nbns() {
    nbns_auto_ip_status=0

    if ! tc_nbns_enabled; then
        return 0
    fi

    if runtime_process_present_by_ucomm "$NBNS_PROC_NAME"; then
        if tc_nbns_bound_udp_137; then
            return 0
        fi
        tc_log "manager NBNS recovery: nbns responder is running without required UDP 137 sockets"
        if tc_nbns_auto_ip_available; then
            stop_runtime_process_by_ucomm "$NBNS_PROC_NAME" "$NBNS_PROC_NAME" || return 1
        else
            nbns_auto_ip_status=$?
            if tc_auto_ip_unavailable_status "$nbns_auto_ip_status"; then
                tc_mark_nbns_deferred_no_ip
                return 0
            fi
            tc_log "manager NBNS recovery: auto-ip check failed with exit code $nbns_auto_ip_status"
            return 1
        fi
    else
        if ! tc_nbns_auto_ip_available; then
            nbns_auto_ip_status=$?
            if tc_auto_ip_unavailable_status "$nbns_auto_ip_status"; then
                tc_mark_nbns_deferred_no_ip
                return 0
            fi
            tc_log "manager NBNS recovery: auto-ip check failed with exit code $nbns_auto_ip_status"
            return 1
        fi
    fi

    if runtime_process_present_by_ucomm "$NBNS_PROC_NAME"; then
        return 0
    fi

    tc_manager_refresh_runtime_identity_for_recovery
    tc_restart_nbns
}
