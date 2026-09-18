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
    if mast_raw=$(tc_read_mast); then
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

    printf 'payload\t%s\n' "$payload_dir"
    tc_manager_file_metadata_signature "$smbd_src"
    tc_manager_file_metadata_signature "$payload_dir/service"
    tc_manager_file_metadata_signature "$payload_dir/telemetry"
}

tc_manager_samba_config_signature() {
    printf '%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n' \
        "${TC_PAYLOAD_DIR:-}" \
        "${TC_PAYLOAD_VOLUME:-}" \
        "${TC_PAYLOAD_DEVICE:-}" \
        "${TC_SMB_BIND_INTERFACES:-}" \
        "${SMB_FRUIT_MODEL:-}" \
        "${SMB_NETBIOS_NAME:-}" \
        "${SMB_SERVER_STRING:-}" \
        "${ANY_PROTOCOL:-}" \
        "${VFS_AIO_FORK_ENABLED:-}" \
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
    TC_MANAGER_RUNTIME_STAGED=0
    TC_MANAGER_LAST_BINARY_SIGNATURE=
    TC_MANAGER_LAST_CONFIG_SIGNATURE=
    TC_MANAGER_LAST_RSYNC_SIGNATURE=
    TC_MANAGER_PENDING_CONFIG_SIGNATURE=
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
    if [ -z "$manager_share_rows" ]; then
        manager_share_rows=$share_row
    else
        manager_share_rows="$manager_share_rows
$share_row"
    fi
}

tc_manager_build_share_state_from_topology() {
    topology_rows=$1
    candidate_count=0
    share_count=0
    manager_share_rows=
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

tc_manager_xattr_volume_key() {
    xattr_key_device=$1
    xattr_key_uuid=$2

    if [ -n "$xattr_key_uuid" ]; then
        printf 'uuid:%s\n' "$xattr_key_uuid"
    else
        printf 'device:%s\n' "$xattr_key_device"
    fi
}

tc_manager_xattr_volume_migrated() {
    xattr_lookup_key=$1

    case "
${TC_MANAGER_XATTR_MIGRATED_VOLUMES:-}
" in
        *"
$xattr_lookup_key
"*) return 0 ;;
        *) return 1 ;;
    esac
}

tc_manager_record_migrated_xattr_volume() {
    xattr_record_key=$1

    tc_manager_xattr_volume_migrated "$xattr_record_key" && return 0
    if [ -z "${TC_MANAGER_XATTR_MIGRATED_VOLUMES:-}" ]; then
        TC_MANAGER_XATTR_MIGRATED_VOLUMES=$xattr_record_key
    else
        TC_MANAGER_XATTR_MIGRATED_VOLUMES="$TC_MANAGER_XATTR_MIGRATED_VOLUMES
$xattr_record_key"
    fi
}

tc_manager_record_migrated_xattr_volumes() {
    xattr_record_keys=$1

    while IFS= read -r xattr_record_key || [ -n "$xattr_record_key" ]; do
        [ -n "$xattr_record_key" ] || continue
        tc_manager_record_migrated_xattr_volume "$xattr_record_key" || return 1
    done <<EOF
$xattr_record_keys
EOF
}

# v3.1.0 migration checkpoint (mdns-redesign.md package 7, owner-approved):
# a small text file beside xattr.tdb records which volumes (by MaSt UUID) a
# manager finished migrating against which generation of the database, so
# a reboot does not walk every disk again just because rows for a detached
# disk remain. It is a migration checkpoint, not runtime state: it lives on
# the disk with the TDB, is absent whenever the TDB is, and the manager is
# its only writer. Format (one record per line):
#   xattr-migration-completed: format=1 migration=1 written=<epoch>
#   source: <size>-<fnv1a64 of xattr.tdb>
#   volume: uuid=<MaSt UUID>
# Anything unexpected -- other format/migration numbers, a source that does
# not match the current database, a malformed line -- discards the whole
# file and the volumes are rescanned. Rescanning repeats work; trusting a
# stale file would skip it, so the file is only ever ignored, never patched.
tc_manager_xattr_checkpoint_path() {
    printf '%s/private/%s\n' "$TC_RESOLVED_PAYLOAD_DIR" "${TC_XATTR_CHECKPOINT_NAME:-xattr-migration-completed.txt}"
}

# The migrator hashes the database; the device has no cksum/md5. Runs the
# RAM copy directly: this is a read-only sub-second call outside the
# signalled copy/cleanup wrapper, and the manager serializes both.
tc_manager_xattr_fingerprint() {
    fingerprint_tdb=$1
    fingerprint_binary="$TC_RESOLVED_PAYLOAD_DIR/xattr-hfs-migrate"
    fingerprint_ram="${TC_MANAGER_XATTR_RAM:-/mnt/Memory/tc-xattr-hfs-migrate}.fp"

    [ -f "$fingerprint_tdb" ] || return 1
    [ -x "$fingerprint_binary" ] || return 1
    /bin/cp "$fingerprint_binary" "$fingerprint_ram" || return 1
    /bin/chmod 755 "$fingerprint_ram" || { /bin/rm -f "$fingerprint_ram"; return 1; }
    fingerprint_output=$("$fingerprint_ram" fingerprint "$fingerprint_tdb" 2>/dev/null) || fingerprint_output=
    /bin/rm -f "$fingerprint_ram"
    case "$fingerprint_output" in
        fingerprint=?*)
            printf '%s\n' "${fingerprint_output#fingerprint=}"
            return 0
            ;;
    esac
    return 1
}

tc_manager_xattr_forget_completed_volumes() {
    TC_MANAGER_XATTR_MIGRATED_VOLUMES=
    TC_MANAGER_XATTR_CHECKPOINT_SOURCE=
}

# Consult the durable checkpoint once per database. Only UUID-keyed volumes
# are ever durable: a volume without a UUID could be any disk that reused
# the /dev/dkN name, so it stays process-local.
tc_manager_xattr_load_checkpoint() {
    checkpoint_tdb=$1
    checkpoint_file=$(tc_manager_xattr_checkpoint_path) || return 1

    TC_MANAGER_XATTR_CHECKPOINT_LOADED=1
    [ -f "$checkpoint_file" ] || return 0
    if [ ! -r "$checkpoint_file" ]; then
        tc_log "metadata migration checkpoint ignored: cannot read $checkpoint_file"
        return 0
    fi
    checkpoint_fingerprint=$(tc_manager_xattr_fingerprint "$checkpoint_tdb") || {
        tc_log "metadata migration checkpoint ignored: cannot fingerprint $checkpoint_tdb"
        return 0
    }
    checkpoint_line_number=0
    checkpoint_reason=
    checkpoint_volumes=
    checkpoint_volume_count=0
    while IFS= read -r checkpoint_line || [ -n "$checkpoint_line" ]; do
        checkpoint_line_number=$((checkpoint_line_number + 1))
        case "$checkpoint_line_number:$checkpoint_line" in
            "1:xattr-migration-completed: format=${TC_XATTR_CHECKPOINT_FORMAT:-1} migration=${TC_XATTR_MIGRATION_VERSION:-1} written="*)
                ;;
            1:*)
                checkpoint_reason="unsupported header"
                break
                ;;
            "2:source: $checkpoint_fingerprint")
                ;;
            "2:source: "*)
                checkpoint_reason="source mismatch recorded=${checkpoint_line#source: } current=$checkpoint_fingerprint"
                break
                ;;
            2:*)
                checkpoint_reason="missing source line"
                break
                ;;
            *":volume: uuid="?*)
                checkpoint_volume_count=$((checkpoint_volume_count + 1))
                if [ -z "$checkpoint_volumes" ]; then
                    checkpoint_volumes="uuid:${checkpoint_line#volume: uuid=}"
                else
                    checkpoint_volumes="$checkpoint_volumes
uuid:${checkpoint_line#volume: uuid=}"
                fi
                ;;
            *)
                checkpoint_reason="malformed line $checkpoint_line_number"
                break
                ;;
        esac
    done <"$checkpoint_file"
    if [ -z "$checkpoint_reason" ] && [ "$checkpoint_line_number" -lt 2 ]; then
        checkpoint_reason="truncated file"
    fi
    if [ -n "$checkpoint_reason" ]; then
        tc_log "metadata migration checkpoint ignored ($checkpoint_reason); pending volumes will be rescanned"
        return 0
    fi
    tc_manager_record_migrated_xattr_volumes "$checkpoint_volumes" || return 1
    TC_MANAGER_XATTR_CHECKPOINT_SOURCE=$checkpoint_fingerprint
    tc_log "metadata migration checkpoint loaded: volumes=$checkpoint_volume_count source=$checkpoint_fingerprint"
}

# Our own cleanup is the only expected writer, and it always ends with a new
# checkpoint carrying the new fingerprint. A database that changed without
# that is a restore or a foreign write: the completed set is no longer about
# this data, so it is dropped and the volumes are rescanned.
tc_manager_xattr_verify_source() {
    verify_tdb=$1

    [ -n "${TC_MANAGER_XATTR_CHECKPOINT_SOURCE:-}" ] || return 0
    verify_fingerprint=$(tc_manager_xattr_fingerprint "$verify_tdb") || verify_fingerprint=unavailable
    [ "$verify_fingerprint" != "$TC_MANAGER_XATTR_CHECKPOINT_SOURCE" ] || return 0
    tc_log "metadata migration source changed outside migration: recorded=$TC_MANAGER_XATTR_CHECKPOINT_SOURCE current=$verify_fingerprint; completed volumes forgotten"
    tc_manager_xattr_forget_completed_volumes
}

tc_manager_xattr_write_checkpoint() {
    write_tdb=$1
    checkpoint_file=$(tc_manager_xattr_checkpoint_path) || return 1
    checkpoint_tmp="$checkpoint_file.tmp"

    write_fingerprint=$(tc_manager_xattr_fingerprint "$write_tdb") || {
        tc_log "metadata migration checkpoint not written: cannot fingerprint $write_tdb"
        return 1
    }
    checkpoint_volume_count=0
    {
        printf 'xattr-migration-completed: format=%s migration=%s written=%s\n' \
            "${TC_XATTR_CHECKPOINT_FORMAT:-1}" "${TC_XATTR_MIGRATION_VERSION:-1}" "$(tc_now_seconds)"
        printf 'source: %s\n' "$write_fingerprint"
        while IFS= read -r checkpoint_key || [ -n "$checkpoint_key" ]; do
            case "$checkpoint_key" in
                uuid:?*)
                    checkpoint_volume_count=$((checkpoint_volume_count + 1))
                    printf 'volume: uuid=%s\n' "${checkpoint_key#uuid:}"
                    ;;
            esac
        done <<EOF
${TC_MANAGER_XATTR_MIGRATED_VOLUMES:-}
EOF
    } >"$checkpoint_tmp" || {
        tc_log "metadata migration checkpoint not written: cannot write $checkpoint_tmp"
        /bin/rm -f "$checkpoint_tmp"
        return 1
    }
    # Same-directory atomic replacement after the data is durable (sync,
    # rename, sync): a crash leaves either the old file or the new one.
    /bin/sync || { /bin/rm -f "$checkpoint_tmp"; return 1; }
    /bin/mv -f "$checkpoint_tmp" "$checkpoint_file" || {
        tc_log "metadata migration checkpoint not written: cannot replace $checkpoint_file"
        /bin/rm -f "$checkpoint_tmp"
        return 1
    }
    /bin/sync || return 1
    TC_MANAGER_XATTR_CHECKPOINT_SOURCE=$write_fingerprint
    tc_log "metadata migration checkpoint written: $checkpoint_file source=$write_fingerprint"
}

tc_manager_xattr_remove_checkpoint() {
    checkpoint_file=$(tc_manager_xattr_checkpoint_path) || return 1
    TC_MANAGER_XATTR_CHECKPOINT_SOURCE=
    [ -f "$checkpoint_file" ] || return 0
    /bin/rm -f "$checkpoint_file" || return 1
    /bin/sync || return 1
    tc_log "metadata migration checkpoint removed: legacy TDB is gone"
}

# Which block device is mounted at a root, from /sbin/mount ("/dev/dk2 on
# /Volumes/dk2 type hfs (local)"). Mount paths and dk names are attachment
# evidence, not identity, so this is checked before and after every scan.
tc_manager_volume_mount_device() {
    mount_root=$1
    /sbin/mount 2>/dev/null | while IFS= read -r mount_line || [ -n "$mount_line" ]; do
        case "$mount_line" in
            *" on $mount_root type "*)
                printf '%s\n' "${mount_line%% on *}"
                break
                ;;
        esac
    done
}

# Fresh MaSt topology for the post-scan identity check; separate so tests
# can substitute rows without acp.
tc_manager_xattr_current_topology_rows() {
    current_mast_raw=$(tc_manager_read_mast_raw) || return 1
    current_runtime_rows=$(tc_manager_parse_mast_runtime_rows "$current_mast_raw") || return 1
    tc_manager_runtime_rows_stable_signature "$current_runtime_rows"
}

tc_manager_xattr_topology_has_row() {
    wanted_device=$1
    wanted_root=$2
    wanted_uuid=$3
    while IFS="$TC_TAB" read -r disk builtin device root name uuid ||
        [ -n "$disk$builtin$device$root$name$uuid" ]; do
        [ "$device" = "$wanted_device" ] || continue
        [ "$root" = "$wanted_root" ] || continue
        [ "$uuid" = "$wanted_uuid" ] || continue
        return 0
    done <<EOF
$4
EOF
    return 1
}

# Scanned roots are recorded as "<device>\t<root>\t<uuid>" rows. A volume
# whose device or UUID differs after the scan may have been swapped mid-walk
# (a reused dk name); its completion is not recorded and it is rescanned.
tc_manager_xattr_verify_scanned_volumes() {
    scanned_rows=$1

    verified_topology_rows=$(tc_manager_xattr_current_topology_rows) || {
        tc_log "metadata migration identity check failed: MaSt unavailable after scan"
        return 1
    }
    while IFS="$TC_TAB" read -r scanned_device scanned_root scanned_uuid ||
        [ -n "$scanned_device$scanned_root$scanned_uuid" ]; do
        [ -n "$scanned_device" ] || continue
        mounted_device=$(tc_manager_volume_mount_device "$scanned_root") || mounted_device=
        if [ "$mounted_device" != "/dev/$scanned_device" ]; then
            tc_log "metadata migration identity check failed: $scanned_root is on '$mounted_device' after scan, expected /dev/$scanned_device"
            return 1
        fi
        if ! tc_manager_xattr_topology_has_row "$scanned_device" "$scanned_root" "$scanned_uuid" "$verified_topology_rows"; then
            tc_log "metadata migration identity check failed: MaSt no longer lists device=$scanned_device root=$scanned_root uuid=$scanned_uuid"
            return 1
        fi
    done <<EOF
$scanned_rows
EOF
    return 0
}

# Failed scans back off (1 min doubling to 30 min) instead of walking the
# tree again every manager pass.
tc_manager_xattr_migration_deferred() {
    TC_MANAGER_XATTR_DEFERRED=0
    [ -n "${TC_MANAGER_XATTR_RETRY_AT:-}" ] || return 1
    deferred_now=$(tc_now_seconds)
    if [ "$deferred_now" -lt "$TC_MANAGER_XATTR_RETRY_AT" ]; then
        TC_MANAGER_XATTR_DEFERRED=1
        return 0
    fi
    return 1
}

tc_manager_xattr_note_failure() {
    failure_min=${TC_XATTR_RETRY_MIN_SECONDS:-60}
    failure_max=${TC_XATTR_RETRY_MAX_SECONDS:-1800}
    if [ -z "${TC_MANAGER_XATTR_RETRY_SECONDS:-}" ]; then
        TC_MANAGER_XATTR_RETRY_SECONDS=$failure_min
    else
        TC_MANAGER_XATTR_RETRY_SECONDS=$((TC_MANAGER_XATTR_RETRY_SECONDS * 2))
        [ "$TC_MANAGER_XATTR_RETRY_SECONDS" -le "$failure_max" ] || TC_MANAGER_XATTR_RETRY_SECONDS=$failure_max
    fi
    TC_MANAGER_XATTR_RETRY_AT=$(( $(tc_now_seconds) + TC_MANAGER_XATTR_RETRY_SECONDS ))
    tc_log "metadata migration will retry in ${TC_MANAGER_XATTR_RETRY_SECONDS}s"
    return 1
}

tc_manager_xattr_note_success() {
    TC_MANAGER_XATTR_RETRY_SECONDS=
    TC_MANAGER_XATTR_RETRY_AT=
}

# Cheap change signature of the legacy TDB for the normal disk pass (review
# 2, R4): inode, size and mtime as `ls -li` prints them -- the device has no
# stat(1), and hashing 11 MB every 10 s is not an option. Any difference
# from the signature recorded at the last migration decision (including
# "absent" <-> "present") is worth one fingerprint; an mtime *ordering*
# against the checkpoint could not see a restored older file. Residual: a
# rewrite in place with the same inode, size and minute.
tc_manager_xattr_tdb_signature() {
    signature_tdb=$1
    [ -f "$signature_tdb" ] || { printf '\n'; return 0; }
    signature_line=$(/bin/ls -li "$signature_tdb" 2>/dev/null) || { printf '\n'; return 0; }
    set -- $signature_line
    printf '%s %s %s %s %s\n' "$1" "$6" "$7" "$8" "$9"
}

tc_manager_xattr_remember_signature() {
    TC_MANAGER_XATTR_TDB_SIGNATURE=$(tc_manager_xattr_tdb_signature "${TC_MANAGER_XATTR_TDB_PATH:-}")
    TC_MANAGER_XATTR_SIGNATURE_KNOWN=1
}

# The normal disk pass never reaches the migration function while every
# mounted volume is complete, so a database replaced (or introduced, or
# removed) underneath a running manager would only be noticed at the next
# start. Compare the signature every pass; on a change, fingerprint once:
# a different source forgets the completed set so the volumes are pending
# again, an identical copy is just re-recorded. No hashing on an unchanged
# database.
tc_manager_xattr_notice_source_change() {
    [ "${TC_MANAGER_XATTR_SIGNATURE_KNOWN:-0}" = 1 ] || return 0
    [ -n "${TC_MANAGER_XATTR_TDB_PATH:-}" ] || return 0
    notice_current=$(tc_manager_xattr_tdb_signature "$TC_MANAGER_XATTR_TDB_PATH")
    [ "$notice_current" != "${TC_MANAGER_XATTR_TDB_SIGNATURE:-}" ] || return 0
    if [ -z "$notice_current" ]; then
        tc_log "metadata migration: legacy TDB disappeared outside migration; nothing to migrate until one appears"
        TC_MANAGER_XATTR_CHECKPOINT_SOURCE=
    elif [ -z "${TC_MANAGER_XATTR_TDB_SIGNATURE:-}" ] || [ -z "${TC_MANAGER_XATTR_CHECKPOINT_SOURCE:-}" ]; then
        # Appeared after a no-TDB completion or a retirement, or changed
        # with no checkpoint to compare against: the completed set says
        # nothing about this database.
        tc_log "metadata migration: legacy TDB appeared or changed outside migration; completed volumes forgotten"
        tc_manager_xattr_forget_completed_volumes
        TC_MANAGER_XATTR_CHECKPOINT_LOADED=0
    else
        tc_manager_xattr_verify_source "$TC_MANAGER_XATTR_TDB_PATH" || return 0
    fi
    TC_MANAGER_XATTR_TDB_SIGNATURE=$notice_current
    return 0
}

tc_manager_pending_xattr_volume_mounted() {
    pending_topology_rows=$1

    [ "${TC_BOOT_XATTR_MIGRATION:-0}" = 1 ] || return 1
    tc_manager_xattr_migration_deferred && return 1
    tc_manager_xattr_notice_source_change
    while IFS="$TC_TAB" read -r disk builtin device root name uuid ||
        [ -n "$disk$builtin$device$root$name$uuid" ]; do
        [ -n "$device" ] || continue
        pending_xattr_key=$(tc_manager_xattr_volume_key "$device" "$uuid") || return 1
        tc_manager_xattr_volume_migrated "$pending_xattr_key" && continue
        is_volume_root_mounted "$root" && return 0
    done <<EOF
$pending_topology_rows
EOF
    return 1
}

tc_manager_stop_boot_xattrs() {
    # Deploy stops the manager before replacing payloads. Stop the dedicated
    # wrapper first so it can terminate its migrator child and remove RAM state.
    if [ -n "${TC_MANAGER_XATTR_PID:-}" ]; then
        kill -TERM "$TC_MANAGER_XATTR_PID" 2>/dev/null || true
        migration_stop_attempt=0
        while [ "$migration_stop_attempt" -lt 5 ]; do
            /bin/kill -0 "$TC_MANAGER_XATTR_PID" 2>/dev/null || break
            migration_stop_attempt=$((migration_stop_attempt + 1))
            sleep 1 || break
        done
        if /bin/kill -0 "$TC_MANAGER_XATTR_PID" 2>/dev/null; then
            tc_log "metadata migration wrapper still running after TERM; sending KILL"
            # Match only the RAM migrator (binary argv[0] or '/bin/sh RAM'
            # shebang), not a parent 'sh -c' whose command line merely
            # contains the RAM path as an argument.
            /usr/bin/pkill -KILL -f "^$TC_MANAGER_XATTR_RAM([[:space:]]|$)" >/dev/null 2>&1 || true
            /usr/bin/pkill -KILL -f "^/bin/sh $TC_MANAGER_XATTR_RAM([[:space:]]|$)" >/dev/null 2>&1 || true
            kill -KILL "$TC_MANAGER_XATTR_PID" 2>/dev/null || true
        fi
        /bin/rm -f "$TC_MANAGER_XATTR_RAM"
        TC_MANAGER_XATTR_PID=
    fi
}

tc_manager_migrate_boot_xattrs() {
    migration_topology_rows=$1

    [ "${TC_BOOT_XATTR_MIGRATION:-0}" = 1 ] || return 0
    migration_tdb_path="$TC_RESOLVED_PAYLOAD_DIR/private/xattr.tdb"
    if [ "$migration_tdb_path" != "${TC_MANAGER_XATTR_TDB_PATH:-}" ]; then
        TC_MANAGER_XATTR_TDB_PATH=$migration_tdb_path
        TC_MANAGER_XATTR_CHECKPOINT_LOADED=0
        TC_MANAGER_XATTR_SIGNATURE_KNOWN=0
        tc_manager_xattr_forget_completed_volumes
        tc_manager_xattr_note_success
    fi
    if tc_manager_xattr_migration_deferred; then
        tc_manager_debug_log "metadata migration deferred until retry time after earlier failure"
        return 1
    fi
    if [ -f "$migration_tdb_path" ]; then
        if [ "${TC_MANAGER_XATTR_CHECKPOINT_LOADED:-0}" != 1 ]; then
            tc_manager_xattr_load_checkpoint "$migration_tdb_path" || return 1
        else
            tc_manager_xattr_verify_source "$migration_tdb_path" || return 1
        fi
    fi
    tc_manager_xattr_remember_signature
    migration_volume_keys=
    migration_scanned_rows=
    set --
    while IFS="$TC_TAB" read -r disk builtin device root name uuid ||
        [ -n "$disk$builtin$device$root$name$uuid" ]; do
        [ -n "$device" ] || continue
        migration_xattr_key=$(tc_manager_xattr_volume_key "$device" "$uuid") || return 1
        tc_manager_xattr_volume_migrated "$migration_xattr_key" && continue
        if ! is_volume_root_mounted "$root"; then
            tc_log "metadata migration pending for unavailable volume: device=/dev/$device root=$root"
            continue
        fi
        migration_mounted_device=$(tc_manager_volume_mount_device "$root") || migration_mounted_device=
        if [ "$migration_mounted_device" != "/dev/$device" ]; then
            tc_log "metadata migration pending for volume with unexpected mount: root=$root mounted='$migration_mounted_device' expected=/dev/$device"
            continue
        fi
        if [ "$#" -eq 0 ]; then
            set -- "$root"
        else
            set -- "$@" "$root"
        fi
        if [ -z "$migration_volume_keys" ]; then
            migration_volume_keys=$migration_xattr_key
            migration_scanned_rows="$device$TC_TAB$root$TC_TAB$uuid"
        else
            migration_volume_keys="$migration_volume_keys
$migration_xattr_key"
            migration_scanned_rows="$migration_scanned_rows
$device$TC_TAB$root$TC_TAB$uuid"
        fi
    done <<EOF
$migration_topology_rows
EOF
    if [ "$#" -eq 0 ]; then
        tc_log "metadata migration skipped: no mounted pending roots"
        return 0
    fi

    tc_log "metadata migration selected roots count=$# roots=$*"

    migration_tdb="$TC_RESOLVED_PAYLOAD_DIR/private/xattr.tdb"
    migration_binary="$TC_RESOLVED_PAYLOAD_DIR/xattr-hfs-migrate"
    migration_wrapper=/mnt/Flash/migrate.sh
    if [ ! -f "$migration_tdb" ]; then
        tc_log "metadata migration skipped: no legacy TDB at $migration_tdb"
        # These volumes have nothing to migrate. Record them as done, or
        # tc_manager_pending_xattr_volume_mounted would report them as newly
        # available on every pass and the manager would restart mDNS each time.
        tc_manager_record_migrated_xattr_volumes "$migration_volume_keys" || return 1
        tc_manager_xattr_remember_signature
        return 0
    fi
    migration_metadata=stream
    [ "$FRUIT_METADATA_NETATALK" != 1 ] || migration_metadata=netatalk
    TC_MANAGER_XATTR_RAM=/mnt/Memory/tc-xattr-hfs-migrate
    rm -f "$TC_MANAGER_XATTR_RAM"
    tc_log "boot metadata migration beginning phase=copy metadata=$migration_metadata tdb=$migration_tdb roots=$*"
    migration_status=0
    TC_MANAGER_XATTR_STATUS=127
    # Symbolic names: USR1/USR2 are 30/31 on NetBSD/macOS but 10/12 on Linux.
    trap 'TC_MANAGER_XATTR_STATUS=0' USR1
    trap 'TC_MANAGER_XATTR_STATUS=1' USR2
    "$migration_wrapper" copy "$migration_tdb" "$migration_metadata" \
        "$migration_binary" "$TC_MANAGER_XATTR_RAM" "$$" "$TC_LOG_FILE" "$@" &
    TC_MANAGER_XATTR_PID=$!
    while /bin/kill -0 "$TC_MANAGER_XATTR_PID" 2>/dev/null; do
        sleep 1 || break
    done
    TC_MANAGER_XATTR_PID=
    migration_signal_wait=0
    while [ "$TC_MANAGER_XATTR_STATUS" = 127 ] && [ "$migration_signal_wait" -lt 10000 ]; do
        migration_signal_wait=$((migration_signal_wait + 1))
    done
    migration_status=$TC_MANAGER_XATTR_STATUS
    trap - USR1 USR2
    rm -f "$TC_MANAGER_XATTR_RAM"
    tc_log "metadata migration export finished status=$migration_status"
    [ "$migration_status" = 0 ] || { tc_manager_xattr_note_failure || return 1; }
    if /bin/sync; then
        tc_log "boot metadata migration copy sync finished status=0"
    else
        migration_sync_status=$?
        tc_log "boot metadata migration copy sync failed status=$migration_sync_status"
        tc_manager_xattr_note_failure || return 1
    fi

    tc_log "boot metadata migration beginning phase=cleanup metadata=$migration_metadata tdb=$migration_tdb roots=$*"
    migration_status=0
    TC_MANAGER_XATTR_STATUS=127
    # Symbolic names: USR1/USR2 are 30/31 on NetBSD/macOS but 10/12 on Linux.
    trap 'TC_MANAGER_XATTR_STATUS=0' USR1
    trap 'TC_MANAGER_XATTR_STATUS=1' USR2
    "$migration_wrapper" cleanup "$migration_tdb" "$migration_metadata" \
        "$migration_binary" "$TC_MANAGER_XATTR_RAM" "$$" "$TC_LOG_FILE" "$@" &
    TC_MANAGER_XATTR_PID=$!
    while /bin/kill -0 "$TC_MANAGER_XATTR_PID" 2>/dev/null; do
        sleep 1 || break
    done
    TC_MANAGER_XATTR_PID=
    migration_signal_wait=0
    while [ "$TC_MANAGER_XATTR_STATUS" = 127 ] && [ "$migration_signal_wait" -lt 10000 ]; do
        migration_signal_wait=$((migration_signal_wait + 1))
    done
    migration_status=$TC_MANAGER_XATTR_STATUS
    trap - USR1 USR2
    rm -f "$TC_MANAGER_XATTR_RAM"
    tc_log "metadata migration cleanup finished status=$migration_status"
    [ "$migration_status" = 0 ] || { tc_manager_xattr_note_failure || return 1; }
    if /bin/sync; then
        tc_log "boot metadata migration cleanup sync finished status=0"
    else
        migration_sync_status=$?
        tc_log "boot metadata migration cleanup sync failed status=$migration_sync_status"
        tc_manager_xattr_note_failure || return 1
    fi
    # The walk proved what it proved only for the disks that were there the
    # whole time. Anything swapped underneath it is rescanned later.
    tc_manager_xattr_verify_scanned_volumes "$migration_scanned_rows" || {
        tc_manager_xattr_note_failure || return 1
    }
    tc_log "boot metadata migration complete status=0"
    tc_manager_record_migrated_xattr_volumes "$migration_volume_keys" || return 1
    tc_manager_xattr_note_success
    if [ ! -f "$migration_tdb" ]; then
        # Every row was retired, or every remaining row was a proven orphan
        # and the migrator set the closed database aside as
        # xattr.tdb.orphaned.N. Nothing is left to checkpoint against.
        tc_log "metadata migration retired the legacy TDB: $migration_tdb"
        tc_manager_xattr_remove_checkpoint || return 1
        tc_manager_xattr_remember_signature
        return 0
    fi
    # Rows remain for volumes that were not there (unresolved) -- the
    # migrator's cleanup line in this log gives the honest split. A failed
    # checkpoint write only costs a rescan on the next manager start.
    tc_manager_xattr_write_checkpoint "$migration_tdb" || tc_log "metadata migration completed without a durable checkpoint"
    tc_manager_xattr_remember_signature
    return 0
}

tc_manager_apply_runtime_from_topology() {
    refresh_reason=$1
    topology_rows=$2
    refresh_start_seconds=$(tc_now_seconds)
    previous_manager_topology_rows=${manager_topology_rows:-}
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

    if ! tc_manager_migrate_boot_xattrs "$topology_rows"; then
        if [ "${TC_MANAGER_XATTR_DEFERRED:-0}" = 1 ]; then
            # Same outcome as the failure below, without repeating its log
            # line every pass while the backoff runs.
            tc_manager_debug_log "metadata migration retry pending; runtime state unchanged"
            if [ "$refresh_reason" = initial ] || ! tc_manager_current_payload_ready; then
                tc_manager_clear_payload_state
            else
                manager_topology_rows=$previous_manager_topology_rows
            fi
            return 1
        fi
        if [ "$refresh_reason" = initial ] || ! tc_manager_current_payload_ready; then
            tc_log "metadata migration failed; retaining pending metadata and withholding initial Samba startup"
            tc_manager_clear_payload_state
        else
            manager_topology_rows=$previous_manager_topology_rows
            tc_log "metadata migration failed for changed topology; preserving the active Samba shares and retrying later"
        fi
        return 1
    fi

    if ! tc_manager_build_share_state_from_topology "$topology_rows"; then
        tc_log "manager disk refresh: no writable MaSt share volumes are available; applying no-payload state"
        tc_manager_apply_diskless_state "$refresh_reason"
        return 0
    fi

    tc_log "manager disk refresh: applying ATA drive settings after share-state build"
    tc_manager_configure_ata_from_topology "$topology_rows"

    tc_manager_set_payload_state
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
        if tc_manager_pending_xattr_volume_mounted "$current_stable_signature"; then
            TC_MANAGER_DISK_PROBE_RESULT=migration_volume_available
            TC_MANAGER_DISK_REFRESH_RESULT=refresh_migration_volume
            tc_manager_apply_runtime_from_topology migration_volume_available "$current_stable_signature" || return 1
            tc_log "manager disk refresh completed for newly available metadata migration volume"
            return 0
        fi
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

}

tc_manager_samba_runtime_files_missing() {
    [ -x "$TC_SMBD_BIN" ] || return 0
    [ -x "$TC_SERVICE_BIN" ] || return 0
    [ -x "$TC_TELEMETRY_BIN" ] || return 0
    [ -f "$RAM_PRIVATE/smbpasswd" ] || return 0
    [ -f "$RAM_PRIVATE/username.map" ] || return 0
    return 1
}

tc_manager_backup_log_for_runtime_reset() {
    TC_MANAGER_RESET_LOG_BACKUP=

    if [ ! -f "$TC_LOG_FILE" ]; then
        return 0
    fi

    TC_MANAGER_RESET_LOG_BACKUP="$RAM_ROOT.manager.log.$$"
    rm -f "$TC_MANAGER_RESET_LOG_BACKUP" >/dev/null 2>&1 || true
    if cp "$TC_LOG_FILE" "$TC_MANAGER_RESET_LOG_BACKUP" >/dev/null 2>&1; then
        return 0
    fi

    TC_MANAGER_RESET_LOG_BACKUP=
    return 0
}

tc_manager_restore_log_after_runtime_reset() {
    if [ -z "${TC_MANAGER_RESET_LOG_BACKUP:-}" ]; then
        return 0
    fi
    if [ ! -f "$TC_MANAGER_RESET_LOG_BACKUP" ]; then
        TC_MANAGER_RESET_LOG_BACKUP=
        return 0
    fi

    tc_ensure_parent_dir "$TC_LOG_FILE"
    cp "$TC_MANAGER_RESET_LOG_BACKUP" "$TC_LOG_FILE" >/dev/null 2>&1 || true
    rm -f "$TC_MANAGER_RESET_LOG_BACKUP" >/dev/null 2>&1 || true
    TC_MANAGER_RESET_LOG_BACKUP=
}

tc_manager_reset_samba_runtime_after_stage_failure() {
    reset_status=0

    tc_prepare_telemetry_reset || return $?
    TC_MANAGER_TELEMETRY_PID=
    tc_log "manager Samba staging recovery: resetting RAM runtime after staging failure"
    if runtime_process_present_by_ucomm smbd; then
        tc_log "manager Samba staging recovery: stopping smbd before RAM runtime reset"
        stop_runtime_process_by_ucomm "smbd" smbd || reset_status=1
    fi
    if runtime_process_present_by_ucomm "$DISCOVERY_PROC_NAME" ||
        runtime_process_present_by_ucomm wcifsnd ||
        runtime_process_present_by_ucomm wcifsfs; then
        tc_log "manager Samba staging recovery: stopping discovery generation before RAM runtime reset"
        if runtime_process_present_by_ucomm "$DISCOVERY_PROC_NAME"; then
            stop_runtime_process_by_ucomm "$DISCOVERY_PROC_NAME" "$DISCOVERY_PROC_NAME" || reset_status=1
        fi
        stop_discovery_conflicts || reset_status=1
        TC_MANAGER_LAST_DISCOVERY_SIGNATURE=
    fi
    if runtime_process_present_by_ucomm "$RSYNC_PROC_NAME"; then
        tc_log "manager Samba staging recovery: stopping rsync before RAM runtime reset"
        stop_runtime_process_by_ucomm "$RSYNC_PROC_NAME" "$RSYNC_PROC_NAME" || reset_status=1
    fi

    if [ "$reset_status" -ne 0 ]; then
        tc_log "manager Samba staging recovery: process cleanup failed; refusing to delete $RAM_ROOT"
        return 1
    fi

    # manager.log lives under RAM_ROOT, so preserve it around the reset long
    # enough for doctor to report the staging failure that triggered recovery.
    tc_manager_backup_log_for_runtime_reset
    rm -rf "$RAM_ROOT" || reset_status=1
    tc_prepare_ram_root || reset_status=1
    tc_manager_restore_log_after_runtime_reset

    if [ "$reset_status" -ne 0 ]; then
        tc_log "manager Samba staging recovery: RAM runtime reset failed"
        return 1
    fi

    TC_MANAGER_RUNTIME_STAGED=0
    TC_MANAGER_LAST_BINARY_SIGNATURE=
    TC_MANAGER_LAST_CONFIG_SIGNATURE=
    TC_MANAGER_LAST_RSYNC_SIGNATURE=
    TC_MANAGER_PENDING_CONFIG_SIGNATURE=
    TC_MANAGER_SMBD_RESTART_REQUIRED=0
    TC_MANAGER_SMBD_RELOAD_REQUIRED=0
    TC_MANAGER_SMBD_APPLY_FAILURE=
    tc_log "manager Samba staging recovery: RAM runtime reset complete"
}

tc_manager_stage_samba_runtime_files_if_needed() {
    if ! tc_manager_select_samba_sources; then
        return 1
    fi

    fresh_binary_signature=$(tc_manager_samba_file_signature "$manager_payload_dir" "$manager_smbd_src")
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
    if tc_stage_runtime "$manager_payload_dir" "$manager_smbd_src"; then
        :
    else
        stage_status=$?
        tc_log "manager Samba runtime file staging failed status=$stage_status; resetting RAM runtime before next manager pass"
        if ! tc_manager_reset_samba_runtime_after_stage_failure; then
            return "$stage_status"
        fi
        tc_log "manager Samba runtime file staging will retry on next manager pass after RAM runtime reset"
        return "$stage_status"
    fi
    if [ "$manager_binary_changed" -eq 1 ] && [ -n "${TC_MANAGER_TELEMETRY_PID:-}" ]; then
        # TERM stops scheduling without killing an active signed debug job.
        kill -TERM "$TC_MANAGER_TELEMETRY_PID" 2>/dev/null || true
        TC_MANAGER_TELEMETRY_PID=
    fi
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
    if [ "$fresh_config_signature" = "${TC_MANAGER_LAST_CONFIG_SIGNATURE:-}" ] &&
        [ -z "${TC_MANAGER_PENDING_CONFIG_SIGNATURE:-}" ] && [ -f "$TC_SMBD_CONF" ]; then
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
        TC_MANAGER_LAST_VALIDATED_BIND_TOKENS=$TC_SMB_BIND_INTERFACES
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

# Guide B.9: the manager owns the last validated Samba bind projection as
# process-local shell state. `validated`
# run: compare with the last validated tokens and reconfigure on change.
# `incomplete`: retain permission only on unchanged interfaces, using the
# native helper's filtered projection; log age since the last validated run.
tc_manager_reconcile_smb_bind_interfaces() {
    TC_MANAGER_SMB_BIND_CHANGED=0
    TC_MANAGER_SMB_BIND_DEFERRED=0
    TC_MANAGER_SMB_BIND_PREVIOUS=

    if ! tc_probe_smb_bind_interfaces; then
        tc_log "manager Samba: bind probe failed; keeping current bind projection"
        return 1
    fi
    case "$TC_SMB_BIND_STATUS" in
        validated)
            TC_MANAGER_BIND_POLICY=$TC_SMB_BIND_POLICY
            TC_MANAGER_LAST_VALIDATED_BIND_TIME=$(tc_now_seconds)
            ;;
        incomplete)
            TC_MANAGER_SMB_BIND_DEFERRED=1
            if [ -n "${TC_MANAGER_LAST_VALIDATED_BIND_TOKENS:-}" ]; then
                bind_age=$(tc_elapsed_seconds_since "${TC_MANAGER_LAST_VALIDATED_BIND_TIME:-0}")
                tc_log "Samba bind: keeping last validated projection (age=${bind_age}s reason=${TC_SMB_BIND_REASON:-unknown})"
            else
                tc_log "Samba bind: no validated projection yet (reason=${TC_SMB_BIND_REASON:-unknown})"
            fi
            # Native policy retention filters out new/recreated interfaces.
            # Without readable kernel ownership, keep the existing sockets.
            [ -n "${TC_MANAGER_LAST_VALIDATED_BIND_TOKENS:-}" ] || return 0
            case "$TC_SMB_BIND_REASON" in iflist|iflist-truncated|addrs) return 0 ;; esac
            TC_MANAGER_BIND_POLICY=$TC_SMB_BIND_POLICY
            ;;
        *)
            tc_log "manager Samba: bind probe returned unknown status '$TC_SMB_BIND_STATUS'"
            return 1
            ;;
    esac

    fresh_bind_interfaces=$TC_SMB_BIND_PROBE_TOKENS
    if [ -z "${TC_MANAGER_LAST_VALIDATED_BIND_TOKENS:-}" ]; then
        TC_MANAGER_LAST_VALIDATED_BIND_TOKENS=$fresh_bind_interfaces
        TC_MANAGER_SMB_BIND_PREVIOUS=
        TC_SMB_BIND_INTERFACES=$fresh_bind_interfaces
        TC_MANAGER_SMB_BIND_CHANGED=1
        tc_log "manager Samba: initialized bind interfaces: $TC_SMB_BIND_INTERFACES"
        return 0
    fi
    if [ "$fresh_bind_interfaces" = "$TC_MANAGER_LAST_VALIDATED_BIND_TOKENS" ]; then
        return 0
    fi

    old_bind_interfaces=$TC_MANAGER_LAST_VALIDATED_BIND_TOKENS
    TC_MANAGER_LAST_VALIDATED_BIND_TOKENS=$fresh_bind_interfaces
    TC_SMB_BIND_INTERFACES=$fresh_bind_interfaces
    TC_MANAGER_SMB_BIND_PREVIOUS=$old_bind_interfaces
    TC_MANAGER_SMB_BIND_CHANGED=1
    tc_log "manager Samba: bind interfaces changed: $old_bind_interfaces -> $TC_SMB_BIND_INTERFACES"
    if ! tc_manager_validate_smbd_runtime_state; then
        TC_MANAGER_LAST_VALIDATED_BIND_TOKENS=$old_bind_interfaces
        TC_SMB_BIND_INTERFACES=$old_bind_interfaces
        TC_MANAGER_SMB_BIND_CHANGED=0
        TC_MANAGER_SMB_BIND_PREVIOUS=
        tc_log "manager Samba: cannot apply bind change; disk runtime validation failed"
        return 1
    fi
    return 0
}

tc_manager_reconcile_smbd() {
    if ! tc_manager_apply_smbd_runtime_changes; then
        tc_log "manager Samba: smbd runtime apply failed reason=${TC_MANAGER_SMBD_APPLY_FAILURE:-unknown}; will retry on next reconciliation pass"
        return 1
    fi
}

tc_manager_launch_discovery() {
    context=$1
    kill_prior=$2
    wait_attempts=$3
    diskless=$4

    tc_launch_discovery "$context" "$kill_prior" "$wait_attempts" "$diskless" "${MDNS_DEBUG_LOGGING:-0}" "${manager_share_rows:-}"
}

tc_manager_launch_current_discovery() {
    context=$1
    wait_attempts=$2

    if tc_manager_current_payload_ready; then
        tc_manager_launch_discovery "$context" 1 "$wait_attempts" 0
    else
        tc_manager_launch_discovery "$context" 1 "$wait_attempts" 1
    fi
}


# Only argv changes need a restart: ACP naming/link changes are handled by
# the native registrant. Keep failed launches pending across manager passes.
tc_manager_reconcile_discovery() {
    discovery_signature=$(printf '%s\n%s\n%s\n%s\n%s\n' "${manager_share_rows:-}" "${manager_payload_ready:-0}" "${SMB_NETBIOS_NAME:-}" "$TC_ADISK_DISK_ADVF" "${MDNS_DEBUG_LOGGING:-0}")
    if runtime_process_present_by_ucomm wcifsfs; then
        tc_log "manager discovery recovery: wcifsfs returned; replacing native registration generation"
        stop_runtime_process_by_ucomm wcifsfs wcifsfs || return 1
        stop_runtime_process_by_ucomm "$DISCOVERY_PROC_NAME" "$DISCOVERY_PROC_NAME" || return 1
        stop_discovery_conflicts || return 1
    elif runtime_process_present_by_ucomm "$DISCOVERY_PROC_NAME" &&
        [ "$discovery_signature" = "${TC_MANAGER_LAST_DISCOVERY_SIGNATURE:-}" ]; then
        return 0
    fi
    tc_manager_launch_current_discovery "manager discovery recovery" 10 || return 1
    TC_MANAGER_LAST_DISCOVERY_SIGNATURE=$discovery_signature
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
        manager_bind_status=retained
    elif [ "${TC_MANAGER_SMB_BIND_CHANGED:-0}" = "1" ]; then
        manager_bind_status=changed
    else
        manager_bind_status=ok
    fi
}

tc_manager_reconcile_discovery_ownership() {
    if runtime_process_present_by_ucomm wcifsfs; then
        tc_log "manager discovery ownership: wcifsfs returned; resetting discovery generation"
        if runtime_process_present_by_ucomm "$DISCOVERY_PROC_NAME"; then
            stop_runtime_process_by_ucomm "$DISCOVERY_PROC_NAME" "$DISCOVERY_PROC_NAME" || return 1
        fi
        stop_discovery_conflicts || return 1
        TC_MANAGER_LAST_DISCOVERY_SIGNATURE=
        manager_service_seconds_until_due=0
        return 0
    fi

    if ! runtime_process_present_by_ucomm "$DISCOVERY_PROC_NAME"; then
        if runtime_process_present_by_ucomm wcifsnd; then
            tc_log "manager discovery ownership: stopping orphaned wcifsnd"
            stop_runtime_process_by_ucomm wcifsnd wcifsnd || return 1
        fi
        TC_MANAGER_LAST_DISCOVERY_SIGNATURE=
        manager_service_seconds_until_due=0
    fi
}

tc_manager_run_disk_step() {
    manager_step_start_seconds=$(tc_now_seconds)
    tc_manager_debug_log "manager pass $manager_iteration_id step=disk start"
    # diskd serves MaSt: with it dead the topology reads as empty and the
    # disk refresh would tear Samba down (seen on the NetBSD 4 device), so
    # put diskd back before reading the topology, not after.
    tc_manager_reconcile_diskd
    if ! tc_manager_reconcile_discovery_ownership; then
        manager_status=1
        manager_disk_status=failed
        tc_manager_log_step_end "$manager_iteration_id" disk "$manager_step_start_seconds" failed
        return 1
    fi
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
    # Pending apply state survives failed passes until commit succeeds.

    if ! tc_manager_stage_samba_runtime_files_if_needed; then
        manager_step_status=1
    fi
    if [ "$manager_step_status" -eq 0 ]; then
        tc_init_runtime_identity || manager_step_status=1
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
    if [ "$manager_step_status" -eq 0 ] && [ -z "${TC_SMB_BIND_INTERFACES:-}" ]; then
        # No validated bind projection yet (every probe so far was incomplete):
        # smb.conf cannot be rendered without interfaces, so wait for the next
        # pass instead of failing the step (B.9).
        tc_log "manager Samba: waiting for a validated bind projection before configuring smbd"
        manager_samba_status=waiting_bind
        tc_manager_log_step_end "$manager_iteration_id" samba "$manager_step_start_seconds" skipped
        return 0
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
    # Pending apply state survives failed passes until commit succeeds.

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
        if [ "${TC_MANAGER_SMB_BIND_CHANGED:-0}" = "1" ] ||
            [ "${TC_MANAGER_SMBD_RESTART_REQUIRED:-0}" = "1" ] ||
            [ "${TC_MANAGER_SMBD_RELOAD_REQUIRED:-0}" = "1" ] ||
            [ -n "${TC_MANAGER_PENDING_CONFIG_SIGNATURE:-}" ]; then
            # A failed apply is retried even when today's tokens are unchanged.
            if [ "${TC_MANAGER_SMB_BIND_CHANGED:-0}" = "1" ]; then
                TC_MANAGER_SMBD_RESTART_REQUIRED=1
            fi
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
    if tc_manager_stop_samba_lane_without_payload; then
        manager_samba_status=no_payload
        tc_manager_log_step_end "$manager_iteration_id" no_payload "$manager_step_start_seconds" ok
        return 0
    fi

    manager_status=1
    manager_samba_status=failed
    tc_manager_log_step_end "$manager_iteration_id" no_payload "$manager_step_start_seconds" failed
    return 1
}


tc_manager_run_rsync_step() {
    manager_step_start_seconds=$(tc_now_seconds)
    tc_manager_debug_log "manager pass $manager_iteration_id step=rsync start"
    if tc_manager_reconcile_rsync; then
        if tc_rsync_enabled; then
            manager_rsync_status=ok
        else
            manager_rsync_status=disabled
        fi
        tc_manager_log_step_end "$manager_iteration_id" rsync "$manager_step_start_seconds" ok
        return 0
    fi

    manager_status=1
    manager_rsync_status=failed
    tc_manager_log_step_end "$manager_iteration_id" rsync "$manager_step_start_seconds" failed
    return 1
}

# Guide B.8 failure contract: the boot relaunch is best effort, so while any
# diskd that is not on loopback exists (ACPd's original survived, or came
# back) Apple's _afpovertcp/_smb/_adisk can be on the LAN. Retry the relaunch
# at the start of every disk pass, with a 5-minute hold after a failed
# attempt so a diskd that will not come up cannot block every pass for
# 30 s. Doctor's loopback check is the gate that reports the degraded state.
tc_manager_reconcile_diskd() {
    diskd_state=$(tc_apple_diskd_state)
    if [ "$diskd_state" = "loopback" ]; then
        TC_MANAGER_DISKD_RETRY_AT=
        return 0
    fi
    diskd_now=$(tc_now_seconds)
    if [ -n "${TC_MANAGER_DISKD_RETRY_AT:-}" ] && [ "$diskd_now" -lt "$TC_MANAGER_DISKD_RETRY_AT" ]; then
        tc_manager_debug_log "manager diskd: state=$diskd_state; relaunch retry deferred"
        return 0
    fi
    tc_log "manager diskd: state=$diskd_state; Apple's SMB/AFP names may be on the LAN, retrying the loopback relaunch"
    if tc_relaunch_diskd_loopback; then
        TC_MANAGER_DISKD_RETRY_AT=
        return 0
    fi
    TC_MANAGER_DISKD_RETRY_AT=$((diskd_now + ${TC_DISKD_RETRY_SECONDS:-300}))
    tc_log "manager diskd: relaunch failed; next attempt in ${TC_DISKD_RETRY_SECONDS:-300}s (doctor reports the degraded state)"
    return 0
}

tc_manager_run_discovery_step() {
    manager_step_start_seconds=$(tc_now_seconds)
    tc_manager_debug_log "manager pass $manager_iteration_id step=discovery start"
    tc_manager_debug_log "manager discovery: reconciling controller"
    # ACPd starts afpserver after boot.sh has already run (observed on the
    # NetBSD 4 device: pid order rc.local < mDNSResponder < smbd < afpserver),
    # so the boot-time stop is not enough; re-check on every service pass.
    tc_stop_apple_afpserver || tc_log "manager discovery: Apple afpserver could not be stopped; AFP port 548 stays open"
    if tc_manager_reconcile_discovery; then
        manager_discovery_status=ok
        tc_manager_log_step_end "$manager_iteration_id" discovery "$manager_step_start_seconds" ok
        return 0
    fi

    manager_status=1
    manager_discovery_status=failed
    tc_manager_log_step_end "$manager_iteration_id" discovery "$manager_step_start_seconds" failed
    return 1
}



tc_manager_run_full_service_steps() {
    service_step_status=0

    tc_prepare_local_hostname_resolution || service_step_status=1
    tc_manager_update_payload_status
    if [ "$manager_payload_expected" -eq 1 ]; then
        tc_manager_run_samba_full_step || service_step_status=1
    else
        tc_manager_run_no_payload_step || service_step_status=1
        manager_rsync_status=no_payload
    fi
    if [ "$manager_payload_expected" -eq 0 ] || [ "$manager_samba_status" = ok ]; then
        tc_manager_run_discovery_step || service_step_status=1
    else
        manager_discovery_status=retained
        tc_log "manager discovery: retaining current generation until Samba configuration succeeds"
    fi
    if [ "$manager_payload_expected" -eq 1 ]; then
        tc_manager_run_rsync_step || service_step_status=1
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
MANAGER_TOPOLOGY_DEBOUNCE_SECONDS=$(tc_sanitize_positive_integer "${MANAGER_TOPOLOGY_DEBOUNCE_SECONDS:-5}" 5)
MANAGER_STOP_POLL_SECONDS=$(tc_sanitize_positive_integer "${MANAGER_STOP_POLL_SECONDS:-1}" 1)
TC_MANAGER_STOP_REQUESTED=0
TC_MANAGER_ITERATION=0
TC_MANAGER_RUNTIME_STAGED=0
TC_RUNTIME_IDENTITY_READY=0
TC_MANAGER_LAST_BINARY_SIGNATURE=
TC_MANAGER_LAST_CONFIG_SIGNATURE=
TC_MANAGER_LAST_RSYNC_SIGNATURE=
TC_MANAGER_PENDING_CONFIG_SIGNATURE=
TC_MANAGER_SMBD_RESTART_REQUIRED=0
TC_MANAGER_SMBD_RELOAD_REQUIRED=0
TC_MANAGER_SMBD_APPLY_FAILURE=
TC_MANAGER_SMB_BIND_PREVIOUS=
# Environment strings are not validated history. A restarted manager waits
# before starting/reconfiguring smbd; an already running smbd stays alone.
TC_SMB_BIND_INTERFACES=
TC_MANAGER_LAST_VALIDATED_BIND_TOKENS=
TC_MANAGER_LAST_VALIDATED_BIND_TIME=0
TC_MANAGER_BIND_POLICY=
TC_MANAGER_MAST_CONFIRMED_STABLE_SIGNATURE=
TC_MANAGER_MAST_CONFIRMED_STABLE_SIGNATURE_READY=0
TC_MANAGER_XATTR_TDB_PATH=
TC_MANAGER_XATTR_MIGRATED_VOLUMES=
manager_payload_ready=0
manager_payload_dir=
manager_payload_volume=
manager_payload_device=
manager_smbd_src=
manager_topology_rows=
manager_share_rows=
manager_service_seconds_until_due=0
manager_bind_seconds_until_due=0
tc_manager_clear_payload_state

tc_log "manager startup beginning"
tc_log "manager intervals: disk=${MANAGER_DISK_POLL_SECONDS}s bind=${MANAGER_BIND_POLL_SECONDS}s services=${MANAGER_SERVICE_POLL_SECONDS}s mast_retry=${MANAGER_MAST_RETRY_SECONDS}s topology_debounce=${MANAGER_TOPOLOGY_DEBOUNCE_SECONDS}s stop_poll=${MANAGER_STOP_POLL_SECONDS}s"

# Keep the scheduler PID in this shell, not a stale runtime marker. The helper
# owns its debug child and RAM files; TERM stops future cycles but lets an
# already running signed debug program finish and clean up.
TC_MANAGER_TELEMETRY_PID=
tc_prepare_telemetry_reset || exit $?
tc_manager_stop_telemetry() {
    if [ -n "$TC_MANAGER_TELEMETRY_PID" ]; then
        kill -TERM "$TC_MANAGER_TELEMETRY_PID" 2>/dev/null || true
    fi
}
trap 'TC_MANAGER_STOP_REQUESTED=1; tc_manager_stop_boot_xattrs; tc_manager_stop_telemetry' TERM INT
trap 'tc_manager_stop_boot_xattrs; tc_manager_stop_telemetry' EXIT

while ! tc_manager_stop_requested; do
    if [ -x "$TC_TELEMETRY_BIN" ] &&
        { [ -z "$TC_MANAGER_TELEMETRY_PID" ] || ! kill -0 "$TC_MANAGER_TELEMETRY_PID" 2>/dev/null; }; then
        "$TC_TELEMETRY_BIN" --daemon </dev/null >/dev/null 2>&1 &
        TC_MANAGER_TELEMETRY_PID=$!
    fi
    TC_MANAGER_ITERATION=$((TC_MANAGER_ITERATION + 1))
    manager_iteration_id=$TC_MANAGER_ITERATION
    manager_iteration_start_seconds=$(tc_now_seconds)
    manager_status=0
    manager_payload_expected=0
    manager_disk_status=skipped
    manager_payload_status=skipped
    manager_samba_status=skipped
    manager_bind_status=skipped
    manager_discovery_status=skipped
    manager_rsync_status=skipped
    manager_services_status=skipped
    manager_scheduler_status=disk_only
    TC_MANAGER_DISK_STATE_CHANGED=0
    manager_services_due=0
    manager_bind_due=0

    if [ "$manager_service_seconds_until_due" -le 0 ]; then
        manager_services_due=1
    fi
    if [ "$manager_bind_seconds_until_due" -le 0 ]; then
        manager_bind_due=1
    fi

    tc_manager_debug_log "manager pass $manager_iteration_id start"

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
        [ "$manager_bind_status" = "changed" ] ||
        [ "$manager_bind_status" = "retained" ]; then
        tc_log "manager pass $manager_iteration_id summary status=$manager_pass_status scheduler=$manager_scheduler_status disk=$manager_disk_status disk_probe=${TC_MANAGER_DISK_PROBE_RESULT:-unknown} disk_refresh=${TC_MANAGER_DISK_REFRESH_RESULT:-unknown} payload=$manager_payload_status samba=$manager_samba_status bind=$manager_bind_status rsync=$manager_rsync_status discovery=$manager_discovery_status services=$manager_services_status duration_seconds=$manager_iteration_duration_seconds"
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
