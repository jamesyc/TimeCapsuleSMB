is_volume_root_mounted() {
    volume_root=$1
    df_line=$(/bin/df -k "$volume_root" 2>/dev/null | /usr/bin/tail -n +2 || true)
    case "$df_line" in
        *" $volume_root") return 0 ;;
    esac
    return 1
}

# Disk mount policy helpers. Boot uses Apple's diskd.useVolume flow so sleeping
# Apple-managed disks stay under diskd's ownership.
tc_request_diskd_use_volume() {
    volume_root=$1
    mount_context=$2
    attempt_label=${3:-}
    request_start_seconds=$(tc_now_seconds)

    mkdir -p "$volume_root"
    if [ -n "$attempt_label" ]; then
        tc_log "$mount_context: requesting diskd.useVolume for $volume_root ($attempt_label)"
    else
        tc_log "$mount_context: requesting diskd.useVolume for $volume_root"
    fi
    if /usr/bin/acp rpc diskd.useVolume path:s:"$volume_root" >/dev/null 2>&1; then
        request_duration_seconds=$(tc_elapsed_seconds_since "$request_start_seconds")
        tc_log "$mount_context: diskd.useVolume command completed for $volume_root duration_seconds=$request_duration_seconds"
        return 0
    fi
    request_duration_seconds=$(tc_elapsed_seconds_since "$request_start_seconds")
    tc_log "$mount_context: diskd.useVolume command failed for $volume_root duration_seconds=$request_duration_seconds"
    return 1
}

tc_wait_for_diskd_volume_mount() {
    volume_root=$1
    mount_context=$2
    mount_started_seconds=${3:-}

    tc_log_runtime_env_warnings
    mount_timeout_seconds=${TC_DISKD_USE_VOLUME_MOUNT_TIMEOUT_SECONDS:-31}
    mount_poll_seconds=${TC_DISKD_USE_VOLUME_MOUNT_POLL_SECONDS:-3}

    elapsed=0
    if tc_is_unsigned_integer "$mount_started_seconds" && [ "$mount_started_seconds" -gt 0 ]; then
        elapsed=$(tc_elapsed_seconds_since "$mount_started_seconds")
        if [ "$elapsed" -le 1 ]; then
            elapsed=0
        fi
        [ "$elapsed" -ge 0 ] || elapsed=0
        tc_log "$mount_context: waiting up to ${mount_timeout_seconds}s total for diskd.useVolume to mount $volume_root"
    else
        tc_log "$mount_context: waiting up to ${mount_timeout_seconds}s for diskd.useVolume to mount $volume_root"
    fi
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
        diskd_request_started_seconds=$(tc_now_seconds)
        tc_request_diskd_use_volume "$volume_root" "$mount_context" "attempt $attempt/$diskd_attempts" || diskd_request_status=$?
        if [ "$diskd_request_status" -eq 0 ]; then
            if tc_wait_for_diskd_volume_mount "$volume_root" "$mount_context" "$diskd_request_started_seconds"; then
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

tc_apply_ata_drive_setting() {
    disk_device=$1
    atactl_command=$2
    timer_name=$3
    timer_value=$4
    volume_root=$5

    if [ "$timer_value" = "0" ]; then
        tc_log "ATA drive settings: disabling $disk_device $timer_name timer after mounted volume $volume_root"
    else
        tc_log "ATA drive settings: setting $disk_device $timer_name timer to ${timer_value}s after mounted volume $volume_root"
    fi
    if /sbin/atactl "$disk_device" "$atactl_command" "$timer_value" >/dev/null 2>&1; then
        if [ "$timer_value" = "0" ]; then
            tc_log "ATA drive settings: disabled $disk_device $timer_name timer"
        else
            tc_log "ATA drive settings: set $disk_device $timer_name timer to ${timer_value}s"
        fi
    else
        tc_log "ATA drive settings: failed to set $disk_device $timer_name timer to ${timer_value}s"
    fi
}

tc_mast_trim_value() {
    TC_MAST_TRIMMED=$1
    while :; do
        case "$TC_MAST_TRIMMED" in
            " "*) TC_MAST_TRIMMED=${TC_MAST_TRIMMED# } ;;
            "$TC_TAB"*) TC_MAST_TRIMMED=${TC_MAST_TRIMMED#"$TC_TAB"} ;;
            *) break ;;
        esac
    done
    while :; do
        case "$TC_MAST_TRIMMED" in
            *" ") TC_MAST_TRIMMED=${TC_MAST_TRIMMED% } ;;
            *"$TC_TAB") TC_MAST_TRIMMED=${TC_MAST_TRIMMED%"$TC_TAB"} ;;
            *) break ;;
        esac
    done
}

tc_mast_clean_assignment_value() {
    TC_MAST_VALUE=$1
    tc_mast_trim_value "$TC_MAST_VALUE"
    TC_MAST_VALUE=$TC_MAST_TRIMMED
    while :; do
        case "$TC_MAST_VALUE" in
            *";"|*",")
                TC_MAST_VALUE=${TC_MAST_VALUE%?}
                tc_mast_trim_value "$TC_MAST_VALUE"
                TC_MAST_VALUE=$TC_MAST_TRIMMED
                ;;
            *) break ;;
        esac
    done
    case "$TC_MAST_VALUE" in
        \"*\")
            TC_MAST_VALUE=${TC_MAST_VALUE#\"}
            TC_MAST_VALUE=${TC_MAST_VALUE%\"}
            ;;
        "<"*">")
            TC_MAST_VALUE=${TC_MAST_VALUE#<}
            TC_MAST_VALUE=${TC_MAST_VALUE%>}
            ;;
    esac
}

tc_mast_bool_value() {
    case "$1" in
        true|TRUE|True|1) TC_MAST_BOOL=1 ;;
        *) TC_MAST_BOOL=0 ;;
    esac
}

tc_mast_format_value() {
    case "$1" in
        [Hh][Ff][Ss]) TC_MAST_FORMAT=hfs ;;
        *) TC_MAST_FORMAT=$1 ;;
    esac
}

tc_mast_hex_nibble() {
    case "$1" in
        0) TC_MAST_HEX_NIBBLE=0 ;;
        1) TC_MAST_HEX_NIBBLE=1 ;;
        2) TC_MAST_HEX_NIBBLE=2 ;;
        3) TC_MAST_HEX_NIBBLE=3 ;;
        4) TC_MAST_HEX_NIBBLE=4 ;;
        5) TC_MAST_HEX_NIBBLE=5 ;;
        6) TC_MAST_HEX_NIBBLE=6 ;;
        7) TC_MAST_HEX_NIBBLE=7 ;;
        8) TC_MAST_HEX_NIBBLE=8 ;;
        9) TC_MAST_HEX_NIBBLE=9 ;;
        10) TC_MAST_HEX_NIBBLE=a ;;
        11) TC_MAST_HEX_NIBBLE=b ;;
        12) TC_MAST_HEX_NIBBLE=c ;;
        13) TC_MAST_HEX_NIBBLE=d ;;
        14) TC_MAST_HEX_NIBBLE=e ;;
        *) TC_MAST_HEX_NIBBLE=f ;;
    esac
}

tc_mast_hex_byte() {
    byte_value=$1
    tc_mast_hex_nibble $((byte_value / 16))
    high_nibble=$TC_MAST_HEX_NIBBLE
    tc_mast_hex_nibble $((byte_value % 16))
    TC_MAST_HEX_BYTE=$high_nibble$TC_MAST_HEX_NIBBLE
}

tc_mast_uuid_from_hex() {
    uuid_hex=$1
    if [ "${#uuid_hex}" -ne 32 ]; then
        TC_MAST_UUID=
        return 1
    fi

    TC_MAST_UUID=
    uuid_index=0
    uuid_rest=$uuid_hex
    while [ -n "$uuid_rest" ]; do
        uuid_char=${uuid_rest%"${uuid_rest#?}"}
        uuid_rest=${uuid_rest#?}
        uuid_index=$((uuid_index + 1))
        case "$uuid_index" in
            9|13|17|21) TC_MAST_UUID="$TC_MAST_UUID-" ;;
        esac
        TC_MAST_UUID="$TC_MAST_UUID$uuid_char"
    done
    return 0
}

tc_mast_uuid_from_hexish() {
    uuid_source=$1
    case "$uuid_source" in
        *"|"*) uuid_source=${uuid_source%%|*} ;;
    esac
    tc_mast_clean_assignment_value "$uuid_source"
    uuid_source=$TC_MAST_VALUE
    uuid_hex=
    uuid_rest=$uuid_source
    while [ -n "$uuid_rest" ]; do
        uuid_char=${uuid_rest%"${uuid_rest#?}"}
        uuid_rest=${uuid_rest#?}
        case "$uuid_char" in
            [0-9]|[a-f]) uuid_hex=$uuid_hex$uuid_char ;;
            A) uuid_hex=${uuid_hex}a ;;
            B) uuid_hex=${uuid_hex}b ;;
            C) uuid_hex=${uuid_hex}c ;;
            D) uuid_hex=${uuid_hex}d ;;
            E) uuid_hex=${uuid_hex}e ;;
            F) uuid_hex=${uuid_hex}f ;;
        esac
    done
    tc_mast_uuid_from_hex "$uuid_hex"
}

tc_mast_b64_char_value() {
    case "$1" in
        A) TC_MAST_B64_VALUE=0 ;; B) TC_MAST_B64_VALUE=1 ;; C) TC_MAST_B64_VALUE=2 ;; D) TC_MAST_B64_VALUE=3 ;;
        E) TC_MAST_B64_VALUE=4 ;; F) TC_MAST_B64_VALUE=5 ;; G) TC_MAST_B64_VALUE=6 ;; H) TC_MAST_B64_VALUE=7 ;;
        I) TC_MAST_B64_VALUE=8 ;; J) TC_MAST_B64_VALUE=9 ;; K) TC_MAST_B64_VALUE=10 ;; L) TC_MAST_B64_VALUE=11 ;;
        M) TC_MAST_B64_VALUE=12 ;; N) TC_MAST_B64_VALUE=13 ;; O) TC_MAST_B64_VALUE=14 ;; P) TC_MAST_B64_VALUE=15 ;;
        Q) TC_MAST_B64_VALUE=16 ;; R) TC_MAST_B64_VALUE=17 ;; S) TC_MAST_B64_VALUE=18 ;; T) TC_MAST_B64_VALUE=19 ;;
        U) TC_MAST_B64_VALUE=20 ;; V) TC_MAST_B64_VALUE=21 ;; W) TC_MAST_B64_VALUE=22 ;; X) TC_MAST_B64_VALUE=23 ;;
        Y) TC_MAST_B64_VALUE=24 ;; Z) TC_MAST_B64_VALUE=25 ;;
        a) TC_MAST_B64_VALUE=26 ;; b) TC_MAST_B64_VALUE=27 ;; c) TC_MAST_B64_VALUE=28 ;; d) TC_MAST_B64_VALUE=29 ;;
        e) TC_MAST_B64_VALUE=30 ;; f) TC_MAST_B64_VALUE=31 ;; g) TC_MAST_B64_VALUE=32 ;; h) TC_MAST_B64_VALUE=33 ;;
        i) TC_MAST_B64_VALUE=34 ;; j) TC_MAST_B64_VALUE=35 ;; k) TC_MAST_B64_VALUE=36 ;; l) TC_MAST_B64_VALUE=37 ;;
        m) TC_MAST_B64_VALUE=38 ;; n) TC_MAST_B64_VALUE=39 ;; o) TC_MAST_B64_VALUE=40 ;; p) TC_MAST_B64_VALUE=41 ;;
        q) TC_MAST_B64_VALUE=42 ;; r) TC_MAST_B64_VALUE=43 ;; s) TC_MAST_B64_VALUE=44 ;; t) TC_MAST_B64_VALUE=45 ;;
        u) TC_MAST_B64_VALUE=46 ;; v) TC_MAST_B64_VALUE=47 ;; w) TC_MAST_B64_VALUE=48 ;; x) TC_MAST_B64_VALUE=49 ;;
        y) TC_MAST_B64_VALUE=50 ;; z) TC_MAST_B64_VALUE=51 ;;
        0) TC_MAST_B64_VALUE=52 ;; 1) TC_MAST_B64_VALUE=53 ;; 2) TC_MAST_B64_VALUE=54 ;; 3) TC_MAST_B64_VALUE=55 ;;
        4) TC_MAST_B64_VALUE=56 ;; 5) TC_MAST_B64_VALUE=57 ;; 6) TC_MAST_B64_VALUE=58 ;; 7) TC_MAST_B64_VALUE=59 ;;
        8) TC_MAST_B64_VALUE=60 ;; 9) TC_MAST_B64_VALUE=61 ;; +) TC_MAST_B64_VALUE=62 ;; /) TC_MAST_B64_VALUE=63 ;;
        =) TC_MAST_B64_VALUE=0 ;;
        *) TC_MAST_B64_VALUE= ;;
    esac
}

tc_mast_uuid_from_base64_data() {
    b64_source=$1
    b64_clean=
    b64_rest=$b64_source
    while [ -n "$b64_rest" ]; do
        b64_char=${b64_rest%"${b64_rest#?}"}
        b64_rest=${b64_rest#?}
        case "$b64_char" in
            " "|"$TC_TAB") ;;
            *) b64_clean=$b64_clean$b64_char ;;
        esac
    done

    uuid_hex=
    b64_rest=$b64_clean
    while [ -n "$b64_rest" ]; do
        b64_c1=${b64_rest%"${b64_rest#?}"}; b64_rest=${b64_rest#?}
        b64_c2=${b64_rest%"${b64_rest#?}"}; b64_rest=${b64_rest#?}
        b64_c3=${b64_rest%"${b64_rest#?}"}; b64_rest=${b64_rest#?}
        b64_c4=${b64_rest%"${b64_rest#?}"}; b64_rest=${b64_rest#?}
        [ -n "$b64_c1$b64_c2" ] || break

        tc_mast_b64_char_value "$b64_c1"; b64_v1=$TC_MAST_B64_VALUE
        tc_mast_b64_char_value "$b64_c2"; b64_v2=$TC_MAST_B64_VALUE
        tc_mast_b64_char_value "$b64_c3"; b64_v3=$TC_MAST_B64_VALUE
        tc_mast_b64_char_value "$b64_c4"; b64_v4=$TC_MAST_B64_VALUE
        [ -n "$b64_v1" ] && [ -n "$b64_v2" ] && [ -n "$b64_v3" ] && [ -n "$b64_v4" ] || {
            TC_MAST_UUID=
            return 1
        }

        tc_mast_hex_byte $((b64_v1 * 4 + b64_v2 / 16))
        uuid_hex=$uuid_hex$TC_MAST_HEX_BYTE
        if [ "$b64_c3" != "=" ]; then
            tc_mast_hex_byte $(((b64_v2 % 16) * 16 + b64_v3 / 4))
            uuid_hex=$uuid_hex$TC_MAST_HEX_BYTE
        fi
        if [ "$b64_c4" != "=" ]; then
            tc_mast_hex_byte $(((b64_v3 % 4) * 64 + b64_v4))
            uuid_hex=$uuid_hex$TC_MAST_HEX_BYTE
        fi
    done
    tc_mast_uuid_from_hex "$uuid_hex"
}

tc_mast_append_runtime_pending_row() {
    [ "$part_format" = "hfs" ] || return 0
    [ -n "$part_device" ] || return 0
    [ -n "$part_name" ] || return 0
    [ -n "$part_uuid" ] || return 0
    case "$part_device" in
        dk[0-9]*) ;;
        *) return 0 ;;
    esac

    pending_line="${part_device}${TC_TAB}/Volumes/${part_device}${TC_TAB}${part_name}${TC_TAB}${part_uuid}${TC_TAB}${part_format}${TC_TAB}${part_users}"
    if [ -z "$mast_runtime_pending_rows" ]; then
        mast_runtime_pending_rows=$pending_line
    else
        mast_runtime_pending_rows="$mast_runtime_pending_rows
$pending_line"
    fi
}

tc_mast_flush_runtime_rows() {
    [ -n "$mast_runtime_pending_rows" ] || return 0
    while IFS="$TC_TAB" read -r pending_part pending_root pending_name pending_uuid pending_format pending_users ||
        [ -n "$pending_part$pending_root$pending_name$pending_uuid$pending_format$pending_users" ]; do
        [ -n "$pending_part" ] || continue
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$disk_device" "$disk_builtin" "$pending_part" "$pending_root" "$pending_name" "$pending_uuid" "$pending_format" "$pending_users"
    done <<EOF
$mast_runtime_pending_rows
EOF
    mast_runtime_pending_rows=
}

tc_mast_reset_partition_state() {
    part_device=
    part_name=
    part_format=
    part_uuid=
    part_users=
}

tc_mast_handle_object_end() {
    if [ "$in_partitions" -eq 1 ]; then
        tc_mast_append_runtime_pending_row
        tc_mast_reset_partition_state
    elif [ -n "$disk_device" ]; then
        tc_mast_flush_runtime_rows
        disk_device=
        disk_builtin=0
    fi
}

tc_mast_handle_key_value() {
    mast_key=$1
    mast_value=$2

    case "$mast_key" in
        builtin)
            if [ "$in_partitions" -eq 0 ]; then
                tc_mast_bool_value "$mast_value"
                disk_builtin=$TC_MAST_BOOL
            fi
            ;;
        partitions)
            in_partitions=1
            ;;
        deviceName)
            if [ "$in_partitions" -eq 1 ]; then
                part_device=$mast_value
            else
                if [ -n "$disk_device" ]; then
                    tc_mast_flush_runtime_rows
                    disk_builtin=0
                fi
                disk_device=$mast_value
            fi
            ;;
        name)
            if [ "$in_partitions" -eq 1 ]; then
                part_name=$mast_value
            fi
            ;;
        format)
            if [ "$in_partitions" -eq 1 ]; then
                tc_mast_format_value "$mast_value"
                part_format=$TC_MAST_FORMAT
            fi
            ;;
        users)
            if [ "$in_partitions" -eq 1 ]; then
                case "$mast_value" in
                    ""|*[!0123456789]*) part_users= ;;
                    *) part_users=$mast_value ;;
                esac
            fi
            ;;
        uuid)
            if [ "$in_partitions" -eq 1 ]; then
                if tc_mast_uuid_from_hexish "$mast_value"; then
                    part_uuid=$TC_MAST_UUID
                else
                    part_uuid=
                fi
            fi
            ;;
    esac
}

tc_mast_handle_xml_data_value() {
    xml_data_key=$1
    xml_data_value=$2
    [ "$xml_data_key" = "uuid" ] || return 0
    [ "$in_partitions" -eq 1 ] || return 0
    if tc_mast_uuid_from_base64_data "$xml_data_value"; then
        part_uuid=$TC_MAST_UUID
    else
        part_uuid=
    fi
}

tc_mast_raw_to_runtime_rows() {
    disk_device=
    disk_builtin=0
    in_partitions=0
    mast_runtime_pending_rows=
    tc_mast_reset_partition_state
    xml_key=
    xml_data_active=0
    xml_data_key=
    xml_data_value=

    while IFS= read -r line || [ -n "$line" ]; do
        tc_mast_trim_value "$line"
        mast_line=$TC_MAST_TRIMMED
        [ -n "$mast_line" ] || continue

        if [ "$xml_data_active" -eq 1 ]; then
            case "$mast_line" in
                *"</data>"*)
                    xml_piece=${mast_line%%"</data>"*}
                    tc_mast_trim_value "$xml_piece"
                    xml_data_value=$xml_data_value$TC_MAST_TRIMMED
                    tc_mast_handle_xml_data_value "$xml_data_key" "$xml_data_value"
                    xml_data_active=0
                    xml_data_key=
                    xml_data_value=
                    xml_key=
                    ;;
                *)
                    tc_mast_trim_value "$mast_line"
                    xml_data_value=$xml_data_value$TC_MAST_TRIMMED
                    ;;
            esac
            continue
        fi

        case "$mast_line" in
            "<key>"*"</key>")
                xml_key=${mast_line#"<key>"}
                xml_key=${xml_key%"</key>"}
                continue
                ;;
            "<string>"*"</string>")
                if [ -n "$xml_key" ]; then
                    xml_value=${mast_line#"<string>"}
                    xml_value=${xml_value%"</string>"}
                    tc_mast_handle_key_value "$xml_key" "$xml_value"
                    xml_key=
                    continue
                fi
                ;;
            "<integer>"*"</integer>")
                if [ -n "$xml_key" ]; then
                    xml_value=${mast_line#"<integer>"}
                    xml_value=${xml_value%"</integer>"}
                    tc_mast_handle_key_value "$xml_key" "$xml_value"
                    xml_key=
                    continue
                fi
                ;;
            "<true/>"|"<true />")
                if [ -n "$xml_key" ]; then
                    tc_mast_handle_key_value "$xml_key" true
                    xml_key=
                    continue
                fi
                ;;
            "<false/>"|"<false />")
                if [ -n "$xml_key" ]; then
                    tc_mast_handle_key_value "$xml_key" false
                    xml_key=
                    continue
                fi
                ;;
            "<data>"*"</data>")
                if [ -n "$xml_key" ]; then
                    xml_value=${mast_line#"<data>"}
                    xml_value=${xml_value%"</data>"}
                    tc_mast_handle_xml_data_value "$xml_key" "$xml_value"
                    xml_key=
                    continue
                fi
                ;;
            "<data>"*)
                if [ -n "$xml_key" ]; then
                    xml_data_active=1
                    xml_data_key=$xml_key
                    xml_data_value=${mast_line#"<data>"}
                    continue
                fi
                ;;
            "<array>")
                if [ "$xml_key" = "partitions" ]; then
                    in_partitions=1
                    xml_key=
                fi
                continue
                ;;
            "</array>")
                if [ "$in_partitions" -eq 1 ]; then
                    in_partitions=0
                fi
                continue
                ;;
            "<dict>")
                if [ "$in_partitions" -eq 1 ]; then
                    tc_mast_reset_partition_state
                fi
                continue
                ;;
            "</dict>")
                tc_mast_handle_object_end
                continue
                ;;
        esac

        case "$mast_line" in
            "}"|"};"*|"},"*)
                tc_mast_handle_object_end
                continue
                ;;
            "]"|"];"*|")"|");"*)
                if [ "$in_partitions" -eq 1 ]; then
                    in_partitions=0
                fi
                continue
                ;;
            "{"|"["|"("|");"|");"*|"MaSt"*)
                ;;
        esac

        case "$mast_line" in
            *"="*)
                mast_key=${mast_line%%=*}
                mast_value=${mast_line#*=}
                tc_mast_trim_value "$mast_key"
                mast_key=$TC_MAST_TRIMMED
                tc_mast_clean_assignment_value "$mast_value"
                mast_value=$TC_MAST_VALUE
                tc_mast_handle_key_value "$mast_key" "$mast_value"
                ;;
        esac
    done

    if [ -n "$part_device" ]; then
        tc_mast_append_runtime_pending_row
    fi
    if [ -n "$disk_device" ]; then
        tc_mast_flush_runtime_rows
    fi
    return 0
}

tc_mast_runtime_rows_to_topology() {
    runtime_rows=$1
    while IFS="$TC_TAB" read -r disk_device builtin part_device volume_root part_name part_uuid part_format part_users ||
        [ -n "$disk_device$builtin$part_device$volume_root$part_name$part_uuid$part_format$part_users" ]; do
        [ -n "$part_device" ] || continue
        printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$disk_device" "$builtin" "$part_device" "$volume_root" "$part_name" "$part_uuid"
    done <<EOF
$runtime_rows
EOF
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

tc_volume_is_writable() {
    volume_root=$1
    test_dir="$volume_root/.tcapsulesmb-write-test.$$"
    if mkdir "$test_dir" >/dev/null 2>&1; then
        rmdir "$test_dir" >/dev/null 2>&1 || true
        return 0
    fi
    return 1
}

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

    : >"$marker" || return 1
}

tc_prepare_share_path() {
    builtin=$1
    volume_root=$2
    share_path=$(tc_share_path_for_volume "$builtin" "$volume_root")

    if [ "$share_path" != "$volume_root" ]; then
        mkdir -p "$share_path" || return 1
    fi
    tc_prepare_time_machine_marker "$share_path" || return 1
    echo "$share_path"
}

tc_verify_payload_dir() {
    payload_dir=$1

    [ -d "$payload_dir" ] || return 1
    [ -x "$payload_dir/smbd" ] || [ -x "$payload_dir/sbin/smbd" ] || return 1
    [ -d "$payload_dir/private" ] || return 1
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
