tc_watchdog_recover_disk_runtime() {
    reason=$1

    if tc_live_reload_disk_runtime "$reason"; then
        return 0
    fi

    tc_exec_start_samba "$reason"
}

tc_watchdog_handle_mast_users_partition() {
    [ "$mast_users_part_format" = "hfs" ] || return 0
    case "$mast_users_part_device" in
        dk[0-9]*) ;;
        *) return 0 ;;
    esac
    tc_active_share_device_is_managed "$mast_users_part_device" || return 0

    case "$TC_MAST_USERS_SEEN_PARTS" in
        *" $mast_users_part_device "*) ;;
        *) TC_MAST_USERS_SEEN_PARTS="$TC_MAST_USERS_SEEN_PARTS$mast_users_part_device " ;;
    esac

    case "$mast_users_part_users" in
        ""|*[!0123456789]*)
            tc_log "watchdog disk check: managed volume $mast_users_part_device has unavailable MaSt users value; skipping reclaim"
            return 0
            ;;
    esac

    if [ "$mast_users_part_users" -eq 0 ]; then
        TC_MAST_USERS_ZERO_COUNT=$((TC_MAST_USERS_ZERO_COUNT + 1))
        tc_log "watchdog disk check: managed volume $mast_users_part_device users=0 requires diskd reclaim"
        if tc_watchdog_wake_or_mount_volume "/dev/$mast_users_part_device" "/Volumes/$mast_users_part_device"; then
            tc_log "watchdog disk check: managed volume $mast_users_part_device reclaimed through diskd.useVolume"
        else
            TC_MAST_USERS_RECLAIM_FAILED=1
            tc_log "watchdog disk check: managed volume $mast_users_part_device reclaim failed"
        fi
    fi
}

tc_watchdog_check_active_mast_users() {
    mast_users_raw_file=${1:-}

    [ -s "$TC_SHARES_TSV" ] || return 0

    if [ -z "$mast_users_raw_file" ]; then
        tc_log "watchdog disk check: MaSt users snapshot argument is required"
        return 1
    fi
    if [ ! -f "$mast_users_raw_file" ]; then
        tc_log "watchdog disk check: MaSt users snapshot is missing: $mast_users_raw_file"
        return 1
    fi

    TC_MAST_USERS_ZERO_COUNT=0
    TC_MAST_USERS_RECLAIM_FAILED=0
    TC_MAST_USERS_SEEN_PARTS=" "
    mast_users_in_partitions=0
    mast_users_disk_device=
    mast_users_part_device=
    mast_users_part_format=
    mast_users_part_users=

    while IFS= read -r line || [ -n "$line" ]; do
        trimmed_line=$(tc_trim_plist_line "$line")
        if tc_plist_is_object_end "$trimmed_line"; then
            if [ "$mast_users_in_partitions" -eq 1 ] && [ -n "$mast_users_part_device" ]; then
                tc_watchdog_handle_mast_users_partition
                mast_users_part_device=
                mast_users_part_format=
                mast_users_part_users=
            elif [ -n "$mast_users_disk_device" ]; then
                mast_users_disk_device=
            fi
        elif tc_plist_is_array_end "$trimmed_line"; then
            mast_users_in_partitions=0
        fi

        key=$(tc_plist_key "$line")
        case "$key" in
            partitions)
                mast_users_in_partitions=1
                ;;
            deviceName)
                value=$(tc_extract_plist_string_key deviceName "$line")
                if [ "$mast_users_in_partitions" -eq 1 ]; then
                    mast_users_part_device=$value
                else
                    mast_users_disk_device=$value
                fi
                ;;
            format)
                if [ "$mast_users_in_partitions" -eq 1 ]; then
                    mast_users_part_format=$(tc_extract_plist_string_key format "$line" | /usr/bin/sed 'y/ABCDEFGHIJKLMNOPQRSTUVWXYZ/abcdefghijklmnopqrstuvwxyz/')
                fi
                ;;
            users)
                if [ "$mast_users_in_partitions" -eq 1 ]; then
                    mast_users_part_users=$(tc_extract_plist_number_key users "$line")
                fi
                ;;
        esac
    done <"$mast_users_raw_file"

    if [ -n "$mast_users_part_device" ]; then
        tc_watchdog_handle_mast_users_partition
    fi

    TC_MAST_USERS_MISSING_ACTIVE=0
    while IFS="$TC_TAB" read -r share_name share_path part_device builtin part_uuid ||
        [ -n "$share_name$share_path$part_device$builtin$part_uuid" ]; do
        [ -n "$part_device" ] || continue
        case "$TC_MAST_USERS_SEEN_PARTS" in
            *" $part_device "*) ;;
            *)
                TC_MAST_USERS_MISSING_ACTIVE=1
                tc_log "watchdog disk check: active managed share $share_name uses /dev/$part_device, but MaSt users snapshot did not include that HFS volume"
                ;;
        esac
    done <"$TC_SHARES_TSV"

    if [ "$TC_MAST_USERS_RECLAIM_FAILED" -ne 0 ] || [ "$TC_MAST_USERS_MISSING_ACTIVE" -ne 0 ]; then
        tc_log "watchdog disk check: MaSt users recovery requires full disk runtime reload"
        return 1
    fi

    if [ "$TC_MAST_USERS_ZERO_COUNT" -gt 0 ]; then
        tc_log "watchdog disk check: reclaimed $TC_MAST_USERS_ZERO_COUNT managed volume(s) with users=0"
    fi
    return 0
}

tc_watchdog_disk_iteration() {
    watchdog_mast_volumes_file="$TC_STATE_DIR/watchdog-volumes.tsv.$$"
    watchdog_mast_raw_file="$TC_STATE_DIR/watchdog-mast.raw.$$"
    watchdog_snapshot_status=0
    tc_watchdog_capture_mast_state "$watchdog_mast_volumes_file" "$watchdog_mast_raw_file" || watchdog_snapshot_status=$?

    if [ "$watchdog_snapshot_status" -eq 0 ] && tc_topology_changed_debounced_from_snapshot "$watchdog_mast_volumes_file" "$watchdog_mast_raw_file"; then
        tc_watchdog_recover_disk_runtime "MaSt topology changed"
        rm -f "$watchdog_mast_volumes_file" "$watchdog_mast_raw_file"
        return 0
    fi

    if [ "$watchdog_snapshot_status" -eq 0 ]; then
        if ! tc_watchdog_check_active_mast_users "$watchdog_mast_raw_file"; then
            tc_watchdog_recover_disk_runtime "managed diskd users dropped to zero"
        fi
    else
        tc_log "watchdog disk check: skipping MaSt users check because snapshot is unavailable"
    fi

    rm -f "$watchdog_mast_volumes_file" "$watchdog_mast_raw_file"
    return 0
}

tc_live_reload_disk_runtime() {
    reason=$1
    old_bind_interfaces=${TC_SMB_BIND_INTERFACES:-}
    bind_interfaces_changed=0

    if ! tc_load_payload_state; then
        tc_log "watchdog recovery: live disk runtime refresh skipped; current payload state is unavailable"
        return 1
    fi
    old_payload_dir=$TC_PAYLOAD_DIR
    old_payload_volume=$TC_PAYLOAD_VOLUME
    old_payload_device=$TC_PAYLOAD_DEVICE

    tc_log "watchdog recovery: attempting live disk runtime refresh: $reason"
    if ! tc_refresh_disk_state; then
        tc_log "watchdog recovery: live disk runtime refresh failed during disk-state refresh"
        return 1
    fi
    if ! tc_load_payload_state; then
        tc_log "watchdog recovery: live disk runtime refresh failed; refreshed payload state is unavailable"
        return 1
    fi
    if [ "$TC_PAYLOAD_DIR" != "$old_payload_dir" ] ||
        [ "$TC_PAYLOAD_VOLUME" != "$old_payload_volume" ] ||
        [ "$TC_PAYLOAD_DEVICE" != "$old_payload_device" ]; then
        tc_log "watchdog recovery: live disk runtime refresh cannot continue because payload home changed from $old_payload_dir to $TC_PAYLOAD_DIR"
        return 1
    fi

    tc_prepare_local_hostname_resolution
    tc_init_runtime_identity
    if fresh_bind_interfaces=$(tc_probe_smb_bind_interfaces); then
        if [ -z "$old_bind_interfaces" ] || [ "$fresh_bind_interfaces" != "$old_bind_interfaces" ]; then
            TC_SMB_BIND_INTERFACES=$fresh_bind_interfaces
            bind_interfaces_changed=1
            tc_log "watchdog recovery: Samba IPv4 bind interfaces changed during live refresh: ${old_bind_interfaces:-none} -> $TC_SMB_BIND_INTERFACES"
        fi
    else
        bind_probe_status=$?
        if [ -z "${TC_SMB_BIND_INTERFACES:-}" ]; then
            tc_log "watchdog recovery: live disk runtime refresh failed; no usable IPv4 bind interface is available"
            return 1
        fi
        if ! tc_auto_ip_unavailable_status "$bind_probe_status"; then
            tc_log "watchdog recovery: live disk runtime refresh failed; Samba IPv4 bind probe exited $bind_probe_status"
            return 1
        fi
    fi

    if [ ! -x "$TC_SMBD_BIN" ]; then
        tc_log "watchdog recovery: live disk runtime refresh failed; RAM smbd binary is missing"
        return 1
    fi
    if ! tc_generate_smb_conf "$TC_PAYLOAD_DIR"; then
        if [ "$bind_interfaces_changed" -eq 1 ]; then
            TC_SMB_BIND_INTERFACES=$old_bind_interfaces
        fi
        return 1
    fi
    tc_watchdog_write_identity_signature

    if runtime_process_present_by_ucomm smbd; then
        if [ "$bind_interfaces_changed" -eq 1 ]; then
            tc_restart_smbd_for_bind_change "live disk runtime refresh" || return 1
        else
            if ! tc_reload_smbd_config; then
                return 1
            fi
        fi
    else
        tc_start_smbd_if_needed || return 1
    fi

    if tc_ensure_mdns_auto_ip_seen; then
        tc_launch_mdns_advertiser "watchdog topology refresh" 1 10
    fi
    if tc_nbns_enabled; then
        if runtime_process_present_by_ucomm "$NBNS_PROC_NAME"; then
            :
        else
            tc_restart_nbns
        fi
    fi
    tc_log "watchdog recovery: live disk runtime refresh complete"
    return 0
}

tc_nbns_enabled() {
    [ "$NBNS_ENABLED" = "1" ]
}

tc_all_managed_services_healthy() {
    if [ "${TC_WATCHDOG_SMB_DEFERRED_NO_IP:-0}" = "1" ]; then
        return 1
    fi

    if ! runtime_process_present_by_ucomm smbd; then
        return 1
    fi
    if ! tc_smbd_bound_ipv4_445; then
        return 1
    fi

    if [ "${TC_WATCHDOG_MDNS_UNAVAILABLE:-0}" = "1" ]; then
        return 1
    fi

    if ! runtime_process_present_by_ucomm "$MDNS_PROC_NAME"; then
        if [ "${TC_WATCHDOG_MDNS_DEFERRED_NO_IP:-0}" != "1" ]; then
            return 1
        fi
    fi

    if tc_nbns_enabled; then
        if ! runtime_process_present_by_ucomm "$NBNS_PROC_NAME"; then
            return 1
        fi
    fi

    return 0
}

tc_watchdog_refresh_runtime_identity_for_recovery() {
    if [ "${TC_WATCHDOG_RECOVERY_IDENTITY_REFRESHED:-0}" != "1" ]; then
        tc_init_runtime_identity
        TC_WATCHDOG_RECOVERY_IDENTITY_REFRESHED=1
    fi
}

tc_watchdog_identity_signature() {
    printf '%s\n%s\n%s\n%s\n' \
        "${MDNS_INSTANCE_NAME:-}" \
        "${MDNS_HOST_LABEL:-}" \
        "${SMB_NETBIOS_NAME:-}" \
        "${SMB_SERVER_STRING:-}"
}

tc_watchdog_write_identity_signature() {
    TC_WATCHDOG_LAST_IDENTITY_SIGNATURE=$(tc_watchdog_identity_signature)
    TC_WATCHDOG_IDENTITY_SIGNATURE_READY=1
}

tc_watchdog_identity_signature_changed() {
    identity_signature=$(tc_watchdog_identity_signature)

    if [ "${TC_WATCHDOG_IDENTITY_SIGNATURE_READY:-0}" != "1" ]; then
        TC_WATCHDOG_LAST_IDENTITY_SIGNATURE=$identity_signature
        TC_WATCHDOG_IDENTITY_SIGNATURE_READY=1
        return 1
    fi

    if [ "$identity_signature" != "$TC_WATCHDOG_LAST_IDENTITY_SIGNATURE" ]; then
        return 0
    fi
    return 1
}

tc_watchdog_apply_identity_change() {
    tc_log "watchdog identity change: refreshing Samba config and advertisers"
    if tc_load_payload_state; then
        tc_generate_smb_conf "$TC_PAYLOAD_DIR"
        if runtime_process_present_by_ucomm smbd; then
            tc_reload_smbd_config || return 1
        fi
    else
        tc_log "watchdog identity change: payload state unavailable; skipping smbd config reload"
    fi

    stop_runtime_process_by_ucomm "$MDNS_PROC_NAME" "$MDNS_PROC_NAME" || true
    if tc_nbns_enabled; then
        stop_runtime_process_by_ucomm "$NBNS_PROC_NAME" "$NBNS_PROC_NAME" || true
    fi
    return 0
}

tc_watchdog_reset_pass_state() {
    TC_WATCHDOG_SMB_DEFERRED_NO_IP=0
    TC_WATCHDOG_MDNS_DEFERRED_NO_IP=0
    TC_WATCHDOG_MDNS_UNAVAILABLE=0
    TC_AIRPORT_FIELDS_READY=0
    TC_AIRPORT_FIELDS_ADVERTISE_MAC=
}

tc_watchdog_initialize_smb_bind_interfaces() {
    if [ -n "${TC_SMB_BIND_INTERFACES:-}" ]; then
        return 0
    fi

    if tc_refresh_smb_bind_interfaces; then
        tc_log "watchdog startup: initialized Samba IPv4 bind interfaces from live probe"
        return 0
    fi

    tc_log "watchdog startup: Samba IPv4 bind interface initialization deferred"
    return 0
}

tc_watchdog_reconcile_identity() {
    tc_prepare_local_hostname_resolution
    tc_init_runtime_identity
    TC_WATCHDOG_RECOVERY_IDENTITY_REFRESHED=1
    if tc_watchdog_identity_signature_changed; then
        tc_watchdog_apply_identity_change || return 1
        tc_watchdog_write_identity_signature
    fi
}

tc_watchdog_reconcile_smbd() {
    if ! tc_start_smbd_if_needed; then
        tc_log "watchdog pass: smbd recovery did not complete"
        return 1
    fi
}

tc_watchdog_reconcile_smb_bind_interfaces() {
    if fresh_bind_interfaces=$(tc_probe_smb_bind_interfaces); then
        if [ -z "${TC_SMB_BIND_INTERFACES:-}" ]; then
            TC_SMB_BIND_INTERFACES=$fresh_bind_interfaces
            tc_log "watchdog pass: initialized Samba IPv4 bind interfaces: $TC_SMB_BIND_INTERFACES"
            return 0
        fi
        if [ "$fresh_bind_interfaces" = "$TC_SMB_BIND_INTERFACES" ]; then
            return 0
        fi

        old_bind_interfaces=$TC_SMB_BIND_INTERFACES
        TC_SMB_BIND_INTERFACES=$fresh_bind_interfaces
        tc_log "watchdog pass: Samba IPv4 bind interfaces changed: $old_bind_interfaces -> $TC_SMB_BIND_INTERFACES"
        if ! tc_prepare_smbd_recovery_disk_runtime; then
            TC_SMB_BIND_INTERFACES=$old_bind_interfaces
            tc_log "watchdog pass: cannot apply Samba bind change; disk runtime preparation failed"
            return 1
        fi
        if ! tc_generate_smb_conf "$TC_PAYLOAD_DIR"; then
            TC_SMB_BIND_INTERFACES=$old_bind_interfaces
            return 1
        fi
        tc_restart_smbd_for_bind_change "IPv4 bind interfaces changed" || return 1
        return 0
    else
        bind_probe_status=$?
    fi

    if tc_auto_ip_unavailable_status "$bind_probe_status"; then
        tc_mark_smb_deferred_no_ip
        return 0
    fi

    tc_log "watchdog pass: Samba IPv4 bind probe failed with exit code $bind_probe_status"
    return 1
}

tc_watchdog_reconcile_mdns() {
    if runtime_process_present_by_ucomm "$MDNS_PROC_NAME"; then
        return 0
    fi

    tc_watchdog_refresh_runtime_identity_for_recovery
    if ! tc_ensure_mdns_auto_ip_seen; then
        return 0
    fi
    if [ "${TC_MDNS_CAPTURE_ATTEMPTED:-0}" != "1" ]; then
        tc_start_mdns_capture
        TC_MDNS_CAPTURE_ATTEMPTED=1
        tc_start_mdns_advertiser
    else
        tc_restart_mdns
    fi
}

tc_watchdog_start_mdns_if_needed() {
    tc_watchdog_reconcile_mdns
}

tc_watchdog_reconcile_nbns() {
    if ! tc_nbns_enabled; then
        return 0
    fi

    if runtime_process_present_by_ucomm "$NBNS_PROC_NAME"; then
        return 0
    fi

    tc_watchdog_refresh_runtime_identity_for_recovery
    tc_restart_nbns
}

tc_watchdog_report_service_health() {
    sleep 1

    if tc_all_managed_services_healthy; then
        if [ "${TC_WATCHDOG_MDNS_DEFERRED_NO_IP:-0}" = "1" ]; then
            tc_log "watchdog steady check: core services healthy; mDNS deferred waiting for usable IPv4"
        else
            tc_log "watchdog steady check: healthy"
        fi
        return 0
    fi

    if [ "${TC_WATCHDOG_SMB_DEFERRED_NO_IP:-0}" = "1" ]; then
        tc_log "watchdog pass: Samba IPv4 bind interface is unavailable"
        return 1
    fi
    tc_log "watchdog pass: one or more managed services are unhealthy"
    return 1
}

tc_watchdog_service_iteration() {
    tc_log "watchdog service pass: checking managed services"
    tc_watchdog_reset_pass_state
    tc_watchdog_reconcile_identity || return 1
    tc_watchdog_reconcile_smb_bind_interfaces || return 1
    tc_watchdog_reconcile_smbd || return 1
    tc_watchdog_reconcile_mdns
    tc_watchdog_reconcile_nbns
    tc_watchdog_report_service_health
}
