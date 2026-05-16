is_volume_root_mounted() {
    volume_root=$1
    df_line=$(/bin/df -k "$volume_root" 2>/dev/null | /usr/bin/tail -n +2 || true)
    case "$df_line" in
        *" $volume_root") return 0 ;;
    esac
    return 1
}

# Disk mount policy helpers. Boot and watchdog share the low-level
# Apple-first flow, but keep separate entry points so their timing can diverge.
tc_request_diskd_use_volume() {
    volume_root=$1
    mount_context=$2
    attempt_label=${3:-}

    mkdir -p "$volume_root"
    if [ -n "$attempt_label" ]; then
        tc_log "$mount_context: requesting diskd.useVolume for $volume_root ($attempt_label)"
    else
        tc_log "$mount_context: requesting diskd.useVolume for $volume_root"
    fi
    if /usr/bin/acp rpc diskd.useVolume path:s:"$volume_root" >/dev/null 2>&1; then
        tc_log "$mount_context: diskd.useVolume command completed for $volume_root"
        return 0
    fi
    tc_log "$mount_context: diskd.useVolume command failed for $volume_root"
    return 1
}

tc_wait_for_diskd_volume_mount() {
    volume_root=$1
    mount_context=$2

    tc_log_runtime_env_warnings
    mount_timeout_seconds=${TC_DISKD_USE_VOLUME_MOUNT_TIMEOUT_SECONDS:-31}
    mount_poll_seconds=${TC_DISKD_USE_VOLUME_MOUNT_POLL_SECONDS:-3}

    tc_log "$mount_context: waiting up to ${mount_timeout_seconds}s for diskd.useVolume to mount $volume_root"
    elapsed=0
    while :; do
        if is_volume_root_mounted "$volume_root"; then
            tc_log "$mount_context: $volume_root is mounted after ${elapsed}s"
            return 0
        fi
        if [ "$elapsed" -ge "$mount_timeout_seconds" ]; then
            tc_log "$mount_context: timed out after ${elapsed}s waiting for $volume_root to mount"
            return 1
        fi

        remaining=$((mount_timeout_seconds - elapsed))
        sleep_seconds=$mount_poll_seconds
        if [ "$sleep_seconds" -gt "$remaining" ]; then
            sleep_seconds=$remaining
        fi
        if [ "$sleep_seconds" -le 0 ]; then
            sleep_seconds=1
        fi
        sleep "$sleep_seconds"
        elapsed=$((elapsed + sleep_seconds))
    done
}

tc_wake_or_mount_volume_with_policy() {
    device_path=$1
    volume_root=$2
    diskd_attempts=$3
    mount_context=${4:-MaSt volume $2}

    if [ -z "$device_path" ] || [ -z "$volume_root" ]; then
        tc_log "$mount_context: diskd activation skipped; missing device or volume root"
        return 1
    fi

    sanitized_diskd_attempts=$(tc_sanitize_positive_integer "$diskd_attempts" 2)
    if [ "$sanitized_diskd_attempts" != "$diskd_attempts" ]; then
        tc_log "$mount_context: invalid diskd attempt count '$diskd_attempts'; using 2 attempts"
    fi
    diskd_attempts=$sanitized_diskd_attempts

    mkdir -p "$volume_root"
    was_mounted=0
    if is_volume_root_mounted "$volume_root"; then
        was_mounted=1
        tc_log "$mount_context: volume already mounted at $volume_root before diskd.useVolume; claiming a diskd user anyway"
    else
        tc_log "$mount_context: volume is not mounted at $volume_root before diskd.useVolume"
    fi
    tc_log "$mount_context: diskd activation beginning for $device_path at $volume_root with ${diskd_attempts} attempt(s)"
    attempt=1
    while [ "$attempt" -le "$diskd_attempts" ]; do
        mounted_without_claim=0
        diskd_request_status=0
        tc_request_diskd_use_volume "$volume_root" "$mount_context" "attempt $attempt/$diskd_attempts" || diskd_request_status=$?
        if [ "$diskd_request_status" -eq 0 ]; then
            if tc_wait_for_diskd_volume_mount "$volume_root" "$mount_context"; then
                if [ "$was_mounted" -eq 1 ]; then
                    tc_log "$mount_context: diskd.useVolume claim complete; $volume_root remained mounted after attempt $attempt/$diskd_attempts"
                else
                    tc_log "$mount_context: mounted at $volume_root after diskd.useVolume attempt $attempt/$diskd_attempts"
                fi
                return 0
            fi
        elif is_volume_root_mounted "$volume_root"; then
            tc_log "$mount_context: diskd.useVolume command failed but $volume_root is mounted; diskd claim is not confirmed"
            mounted_without_claim=1
        fi
        if [ "$mounted_without_claim" -eq 1 ]; then
            tc_log "$mount_context: diskd activation incomplete after attempt $attempt/$diskd_attempts"
        else
            tc_log "$mount_context: $volume_root is not mounted after diskd.useVolume attempt $attempt/$diskd_attempts"
        fi
        if [ "$attempt" -lt "$diskd_attempts" ]; then
            tc_log "$mount_context: waiting 1s before diskd.useVolume retry"
            sleep 1
        fi
        attempt=$((attempt + 1))
    done

    tc_log "$mount_context: diskd.useVolume did not mount $volume_root after ${diskd_attempts} attempt(s); leaving volume unavailable without mount_hfs fallback"
    return 1
}

tc_wake_or_mount_volume() {
    tc_wake_or_mount_volume_with_policy "$1" "$2" "$DISKD_USE_VOLUME_ATTEMPTS" "MaSt volume $2"
}

tc_watchdog_wake_or_mount_volume() {
    tc_wake_or_mount_volume_with_policy "$1" "$2" "$WATCHDOG_DISKD_USE_VOLUME_ATTEMPTS" "watchdog volume $2"
}

tc_mount_mast_volumes_for_boot() {
    volumes_file=$1
    volume_count=0
    mounted_count=0
    failed_count=0

    tc_log "boot disk load: activating MaSt volumes through diskd.useVolume"
    while IFS="$TC_TAB" read -r disk_device builtin part_device volume_root part_name part_uuid ||
        [ -n "$disk_device$builtin$part_device$volume_root$part_name$part_uuid" ]; do
        [ -n "$part_device" ] || continue
        volume_count=$((volume_count + 1))
        tc_log "boot disk load: activating volume $volume_count: disk=$disk_device builtin=$builtin device=/dev/$part_device root=$volume_root name=$part_name"
        if tc_wake_or_mount_volume "/dev/$part_device" "$volume_root"; then
            mounted_count=$((mounted_count + 1))
            tc_log "boot disk load: volume active: /dev/$part_device at $volume_root"
        else
            failed_count=$((failed_count + 1))
            tc_log "boot disk load: volume inactive after diskd attempts: /dev/$part_device at $volume_root"
        fi
    done <"$volumes_file"
    tc_log "boot disk load: diskd activation complete: total=$volume_count mounted=$mounted_count failed=$failed_count"
}

tc_configure_ata_idle_for_mast_disks() {
    volumes_file=$1

    tc_log "ATA idle tuning: scanning built-in ATA disks after share-state build"
    if ! tc_is_unsigned_integer "$ATA_IDLE_SECONDS"; then
        tc_log "ATA idle tuning skipped; invalid ATA_IDLE_SECONDS=$ATA_IDLE_SECONDS"
        return 0
    fi
    if [ "$ATA_IDLE_SECONDS" -eq 0 ]; then
        tc_log "ATA idle tuning disabled"
        return 0
    fi

    configured_disks=" "
    while IFS="$TC_TAB" read -r disk_device builtin part_device volume_root part_name part_uuid ||
        [ -n "$disk_device$builtin$part_device$volume_root$part_name$part_uuid" ]; do
        [ -n "$disk_device" ] || continue
        if [ "$builtin" != "1" ]; then
            tc_log "ATA idle tuning: skipping $disk_device for /dev/$part_device; MaSt marks disk as external"
            continue
        fi
        case "$disk_device" in
            wd[0-9]*) ;;
            *)
                tc_log "ATA idle tuning: skipping $disk_device for /dev/$part_device; not a wd ATA disk"
                continue
                ;;
        esac
        if ! is_volume_root_mounted "$volume_root"; then
            tc_log "ATA idle tuning: skipping $disk_device for /dev/$part_device; $volume_root is not mounted"
            continue
        fi
        case "$configured_disks" in
            *" $disk_device "*) continue ;;
        esac
        configured_disks="$configured_disks$disk_device "

        tc_log "ATA idle tuning: setting $disk_device idle timer to ${ATA_IDLE_SECONDS}s after mounted volume $volume_root"
        if /sbin/atactl "$disk_device" setidle "$ATA_IDLE_SECONDS" >/dev/null 2>&1; then
            tc_log "ATA idle tuning: set $disk_device idle timer to ${ATA_IDLE_SECONDS}s"
        else
            tc_log "ATA idle tuning: failed to set $disk_device idle timer to ${ATA_IDLE_SECONDS}s"
        fi
    done <"$volumes_file"
}

tc_plist_key() {
    printf '%s\n' "$1" | /usr/bin/sed -n 's/^[[:space:]]*\([A-Za-z][A-Za-z0-9_]*\)[[:space:]]*=.*/\1/p'
}

tc_trim_plist_line() {
    printf '%s\n' "$1" | /usr/bin/sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

tc_plist_is_object_end() {
    case "$1" in
        "}"|"};"*|"},"*) return 0 ;;
        *) return 1 ;;
    esac
}

tc_plist_is_array_end() {
    case "$1" in
        "]"|"];"*|")"|");"*) return 0 ;;
        *) return 1 ;;
    esac
}

tc_extract_plist_string_key() {
    extract_key=$1
    extract_line=$2
    printf '%s\n' "$extract_line" | /usr/bin/sed -n 's/^[[:space:]]*'"$extract_key"'[[:space:]]*=[[:space:]]*"\(.*\)"[[:space:]]*[;,]*[[:space:]]*$/\1/p'
}

tc_extract_plist_bool_key() {
    extract_key=$1
    extract_line=$2
    value=$(printf '%s\n' "$extract_line" | /usr/bin/sed -n \
        -e 's/^[[:space:]]*'"$extract_key"'[[:space:]]*=[[:space:]]*\(true\)[[:space:]]*[;,]*[[:space:]]*$/\1/p' \
        -e 's/^[[:space:]]*'"$extract_key"'[[:space:]]*=[[:space:]]*\(false\)[[:space:]]*[;,]*[[:space:]]*$/\1/p')
    [ "$value" = "true" ] && echo 1 || echo 0
}

tc_extract_plist_number_key() {
    extract_key=$1
    extract_line=$2
    printf '%s\n' "$extract_line" | /usr/bin/sed -n 's/^[[:space:]]*'"$extract_key"'[[:space:]]*=[[:space:]]*\([0-9][0-9]*\)[[:space:]]*[;,]*[[:space:]]*$/\1/p'
}

tc_format_uuid_key() {
    extract_key=$1
    extract_line=$2
    hex=$(printf '%s\n' "$extract_line" | /usr/bin/sed -n \
        -e 's/^[[:space:]]*'"$extract_key"'[[:space:]]*=[[:space:]]*<\([^>]*\)>[[:space:]]*[;,]*[[:space:]]*$/\1/p' \
        -e 's/^[[:space:]]*'"$extract_key"'[[:space:]]*=[[:space:]]*"\([0-9A-Fa-f-]*\)"[[:space:]]*[;,]*[[:space:]]*$/\1/p' \
        -e 's/^[[:space:]]*'"$extract_key"'[[:space:]]*=[[:space:]]*\([0-9A-Fa-f][0-9A-Fa-f -]*\).*/\1/p' \
        | /usr/bin/sed 's/[[:space:]-]//g')
    echo "$hex" | /usr/bin/sed -n 's/^\([0-9A-Fa-f]\{8\}\)\([0-9A-Fa-f]\{4\}\)\([0-9A-Fa-f]\{4\}\)\([0-9A-Fa-f]\{4\}\)\([0-9A-Fa-f]\{12\}\)$/\1-\2-\3-\4-\5/p' | /usr/bin/sed 'y/ABCDEFGHIJKLMNOPQRSTUVWXYZ/abcdefghijklmnopqrstuvwxyz/'
}

tc_emit_mast_volume() {
    emit_out_file=$1
    if [ "$part_format" != "hfs" ] || [ -z "$part_device" ] || [ -z "$part_name" ] || [ -z "$part_uuid" ]; then
        return 0
    fi
    case "$part_device" in
        dk[0-9]*) ;;
        *) return 0 ;;
    esac
    printf '%s\t%s\t%s\t%s\n' "$part_device" "/Volumes/$part_device" "$part_name" "$part_uuid" >>"$emit_out_file"
}

tc_flush_mast_disk() {
    flush_pending_file=$1
    flush_out_file=$2
    flush_disk_device=$3
    flush_disk_builtin=$4
    [ -s "$flush_pending_file" ] || return 0
    while IFS="$TC_TAB" read -r pending_part pending_root pending_name pending_uuid ||
        [ -n "$pending_part$pending_root$pending_name$pending_uuid" ]; do
        printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$flush_disk_device" "$flush_disk_builtin" "$pending_part" "$pending_root" "$pending_name" "$pending_uuid" >>"$flush_out_file"
    done <"$flush_pending_file"
    : >"$flush_pending_file"
}

tc_read_mast_volumes_to() {
    out_file=$1
    raw_file=$2
    pending_file="$out_file.pending.$$"
    : >"$out_file"
    : >"$raw_file"
    : >"$pending_file"

    if ! /usr/bin/acp -A MaSt >"$raw_file" 2>/dev/null; then
        rm -f "$pending_file"
        return 1
    fi

    disk_device=
    disk_builtin=0
    in_partitions=0
    part_device=
    part_name=
    part_format=
    part_uuid=

    while IFS= read -r line || [ -n "$line" ]; do
        trimmed_line=$(tc_trim_plist_line "$line")
        if tc_plist_is_object_end "$trimmed_line"; then
            if [ "$in_partitions" -eq 1 ] && [ -n "$part_device" ]; then
                tc_emit_mast_volume "$pending_file"
                part_device=
                part_name=
                part_format=
                part_uuid=
            elif [ -n "$disk_device" ]; then
                tc_flush_mast_disk "$pending_file" "$out_file" "$disk_device" "$disk_builtin"
                disk_device=
                disk_builtin=0
            fi
        elif tc_plist_is_array_end "$trimmed_line"; then
            in_partitions=0
        fi

        key=$(tc_plist_key "$line")
        case "$key" in
            builtin)
                if [ "$in_partitions" -eq 0 ]; then
                    disk_builtin=$(tc_extract_plist_bool_key builtin "$line")
                fi
                ;;
            partitions)
                in_partitions=1
                ;;
            deviceName)
                value=$(tc_extract_plist_string_key deviceName "$line")
                if [ "$in_partitions" -eq 1 ]; then
                    part_device=$value
                else
                    if [ -n "$disk_device" ]; then
                        tc_flush_mast_disk "$pending_file" "$out_file" "$disk_device" "$disk_builtin"
                        disk_builtin=0
                    fi
                    disk_device=$value
                fi
                ;;
            name)
                if [ "$in_partitions" -eq 1 ]; then
                    part_name=$(tc_extract_plist_string_key name "$line")
                fi
                ;;
            format)
                if [ "$in_partitions" -eq 1 ]; then
                    part_format=$(tc_extract_plist_string_key format "$line" | /usr/bin/sed 'y/ABCDEFGHIJKLMNOPQRSTUVWXYZ/abcdefghijklmnopqrstuvwxyz/')
                fi
                ;;
            uuid)
                if [ "$in_partitions" -eq 1 ]; then
                    part_uuid=$(tc_format_uuid_key uuid "$line")
                fi
                ;;
        esac
    done <"$raw_file"

    if [ -n "$disk_device" ]; then
        tc_flush_mast_disk "$pending_file" "$out_file" "$disk_device" "$disk_builtin"
    fi
    rm -f "$pending_file"
    [ -s "$out_file" ]
}

tc_wait_for_mast_volumes_to() {
    out_file=$1
    raw_file=$2
    timeout_seconds=${3:-$MAST_DISCOVERY_WAIT_SECONDS}
    elapsed=0
    sleep_seconds=3

    while :; do
        if tc_read_mast_volumes_to "$out_file" "$raw_file"; then
            if [ "$elapsed" -gt 0 ]; then
                tc_log "MaSt discovery succeeded after ${elapsed}s"
            fi
            return 0
        fi

        if [ "$elapsed" -ge "$timeout_seconds" ]; then
            tc_log "MaSt discovery timed out after ${elapsed}s with no valid HFS volumes"
            return 1
        fi

        if [ "$elapsed" -eq 0 ]; then
            tc_log "MaSt discovery not ready; waiting up to ${timeout_seconds}s for valid HFS volumes"
        elif [ $((elapsed % 15)) -eq 0 ]; then
            tc_log "MaSt discovery still waiting after ${elapsed}s"
        fi

        sleep "$sleep_seconds"
        elapsed=$((elapsed + sleep_seconds))
    done
}

tc_print_topology_signature() {
    tmp_dir="/tmp/tcapsulesmb-topology.$$"
    mkdir -p "$tmp_dir"
    if tc_read_mast_volumes_to "$tmp_dir/volumes.tsv" "$tmp_dir/mast.raw"; then
        /bin/cat "$tmp_dir/volumes.tsv"
        rm -rf "$tmp_dir"
        return 0
    fi
    rm -rf "$tmp_dir"
    return 1
}

tc_sanitize_share_name() {
    sanitized=$(printf '%s' "$1" \
        | /usr/bin/sed \
            -e 's/[[:cntrl:]]/_/g' \
            -e 's/[\/\\:\*\?"<>|,=]/_/g' \
            -e 's/[][]/_/g' \
            -e 's/^[[:space:]]*//' \
            -e 's/[[:space:]]*$//')
    if [ -z "$sanitized" ]; then
        sanitized="Disk $2"
    fi
    echo "$sanitized"
}

tc_byte_len() (
    LC_ALL=C
    byte_value=$1
    echo ${#byte_value}
)

tc_truncate_to_bytes() {
    truncate_value=$1
    truncate_max=$2

    if [ "$truncate_max" -le 0 ]; then
        echo ""
        return 0
    fi
    if [ "$(tc_byte_len "$truncate_value")" -le "$truncate_max" ]; then
        echo "$truncate_value"
        return 0
    fi
    printf '%s' "$truncate_value" | /bin/dd bs=1 count="$truncate_max" 2>/dev/null
    echo ""
}

tc_adisk_share_name_budget() {
    disk_key=$1
    adisk_uuid=$2
    adisk_disk_advf=$3

    budget=$((TC_ADISK_TXT_MAX_BYTES - $(tc_byte_len "$disk_key") - TC_ADISK_TXT_ADVF_PREFIX_BYTES - $(tc_byte_len "$adisk_disk_advf") - TC_ADISK_TXT_ADVN_MID_BYTES - TC_ADISK_TXT_ADVU_PREFIX_BYTES - $(tc_byte_len "$adisk_uuid")))
    if [ "$budget" -lt 1 ]; then
        budget=1
    fi
    echo "$budget"
}

tc_bound_share_name() {
    bound_base=$1
    bound_max=$2
    bound_value=$(tc_truncate_to_bytes "$bound_base" "$bound_max")
    if [ -z "$bound_value" ]; then
        bound_value=$(tc_truncate_to_bytes "Disk" "$bound_max")
    fi
    echo "$bound_value"
}

tc_share_name_with_suffix() {
    suffix_base=$1
    suffix_text=$2
    suffix_max=$3
    suffix_len=$(tc_byte_len "$suffix_text")
    prefix_max=$((suffix_max - suffix_len))

    if [ "$prefix_max" -le 0 ]; then
        tc_bound_share_name "$suffix_text" "$suffix_max"
        return 0
    fi
    prefix=$(tc_bound_share_name "$suffix_base" "$prefix_max")
    echo "${prefix}${suffix_text}"
}

tc_share_name_exists() {
    wanted=$1
    [ -f "$TC_USED_SHARE_NAMES_FILE" ] || return 1
    while IFS= read -r existing; do
        [ "$existing" = "$wanted" ] && return 0
    done <"$TC_USED_SHARE_NAMES_FILE"
    return 1
}

tc_unique_share_name() {
    base=$1
    device=$2
    max_bytes=$3
    candidate=$(tc_bound_share_name "$base" "$max_bytes")
    suffix=1
    if tc_share_name_exists "$candidate"; then
        candidate=$(tc_share_name_with_suffix "$base" " ($device)" "$max_bytes")
    fi
    while tc_share_name_exists "$candidate"; do
        candidate=$(tc_share_name_with_suffix "$base" " ($device-$suffix)" "$max_bytes")
        suffix=$((suffix + 1))
    done
    echo "$candidate" >>"$TC_USED_SHARE_NAMES_FILE"
    echo "$candidate"
}

tc_volume_is_writable() {
    volume_root=$1
    test_dir="$volume_root/.tcapsulesmb-write-test.$$"
    if mkdir "$test_dir" >/dev/null 2>&1; then
        rmdir "$test_dir" >/dev/null 2>&1 || true
        return 0
    fi
    return 1
}

# Share-state helpers. These convert mounted MaSt volumes into the runtime
# state files used by Samba and mDNS.
tc_share_path_for_volume() {
    builtin=$1
    volume_root=$2

    if [ "$builtin" = "1" ] && [ "$INTERNAL_SHARE_USE_DISK_ROOT" != "1" ]; then
        echo "$volume_root/ShareRoot"
    else
        echo "$volume_root"
    fi
}

tc_prepare_time_machine_marker() {
    share_path=$1
    marker="$share_path/.com.apple.timemachine.supported"

    : >"$marker"
}

tc_prepare_share_path() {
    builtin=$1
    volume_root=$2
    share_path=$(tc_share_path_for_volume "$builtin" "$volume_root")

    if [ "$share_path" != "$volume_root" ]; then
        mkdir -p "$share_path"
    fi
    tc_prepare_time_machine_marker "$share_path"
    echo "$share_path"
}

tc_append_share_state_row() {
    share_name=$1
    share_path=$2
    part_device=$3
    builtin=$4
    part_uuid=$5

    printf '%s\t%s\t%s\t%s\t%s\n' "$share_name" "$share_path" "$part_device" "$builtin" "$part_uuid" >>"$TC_SHARES_TSV"
    printf '%s\t%s\t%s\t%s\n' "$share_name" "$part_device" "$part_uuid" "$TC_ADISK_DISK_ADVF" >>"$TC_ADISK_TSV"
}

tc_active_share_device_is_managed() {
    wanted_part_device=$1
    [ -s "$TC_SHARES_TSV" ] || return 1

    while IFS="$TC_TAB" read -r share_name share_path part_device builtin part_uuid ||
        [ -n "$share_name$share_path$part_device$builtin$part_uuid" ]; do
        [ -n "$part_device" ] || continue
        if [ "$part_device" = "$wanted_part_device" ]; then
            return 0
        fi
    done <"$TC_SHARES_TSV"
    return 1
}

tc_build_share_state() {
    volumes_file=${1:-}
    used_share_names_file="$TC_STATE_DIR/share-names.$$"
    candidate_count=0
    share_count=0

    if [ -z "$volumes_file" ]; then
        tc_log "share state build skipped; missing MaSt volumes snapshot"
        return 1
    fi

    : >"$TC_SHARES_TSV"
    : >"$TC_ADISK_TSV"
    TC_USED_SHARE_NAMES_FILE=$used_share_names_file
    : >"$TC_USED_SHARE_NAMES_FILE"

    tc_log "share state build: scanning mounted writable MaSt volumes"
    disk_device=
    builtin=
    part_device=
    volume_root=
    part_name=
    part_uuid=
    while IFS="$TC_TAB" read -r disk_device builtin part_device volume_root part_name part_uuid ||
        [ -n "$disk_device$builtin$part_device$volume_root$part_name$part_uuid" ]; do
        [ -n "$part_device" ] || continue
        candidate_count=$((candidate_count + 1))
        device_path="/dev/$part_device"
        tc_log "share candidate: device=$device_path disk=$disk_device builtin=$builtin root=$volume_root name=$part_name"
        if ! is_volume_root_mounted "$volume_root"; then
            tc_log "share skipped: $device_path at $volume_root is not mounted"
            continue
        fi
        if ! tc_volume_is_writable "$volume_root"; then
            tc_log "share skipped: $volume_root is not writable"
            continue
        fi

        share_path=$(tc_prepare_share_path "$builtin" "$volume_root")
        base_name=$(tc_sanitize_share_name "$part_name" "$part_device")
        share_name_budget=$(tc_adisk_share_name_budget "$part_device" "$part_uuid" "$TC_ADISK_DISK_ADVF")
        share_name=$(tc_unique_share_name "$base_name" "$part_device" "$share_name_budget")
        tc_append_share_state_row "$share_name" "$share_path" "$part_device" "$builtin" "$part_uuid"
        share_count=$((share_count + 1))
        tc_log "share prepared: $share_name -> $share_path uuid=$part_uuid builtin=$builtin"
    done <"$volumes_file"

    rm -f "$TC_USED_SHARE_NAMES_FILE"
    tc_log "share state build complete: candidates=$candidate_count shares=$share_count"
    [ -s "$TC_SHARES_TSV" ]
}

tc_verify_payload_dir() {
    payload_dir=$1

    [ -d "$payload_dir" ] || return 1
    [ -x "$payload_dir/smbd" ] || [ -x "$payload_dir/sbin/smbd" ] || return 1
    [ -f "$payload_dir/private/smbpasswd" ] || return 1
    [ -f "$payload_dir/private/username.map" ] || return 1
}

tc_log_limited_command_output() {
    label=$1
    shift
    output_file="$TC_STATE_DIR/payload-diagnostic.$$"
    output_rc=0
    output_line_count=0
    output_max_lines=12

    rm -f "$output_file"
    "$@" >"$output_file" 2>&1 || output_rc=$?

    tc_log "payload diagnostic command: $label"
    if [ -s "$output_file" ]; then
        while IFS= read -r output_line || [ -n "$output_line" ]; do
            if [ "$output_line_count" -lt "$output_max_lines" ]; then
                tc_log "payload diagnostic $label: $output_line"
            fi
            output_line_count=$((output_line_count + 1))
        done <"$output_file"
        if [ "$output_line_count" -gt "$output_max_lines" ]; then
            tc_log "payload diagnostic $label: truncated after $output_max_lines lines"
        fi
    else
        tc_log "payload diagnostic $label: (empty)"
    fi
    if [ "$output_rc" -ne 0 ]; then
        tc_log "payload diagnostic $label: exit $output_rc"
    fi
    rm -f "$output_file"
}

tc_log_payload_candidate_diagnostics() {
    diagnostic_context=$1
    shift
    volume_root=$1
    payload_dir=$2
    private_dir="$payload_dir/private"

    tc_log "payload candidate diagnostics ($diagnostic_context): volume=$volume_root payload=$payload_dir"
    tc_log_limited_command_output "df -k $volume_root" /bin/df -k "$volume_root"
    tc_log_limited_command_output "ls -la $volume_root" /bin/ls -la "$volume_root"
    tc_log_limited_command_output "ls -la $payload_dir" /bin/ls -la "$payload_dir"
    tc_log_limited_command_output "ls -la $private_dir" /bin/ls -la "$private_dir"
}

tc_scan_payload_candidates_for_builtin() {
    volumes_file=$1
    desired_builtin=$2

    while IFS="$TC_TAB" read -r disk_device builtin part_device volume_root part_name part_uuid ||
        [ -n "$disk_device$builtin$part_device$volume_root$part_name$part_uuid" ]; do
        [ -n "$part_device" ] || continue
        [ "$builtin" = "$desired_builtin" ] || continue
        candidate="$volume_root/$PAYLOAD_DIR_NAME"
        if is_volume_root_mounted "$volume_root"; then
            if tc_verify_payload_dir "$candidate"; then
                tc_log "payload candidate valid: $candidate builtin=$builtin"
                if [ -z "$selected_payload_dir" ]; then
                    selected_payload_dir=$candidate
                    selected_payload_volume=$volume_root
                    selected_payload_device="/dev/$part_device"
                fi
            else
                tc_log "payload candidate invalid: missing managed payload at $candidate"
                if [ -z "$first_invalid_payload_dir" ]; then
                    first_invalid_payload_dir=$candidate
                    first_invalid_payload_volume=$volume_root
                    first_invalid_payload_device="/dev/$part_device"
                    tc_log "payload discovery first invalid payload check failed for $candidate"
                    tc_log_payload_candidate_diagnostics "first failure" "$volume_root" "$candidate"
                fi
            fi
        else
            tc_log "payload candidate unavailable: /dev/$part_device at $volume_root is not mounted"
        fi
    done <"$volumes_file"
}

tc_resolve_payload() {
    volumes_file=${1:-}
    TC_RESOLVED_PAYLOAD_DIR=
    TC_RESOLVED_PAYLOAD_VOLUME=
    TC_RESOLVED_PAYLOAD_DEVICE=
    selected_payload_dir=
    selected_payload_volume=
    selected_payload_device=
    first_invalid_payload_dir=
    first_invalid_payload_volume=
    first_invalid_payload_device=

    if [ -z "$volumes_file" ]; then
        tc_log "payload discovery skipped; missing MaSt volumes snapshot"
        return 1
    fi

    tc_scan_payload_candidates_for_builtin "$volumes_file" 1
    tc_scan_payload_candidates_for_builtin "$volumes_file" 0

    if [ -n "$selected_payload_dir" ]; then
        TC_RESOLVED_PAYLOAD_DIR=$selected_payload_dir
        TC_RESOLVED_PAYLOAD_VOLUME=$selected_payload_volume
        TC_RESOLVED_PAYLOAD_DEVICE=$selected_payload_device
        tc_log "payload directory selected from mounted MaSt volumes: $TC_RESOLVED_PAYLOAD_DIR"
        return 0
    fi

    if [ -n "$first_invalid_payload_dir" ]; then
        tc_log "payload discovery failed: first mounted payload candidate is invalid at $first_invalid_payload_dir"
        tc_log "payload discovery: mount_hfs retry skipped; runtime uses diskd.useVolume-only activation"
        tc_log_payload_candidate_diagnostics "after final failure" "$first_invalid_payload_volume" "$first_invalid_payload_dir"
    fi

    tc_log "no valid payload directory found on mounted MaSt volumes"
    return 1
}

tc_write_payload_state() {
    payload_dir=$1
    volume_root=$2
    device_path=$3
    printf '%s\t%s\t%s\n' "$payload_dir" "$volume_root" "$device_path" >"$TC_PAYLOAD_TSV"
}

tc_load_payload_state() {
    TC_PAYLOAD_DIR=
    TC_PAYLOAD_VOLUME=
    TC_PAYLOAD_DEVICE=

    if [ -s "$TC_PAYLOAD_TSV" ]; then
        IFS="$TC_TAB" read -r TC_PAYLOAD_DIR TC_PAYLOAD_VOLUME TC_PAYLOAD_DEVICE <"$TC_PAYLOAD_TSV"
        if [ -n "$TC_PAYLOAD_DIR" ] && [ -n "$TC_PAYLOAD_VOLUME" ] && [ -n "$TC_PAYLOAD_DEVICE" ]; then
            tc_set_payload_log_dir "$TC_PAYLOAD_DIR" "$TC_PAYLOAD_VOLUME"
            return 0
        fi
    fi

    return 1
}

tc_log_mast_volume_state() {
    volumes_file=$1

    while IFS="$TC_TAB" read -r disk_device builtin part_device volume_root part_name part_uuid ||
        [ -n "$disk_device$builtin$part_device$volume_root$part_name$part_uuid" ]; do
        [ -n "$part_device" ] || continue
        tc_log "MaSt volume: disk=$disk_device builtin=$builtin part=$part_device root=$volume_root name=$part_name uuid=$part_uuid"
    done <"$volumes_file"
}

