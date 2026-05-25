#!/bin/sh
set -eu

PATH=/bin:/sbin:/usr/bin:/usr/sbin

. /mnt/Flash/common.sh
. /mnt/Flash/tcapsulesmb.conf

tc_init_runtime_env
tc_set_log "$RAM_VAR/manager.log" "manager"
TC_LOG_MAX_BYTES=102400

case "${1:-}" in
    "")
        ;;
    *)
        tc_log "unknown manager.sh mode: $1"
        exit 2
        ;;
esac

tc_manager_debug_log() {
    tc_smbd_debug_log "$@"
}

tc_manager_stop_requested() {
    [ "${TC_MANAGER_STOP_REQUESTED:-0}" = "1" ]
}

tc_manager_sleep_until_due() {
    manager_sleep_remaining=$1
    manager_sleep_chunk=$MANAGER_STOP_POLL_SECONDS

    while [ "$manager_sleep_remaining" -gt 0 ]; do
        if tc_manager_stop_requested; then
            return 1
        fi
        if [ "$manager_sleep_chunk" -gt "$manager_sleep_remaining" ]; then
            manager_sleep_chunk=$manager_sleep_remaining
        fi
        sleep "$manager_sleep_chunk" || {
            tc_manager_stop_requested && return 1
            return 1
        }
        manager_sleep_remaining=$((manager_sleep_remaining - manager_sleep_chunk))
    done

    tc_manager_stop_requested && return 1
    return 0
}

tc_manager_log_step_end() {
    iteration_id=$1
    step_name=$2
    step_start_seconds=$3
    step_status=$4
    step_duration_seconds=$(tc_elapsed_seconds_since "$step_start_seconds")

    case "$step_status" in
        ok|skipped)
            tc_manager_debug_log "manager pass $iteration_id step=$step_name end status=$step_status duration_seconds=$step_duration_seconds"
            ;;
        *)
            tc_log "manager pass $iteration_id step=$step_name end status=$step_status duration_seconds=$step_duration_seconds"
            ;;
    esac
}

tc_manager_read_mast_raw() {
    if [ ! -x /usr/bin/acp ]; then
        tc_log "manager MaSt probe failed: /usr/bin/acp unavailable"
        return 1
    fi
    if mast_raw=$(/usr/bin/acp -A MaSt 2>/dev/null); then
        printf '%s\n' "$mast_raw"
        return 0
    else
        mast_read_status=$?
    fi
    tc_log "manager MaSt probe failed: acp exited $mast_read_status"
    return "$mast_read_status"
}

tc_manager_read_mast_raw_with_retry() {
    if mast_raw=$(tc_manager_read_mast_raw); then
        printf '%s\n' "$mast_raw"
        return 0
    else
        first_mast_status=$?
    fi
    tc_log "manager MaSt probe retrying once in ${MANAGER_MAST_RETRY_SECONDS}s after status=$first_mast_status"
    sleep "$MANAGER_MAST_RETRY_SECONDS"
    if mast_raw=$(tc_manager_read_mast_raw); then
        printf '%s\n' "$mast_raw"
        return 0
    else
        retry_mast_status=$?
    fi
    tc_log "manager MaSt probe failed after retry: first_status=$first_mast_status retry_status=$retry_mast_status"
    return "$retry_mast_status"
}

tc_manager_count_rows() {
    count_rows_input=$1
    count_rows=0
    while IFS= read -r count_line || [ -n "$count_line" ]; do
        [ -n "$count_line" ] || continue
        count_rows=$((count_rows + 1))
    done <<EOF
$count_rows_input
EOF
    echo "$count_rows"
}

tc_manager_log_topology_rows() {
    topology_rows=$1
    topology_count=0
    while IFS="$TC_TAB" read -r disk_device builtin part_device volume_root part_name part_uuid ||
        [ -n "$disk_device$builtin$part_device$volume_root$part_name$part_uuid" ]; do
        [ -n "$part_device" ] || continue
        topology_count=$((topology_count + 1))
        tc_log "manager MaSt topology: disk=$disk_device builtin=$builtin part=$part_device root=$volume_root name=$part_name uuid=$part_uuid"
    done <<EOF
$topology_rows
EOF
    tc_log "manager MaSt topology rows=$topology_count"
}

tc_manager_parse_mast_runtime_rows() {
    mast_raw=$1
    printf '%s\n' "$mast_raw" | tc_mast_raw_to_runtime_rows
}

tc_manager_runtime_rows_stable_signature() {
    runtime_rows=$1
    tc_mast_runtime_rows_to_topology "$runtime_rows"
}

tc_manager_current_payload_ready() {
    [ "${manager_payload_ready:-0}" = "1" ] &&
        [ -n "${manager_payload_dir:-}" ] &&
        [ -n "${manager_payload_volume:-}" ] &&
        [ -n "${manager_payload_device:-}" ]
}

tc_manager_select_current_payload() {
    TC_PAYLOAD_DIR=
    TC_PAYLOAD_VOLUME=
    TC_PAYLOAD_DEVICE=
    if ! tc_manager_current_payload_ready; then
        return 1
    fi

    TC_PAYLOAD_DIR=$manager_payload_dir
    TC_PAYLOAD_VOLUME=$manager_payload_volume
    TC_PAYLOAD_DEVICE=$manager_payload_device
    tc_set_payload_log_dir "$TC_PAYLOAD_DIR" "$TC_PAYLOAD_VOLUME"
}

tc_manager_materialize_adisk_state() {
    tc_ensure_parent_dir "$TC_ADISK_TSV"
    if tc_manager_current_payload_ready && [ -n "${manager_adisk_rows:-}" ]; then
        printf '%s\n' "$manager_adisk_rows" >"$TC_ADISK_TSV" || return 1
    else
        : >"$TC_ADISK_TSV" || return 1
    fi
}

tc_manager_generate_smb_conf() {
    if ! tc_manager_select_current_payload; then
        tc_log "manager Samba config skipped: payload state is unavailable"
        return 1
    fi
    tc_generate_smb_conf_from_share_rows "$manager_payload_dir" "${manager_share_rows:-}"
}

tc_manager_file_metadata_signature() {
    metadata_path=$1

    if [ ! -f "$metadata_path" ]; then
        printf '%s\tmissing\n' "$metadata_path"
        return 0
    fi

    set -- $(/bin/ls -ln "$metadata_path" 2>/dev/null)
    printf '%s\t%s\t%s\t%s\t%s\n' "$metadata_path" "${5:-}" "${6:-}" "${7:-}" "${8:-}"
}

tc_manager_samba_file_signature() {
    payload_dir=$1
    smbd_src=$2
    nbns_src=$3

    printf 'payload\t%s\n' "$payload_dir"
    tc_manager_file_metadata_signature "$smbd_src"
    tc_manager_file_metadata_signature "$payload_dir/private/smbpasswd"
    tc_manager_file_metadata_signature "$payload_dir/private/username.map"
    if [ "$NBNS_ENABLED" = "1" ] && [ -n "$nbns_src" ]; then
        tc_manager_file_metadata_signature "$nbns_src"
    else
        printf 'nbns\t%s\n' "disabled-or-missing"
    fi
}

tc_manager_samba_config_signature() {
    printf '%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n' \
        "${TC_PAYLOAD_DIR:-}" \
        "${TC_PAYLOAD_VOLUME:-}" \
        "${TC_PAYLOAD_DEVICE:-}" \
        "${TC_SMB_BIND_INTERFACES:-}" \
        "${MDNS_DEVICE_MODEL:-}" \
        "${SMB_NETBIOS_NAME:-}" \
        "${SMB_SERVER_STRING:-}" \
        "${ANY_PROTOCOL:-}" \
        "${TC_SMBD_DISK_LOGGING_ENABLED:-}" \
        "${PAYLOAD_DIR_NAME:-}" \
        "${manager_share_rows:-}"
}

tc_manager_clear_payload_state() {
    manager_payload_ready=0
    manager_payload_dir=
    manager_payload_volume=
    manager_payload_device=
    TC_PAYLOAD_DIR=
    TC_PAYLOAD_VOLUME=
    TC_PAYLOAD_DEVICE=
    tc_clear_payload_log_dir
    manager_share_rows=
    manager_adisk_rows=
    TC_MANAGER_RUNTIME_STAGED=0
    TC_MANAGER_LAST_BINARY_SIGNATURE=
    TC_MANAGER_LAST_CONFIG_SIGNATURE=
    TC_MANAGER_PENDING_CONFIG_SIGNATURE=
    tc_manager_materialize_adisk_state || true
}

tc_manager_set_payload_state() {
    manager_payload_ready=1
    manager_payload_dir=$TC_RESOLVED_PAYLOAD_DIR
    manager_payload_volume=$TC_RESOLVED_PAYLOAD_VOLUME
    manager_payload_device=$TC_RESOLVED_PAYLOAD_DEVICE
    TC_PAYLOAD_DIR=$manager_payload_dir
    TC_PAYLOAD_VOLUME=$manager_payload_volume
    TC_PAYLOAD_DEVICE=$manager_payload_device
    tc_set_payload_log_dir "$TC_PAYLOAD_DIR" "$TC_PAYLOAD_VOLUME"
}

tc_manager_activate_topology() {
    topology_rows=$1
    volume_count=0
    mounted_count=0
    failed_count=0
    skipped_count=0
    activated_part_devices=" "

    tc_log "manager disk refresh: activating MaSt volumes through diskd.useVolume"
    while IFS="$TC_TAB" read -r disk_device builtin part_device volume_root part_name part_uuid ||
        [ -n "$disk_device$builtin$part_device$volume_root$part_name$part_uuid" ]; do
        [ -n "$part_device" ] || continue
        volume_count=$((volume_count + 1))
        case "$activated_part_devices" in
            *" $part_device "*)
                skipped_count=$((skipped_count + 1))
                tc_log "manager disk refresh: activation skipped for duplicate MaSt volume /dev/$part_device at $volume_root"
                continue
                ;;
        esac
        activated_part_devices="$activated_part_devices$part_device "
        tc_log "manager disk refresh: activating volume $volume_count: disk=$disk_device builtin=$builtin device=/dev/$part_device root=$volume_root name=$part_name"
        if tc_wake_or_mount_volume "/dev/$part_device" "$volume_root"; then
            mounted_count=$((mounted_count + 1))
            tc_log "manager disk refresh: volume active: /dev/$part_device at $volume_root"
        else
            failed_count=$((failed_count + 1))
            tc_log "manager disk refresh: volume inactive after diskd attempts: /dev/$part_device at $volume_root"
        fi
    done <<EOF
$topology_rows
EOF
    tc_log "manager disk refresh: diskd activation complete: total=$volume_count mounted=$mounted_count failed=$failed_count skipped=$skipped_count"
}

tc_manager_scan_payload_candidates_for_builtin() {
    desired_builtin=$1
    topology_rows=$2

    while IFS="$TC_TAB" read -r disk_device builtin part_device volume_root part_name part_uuid ||
        [ -n "$disk_device$builtin$part_device$volume_root$part_name$part_uuid" ]; do
        [ -n "$part_device" ] || continue
        [ "$builtin" = "$desired_builtin" ] || continue
        candidate="$volume_root/$PAYLOAD_DIR_NAME"
        if is_volume_root_mounted "$volume_root"; then
            if tc_verify_payload_dir "$candidate"; then
                tc_log "manager payload candidate valid: $candidate builtin=$builtin"
                if [ -z "$selected_payload_dir" ]; then
                    selected_payload_dir=$candidate
                    selected_payload_volume=$volume_root
                    selected_payload_device="/dev/$part_device"
                fi
            else
                tc_log "manager payload candidate invalid: missing managed payload at $candidate"
                if [ -z "$first_invalid_payload_dir" ]; then
                    first_invalid_payload_dir=$candidate
                    first_invalid_payload_volume=$volume_root
                fi
            fi
        else
            tc_log "manager payload candidate unavailable: /dev/$part_device at $volume_root is not mounted"
        fi
    done <<EOF
$topology_rows
EOF
}

tc_manager_resolve_payload_from_topology() {
    topology_rows=$1
    TC_RESOLVED_PAYLOAD_DIR=
    TC_RESOLVED_PAYLOAD_VOLUME=
    TC_RESOLVED_PAYLOAD_DEVICE=
    selected_payload_dir=
    selected_payload_volume=
    selected_payload_device=
    first_invalid_payload_dir=
    first_invalid_payload_volume=

    tc_manager_scan_payload_candidates_for_builtin 1 "$topology_rows"
    tc_manager_scan_payload_candidates_for_builtin 0 "$topology_rows"

    if [ -n "$selected_payload_dir" ]; then
        TC_RESOLVED_PAYLOAD_DIR=$selected_payload_dir
        TC_RESOLVED_PAYLOAD_VOLUME=$selected_payload_volume
        TC_RESOLVED_PAYLOAD_DEVICE=$selected_payload_device
        tc_log "manager payload selected: dir=$TC_RESOLVED_PAYLOAD_DIR volume=$TC_RESOLVED_PAYLOAD_VOLUME device=$TC_RESOLVED_PAYLOAD_DEVICE"
        return 0
    fi

    if [ -n "$first_invalid_payload_dir" ]; then
        tc_log "manager payload discovery failed: first mounted payload candidate is invalid at $first_invalid_payload_dir"
        tc_log_payload_candidate_diagnostics "manager refresh" "$first_invalid_payload_volume" "$first_invalid_payload_dir"
    fi
    tc_log "manager payload discovery: no valid payload directory found"
    return 1
}

tc_manager_share_name_exists() {
    wanted_share_name=$1
    while IFS= read -r existing_share_name || [ -n "$existing_share_name" ]; do
        [ "$existing_share_name" = "$wanted_share_name" ] && return 0
    done <<EOF
$TC_MANAGER_USED_SHARE_NAMES
EOF
    return 1
}

tc_manager_record_share_name() {
    share_name_to_record=$1
    if [ -z "$TC_MANAGER_USED_SHARE_NAMES" ]; then
        TC_MANAGER_USED_SHARE_NAMES=$share_name_to_record
    else
        TC_MANAGER_USED_SHARE_NAMES="$TC_MANAGER_USED_SHARE_NAMES
$share_name_to_record"
    fi
}

tc_manager_set_unique_share_name() {
    base=$1
    device=$2
    max_bytes=$3
    candidate=$(tc_bound_share_name "$base" "$max_bytes")
    suffix=1
    if tc_manager_share_name_exists "$candidate"; then
        candidate=$(tc_share_name_with_suffix "$base" " ($device)" "$max_bytes")
    fi
    while tc_manager_share_name_exists "$candidate"; do
        candidate=$(tc_share_name_with_suffix "$base" " ($device-$suffix)" "$max_bytes")
        suffix=$((suffix + 1))
    done
    tc_manager_record_share_name "$candidate"
    TC_MANAGER_UNIQUE_SHARE_NAME=$candidate
}

tc_manager_append_share_rows() {
    share_row=$(printf '%s\t%s\t%s\t%s\t%s\n' "$share_name" "$share_path" "$part_device" "$builtin" "$part_uuid")
    adisk_row=$(printf '%s\t%s\t%s\t%s\n' "$share_name" "$part_device" "$part_uuid" "$TC_ADISK_DISK_ADVF")
    if [ -z "$manager_share_rows" ]; then
        manager_share_rows=$share_row
    else
        manager_share_rows="$manager_share_rows
$share_row"
    fi
    if [ -z "$manager_adisk_rows" ]; then
        manager_adisk_rows=$adisk_row
    else
        manager_adisk_rows="$manager_adisk_rows
$adisk_row"
    fi
}

tc_manager_build_share_state_from_topology() {
    topology_rows=$1
    candidate_count=0
    share_count=0
    manager_share_rows=
    manager_adisk_rows=
    TC_MANAGER_USED_SHARE_NAMES=

    tc_log "manager share state: scanning mounted writable MaSt volumes"
    while IFS="$TC_TAB" read -r disk_device builtin part_device volume_root part_name part_uuid ||
        [ -n "$disk_device$builtin$part_device$volume_root$part_name$part_uuid" ]; do
        [ -n "$part_device" ] || continue
        candidate_count=$((candidate_count + 1))
        device_path="/dev/$part_device"
        tc_log "manager share candidate: device=$device_path disk=$disk_device builtin=$builtin root=$volume_root name=$part_name"
        if ! is_volume_root_mounted "$volume_root"; then
            tc_log "manager share skipped: $device_path at $volume_root is not mounted"
            continue
        fi
        if ! tc_volume_is_writable "$volume_root"; then
            tc_log "manager share skipped: $volume_root is not writable"
            continue
        fi

        share_path=$(tc_prepare_share_path "$builtin" "$volume_root") || return 1
        base_name=$(tc_sanitize_share_name "$part_name" "$part_device")
        share_name_budget=$(tc_adisk_share_name_budget "$part_device" "$part_uuid" "$TC_ADISK_DISK_ADVF")
        tc_manager_set_unique_share_name "$base_name" "$part_device" "$share_name_budget"
        share_name=$TC_MANAGER_UNIQUE_SHARE_NAME
        tc_manager_append_share_rows
        share_count=$((share_count + 1))
        tc_log "manager share prepared: $share_name -> $share_path uuid=$part_uuid builtin=$builtin"
    done <<EOF
$topology_rows
EOF

    tc_log "manager share state complete: candidates=$candidate_count shares=$share_count"
    [ "$share_count" -gt 0 ]
}

tc_manager_configure_ata_from_topology() {
    topology_rows=$1
    tc_ata_idle_value=${ATA_IDLE_SECONDS:-300}
    tc_ata_standby_value=${ATA_STANDBY:-}
    tc_ata_apply_idle=0
    tc_ata_apply_standby=0

    tc_log "manager ATA settings: scanning built-in ATA disks after share-state build"
    if tc_is_unsigned_integer "$tc_ata_idle_value"; then
        tc_ata_apply_idle=1
    else
        tc_log "manager ATA settings: idle tuning skipped; invalid ATA_IDLE_SECONDS=$tc_ata_idle_value"
    fi
    if [ -n "$tc_ata_standby_value" ]; then
        if tc_is_unsigned_integer "$tc_ata_standby_value"; then
            tc_ata_apply_standby=1
        else
            tc_log "manager ATA settings: standby tuning skipped; invalid ATA_STANDBY=$tc_ata_standby_value"
        fi
    fi
    if [ "$tc_ata_apply_idle" != "1" ] && [ "$tc_ata_apply_standby" != "1" ]; then
        tc_log "manager ATA settings: no valid drive settings configured"
        return 0
    fi

    configured_disks=" "
    while IFS="$TC_TAB" read -r disk_device builtin part_device volume_root part_name part_uuid ||
        [ -n "$disk_device$builtin$part_device$volume_root$part_name$part_uuid" ]; do
        [ -n "$disk_device" ] || continue
        if [ "$builtin" != "1" ]; then
            tc_log "manager ATA settings: skipping $disk_device for /dev/$part_device; MaSt marks disk as external"
            continue
        fi
        case "$disk_device" in
            wd[0-9]*) ;;
            *)
                tc_log "manager ATA settings: skipping $disk_device for /dev/$part_device; not a wd ATA disk"
                continue
                ;;
        esac
        if ! is_volume_root_mounted "$volume_root"; then
            tc_log "manager ATA settings: skipping $disk_device for /dev/$part_device; $volume_root is not mounted"
            continue
        fi
        case "$configured_disks" in
            *" $disk_device "*) continue ;;
        esac
        configured_disks="$configured_disks$disk_device "

        if [ "$tc_ata_apply_idle" = "1" ]; then
            tc_apply_ata_drive_setting "$disk_device" setidle idle "$tc_ata_idle_value" "$volume_root"
        fi
        if [ "$tc_ata_apply_standby" = "1" ]; then
            tc_apply_ata_drive_setting "$disk_device" setstandby standby "$tc_ata_standby_value" "$volume_root"
        fi
    done <<EOF
$topology_rows
EOF
}

tc_manager_apply_diskless_state() {
    refresh_reason=$1

    manager_topology_rows=
    tc_manager_clear_payload_state
    TC_MANAGER_RUNTIME_STAGED=0
    TC_MANAGER_LAST_BINARY_SIGNATURE=
    TC_MANAGER_LAST_CONFIG_SIGNATURE=
    TC_MANAGER_PENDING_CONFIG_SIGNATURE=
    TC_MANAGER_DISK_STATE_CHANGED=1
    tc_log "manager disk refresh complete: diskless/no-payload state applied reason=$refresh_reason"
}

tc_manager_apply_runtime_from_topology() {
    refresh_reason=$1
    topology_rows=$2
    refresh_start_seconds=$(tc_now_seconds)
    manager_topology_rows=$topology_rows
    topology_count=$(tc_manager_count_rows "$topology_rows")

    tc_log "manager disk refresh start: reason=$refresh_reason topology_rows=$topology_count"
    if [ "$topology_count" -eq 0 ]; then
        tc_manager_apply_diskless_state "$refresh_reason"
        return 0
    fi

    tc_manager_log_topology_rows "$topology_rows"
    tc_manager_activate_topology "$topology_rows"

    if ! tc_manager_resolve_payload_from_topology "$topology_rows"; then
        tc_manager_apply_diskless_state "$refresh_reason"
        return 0
    fi

    if ! tc_manager_build_share_state_from_topology "$topology_rows"; then
        tc_log "manager disk refresh: no writable MaSt share volumes are available; applying no-payload state"
        tc_manager_apply_diskless_state "$refresh_reason"
        return 0
    fi

    tc_log "manager disk refresh: applying ATA drive settings after share-state build"
    tc_manager_configure_ata_from_topology "$topology_rows"

    tc_manager_set_payload_state
    tc_manager_materialize_adisk_state || return 1
    if tc_payload_log_dir_ready; then
        tc_log "manager payload smbd log directory ready at $TC_PAYLOAD_LOG_DIR"
    else
        tc_log "manager payload smbd log directory unavailable at $TC_PAYLOAD_LOG_DIR"
    fi

    TC_MANAGER_DISK_STATE_CHANGED=1
    refresh_duration_seconds=$(tc_elapsed_seconds_since "$refresh_start_seconds")
    tc_log "manager disk refresh complete: reason=$refresh_reason payload=$TC_PAYLOAD_DIR shares=$(tc_manager_count_rows "$manager_share_rows") duration_seconds=$refresh_duration_seconds"
}

tc_manager_share_rows_include_device() {
    wanted_part_device=$1
    share_rows=$2

    [ -n "$share_rows" ] || return 1
    while IFS="$TC_TAB" read -r share_name share_path part_device builtin part_uuid ||
        [ -n "$share_name$share_path$part_device$builtin$part_uuid" ]; do
        [ -n "$part_device" ] || continue
        [ "$part_device" = "$wanted_part_device" ] && return 0
    done <<EOF
$share_rows
EOF
    return 1
}

tc_manager_check_active_mast_users() {
    mast_runtime_rows=$1
    active_share_rows=$2

    [ -n "$active_share_rows" ] || return 0

    mast_users_zero_count=0
    mast_users_reclaim_failed=0
    mast_users_seen_parts=" "

    while IFS="$TC_TAB" read -r disk_device builtin part_device volume_root part_name part_uuid part_format part_users ||
        [ -n "$disk_device$builtin$part_device$volume_root$part_name$part_uuid$part_format$part_users" ]; do
        [ -n "$part_device" ] || continue
        [ "$part_format" = "hfs" ] || continue
        tc_manager_share_rows_include_device "$part_device" "$active_share_rows" || continue

        case "$mast_users_seen_parts" in
            *" $part_device "*) ;;
            *) mast_users_seen_parts="$mast_users_seen_parts$part_device " ;;
        esac
        case "$part_users" in
            ""|*[!0123456789]*)
                tc_log "manager disk check: managed volume $part_device has unavailable MaSt users value; skipping reclaim"
                ;;
            0)
                mast_users_zero_count=$((mast_users_zero_count + 1))
                tc_log "manager disk check: managed volume $part_device users=0 requires diskd reclaim"
                if tc_wake_or_mount_volume "/dev/$part_device" "$volume_root"; then
                    tc_log "manager disk check: managed volume $part_device reclaimed through diskd.useVolume"
                else
                    mast_users_reclaim_failed=1
                    tc_log "manager disk check: managed volume $part_device reclaim failed"
                fi
                ;;
        esac
    done <<EOF
$mast_runtime_rows
EOF

    mast_users_missing_active=0
    while IFS="$TC_TAB" read -r share_name share_path part_device builtin part_uuid ||
        [ -n "$share_name$share_path$part_device$builtin$part_uuid" ]; do
        [ -n "$part_device" ] || continue
        case "$mast_users_seen_parts" in
            *" $part_device "*) ;;
            *)
                mast_users_missing_active=1
                tc_log "manager disk check: active managed share $share_name uses /dev/$part_device, but MaSt users snapshot did not include that HFS volume"
                ;;
        esac
    done <<EOF
$active_share_rows
EOF

    if [ "$mast_users_reclaim_failed" -ne 0 ] || [ "$mast_users_missing_active" -ne 0 ]; then
        tc_log "manager disk check: MaSt users recovery requires full disk runtime reload"
        return 1
    fi

    if [ "$mast_users_zero_count" -gt 0 ]; then
        tc_log "manager disk check: reclaimed $mast_users_zero_count managed volume(s) with users=0"
    fi
    return 0
}

tc_manager_reconcile_disk_state() {
    TC_MANAGER_DISK_PROBE_RESULT=unknown
    TC_MANAGER_DISK_REFRESH_RESULT=skipped

    current_mast_raw=$(tc_manager_read_mast_raw_with_retry) || {
        TC_MANAGER_DISK_PROBE_RESULT=failed_after_retry
        TC_MANAGER_DISK_REFRESH_RESULT=skipped_probe_failed
        return 1
    }
    current_runtime_rows=$(tc_manager_parse_mast_runtime_rows "$current_mast_raw") || {
        TC_MANAGER_DISK_PROBE_RESULT=runtime_parse_failed
        TC_MANAGER_DISK_REFRESH_RESULT=skipped_runtime_parse_failed
        return 1
    }
    current_stable_signature=$(tc_manager_runtime_rows_stable_signature "$current_runtime_rows") || {
        TC_MANAGER_DISK_PROBE_RESULT=stable_signature_failed
        TC_MANAGER_DISK_REFRESH_RESULT=skipped_stable_signature_failed
        return 1
    }

    if [ "${TC_MANAGER_MAST_CONFIRMED_STABLE_SIGNATURE_READY:-0}" != "1" ]; then
        TC_MANAGER_DISK_PROBE_RESULT=initial
        TC_MANAGER_DISK_REFRESH_RESULT=refresh_initial
        tc_manager_apply_runtime_from_topology initial "$current_stable_signature" || return 1
        TC_MANAGER_MAST_CONFIRMED_STABLE_SIGNATURE=$current_stable_signature
        TC_MANAGER_MAST_CONFIRMED_STABLE_SIGNATURE_READY=1
        tc_log "manager MaSt stable signature recorded from initial disk refresh input"
        return 0
    fi

    if [ "$current_stable_signature" = "$TC_MANAGER_MAST_CONFIRMED_STABLE_SIGNATURE" ]; then
        if ! tc_manager_check_active_mast_users "$current_runtime_rows" "${manager_share_rows:-}"; then
            TC_MANAGER_DISK_PROBE_RESULT=active_users_dropped
            TC_MANAGER_DISK_REFRESH_RESULT=refresh_active_users
            tc_manager_apply_runtime_from_topology active_users_dropped "$current_stable_signature" || return 1
            TC_MANAGER_MAST_CONFIRMED_STABLE_SIGNATURE=$current_stable_signature
            TC_MANAGER_MAST_CONFIRMED_STABLE_SIGNATURE_READY=1
            tc_log "manager MaSt stable signature recorded from active-users disk refresh input"
            return 0
        fi
        TC_MANAGER_DISK_PROBE_RESULT=unchanged
        TC_MANAGER_DISK_REFRESH_RESULT=skipped_unchanged
        TC_MANAGER_DISK_STATE_CHANGED=0
        tc_manager_debug_log "manager MaSt stable signature unchanged; disk refresh skipped"
        return 0
    fi

    pending_stable_signature=$current_stable_signature
    TC_MANAGER_DISK_PROBE_RESULT=pending_change
    tc_log "manager MaSt stable signature changed; debouncing ${MANAGER_TOPOLOGY_DEBOUNCE_SECONDS}s before disk refresh"
    sleep "$MANAGER_TOPOLOGY_DEBOUNCE_SECONDS"
    debounced_mast_raw=$(tc_manager_read_mast_raw) || {
        TC_MANAGER_DISK_PROBE_RESULT=debounce_failed
        TC_MANAGER_DISK_REFRESH_RESULT=skipped_debounce_failed
        tc_log "manager MaSt debounce probe failed; preserving current runtime state"
        return 1
    }
    debounced_runtime_rows=$(tc_manager_parse_mast_runtime_rows "$debounced_mast_raw") || {
        TC_MANAGER_DISK_PROBE_RESULT=debounce_runtime_parse_failed
        TC_MANAGER_DISK_REFRESH_RESULT=skipped_debounce_runtime_parse_failed
        return 1
    }
    debounced_stable_signature=$(tc_manager_runtime_rows_stable_signature "$debounced_runtime_rows") || {
        TC_MANAGER_DISK_PROBE_RESULT=debounce_stable_signature_failed
        TC_MANAGER_DISK_REFRESH_RESULT=skipped_debounce_stable_signature_failed
        return 1
    }

    if [ "$debounced_stable_signature" = "$TC_MANAGER_MAST_CONFIRMED_STABLE_SIGNATURE" ]; then
        TC_MANAGER_DISK_PROBE_RESULT=change_cleared
        TC_MANAGER_DISK_REFRESH_RESULT=skipped_change_cleared
        TC_MANAGER_DISK_STATE_CHANGED=0
        tc_log "manager MaSt stable signature change cleared after debounce; disk refresh skipped"
        return 0
    fi
    if [ "$debounced_stable_signature" != "$pending_stable_signature" ]; then
        TC_MANAGER_DISK_PROBE_RESULT=unstable
        TC_MANAGER_DISK_REFRESH_RESULT=skipped_unstable
        TC_MANAGER_DISK_STATE_CHANGED=0
        tc_log "manager MaSt stable signature still changing after debounce; postponing disk refresh"
        return 0
    fi

    TC_MANAGER_DISK_PROBE_RESULT=change_confirmed
    TC_MANAGER_DISK_REFRESH_RESULT=refresh_confirmed_change
    tc_manager_apply_runtime_from_topology topology_changed "$debounced_stable_signature" || return 1
    TC_MANAGER_MAST_CONFIRMED_STABLE_SIGNATURE=$debounced_stable_signature
    TC_MANAGER_MAST_CONFIRMED_STABLE_SIGNATURE_READY=1
    tc_log "manager MaSt stable signature recorded from confirmed-change disk refresh input"
    return 0
}

tc_manager_select_samba_sources() {
    if ! tc_manager_select_current_payload; then
        tc_log "manager Samba staging skipped: payload state is unavailable"
        return 1
    fi

    manager_smbd_src=$(tc_find_payload_smbd "$manager_payload_dir") || {
        tc_log "manager Samba staging failed: missing smbd binary in $manager_payload_dir"
        return 1
    }

    manager_nbns_src=
    if [ "$NBNS_ENABLED" = "1" ]; then
        if manager_nbns_src=$(tc_find_payload_nbns "$manager_payload_dir"); then
            :
        else
            manager_nbns_src=
        fi
    fi
}

tc_manager_samba_runtime_files_missing() {
    [ -x "$TC_SMBD_BIN" ] || return 0
    [ -f "$RAM_PRIVATE/smbpasswd" ] || return 0
    [ -f "$RAM_PRIVATE/username.map" ] || return 0
    if [ "$NBNS_ENABLED" = "1" ] && [ -n "${manager_nbns_src:-}" ]; then
        [ -x "$TC_NBNS_BIN" ] || return 0
    fi
    return 1
}

tc_manager_stage_samba_runtime_files_if_needed() {
    if ! tc_manager_select_samba_sources; then
        return 1
    fi

    fresh_binary_signature=$(tc_manager_samba_file_signature "$manager_payload_dir" "$manager_smbd_src" "$manager_nbns_src")
    manager_stage_needed=0
    manager_binary_changed=0
    if [ "${TC_MANAGER_RUNTIME_STAGED:-0}" != "1" ]; then
        manager_stage_needed=1
    elif [ "$fresh_binary_signature" != "${TC_MANAGER_LAST_BINARY_SIGNATURE:-}" ]; then
        manager_stage_needed=1
        manager_binary_changed=1
    elif tc_manager_samba_runtime_files_missing; then
        manager_stage_needed=1
    fi

    if [ "$manager_stage_needed" -eq 0 ]; then
        tc_manager_debug_log "manager Samba runtime file staging unchanged"
        return 0
    fi

    tc_log "manager Samba runtime file staging required"
    tc_stage_runtime "$manager_payload_dir" "$manager_smbd_src" "$manager_nbns_src" || return 1
    TC_MANAGER_LAST_BINARY_SIGNATURE=$fresh_binary_signature
    TC_MANAGER_RUNTIME_STAGED=1
    tc_log "manager Samba runtime file staging complete"

    if runtime_process_present_by_ucomm smbd; then
        if [ "$manager_binary_changed" -eq 1 ]; then
            TC_MANAGER_SMBD_RESTART_REQUIRED=1
        else
            TC_MANAGER_SMBD_RELOAD_REQUIRED=1
        fi
    fi
    return 0
}

tc_manager_render_smb_conf_if_needed() {
    fresh_config_signature=$(tc_manager_samba_config_signature)
    if [ "$fresh_config_signature" = "${TC_MANAGER_LAST_CONFIG_SIGNATURE:-}" ] && [ -f "$TC_SMBD_CONF" ]; then
        tc_manager_debug_log "manager Samba config render unchanged"
        return 0
    fi

    tc_log "manager Samba config render required"
    tc_manager_generate_smb_conf || return 1
    TC_MANAGER_PENDING_CONFIG_SIGNATURE=$fresh_config_signature
    if runtime_process_present_by_ucomm smbd; then
        TC_MANAGER_SMBD_RELOAD_REQUIRED=1
    fi
    return 0
}

tc_manager_commit_smbd_runtime_apply() {
    if [ -n "${TC_MANAGER_PENDING_CONFIG_SIGNATURE:-}" ]; then
        TC_MANAGER_LAST_CONFIG_SIGNATURE=$TC_MANAGER_PENDING_CONFIG_SIGNATURE
        TC_MANAGER_PENDING_CONFIG_SIGNATURE=
    fi
    TC_MANAGER_SMBD_RESTART_REQUIRED=0
    TC_MANAGER_SMBD_RELOAD_REQUIRED=0
}

tc_manager_restore_smb_bind_after_config_failure() {
    if [ "${TC_MANAGER_SMB_BIND_CHANGED:-0}" = "1" ]; then
        TC_SMB_BIND_INTERFACES=${TC_MANAGER_SMB_BIND_PREVIOUS:-}
        TC_MANAGER_SMB_BIND_CHANGED=0
        TC_MANAGER_SMB_BIND_PREVIOUS=
        tc_log "manager Samba: restored previous bind interfaces after config render failure"
    fi
}

tc_manager_validate_smbd_runtime_state() {
    recovery_status=0
    recovery_share_count=0

    if ! tc_manager_select_current_payload; then
        tc_log "manager smbd validation skipped: payload state is unavailable"
        return 1
    fi

    tc_log "manager smbd validation: checking payload volume before smbd restart: device=$manager_payload_device root=$manager_payload_volume"
    if ! is_volume_root_mounted "$manager_payload_volume"; then
        tc_log "manager smbd validation: payload volume is not mounted before smbd restart: device=$manager_payload_device root=$manager_payload_volume"
        return 1
    fi

    if ! tc_verify_payload_dir "$manager_payload_dir"; then
        tc_log "manager smbd validation: payload directory is invalid before smbd restart: $manager_payload_dir"
        return 1
    fi

    if [ -z "${manager_share_rows:-}" ]; then
        tc_log "manager smbd validation: active share state missing; smbd restart will use existing config"
        return 0
    fi

    while IFS="$TC_TAB" read -r share_name share_path part_device builtin part_uuid ||
        [ -n "$share_name$share_path$part_device$builtin$part_uuid" ]; do
        [ -n "$part_device" ] || continue
        recovery_share_count=$((recovery_share_count + 1))
        tc_log "manager smbd validation: checking active share volume before smbd restart: share=$share_name device=/dev/$part_device root=/Volumes/$part_device"
        if is_volume_root_mounted "/Volumes/$part_device"; then
            :
        else
            recovery_status=1
            tc_log "manager smbd validation: active share volume is not mounted before smbd restart: share=$share_name device=/dev/$part_device root=/Volumes/$part_device"
        fi
    done <<EOF
$manager_share_rows
EOF

    if [ "$recovery_share_count" -eq 0 ]; then
        tc_log "manager smbd validation: active share state has no valid rows; smbd restart will use existing config"
        return 0
    fi

    if [ "$recovery_status" -ne 0 ]; then
        tc_log "manager smbd validation: one or more active share volumes are unavailable before smbd restart"
    fi
    return "$recovery_status"
}

tc_manager_start_smbd_if_needed() {
    if runtime_process_present_by_ucomm smbd; then
        if tc_smbd_bound_tcp_445; then
            return 0
        fi
        tc_log "manager smbd recovery: smbd is running without required TCP 445 listeners; restarting"
        tc_log_smbd_socket_diagnostics
        stop_runtime_process_by_ucomm "smbd" smbd || return 1
    fi

    if [ ! -x "$TC_SMBD_BIN" ] || [ ! -f "$TC_SMBD_CONF" ]; then
        tc_log "manager smbd recovery: smbd is not running, but runtime is not staged yet"
        return 0
    fi

    tc_watchdog_refresh_runtime_identity_for_recovery
    tc_manager_validate_smbd_runtime_state || return 1
    rm -rf "$LOCKS_ROOT"/* >/dev/null 2>&1 || true
    "$TC_SMBD_BIN" -D -s "$TC_SMBD_CONF" >/dev/null 2>&1 || true
    tc_log "manager smbd recovery: smbd restart requested"
    if wait_for_process smbd 15 && tc_wait_for_smbd_ipv4_445 15; then
        return 0
    fi
    tc_log "manager smbd recovery: smbd restart failed to bind required TCP 445 listeners"
    tc_log_smbd_socket_diagnostics
    stop_runtime_process_by_ucomm "smbd" smbd || true
    return 1
}

tc_manager_apply_smbd_runtime_changes() {
    TC_MANAGER_SMBD_APPLY_FAILURE=

    if [ "${TC_MANAGER_SMBD_RESTART_REQUIRED:-0}" = "1" ]; then
        tc_log "manager smbd recovery: restarting smbd after staged runtime change"
        if runtime_process_present_by_ucomm smbd; then
            if ! stop_runtime_process_by_ucomm "smbd" smbd; then
                TC_MANAGER_SMBD_APPLY_FAILURE=stop_failed
                return 1
            fi
        fi
        TC_MANAGER_SMBD_RELOAD_REQUIRED=0
        if tc_manager_start_smbd_if_needed; then
            tc_manager_commit_smbd_runtime_apply
            return 0
        fi
        TC_MANAGER_SMBD_APPLY_FAILURE=restart_failed
        return 1
    fi

    if [ "${TC_MANAGER_SMBD_RELOAD_REQUIRED:-0}" = "1" ] &&
        runtime_process_present_by_ucomm smbd &&
        tc_smbd_bound_tcp_445; then
        if tc_reload_smbd_config; then
            tc_manager_commit_smbd_runtime_apply
            return 0
        fi
        tc_log "manager smbd recovery: smbd config reload failed; restarting"
        if ! stop_runtime_process_by_ucomm "smbd" smbd; then
            TC_MANAGER_SMBD_APPLY_FAILURE=stop_after_reload_failed
            return 1
        fi
        TC_MANAGER_SMBD_RELOAD_REQUIRED=0
        if tc_manager_start_smbd_if_needed; then
            tc_manager_commit_smbd_runtime_apply
            return 0
        fi
        TC_MANAGER_SMBD_APPLY_FAILURE=restart_after_reload_failed
        return 1
    fi

    if tc_manager_start_smbd_if_needed; then
        tc_manager_commit_smbd_runtime_apply
        return 0
    fi
    TC_MANAGER_SMBD_APPLY_FAILURE=start_failed
    return 1
}

tc_manager_reconcile_smb_bind_interfaces() {
    TC_MANAGER_SMB_BIND_CHANGED=0
    TC_MANAGER_SMB_BIND_DEFERRED=0
    TC_MANAGER_SMB_BIND_PREVIOUS=

    if fresh_bind_interfaces=$(tc_probe_smb_bind_interfaces); then
        if [ -z "${TC_SMB_BIND_INTERFACES:-}" ]; then
            TC_MANAGER_SMB_BIND_PREVIOUS=
            TC_SMB_BIND_INTERFACES=$fresh_bind_interfaces
            TC_MANAGER_SMB_BIND_CHANGED=1
            tc_log "manager Samba: initialized bind interfaces: $TC_SMB_BIND_INTERFACES"
            return 0
        fi
        if [ "$fresh_bind_interfaces" = "$TC_SMB_BIND_INTERFACES" ]; then
            return 0
        fi

        old_bind_interfaces=$TC_SMB_BIND_INTERFACES
        TC_SMB_BIND_INTERFACES=$fresh_bind_interfaces
        TC_MANAGER_SMB_BIND_PREVIOUS=$old_bind_interfaces
        TC_MANAGER_SMB_BIND_CHANGED=1
        tc_log "manager Samba: bind interfaces changed: $old_bind_interfaces -> $TC_SMB_BIND_INTERFACES"
        if ! tc_manager_validate_smbd_runtime_state; then
            TC_SMB_BIND_INTERFACES=$old_bind_interfaces
            TC_MANAGER_SMB_BIND_CHANGED=0
            TC_MANAGER_SMB_BIND_PREVIOUS=
            tc_log "manager Samba: cannot apply bind change; disk runtime validation failed"
            return 1
        fi
        return 0
    else
        bind_probe_status=$?
    fi

    if tc_auto_ip_unavailable_status "$bind_probe_status"; then
        TC_MANAGER_SMB_BIND_DEFERRED=1
        tc_mark_smb_deferred_no_ip
        return 0
    fi

    tc_log "manager Samba: bind probe failed with exit code $bind_probe_status"
    return 1
}

tc_manager_reconcile_smbd() {
    if ! tc_manager_apply_smbd_runtime_changes; then
        tc_log "manager Samba: smbd runtime apply failed reason=${TC_MANAGER_SMBD_APPLY_FAILURE:-unknown}; will retry on next reconciliation pass"
        return 1
    fi
}

tc_manager_launch_mdns_advertiser() {
    context=$1
    kill_prior=$2
    wait_attempts=$3
    diskless=$4

    tc_manager_materialize_adisk_state || return 1
    tc_launch_mdns_advertiser "$context" "$kill_prior" "$wait_attempts" "$diskless"
}

tc_manager_launch_current_mdns_advertiser() {
    context=$1
    wait_attempts=$2

    if tc_manager_current_payload_ready; then
        tc_manager_launch_mdns_advertiser "$context" 1 "$wait_attempts" 0
    else
        tc_manager_launch_mdns_advertiser "$context" 1 "$wait_attempts" 1
    fi
}

tc_manager_prepare_mdns_snapshot() {
    if tc_mdnsresponder_alive; then
        tc_log "manager mDNS snapshot: Apple mDNSResponder is alive; settling 3s before capture"
        sleep 3
        if tc_capture_mdns_snapshot_for_manager; then
            return 0
        fi
        tc_log "manager mDNS snapshot: capture failed; retrying once"
        if tc_capture_mdns_snapshot_for_manager; then
            return 0
        fi
        tc_log "manager mDNS snapshot: capture retry failed; checking for fresh snapshot fallback"
    else
        tc_log "manager mDNS snapshot: Apple mDNSResponder is not alive; capture skipped"
    fi

    if tc_mdns_snapshot_newer_than_boot; then
        return 0
    fi

    tc_log "manager mDNS snapshot: no fresh snapshot exists; generating AirPort fallback"
    tc_generate_mdns
    return 0
}

tc_manager_reconcile_mdns() {
    mdns_auto_ip_status=0

    if runtime_process_present_by_ucomm "$MDNS_PROC_NAME"; then
        if tc_mdns_bound_udp_5353; then
            return 0
        fi
        tc_log "manager mDNS recovery: mdns advertiser is running without required UDP 5353 listeners"
        if tc_mdns_auto_ip_available; then
            TC_MDNS_AUTO_IP_SEEN=1
            stop_runtime_process_by_ucomm "$MDNS_PROC_NAME" "$MDNS_PROC_NAME" || return 1
        else
            mdns_auto_ip_status=$?
            if tc_auto_ip_unavailable_status "$mdns_auto_ip_status"; then
                tc_mark_mdns_deferred_no_ip
                return 0
            fi
            TC_WATCHDOG_MDNS_UNAVAILABLE=1
            tc_log "manager mDNS recovery: mDNS auto-ip check failed with exit code $mdns_auto_ip_status"
            return 0
        fi
    fi

    if runtime_process_present_by_ucomm "$MDNS_PROC_NAME"; then
        return 0
    fi

    tc_watchdog_refresh_runtime_identity_for_recovery
    if ! tc_ensure_mdns_auto_ip_seen; then
        return 0
    fi
    tc_manager_prepare_mdns_snapshot
    tc_manager_launch_current_mdns_advertiser "manager recovery" 10
}

tc_manager_wait_for_nbns_ready() {
    wait_attempts=$1

    if ! tc_nbns_enabled; then
        return 0
    fi
    if [ "${TC_WATCHDOG_NBNS_DEFERRED_NO_IP:-0}" = "1" ]; then
        tc_log "manager NBNS: readiness wait skipped; NBNS deferred waiting for usable address"
        return 0
    fi

    wait_attempt=0
    while [ "$wait_attempt" -le "$wait_attempts" ]; do
        if runtime_process_present_by_ucomm "$NBNS_PROC_NAME" &&
            tc_nbns_bound_udp_137; then
            tc_manager_debug_log "manager NBNS: responder ready on required UDP 137 sockets"
            return 0
        fi

        if [ "$wait_attempt" -eq "$wait_attempts" ]; then
            break
        fi
        wait_attempt=$((wait_attempt + 1))
        sleep 1
    done

    tc_log "manager NBNS: responder did not become ready on required UDP 137 sockets after ${wait_attempts}s"
    return 1
}

tc_manager_update_payload_status() {
    if tc_manager_select_current_payload; then
        manager_payload_expected=1
        manager_payload_status=ready
    else
        manager_payload_expected=0
        manager_payload_status=none
    fi
}

tc_manager_samba_runtime_ready_for_bind_tick() {
    [ "${TC_MANAGER_RUNTIME_STAGED:-0}" = "1" ] &&
        [ -x "$TC_SMBD_BIN" ] &&
        [ -f "$TC_SMBD_CONF" ]
}

tc_manager_record_successful_bind_status() {
    if [ "${TC_MANAGER_SMB_BIND_DEFERRED:-0}" = "1" ]; then
        manager_bind_status=deferred_no_ip
    elif [ "${TC_MANAGER_SMB_BIND_CHANGED:-0}" = "1" ]; then
        manager_bind_status=changed
    else
        manager_bind_status=ok
    fi
}

tc_manager_run_identity_step() {
    manager_step_start_seconds=$(tc_now_seconds)
    tc_manager_debug_log "manager pass $manager_iteration_id step=identity start"
    manager_step_status=0
    tc_manager_debug_log "manager identity: refreshing runtime naming and local hostname"
    if ! tc_prepare_local_hostname_resolution; then
        manager_step_status=1
    fi
    if [ "$manager_step_status" -eq 0 ] && ! tc_init_runtime_identity; then
        manager_step_status=1
    fi
    if [ "$manager_step_status" -eq 0 ]; then
        TC_WATCHDOG_RECOVERY_IDENTITY_REFRESHED=1
        if tc_watchdog_identity_signature_changed; then
            TC_MANAGER_IDENTITY_CHANGED=1
            tc_log "manager identity change: refreshing managed advertisers and Samba config"
            if tc_manager_current_payload_ready && [ -f "$TC_SMBD_CONF" ]; then
                if ! tc_manager_generate_smb_conf; then
                    manager_step_status=1
                fi
                if [ "$manager_step_status" -eq 0 ] && runtime_process_present_by_ucomm smbd; then
                    if ! tc_reload_smbd_config; then
                        manager_step_status=1
                    fi
                fi
            fi
            if [ "$manager_step_status" -eq 0 ] && tc_manager_current_payload_ready && [ -f "$TC_SMBD_CONF" ]; then
                TC_MANAGER_LAST_CONFIG_SIGNATURE=$(tc_manager_samba_config_signature)
            fi
            if [ "$manager_step_status" -eq 0 ]; then
                stop_runtime_process_by_ucomm "$MDNS_PROC_NAME" "$MDNS_PROC_NAME" || true
                if tc_nbns_enabled; then
                    stop_runtime_process_by_ucomm "$NBNS_PROC_NAME" "$NBNS_PROC_NAME" || true
                fi
                if ! tc_watchdog_write_identity_signature; then
                    manager_step_status=1
                fi
            fi
        fi
    fi
    if [ "$manager_step_status" -eq 0 ]; then
        manager_identity_status=ok
        tc_manager_log_step_end "$manager_iteration_id" identity "$manager_step_start_seconds" ok
        return 0
    fi

    manager_status=1
    manager_identity_status=failed
    tc_manager_log_step_end "$manager_iteration_id" identity "$manager_step_start_seconds" failed
    return 1
}

tc_manager_run_disk_step() {
    manager_step_start_seconds=$(tc_now_seconds)
    tc_manager_debug_log "manager pass $manager_iteration_id step=disk start"
    if tc_manager_reconcile_disk_state; then
        manager_disk_status=ok
        tc_manager_log_step_end "$manager_iteration_id" disk "$manager_step_start_seconds" ok
        return 0
    fi

    manager_status=1
    manager_disk_status=failed
    tc_manager_log_step_end "$manager_iteration_id" disk "$manager_step_start_seconds" failed
    return 1
}

tc_manager_run_samba_full_step() {
    manager_step_start_seconds=$(tc_now_seconds)
    tc_manager_debug_log "manager pass $manager_iteration_id step=samba start"
    manager_step_status=0
    tc_manager_debug_log "manager Samba: reconciling staged runtime, bind interfaces, and smbd"
    TC_MANAGER_SMBD_RESTART_REQUIRED=0
    TC_MANAGER_SMBD_RELOAD_REQUIRED=0
    TC_MANAGER_PENDING_CONFIG_SIGNATURE=

    if ! tc_manager_stage_samba_runtime_files_if_needed; then
        manager_step_status=1
    fi
    if [ "$manager_step_status" -eq 0 ]; then
        tc_manager_debug_log "manager Samba: reconciling bind interfaces"
        if tc_manager_reconcile_smb_bind_interfaces; then
            if [ "${TC_MANAGER_SMB_BIND_CHANGED:-0}" = "1" ]; then
                TC_MANAGER_SMBD_RESTART_REQUIRED=1
            fi
            tc_manager_record_successful_bind_status
        else
            manager_bind_status=failed
            manager_step_status=1
        fi
    fi
    if [ "$manager_step_status" -eq 0 ]; then
        tc_manager_debug_log "manager Samba: rendering config"
        if ! tc_manager_render_smb_conf_if_needed; then
            tc_manager_restore_smb_bind_after_config_failure
            manager_step_status=1
        fi
    fi
    if [ "$manager_step_status" -eq 0 ]; then
        tc_manager_debug_log "manager Samba: reconciling smbd"
        if ! tc_manager_reconcile_smbd; then
            manager_step_status=1
        fi
    fi
    if [ "$manager_step_status" -eq 0 ]; then
        manager_samba_status=ok
        tc_manager_log_step_end "$manager_iteration_id" samba "$manager_step_start_seconds" ok
        return 0
    fi

    manager_status=1
    manager_samba_status=failed
    tc_manager_log_step_end "$manager_iteration_id" samba "$manager_step_start_seconds" failed
    return 1
}

tc_manager_run_samba_bind_step() {
    manager_step_start_seconds=$(tc_now_seconds)
    tc_manager_debug_log "manager pass $manager_iteration_id step=samba_bind start"
    TC_MANAGER_SMBD_RESTART_REQUIRED=0
    TC_MANAGER_SMBD_RELOAD_REQUIRED=0
    TC_MANAGER_PENDING_CONFIG_SIGNATURE=

    if ! tc_manager_current_payload_ready; then
        manager_bind_status=skipped_no_payload
        tc_log "manager Samba bind: skipped because no payload is active"
        tc_manager_log_step_end "$manager_iteration_id" samba_bind "$manager_step_start_seconds" skipped
        return 0
    fi
    if ! tc_manager_samba_runtime_ready_for_bind_tick; then
        manager_bind_status=skipped_runtime
        tc_log "manager Samba bind: runtime is not staged; waiting for full service reconciliation"
        tc_manager_log_step_end "$manager_iteration_id" samba_bind "$manager_step_start_seconds" skipped
        return 0
    fi

    tc_manager_debug_log "manager Samba bind: checking bind interfaces"
    if tc_manager_reconcile_smb_bind_interfaces; then
        if [ "${TC_MANAGER_SMB_BIND_CHANGED:-0}" = "1" ]; then
            TC_MANAGER_SMBD_RESTART_REQUIRED=1
            if ! tc_manager_render_smb_conf_if_needed; then
                tc_manager_restore_smb_bind_after_config_failure
                manager_status=1
                manager_bind_status=failed
                tc_manager_log_step_end "$manager_iteration_id" samba_bind "$manager_step_start_seconds" failed
                return 1
            fi
            if ! tc_manager_reconcile_smbd; then
                manager_status=1
                manager_bind_status=failed
                tc_manager_log_step_end "$manager_iteration_id" samba_bind "$manager_step_start_seconds" failed
                return 1
            fi
        fi
        tc_manager_record_successful_bind_status
        tc_manager_log_step_end "$manager_iteration_id" samba_bind "$manager_step_start_seconds" ok
        return 0
    fi

    manager_status=1
    manager_bind_status=failed
    tc_manager_log_step_end "$manager_iteration_id" samba_bind "$manager_step_start_seconds" failed
    return 1
}

tc_manager_run_no_payload_step() {
    manager_step_start_seconds=$(tc_now_seconds)
    tc_manager_debug_log "manager pass $manager_iteration_id step=no_payload start"
    tc_manager_debug_log "manager no_payload: clearing staged runtime and stopping Samba lane"
    TC_MANAGER_RUNTIME_STAGED=0
    TC_MANAGER_LAST_BINARY_SIGNATURE=
    TC_MANAGER_LAST_CONFIG_SIGNATURE=
    TC_MANAGER_PENDING_CONFIG_SIGNATURE=
    TC_MANAGER_SMBD_RESTART_REQUIRED=0
    TC_MANAGER_SMBD_RELOAD_REQUIRED=0
    if tc_watchdog_stop_samba_lane_without_payload; then
        manager_samba_status=no_payload
        tc_manager_log_step_end "$manager_iteration_id" no_payload "$manager_step_start_seconds" ok
        return 0
    fi

    manager_status=1
    manager_samba_status=failed
    tc_manager_log_step_end "$manager_iteration_id" no_payload "$manager_step_start_seconds" failed
    return 1
}

tc_manager_run_nbns_reconcile_before_mdns() {
    tc_manager_debug_log "manager NBNS: reconciling responder before mDNS so startup can overlap mDNS capture"
    if tc_watchdog_reconcile_nbns; then
        manager_nbns_reconcile_status=ok
        tc_manager_debug_log "manager NBNS: reconcile requested; readiness check will run after mDNS"
        return 0
    fi

    manager_status=1
    manager_nbns_status=failed
    manager_nbns_reconcile_status=failed
    tc_log "manager NBNS: reconcile failed before mDNS"
    return 1
}

tc_manager_run_mdns_step() {
    manager_step_start_seconds=$(tc_now_seconds)
    tc_manager_debug_log "manager pass $manager_iteration_id step=mdns start"
    tc_manager_debug_log "manager mDNS: reconciling advertiser"
    if [ "${TC_MANAGER_DISK_STATE_CHANGED:-0}" = "1" ] || [ "${TC_MANAGER_IDENTITY_CHANGED:-0}" = "1" ]; then
        tc_log "manager mDNS refresh required after disk or identity change"
        stop_runtime_process_by_ucomm "$MDNS_PROC_NAME" "$MDNS_PROC_NAME" || true
    fi
    if tc_manager_reconcile_mdns; then
        manager_mdns_status=ok
        tc_manager_log_step_end "$manager_iteration_id" mdns "$manager_step_start_seconds" ok
        return 0
    fi

    manager_status=1
    manager_mdns_status=failed
    tc_manager_log_step_end "$manager_iteration_id" mdns "$manager_step_start_seconds" failed
    return 1
}

tc_manager_run_nbns_wait_step() {
    manager_step_start_seconds=$(tc_now_seconds)
    tc_manager_debug_log "manager pass $manager_iteration_id step=nbns start"
    if [ "$manager_nbns_reconcile_status" = "ok" ] && tc_manager_wait_for_nbns_ready 10; then
        manager_nbns_status=ok
        tc_manager_log_step_end "$manager_iteration_id" nbns "$manager_step_start_seconds" ok
        return 0
    fi

    manager_status=1
    manager_nbns_status=failed
    tc_manager_log_step_end "$manager_iteration_id" nbns "$manager_step_start_seconds" failed
    return 1
}

tc_manager_run_full_service_steps() {
    service_step_status=0

    tc_manager_run_identity_step || service_step_status=1
    tc_manager_update_payload_status
    if [ "$manager_payload_expected" -eq 1 ]; then
        tc_manager_run_samba_full_step || service_step_status=1
    else
        tc_manager_run_no_payload_step || service_step_status=1
    fi

    if [ "$manager_payload_expected" -eq 1 ]; then
        tc_manager_run_nbns_reconcile_before_mdns || service_step_status=1
    fi
    tc_manager_run_mdns_step || service_step_status=1
    if [ "$manager_payload_expected" -eq 1 ]; then
        tc_manager_run_nbns_wait_step || service_step_status=1
    fi

    if [ "$service_step_status" -eq 0 ]; then
        manager_services_status=ok
        return 0
    fi
    manager_services_status=failed
    return 1
}

tc_prepare_ram_root

MANAGER_DISK_POLL_SECONDS=$(tc_sanitize_positive_integer "${MANAGER_DISK_POLL_SECONDS:-10}" 10)
MANAGER_BIND_POLL_SECONDS=$(tc_sanitize_positive_integer "${MANAGER_BIND_POLL_SECONDS:-$MANAGER_DISK_POLL_SECONDS}" "$MANAGER_DISK_POLL_SECONDS")
MANAGER_SERVICE_POLL_SECONDS=$(tc_sanitize_positive_integer "${MANAGER_SERVICE_POLL_SECONDS:-30}" 30)
MANAGER_MAST_RETRY_SECONDS=$(tc_sanitize_positive_integer "${MANAGER_MAST_RETRY_SECONDS:-5}" 5)
MANAGER_TOPOLOGY_DEBOUNCE_SECONDS=$(tc_sanitize_positive_integer "${WATCHDOG_TOPOLOGY_DEBOUNCE_SECONDS:-5}" 5)
MANAGER_STOP_POLL_SECONDS=$(tc_sanitize_positive_integer "${MANAGER_STOP_POLL_SECONDS:-1}" 1)
TC_MANAGER_STOP_REQUESTED=0
TC_MANAGER_ITERATION=0
TC_MANAGER_RUNTIME_STAGED=0
TC_MANAGER_LAST_BINARY_SIGNATURE=
TC_MANAGER_LAST_CONFIG_SIGNATURE=
TC_MANAGER_PENDING_CONFIG_SIGNATURE=
TC_MANAGER_SMBD_RESTART_REQUIRED=0
TC_MANAGER_SMBD_RELOAD_REQUIRED=0
TC_MANAGER_SMBD_APPLY_FAILURE=
TC_MANAGER_SMB_BIND_PREVIOUS=
TC_MANAGER_MAST_CONFIRMED_STABLE_SIGNATURE=
TC_MANAGER_MAST_CONFIRMED_STABLE_SIGNATURE_READY=0
manager_payload_ready=0
manager_payload_dir=
manager_payload_volume=
manager_payload_device=
manager_smbd_src=
manager_nbns_src=
manager_topology_rows=
manager_share_rows=
manager_adisk_rows=
manager_service_seconds_until_due=0
manager_bind_seconds_until_due=0
tc_manager_clear_payload_state

tc_log "manager startup beginning"
tc_log "manager intervals: disk=${MANAGER_DISK_POLL_SECONDS}s bind=${MANAGER_BIND_POLL_SECONDS}s services=${MANAGER_SERVICE_POLL_SECONDS}s mast_retry=${MANAGER_MAST_RETRY_SECONDS}s topology_debounce=${MANAGER_TOPOLOGY_DEBOUNCE_SECONDS}s stop_poll=${MANAGER_STOP_POLL_SECONDS}s"

trap 'TC_MANAGER_STOP_REQUESTED=1' TERM INT

while ! tc_manager_stop_requested; do
    TC_MANAGER_ITERATION=$((TC_MANAGER_ITERATION + 1))
    manager_iteration_id=$TC_MANAGER_ITERATION
    manager_iteration_start_seconds=$(tc_now_seconds)
    manager_status=0
    manager_payload_expected=0
    manager_identity_status=skipped
    manager_disk_status=skipped
    manager_payload_status=skipped
    manager_samba_status=skipped
    manager_bind_status=skipped
    manager_mdns_status=skipped
    manager_nbns_status=skipped
    manager_services_status=skipped
    manager_scheduler_status=disk_only
    TC_WATCHDOG_RECOVERY_IDENTITY_REFRESHED=0
    TC_MANAGER_DISK_STATE_CHANGED=0
    TC_MANAGER_IDENTITY_CHANGED=0
    manager_nbns_reconcile_status=skipped
    manager_services_due=0
    manager_bind_due=0

    if [ "$manager_service_seconds_until_due" -le 0 ]; then
        manager_services_due=1
    fi
    if [ "$manager_bind_seconds_until_due" -le 0 ]; then
        manager_bind_due=1
    fi

    tc_manager_debug_log "manager pass $manager_iteration_id start"
    tc_watchdog_reset_pass_state

    tc_manager_run_disk_step || true
    tc_manager_update_payload_status

    if [ "${TC_MANAGER_DISK_STATE_CHANGED:-0}" = "1" ]; then
        manager_services_due=1
        manager_bind_due=1
        tc_log "manager scheduler: disk state changed; running full service reconciliation now"
    fi

    if [ "$manager_services_due" -eq 0 ] &&
        [ "$manager_bind_due" -eq 1 ] &&
        [ "$manager_payload_expected" -eq 1 ] &&
        ! tc_manager_samba_runtime_ready_for_bind_tick; then
        manager_services_due=1
        tc_log "manager scheduler: Samba runtime is not ready for bind-only check; running full service reconciliation now"
    fi

    if [ "$manager_services_due" -eq 1 ]; then
        manager_scheduler_status=services
        tc_manager_debug_log "manager scheduler: full service reconciliation due"
        if tc_manager_run_full_service_steps; then
            manager_service_seconds_until_due=$MANAGER_SERVICE_POLL_SECONDS
        else
            manager_service_seconds_until_due=0
        fi
        manager_bind_seconds_until_due=$MANAGER_BIND_POLL_SECONDS
    elif [ "$manager_bind_due" -eq 1 ]; then
        manager_scheduler_status=bind_only
        tc_manager_debug_log "manager scheduler: Samba bind reconciliation due"
        if tc_manager_run_samba_bind_step; then
            manager_bind_seconds_until_due=$MANAGER_BIND_POLL_SECONDS
        else
            manager_bind_seconds_until_due=0
        fi
    else
        tc_manager_debug_log "manager scheduler: service reconciliation skipped on disk-only pass"
    fi

    manager_iteration_duration_seconds=$(tc_elapsed_seconds_since "$manager_iteration_start_seconds")
    if [ "$manager_status" -eq 0 ]; then
        manager_pass_status=ok
    else
        manager_pass_status=failed
    fi
    manager_next_service_seconds=$((manager_service_seconds_until_due - MANAGER_DISK_POLL_SECONDS))
    manager_next_bind_seconds=$((manager_bind_seconds_until_due - MANAGER_DISK_POLL_SECONDS))
    if [ "$manager_next_service_seconds" -lt 0 ]; then
        manager_next_service_seconds=0
    fi
    if [ "$manager_next_bind_seconds" -lt 0 ]; then
        manager_next_bind_seconds=0
    fi
    if tc_smbd_debug_logging_enabled ||
        [ "$manager_pass_status" != "ok" ] ||
        [ "${TC_MANAGER_DISK_STATE_CHANGED:-0}" = "1" ] ||
        [ "${TC_MANAGER_IDENTITY_CHANGED:-0}" = "1" ] ||
        [ "$manager_bind_status" = "changed" ] ||
        [ "$manager_bind_status" = "deferred_no_ip" ]; then
        tc_log "manager pass $manager_iteration_id summary status=$manager_pass_status scheduler=$manager_scheduler_status identity=$manager_identity_status disk=$manager_disk_status disk_probe=${TC_MANAGER_DISK_PROBE_RESULT:-unknown} disk_refresh=${TC_MANAGER_DISK_REFRESH_RESULT:-unknown} payload=$manager_payload_status samba=$manager_samba_status bind=$manager_bind_status mdns=$manager_mdns_status nbns=$manager_nbns_status services=$manager_services_status duration_seconds=$manager_iteration_duration_seconds"
    fi
    tc_manager_debug_log "manager sleeping ${MANAGER_DISK_POLL_SECONDS}s after $manager_pass_status pass next_service=${manager_next_service_seconds}s next_bind=${manager_next_bind_seconds}s"
    if ! tc_manager_sleep_until_due "$MANAGER_DISK_POLL_SECONDS"; then
        break
    fi
    manager_service_seconds_until_due=$manager_next_service_seconds
    manager_bind_seconds_until_due=$manager_next_bind_seconds
done

if tc_manager_stop_requested; then
    tc_log "manager stop requested; exiting"
fi
