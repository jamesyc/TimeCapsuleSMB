#!/bin/sh

PATH=/bin:/sbin:/usr/bin:/usr/sbin

RAM_ROOT=/mnt/Memory/samba4
RAM_SBIN="$RAM_ROOT/sbin"
RAM_ETC="$RAM_ROOT/etc"
RAM_VAR="$RAM_ROOT/var"
RAM_PRIVATE="$RAM_ROOT/private"
LOCKS_ROOT=/mnt/Locks

MDNS_PROC_NAME=mdns-advertiser
NBNS_PROC_NAME=nbns-advertiser
ALL_MDNS_SNAPSHOT=/mnt/Flash/allmdns.txt
APPLE_MDNS_SNAPSHOT=/mnt/Flash/applemdns.txt

TC_CONFIG_FILE=/mnt/Flash/tcapsulesmb.conf
TC_STATE_DIR="$RAM_VAR"
TC_MAST_RAW="$TC_STATE_DIR/mast.raw"
TC_VOLUMES_TSV="$TC_STATE_DIR/volumes.tsv"
TC_SHARES_TSV="$TC_STATE_DIR/shares.tsv"
TC_ADISK_TSV="$TC_STATE_DIR/adisk.tsv"
TC_PAYLOAD_TSV="$TC_STATE_DIR/payload.tsv"
TC_TOPOLOGY_SIGNATURE="$TC_STATE_DIR/topology.signature"
TC_USED_SHARE_NAMES_FILE=
TC_TAB=$(printf '\t')

TC_LOG_FILE="$TC_STATE_DIR/runtime.log"
TC_LOG_PREFIX=runtime
TC_LOG_MODE=ram_rewrite
TC_LOG_FALLBACK_FILE=
TC_LOG_VOLUME=
TC_LOG_MAX_BYTES=131072
TC_MDNS_BIN=/mnt/Flash/mdns-advertiser
TC_NBNS_BIN="$RAM_SBIN/nbns-advertiser"
TC_SMBD_BIN="$RAM_SBIN/smbd"
TC_SMBD_CONF="$RAM_ETC/smb.conf"
TC_MDNS_LOG_FILE="$RAM_VAR/mdns.log"
TC_NBNS_LOG_FILE="$RAM_VAR/nbns.log"
TC_PAYLOAD_LOG_DIR=
TC_PAYLOAD_LOG_VOLUME=
TC_MDNS_CAPTURE_STATUS_FILE=
TC_RUNTIME_LOG_MAX_BYTES=131072
TC_SMBD_DISK_LOGGING_ENABLED=0
TC_ADISK_DISK_ADVF=0x1093
TC_ADISK_TXT_MAX_BYTES=255
TC_ADISK_TXT_ADVF_PREFIX_BYTES=6
TC_ADISK_TXT_ADVN_MID_BYTES=6
TC_ADISK_TXT_ADVU_PREFIX_BYTES=6
TC_SAMBA_VM_BUFCACHE=5
TC_MDNS_CAPTURE_PID=
TC_APPLE_MDNS_SNAPSHOT_START=

LEGACY_PREFIX_NETBSD7=/root/tc-netbsd7
LEGACY_PREFIX_NETBSD4=/root/tc-netbsd4
LEGACY_PREFIX_NETBSD4LE=/root/tc-netbsd4le
LEGACY_PREFIX_NETBSD4BE=/root/tc-netbsd4be

tc_init_runtime_env() {
    APPLE_MOUNT_WAIT_SECONDS=${APPLE_MOUNT_WAIT_SECONDS:-30}
    MAST_DISCOVERY_WAIT_SECONDS=${MAST_DISCOVERY_WAIT_SECONDS:-120}
    WATCHDOG_MOUNT_WAIT_SECONDS=${WATCHDOG_MOUNT_WAIT_SECONDS:-$APPLE_MOUNT_WAIT_SECONDS}
    MDNS_CAPTURE_WAIT_SECONDS=${MDNS_CAPTURE_WAIT_SECONDS:-75}
    INTERNAL_SHARE_USE_DISK_ROOT=${INTERNAL_SHARE_USE_DISK_ROOT:-0}
    NBNS_ENABLED=${NBNS_ENABLED:-0}
    TC_SMBD_DISK_LOGGING_ENABLED=${SMBD_DEBUG_LOGGING:-0}
}

tc_set_log() {
    TC_LOG_FILE=$1
    TC_LOG_PREFIX=$2
    TC_LOG_MODE=ram_rewrite
    TC_LOG_FALLBACK_FILE=
    TC_LOG_VOLUME=
    TC_LOG_MAX_BYTES=131072
}

tc_set_payload_append_log() {
    TC_LOG_FILE=$1
    TC_LOG_PREFIX=$2
    TC_LOG_VOLUME=$3
    TC_LOG_FALLBACK_FILE=$4
    TC_LOG_MODE=payload_append
    TC_LOG_MAX_BYTES=$(tc_runtime_log_max_bytes)

    line="$(date '+%Y-%m-%d %H:%M:%S') $TC_LOG_PREFIX: log target configured: payload=$TC_LOG_FILE volume=$TC_LOG_VOLUME fallback=${TC_LOG_FALLBACK_FILE:-none}"
    if ! tc_payload_append_log_line "$line" && [ -n "$TC_LOG_FALLBACK_FILE" ]; then
        tc_ram_rewrite_log_line "$TC_LOG_FALLBACK_FILE" "$line"
    fi
}

tc_runtime_logs_unbounded() {
    [ "${SMBD_DEBUG_LOGGING:-0}" = "1" ] || [ "${MDNS_DEBUG_LOGGING:-0}" = "1" ]
}

tc_runtime_log_max_bytes() {
    if tc_runtime_logs_unbounded; then
        echo 0
    else
        echo "$TC_RUNTIME_LOG_MAX_BYTES"
    fi
}

tc_smbd_max_log_size() {
    if [ "$TC_SMBD_DISK_LOGGING_ENABLED" = "1" ]; then
        echo 0
    else
        echo 128
    fi
}

tc_log_file_size() {
    log_path=$1
    [ -f "$log_path" ] || {
        echo 0
        return 0
    }
    set -- $(/bin/ls -ln "$log_path" 2>/dev/null)
    case "${5:-}" in
        ""|*[!0123456789]*) echo 0 ;;
        *) echo "$5" ;;
    esac
}

tc_trim_log_file_if_needed() {
    trim_log_path=$1
    trim_log_bytes=$2
    trim_log_tmp="${trim_log_path}.tmp.$$"

    [ "$trim_log_bytes" -gt 0 ] || return 0
    [ -f "$trim_log_path" ] || return 0
    current_size=$(tc_log_file_size "$trim_log_path")
    [ -n "$current_size" ] || current_size=0
    [ "$current_size" -gt "$trim_log_bytes" ] || return 0

    /usr/bin/tail -c "$trim_log_bytes" "$trim_log_path" >"$trim_log_tmp" 2>/dev/null || /bin/cat "$trim_log_path" >"$trim_log_tmp" 2>/dev/null || true
    mv "$trim_log_tmp" "$trim_log_path"
}

tc_append_bounded_log_line() {
    log_path=$1
    max_bytes=$2
    line=$3

    ensure_parent_dir "$log_path"
    printf '%s\n' "$line" >>"$log_path" || return 1
    tc_trim_log_file_if_needed "$log_path" "$max_bytes"
}

tc_payload_append_log_line() {
    line=$1

    [ -n "$TC_LOG_FILE" ] || return 1
    [ -n "$TC_LOG_VOLUME" ] || return 1
    is_volume_root_mounted "$TC_LOG_VOLUME" || return 1
    tc_append_bounded_log_line "$TC_LOG_FILE" "$TC_LOG_MAX_BYTES" "$line"
}

tc_ram_rewrite_log_line() {
    log_path=$1
    line=$2
    log_dir=${log_path%/*}
    tmp_log="$log_path.tmp.$$"

    [ -d "$log_dir" ] || mkdir -p "$log_dir"
    {
        if [ -f "$log_path" ]; then
            /usr/bin/tail -n 255 "$log_path" 2>/dev/null || true
        fi
        echo "$line"
    } >"$tmp_log"
    mv "$tmp_log" "$log_path"
}

tc_log() {
    line="$(date '+%Y-%m-%d %H:%M:%S') $TC_LOG_PREFIX: $*"

    if [ "$TC_LOG_MODE" = "payload_append" ]; then
        if tc_payload_append_log_line "$line"; then
            return 0
        fi
        if [ -n "$TC_LOG_FALLBACK_FILE" ]; then
            tc_ram_rewrite_log_line "$TC_LOG_FALLBACK_FILE" "$line"
            return 0
        fi
    fi

    tc_ram_rewrite_log_line "$TC_LOG_FILE" "$line"
}

get_iface_ipv4() {
    iface=$1
    /sbin/ifconfig "$iface" 2>/dev/null | sed -n 's/^[[:space:]]*inet[[:space:]]\([0-9.]*\).*/\1/p' | sed -n '1p'
}

get_iface_mac() {
    iface=$1
    /sbin/ifconfig "$iface" 2>/dev/null \
        | sed -n \
            -e 's/^[[:space:]]*ether[[:space:]]\([0-9A-Fa-f:]*\).*/\1/p' \
            -e 's/^[[:space:]]*address[[:space:]]\([0-9A-Fa-f:]*\).*/\1/p' \
        | sed -n '1p'
}

get_radio_mac() {
    radio_iface=$1
    /sbin/ifconfig "$radio_iface" 2>/dev/null \
        | sed -n \
            -e 's/^[[:space:]]*ether[[:space:]]\([0-9A-Fa-f:]*\).*/\1/p' \
            -e 's/^[[:space:]]*address[[:space:]]\([0-9A-Fa-f:]*\).*/\1/p' \
        | sed -n '1p'
}

get_airport_srcv() {
    /usr/bin/uname -a 2>/dev/null | sed -n 's/.*AirPortFW-\([0-9][0-9.]*\).*/\1/p' | sed -n '1p'
}

get_airport_acp_value() {
    acp_key=$1
    acp_value=$(/usr/bin/acp -q "$acp_key" 2>/dev/null | sed -n '1p')
    [ -n "$acp_value" ] || return 1
    printf '%s\n' "$acp_value"
}

airport_acp_decimal_value() {
    acp_value=$1
    case "$acp_value" in
        0x*|0X*)
            if acp_decimal=$(printf '%d' "$acp_value" 2>/dev/null); then
                printf '%s\n' "$acp_decimal"
                return 0
            fi
            return 1
            ;;
        *)
            printf '%s\n' "$acp_value"
            ;;
    esac
}

airport_acp_hex_value() {
    acp_value=$1
    case "$acp_value" in
        0x*|0X*)
            if acp_decimal=$(printf '%d' "$acp_value" 2>/dev/null); then
                printf '0x%X\n' "$acp_decimal"
                return 0
            fi
            return 1
            ;;
        *)
            printf '%s\n' "$acp_value"
            ;;
    esac
}

airport_acp_bool_decimal_value() {
    acp_value=$1
    case "$acp_value" in
        false|False|FALSE)
            echo 0
            ;;
        true|True|TRUE)
            echo 1
            ;;
        *)
            airport_acp_decimal_value "$acp_value"
            ;;
    esac
}

get_airport_system_name() {
    get_airport_acp_value syNm
}

get_airport_host_label() {
    /bin/hostname 2>/dev/null | sed -n '1p'
}

get_airport_rast() {
    /usr/bin/acp -A WiFi 2>/dev/null | sed -n 's/^[[:space:]]*raSt=\([^[:space:]]*\).*/\1/p' | sed -n '1p'
}

get_airport_rana() {
    acp_value=$(get_airport_acp_value raNA) || return 1
    airport_acp_bool_decimal_value "$acp_value"
}

get_airport_syfl() {
    acp_value=$(get_airport_acp_value syFl) || return 1
    airport_acp_hex_value "$acp_value"
}

get_airport_syap() {
    acp_value=$(get_airport_acp_value syAP) || return 1
    airport_acp_decimal_value "$acp_value"
}

get_airport_syvs() {
    if airport_syvs=$(get_airport_acp_value syVs); then
        printf '%s\n' "$airport_syvs"
        return 0
    fi
    airport_srcv=$1
    digits=$(printf '%s' "$airport_srcv" | sed -n 's/^\([0-9]\)\([0-9]\)\([0-9]\).*/\1.\2.\3/p')
    if [ -n "$digits" ]; then
        echo "$digits"
        return 0
    fi
    return 1
}

get_airport_bjsd() {
    acp_value=$(get_airport_acp_value bjSd) || return 1
    airport_acp_decimal_value "$acp_value"
}

wait_for_process() {
    proc_name=$1
    max_attempts=${2:-10}
    attempt=0
    while [ "$attempt" -lt "$max_attempts" ]; do
        if runtime_process_present_by_ucomm "$proc_name"; then
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    return 1
}

runtime_process_present_by_ucomm() {
    proc_name=$1
    if ps_out=$(/bin/ps axww -o pid= -o stat= -o ucomm= -o command= 2>/dev/null); then
        old_ifs=$IFS
        IFS='
'
        for line in $ps_out; do
            [ -n "$line" ] || continue
            line_ifs=$IFS
            IFS=' 	'
            set -- $line
            IFS=$line_ifs
            [ "$#" -ge 3 ] || continue
            case "$2" in
                Z*) continue ;;
            esac

            if [ "$3" = "$proc_name" ]; then
                IFS=$old_ifs
                return 0
            fi
        done
        IFS=$old_ifs
    fi

    return 1
}

runtime_watchdog_pids() {
    if ps_out=$(/bin/ps axww -o pid= -o stat= -o ucomm= -o command= 2>/dev/null); then
        old_ifs=$IFS
        IFS='
'
        for line in $ps_out; do
            [ -n "$line" ] || continue
            line_ifs=$IFS
            IFS=' 	'
            set -- $line
            IFS=$line_ifs
            [ "$#" -ge 4 ] || continue
            watchdog_pid=$1
            watchdog_stat=$2
            watchdog_ucomm=$3
            shift 3
            case "$watchdog_stat" in
                Z*) continue ;;
            esac
            [ "$watchdog_ucomm" = "sh" ] || continue
            if [ "${1:-}" = "/mnt/Flash/watchdog.sh" ]; then
                printf '%s\n' "$watchdog_pid"
                continue
            fi
            if [ "${1:-}" = "/bin/sh" ] || [ "${1:-}" = "sh" ]; then
                [ "${2:-}" = "/mnt/Flash/watchdog.sh" ] && printf '%s\n' "$watchdog_pid"
            fi
        done
        IFS=$old_ifs
    fi
}

runtime_watchdog_present() {
    [ -n "$(runtime_watchdog_pids)" ]
}

kill_watchdog_pids() {
    watchdog_signal=$1
    for watchdog_pid in $(runtime_watchdog_pids); do
        case "$watchdog_signal" in
            KILL) /bin/kill -9 "$watchdog_pid" >/dev/null 2>&1 || true ;;
            TERM|"") /bin/kill "$watchdog_pid" >/dev/null 2>&1 || true ;;
            *) return 1 ;;
        esac
    done
}

wait_for_runtime_process_absent_by_ucomm() {
    proc_name=$1
    max_attempts=${2:-5}
    attempt=0

    while runtime_process_present_by_ucomm "$proc_name"; do
        if [ "$attempt" -ge "$max_attempts" ]; then
            return 1
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    return 0
}

wait_for_watchdog_absent() {
    max_attempts=${1:-5}
    attempt=0

    while runtime_watchdog_present; do
        if [ "$attempt" -ge "$max_attempts" ]; then
            return 1
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    return 0
}

stop_runtime_process_by_ucomm() {
    label=$1
    proc_name=$2
    case "$proc_name" in
        ""|*[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-]*)
            tc_log "refusing unsafe process name for $label: $proc_name"
            return 1
            ;;
    esac
    pkill_pattern="^$proc_name$"

    tc_log "stopping old $label"
    /usr/bin/pkill "$pkill_pattern" >/dev/null 2>&1 || true

    if wait_for_runtime_process_absent_by_ucomm "$proc_name" 5; then
        return 0
    fi

    tc_log "old $label still running after TERM; sending KILL"
    /usr/bin/pkill -9 "$pkill_pattern" >/dev/null 2>&1 || true

    if wait_for_runtime_process_absent_by_ucomm "$proc_name" 5; then
        return 0
    fi

    tc_log "old $label survived KILL"
    return 1
}

stop_watchdog_process() {
    tc_log "stopping old watchdog"
    kill_watchdog_pids TERM

    if wait_for_watchdog_absent 5; then
        return 0
    fi

    tc_log "old watchdog still running after TERM; sending KILL"
    kill_watchdog_pids KILL

    if wait_for_watchdog_absent 5; then
        return 0
    fi

    tc_log "old watchdog survived KILL"
    return 1
}

stop_apple_nbns_conflicts() {
    cleanup_status=0

    stop_runtime_process_by_ucomm "wcifsnd" "wcifsnd" || cleanup_status=1
    stop_runtime_process_by_ucomm "wcifsfs" "wcifsfs" || cleanup_status=1

    return "$cleanup_status"
}

stop_nbns_conflicts() {
    cleanup_status=0

    stop_apple_nbns_conflicts || cleanup_status=1
    stop_runtime_process_by_ucomm "$NBNS_PROC_NAME" "$NBNS_PROC_NAME" || cleanup_status=1

    return "$cleanup_status"
}

is_volume_root_mounted() {
    volume_root=$1
    df_line=$(/bin/df -k "$volume_root" 2>/dev/null | /usr/bin/tail -n +2 || true)
    case "$df_line" in
        *" $volume_root") return 0 ;;
    esac
    return 1
}

mount_hfs_bounded() {
    dev_path=$1
    volume_root=$2
    timeout_seconds=${3:-30}
    mount_context=${4:-mount candidate}
    created_mountpoint=0

    if [ ! -b "$dev_path" ]; then
        tc_log "$mount_context skipped; missing block device $dev_path"
        return 1
    fi

    if [ ! -d "$volume_root" ]; then
        mkdir -p "$volume_root"
        created_mountpoint=1
        tc_log "created mountpoint $volume_root for $dev_path"
    fi

    tc_log "launching mount_hfs for $dev_path at $volume_root"
    /sbin/mount_hfs "$dev_path" "$volume_root" >/dev/null 2>&1 &
    mount_pid=$!
    attempt=0
    while kill -0 "$mount_pid" >/dev/null 2>&1; do
        if [ "$attempt" -ge "$timeout_seconds" ]; then
            kill "$mount_pid" >/dev/null 2>&1 || true
            sleep 1
            kill -9 "$mount_pid" >/dev/null 2>&1 || true
            wait "$mount_pid" >/dev/null 2>&1 || true
            tc_log "mount_hfs command did not exit promptly for $dev_path at $volume_root; re-checking mount state"
            if is_volume_root_mounted "$volume_root"; then
                tc_log "mount_hfs command timed out, but volume is mounted"
                return 0
            fi
            if [ "$created_mountpoint" -eq 1 ]; then
                /bin/rmdir "$volume_root" >/dev/null 2>&1 || true
            fi
            tc_log "mount_hfs timed out for $dev_path at $volume_root and volume was not mounted at the immediate re-check"
            return 1
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    wait "$mount_pid" >/dev/null 2>&1 || true

    if is_volume_root_mounted "$volume_root"; then
        tc_log "mounted $dev_path at $volume_root after ${attempt}s"
        return 0
    fi

    if [ "$created_mountpoint" -eq 1 ]; then
        /bin/rmdir "$volume_root" >/dev/null 2>&1 || true
    fi

    tc_log "mount_hfs exited for $dev_path at $volume_root, but volume is not mounted"
    return 1
}

# Disk mount policy helpers. Boot and watchdog share the low-level
# Apple-first flow, but keep separate entry points so their timing can diverge.
tc_wake_or_mount_volume_with_policy() {
    device_path=$1
    volume_root=$2
    apple_wait_seconds=$3
    mount_timeout_seconds=$4
    mount_context=$5

    if [ -z "$device_path" ] || [ -z "$volume_root" ]; then
        return 1
    fi

    if is_volume_root_mounted "$volume_root"; then
        return 0
    fi

    mkdir -p "$volume_root"
    tc_log "$mount_context: requesting diskd.useVolume for $volume_root"
    /usr/bin/acp rpc diskd.useVolume path:s:"$volume_root" >/dev/null 2>&1 || true
    attempt=0
    while [ "$attempt" -lt "$apple_wait_seconds" ]; do
        if is_volume_root_mounted "$volume_root"; then
            tc_log "$mount_context: observed $volume_root mounted after diskd.useVolume wait: ${attempt}s"
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done

    tc_log "$mount_context: diskd.useVolume wait timed out after ${apple_wait_seconds}s; manual fallback will handle remaining unmounted volumes"
    mount_hfs_bounded "$device_path" "$volume_root" "$mount_timeout_seconds" "$mount_context"
}

tc_wake_or_mount_volume() {
    tc_wake_or_mount_volume_with_policy "$1" "$2" "$APPLE_MOUNT_WAIT_SECONDS" 30 "MaSt volume $2"
}

tc_boot_wake_or_mount_volume() {
    tc_wake_or_mount_volume "$1" "$2"
}

tc_watchdog_wake_or_mount_volume() {
    tc_wake_or_mount_volume_with_policy "$1" "$2" "$WATCHDOG_MOUNT_WAIT_SECONDS" 30 "watchdog volume $2"
}

tc_request_apple_mount_for_volume() {
    device_path=$1
    volume_root=$2
    mount_context=$3

    if [ -z "$device_path" ] || [ -z "$volume_root" ]; then
        return 1
    fi

    if is_volume_root_mounted "$volume_root"; then
        return 0
    fi

    mkdir -p "$volume_root"
    tc_log "$mount_context: requesting diskd.useVolume for $volume_root"
    /usr/bin/acp rpc diskd.useVolume path:s:"$volume_root" >/dev/null 2>&1 || true
    return 0
}

tc_request_apple_mounts_for_mast_volumes() {
    volumes_file=$1

    while IFS="$TC_TAB" read -r disk_device builtin part_device volume_root part_name part_uuid ||
        [ -n "$disk_device$builtin$part_device$volume_root$part_name$part_uuid" ]; do
        [ -n "$part_device" ] || continue
        tc_request_apple_mount_for_volume "/dev/$part_device" "$volume_root" "MaSt volume $volume_root"
    done <"$volumes_file"
}

tc_all_mast_volumes_mounted() {
    volumes_file=$1
    found=0

    while IFS="$TC_TAB" read -r disk_device builtin part_device volume_root part_name part_uuid ||
        [ -n "$disk_device$builtin$part_device$volume_root$part_name$part_uuid" ]; do
        [ -n "$part_device" ] || continue
        found=1
        if ! is_volume_root_mounted "$volume_root"; then
            return 1
        fi
    done <"$volumes_file"

    [ "$found" -eq 1 ]
}

tc_wait_for_apple_mast_mounts() {
    volumes_file=$1
    wait_seconds=${2:-$APPLE_MOUNT_WAIT_SECONDS}
    elapsed=0

    if tc_all_mast_volumes_mounted "$volumes_file"; then
        tc_log "MaSt volume diskd.useVolume wait skipped; all volumes already mounted"
        return 0
    fi

    tc_log "MaSt volume diskd.useVolume wait beginning for up to ${wait_seconds}s"
    while [ "$elapsed" -lt "$wait_seconds" ]; do
        sleep 1
        elapsed=$((elapsed + 1))
        if tc_all_mast_volumes_mounted "$volumes_file"; then
            tc_log "MaSt volume diskd.useVolume wait complete; all volumes mounted after ${elapsed}s"
            return 0
        fi
    done

    tc_log "MaSt volume diskd.useVolume wait timed out after ${wait_seconds}s; manual fallback will handle remaining unmounted volumes"
    return 1
}

tc_mount_remaining_mast_volumes() {
    volumes_file=$1
    status=0

    while IFS="$TC_TAB" read -r disk_device builtin part_device volume_root part_name part_uuid ||
        [ -n "$disk_device$builtin$part_device$volume_root$part_name$part_uuid" ]; do
        [ -n "$part_device" ] || continue
        if is_volume_root_mounted "$volume_root"; then
            continue
        fi
        if mount_hfs_bounded "/dev/$part_device" "$volume_root" 30 "MaSt volume $volume_root"; then
            :
        else
            status=1
        fi
    done <"$volumes_file"

    return "$status"
}

tc_mount_mast_volumes_for_boot() {
    volumes_file=$1

    tc_log "boot disk load: requesting diskd.useVolume for all MaSt volumes"
    tc_request_apple_mounts_for_mast_volumes "$volumes_file"
    tc_wait_for_apple_mast_mounts "$volumes_file" "$APPLE_MOUNT_WAIT_SECONDS" || true
    tc_log "boot disk load: checking for unmounted volumes after shared diskd wait"
    tc_mount_remaining_mast_volumes "$volumes_file" || true
}

tc_plist_key() {
    printf '%s\n' "$1" | /usr/bin/sed -n 's/^[[:space:]]*\([A-Za-z][A-Za-z0-9_]*\)[[:space:]]*=.*/\1/p'
}

tc_trim_plist_line() {
    printf '%s\n' "$1" | /usr/bin/sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
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
        case "$trimmed_line" in
            "}"|"};"*|"},"*)
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
                ;;
            "]"|"];"*|");"|");"*)
                in_partitions=0
                ;;
        esac

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

tc_build_share_state() {
    volumes_file=${1:-$TC_VOLUMES_TSV}
    used_share_names_file="$TC_STATE_DIR/share-names.$$"
    : >"$TC_SHARES_TSV"
    : >"$TC_ADISK_TSV"
    TC_USED_SHARE_NAMES_FILE=$used_share_names_file
    : >"$TC_USED_SHARE_NAMES_FILE"

    disk_device=
    builtin=
    part_device=
    volume_root=
    part_name=
    part_uuid=
    while IFS="$TC_TAB" read -r disk_device builtin part_device volume_root part_name part_uuid ||
        [ -n "$disk_device$builtin$part_device$volume_root$part_name$part_uuid" ]; do
        [ -n "$part_device" ] || continue
        device_path="/dev/$part_device"
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
        tc_log "share prepared: $share_name -> $share_path uuid=$part_uuid builtin=$builtin"
    done <"$volumes_file"

    rm -f "$TC_USED_SHARE_NAMES_FILE"
    [ -s "$TC_SHARES_TSV" ]
}

tc_mount_active_volume_job() {
    share_name=$1
    share_path=$2
    device_path=$3
    volume_root=$4
    context=$5
    df_line=

    tc_log "$context check starting: share=$share_name path=$share_path device=$device_path root=$volume_root"
    if [ -z "$share_name" ] || [ -z "$share_path" ] || [ -z "$device_path" ] || [ -z "$volume_root" ]; then
        tc_log "$context check failed: malformed active share row share=$share_name path=$share_path device=$device_path root=$volume_root"
        return 1
    fi

    df_line=$(/bin/df -k "$volume_root" 2>/dev/null | /usr/bin/tail -n +2 || true)
    if [ -n "$df_line" ]; then
        tc_log "$context df before wake: $df_line"
    else
        tc_log "$context df before wake: no df output for $volume_root"
    fi

    if tc_watchdog_wake_or_mount_volume "$device_path" "$volume_root"; then
        df_line=$(/bin/df -k "$volume_root" 2>/dev/null | /usr/bin/tail -n +2 || true)
        if [ -n "$df_line" ]; then
            tc_log "$context df after wake: $df_line"
        else
            tc_log "$context df after wake: no df output for $volume_root"
        fi
        if [ -d "$share_path" ]; then
            tc_log "$context available: share=$share_name path=$share_path device=$device_path root=$volume_root"
            return 0
        else
            tc_log "$context unavailable: share path missing after mount: $share_path"
            return 1
        fi
    else
        df_line=$(/bin/df -k "$volume_root" 2>/dev/null | /usr/bin/tail -n +2 || true)
        if [ -n "$df_line" ]; then
            tc_log "$context df after failed wake: $df_line"
        else
            tc_log "$context df after failed wake: no df output for $volume_root"
        fi
        tc_log "$context unavailable: $device_path at $volume_root for share=$share_name path=$share_path"
        return 1
    fi
}

tc_mount_active_volumes_from_state() {
    state_file=$TC_SHARES_TSV
    status=0
    job_count=0

    if [ ! -s "$state_file" ]; then
        tc_log "active share state missing; runtime reload required"
        return 2
    fi

    tc_log "active share check beginning: state=$state_file"

    while IFS="$TC_TAB" read -r share_name share_path part_device builtin part_uuid; do
        [ -n "$part_device" ] || continue
        job_count=$((job_count + 1))
        tc_log "active share check row $job_count: share=$share_name path=$share_path device=/dev/$part_device root=/Volumes/$part_device builtin=$builtin uuid=$part_uuid"
        if tc_mount_active_volume_job "$share_name" "$share_path" "/dev/$part_device" "/Volumes/$part_device" "active share volume"; then
            tc_log "active share check row $job_count succeeded"
        else
            tc_log "active share check row $job_count failed"
            status=1
        fi
    done <"$state_file"

    if [ "$job_count" -eq 0 ]; then
        tc_log "active share check found no valid share rows; runtime reload required"
        return 2
    fi

    if [ "$status" -eq 0 ]; then
        tc_log "active share check complete: all $job_count active share volumes available"
    else
        tc_log "active share check complete: one or more of $job_count active share volumes unavailable"
    fi
    return "$status"
}

tc_verify_payload_dir() {
    payload_dir=$1

    [ -d "$payload_dir" ] || return 1
    [ -x "$payload_dir/smbd" ] || [ -x "$payload_dir/sbin/smbd" ] || return 1
    [ -f "$payload_dir/private/smbpasswd" ] || return 1
    [ -f "$payload_dir/private/username.map" ] || return 1
}

tc_emit_payload_candidate_volumes() {
    volumes_file=${1:-$TC_VOLUMES_TSV}

    for desired_builtin in 1 0; do
        while IFS="$TC_TAB" read -r disk_device builtin part_device volume_root part_name part_uuid ||
            [ -n "$disk_device$builtin$part_device$volume_root$part_name$part_uuid" ]; do
            [ -n "$part_device" ] || continue
            [ "$builtin" = "$desired_builtin" ] || continue
            printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$disk_device" "$builtin" "$part_device" "$volume_root" "$part_name" "$part_uuid"
        done <"$volumes_file"
    done
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
            fi
        else
            tc_log "payload candidate unavailable: /dev/$part_device at $volume_root is not mounted"
        fi
    done <"$volumes_file"
}

tc_resolve_payload() {
    volumes_file=${1:-$TC_VOLUMES_TSV}
    TC_RESOLVED_PAYLOAD_DIR=
    TC_RESOLVED_PAYLOAD_VOLUME=
    TC_RESOLVED_PAYLOAD_DEVICE=
    selected_payload_dir=
    selected_payload_volume=
    selected_payload_device=

    tc_scan_payload_candidates_for_builtin "$volumes_file" 1
    tc_scan_payload_candidates_for_builtin "$volumes_file" 0

    if [ -n "$selected_payload_dir" ]; then
        TC_RESOLVED_PAYLOAD_DIR=$selected_payload_dir
        TC_RESOLVED_PAYLOAD_VOLUME=$selected_payload_volume
        TC_RESOLVED_PAYLOAD_DEVICE=$selected_payload_device
        tc_log "payload directory selected from mounted MaSt volumes: $TC_RESOLVED_PAYLOAD_DIR"
        return 0
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

tc_read_payload_state() {
    TC_PAYLOAD_DIR=
    TC_PAYLOAD_VOLUME=
    TC_PAYLOAD_DEVICE=

    if [ -s "$TC_PAYLOAD_TSV" ]; then
        IFS="$TC_TAB" read -r TC_PAYLOAD_DIR TC_PAYLOAD_VOLUME TC_PAYLOAD_DEVICE <"$TC_PAYLOAD_TSV"
        if [ -n "$TC_PAYLOAD_DIR" ] && [ -n "$TC_PAYLOAD_VOLUME" ] && [ -n "$TC_PAYLOAD_DEVICE" ]; then
            return 0
        fi
    fi

    return 1
}

tc_payload_available() {
    tc_read_payload_state || return 1
    tc_watchdog_wake_or_mount_volume "$TC_PAYLOAD_DEVICE" "$TC_PAYLOAD_VOLUME" && tc_verify_payload_dir "$TC_PAYLOAD_DIR"
}

tc_log_mast_volume_state() {
    volumes_file=$1

    while IFS="$TC_TAB" read -r disk_device builtin part_device volume_root part_name part_uuid ||
        [ -n "$disk_device$builtin$part_device$volume_root$part_name$part_uuid" ]; do
        [ -n "$part_device" ] || continue
        tc_log "MaSt volume: disk=$disk_device builtin=$builtin part=$part_device root=$volume_root name=$part_name uuid=$part_uuid"
    done <"$volumes_file"
}

tc_refresh_disk_state() {
    volumes_file="$TC_STATE_DIR/mast-volumes.$$"
    raw_file="$TC_STATE_DIR/mast.raw.$$"

    rm -f "$volumes_file" "$raw_file"
    if ! tc_wait_for_mast_volumes_to "$volumes_file" "$raw_file" "$MAST_DISCOVERY_WAIT_SECONDS"; then
        tc_log "MaSt discovery failed or returned no valid HFS volumes"
        rm -f "$volumes_file" "$raw_file"
        return 1
    fi
    /bin/cat "$volumes_file" >"$TC_TOPOLOGY_SIGNATURE"
    tc_log_mast_volume_state "$volumes_file"

    tc_mount_mast_volumes_for_boot "$volumes_file"

    tc_log "building share state from mounted writable MaSt volumes"
    if ! tc_build_share_state "$volumes_file"; then
        tc_log "no writable MaSt share volumes are available"
        rm -f "$volumes_file" "$raw_file"
        return 1
    fi

    if ! tc_resolve_payload "$volumes_file"; then
        tc_log "payload discovery failed"
        rm -f "$volumes_file" "$raw_file"
        return 1
    fi

    tc_write_payload_state "$TC_RESOLVED_PAYLOAD_DIR" "$TC_RESOLVED_PAYLOAD_VOLUME" "$TC_RESOLVED_PAYLOAD_DEVICE"
    tc_set_payload_log_dir "$TC_RESOLVED_PAYLOAD_DIR" "$TC_RESOLVED_PAYLOAD_VOLUME"
    if tc_payload_log_dir_ready; then
        tc_log "payload runtime logs enabled at $TC_PAYLOAD_LOG_DIR"
    else
        tc_log "payload runtime log directory unavailable at $TC_PAYLOAD_LOG_DIR"
    fi

    tc_log "disk-state refresh complete: runtime state written"
    rm -f "$volumes_file" "$raw_file"
    return 0
}

tc_stage_disk_runtime() {
    bind_interfaces=$1

    if ! tc_read_payload_state; then
        tc_log "payload discovery failed: payload state is unavailable"
        return 1
    fi
    tc_set_payload_log_dir "$TC_PAYLOAD_DIR" "$TC_PAYLOAD_VOLUME"

    SMBD_SRC=$(tc_find_payload_smbd "$TC_PAYLOAD_DIR") || {
        tc_log "payload discovery failed: missing smbd binary in $TC_PAYLOAD_DIR"
        return 1
    }

    NBNS_SRC=
    if [ "$NBNS_ENABLED" = "1" ]; then
        if NBNS_SRC=$(tc_find_payload_nbns "$TC_PAYLOAD_DIR"); then
            :
        else
            NBNS_SRC=
        fi
    fi

    tc_stage_runtime "$TC_PAYLOAD_DIR" "$SMBD_SRC" "$NBNS_SRC"
    tc_generate_smb_conf "$TC_PAYLOAD_DIR" "$bind_interfaces"
    tc_log "runtime staging complete under $RAM_ROOT"
}

tc_find_payload_smbd() {
    payload_dir=$1

    if [ -x "$payload_dir/smbd" ]; then
        tc_log "selected smbd binary $payload_dir/smbd"
        echo "$payload_dir/smbd"
        return 0
    fi

    if [ -x "$payload_dir/sbin/smbd" ]; then
        tc_log "selected smbd binary $payload_dir/sbin/smbd"
        echo "$payload_dir/sbin/smbd"
        return 0
    fi

    tc_log "no smbd binary found in $payload_dir"
    return 1
}

tc_find_payload_nbns() {
    payload_dir=$1

    if [ -x "$payload_dir/nbns-advertiser" ]; then
        tc_log "selected nbns binary $payload_dir/nbns-advertiser"
        echo "$payload_dir/nbns-advertiser"
        return 0
    fi

    tc_log "nbns binary not found in $payload_dir"
    return 1
}

tc_select_cache_directory() {
    payload_dir=$1
    kernel_release=$(/usr/bin/uname -r 2>/dev/null || true)
    case "$kernel_release" in
        4.*) echo "$payload_dir/cache" ;;
        *) echo "$RAM_VAR" ;;
    esac
}

tc_set_payload_log_dir() {
    payload_dir=$1
    payload_volume=$2

    TC_PAYLOAD_LOG_DIR="$payload_dir/logs"
    TC_PAYLOAD_LOG_VOLUME="$payload_volume"
    TC_MDNS_LOG_FILE="$TC_PAYLOAD_LOG_DIR/mdns.log"
    TC_NBNS_LOG_FILE="$TC_PAYLOAD_LOG_DIR/nbns.log"
}

tc_payload_log_dir_ready() {
    [ -n "$TC_PAYLOAD_LOG_DIR" ] || return 1
    [ -n "$TC_PAYLOAD_LOG_VOLUME" ] || return 1
    is_volume_root_mounted "$TC_PAYLOAD_LOG_VOLUME" || return 1
    mkdir -p "$TC_PAYLOAD_LOG_DIR" || return 1
    chmod 755 "$TC_PAYLOAD_LOG_DIR" >/dev/null 2>&1 || true
    tc_prepare_smbd_core_dir "$TC_PAYLOAD_LOG_DIR" || return 1
}

tc_prepare_smbd_core_dir() {
    log_dir=$1

    [ -n "$log_dir" ] || return 1
    # Samba derives its core path from the log directory as cores/smbd.
    # Prepare it on the payload disk so panic dumps do not target RAM.
    mkdir -p "$log_dir/cores/smbd" || return 1
    chmod 700 "$log_dir/cores" "$log_dir/cores/smbd" >/dev/null 2>&1 || true
}

tc_prepare_runtime_log_file() {
    log_path=$1
    max_bytes=$(tc_runtime_log_max_bytes)

    case "$log_path" in
        "$TC_PAYLOAD_LOG_DIR"/*)
            if [ -n "$TC_PAYLOAD_LOG_DIR" ]; then
                tc_payload_log_dir_ready || return 1
            else
                ensure_parent_dir "$log_path"
            fi
            ;;
        *)
            ensure_parent_dir "$log_path"
            ;;
    esac
    : >>"$log_path" || return 1
    tc_trim_log_file_if_needed "$log_path" "$max_bytes"
}

tc_stage_runtime() {
    payload_dir=$1
    smbd_src=$2
    nbns_src=${3:-}

    cp "$smbd_src" "$TC_SMBD_BIN"
    chmod 755 "$TC_SMBD_BIN"

    for required_file in smbpasswd username.map; do
        if [ ! -f "$payload_dir/private/$required_file" ]; then
            tc_log "required Samba private file missing: $payload_dir/private/$required_file"
            return 1
        fi
        cp "$payload_dir/private/$required_file" "$RAM_PRIVATE/$required_file"
    done
    chmod 600 "$RAM_PRIVATE/smbpasswd" "$RAM_PRIVATE/username.map"
    tc_log "staged Samba auth files into RAM private directory"

    if [ "$NBNS_ENABLED" = "1" ] && [ -n "$nbns_src" ] && [ -x "$nbns_src" ]; then
        cp "$nbns_src" "$TC_NBNS_BIN"
        chmod 755 "$TC_NBNS_BIN"
        tc_log "staged nbns runtime binary"
    else
        tc_log "nbns runtime staging skipped"
    fi
}

tc_generate_smb_conf() {
    payload_dir=$1
    bind_interfaces=$2
    cache_directory=$(tc_select_cache_directory "$payload_dir")
    smbd_log="$payload_dir/logs/log.smbd"
    smbd_max_log_size=$(tc_smbd_max_log_size)
    smbd_log_level_line=

    mkdir -p "$payload_dir/logs"
    chmod 755 "$payload_dir/logs" >/dev/null 2>&1 || true
    tc_prepare_smbd_core_dir "$payload_dir/logs" || true
    if [ "$TC_SMBD_DISK_LOGGING_ENABLED" = "1" ]; then
        smbd_log_level_line="    log level = 5 vfs:8 fruit:8"
        : >>"$smbd_log" || true
        tc_log "smbd debug logging enabled at $smbd_log"
    else
        trim_log_file "$smbd_log" "$TC_RUNTIME_LOG_MAX_BYTES"
    fi

    {
        cat <<EOF
[global]
    netbios name = $SMB_NETBIOS_NAME
    workgroup = WORKGROUP
    server string = Time Capsule Samba 4
    interfaces = $bind_interfaces
    bind interfaces only = yes
    security = user
    map to guest = Never
    restrict anonymous = 2
    guest account = nobody
    null passwords = no
    ea support = yes
    passdb backend = smbpasswd:$RAM_PRIVATE/smbpasswd
    username map = $RAM_PRIVATE/username.map
    dos charset = ASCII
    min protocol = SMB2
    max protocol = SMB3
    server multi channel support = no
    load printers = no
    disable spoolss = yes
    dfree command = /mnt/Flash/dfree.sh
    pid directory = $RAM_VAR
    lock directory = $LOCKS_ROOT
    state directory = $RAM_VAR
    cache directory = $cache_directory
    private dir = $RAM_PRIVATE
    dbwrap_tdb_max_dead:* = 0
    log file = $smbd_log
    max log size = $smbd_max_log_size
$smbd_log_level_line
    smb ports = 445
    deadtime = 60
    max open files = 512
    max smbd processes = 16
    reset on zero vc = yes
    fruit:aapl = yes
    fruit:model = MacSamba
    fruit:advertise_fullsync = true
    fruit:nfs_aces = no
    fruit:veto_appledouble = yes
    fruit:wipe_intentionally_left_blank_rfork = yes
    fruit:delete_empty_adfiles = yes
EOF

        while IFS="$TC_TAB" read -r share_name share_path part_device builtin part_uuid; do
            [ -n "$share_name" ] || continue
            cat <<EOF

[$share_name]
    path = $share_path
    browseable = yes
    read only = no
    guest ok = no
    valid users = $SMB_SAMBA_USER root
    veto files = /$PAYLOAD_DIR_NAME/
    vfs objects = catia fruit streams_xattr acl_xattr xattr_tdb
    acl_xattr:ignore system acls = yes
    streams_xattr:max xattrs per stream = 2
    fruit:resource = file
    fruit:metadata = stream
    fruit:encoding = native
    fruit:time machine = yes
    fruit:posix_rename = yes
    xattr_tdb:file = $payload_dir/private/xattr.tdb
    force user = root
    force group = wheel
    create mask = 0666
    directory mask = 0777
    force create mode = 0666
    force directory mode = 0777
EOF
        done <"$TC_SHARES_TSV"
    } >"$TC_SMBD_CONF"
}

tc_cleanup_old_runtime() {
    cleanup_status=0

    tc_log "cleaning old managed runtime processes and RAM state"
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

tc_wait_for_bind_interfaces() {
    attempt=0

    sleep 1
    while [ "$attempt" -lt 60 ]; do
        iface_ip=$(get_iface_ipv4 "$NET_IFACE" || true)
        if [ -n "$iface_ip" ] && [ "$iface_ip" != "0.0.0.0" ]; then
            tc_log "network interface $NET_IFACE ready with IPv4 $iface_ip"
            echo "127.0.0.1/8 $iface_ip/24"
            return 0
        fi

        attempt=$((attempt + 1))
        sleep 1
    done

    tc_log "timed out waiting for IPv4 on $NET_IFACE"
    return 1
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
        tc_log "local hostname resolution already present for $device_hostname"
    elif printf '127.0.0.1\t%s %s.local\n' "$device_hostname" "$device_hostname" >>/etc/hosts; then
        tc_log "local hostname resolution prepared for $device_hostname"
    else
        tc_log "local hostname resolution could not update /etc/hosts"
    fi
}

ensure_parent_dir() {
    target_path=$1
    parent_dir=${target_path%/*}
    if [ -n "$parent_dir" ] && [ "$parent_dir" != "$target_path" ]; then
        mkdir -p "$parent_dir"
    fi
}

trim_log_file() {
    trim_log_path=$1
    trim_log_bytes=${2:-65536}
    trim_log_tmp="${trim_log_path}.tmp.$$"

    ensure_parent_dir "$trim_log_path"
    if [ -f "$trim_log_path" ]; then
        /usr/bin/tail -c "$trim_log_bytes" "$trim_log_path" >"$trim_log_tmp" 2>/dev/null || /bin/cat "$trim_log_path" >"$trim_log_tmp" 2>/dev/null || true
        mv "$trim_log_tmp" "$trim_log_path"
    else
        : >"$trim_log_path"
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
    if [ -z "${AIRPORT_SYAP:-}" ]; then
        AIRPORT_SYAP=$(get_airport_syap || true)
    fi

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

    snapshot_current=$(/bin/ls -lnT "$snapshot_path" 2>/dev/null || true)
    if [ -z "$TC_APPLE_MDNS_SNAPSHOT_START" ]; then
        tc_log "trusted Apple mDNS snapshot was created during this boot run: $snapshot_path"
    elif [ "$snapshot_current" != "$TC_APPLE_MDNS_SNAPSHOT_START" ]; then
        tc_log "trusted Apple mDNS snapshot was updated during this boot run: $snapshot_path"
    else
        tc_log "trusted Apple mDNS snapshot predates this boot run; accepting stale snapshot: $snapshot_path"
    fi
    return 0
}

tc_prepare_mdns_identity() {
    iface_mac=$1
    context=$2

    if [ ! -x "$TC_MDNS_BIN" ]; then
        tc_log "$context: mdns skipped; missing $TC_MDNS_BIN"
        return 1
    fi

    tc_log "$context: interface $NET_IFACE mac=${iface_mac:-missing}"
    if [ -z "$iface_mac" ]; then
        tc_log "$context: mdns skipped; missing $NET_IFACE MAC address"
        return 1
    fi

    if derive_airport_fields "$iface_mac"; then
        tc_log "$context: derived airport fields instance=${AIRPORT_INSTANCE_NAME:-missing} host=${AIRPORT_HOST_LABEL:-missing} wama=${AIRPORT_WAMA:-missing} rama=${AIRPORT_RAMA:-missing} ram2=${AIRPORT_RAM2:-missing} rast=${AIRPORT_RAST:-missing} rana=${AIRPORT_RANA:-missing} syfl=${AIRPORT_SYFL:-missing} syap=${AIRPORT_SYAP:-missing} syvs=${AIRPORT_SYVS:-missing} srcv=${AIRPORT_SRCV:-missing} bjsd=${AIRPORT_BJSD:-missing}"
    else
        tc_log "$context: airport clone fields incomplete; skipping _airport._tcp advertisement"
    fi
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

tc_generate_mdns() {
    iface_mac=$(get_iface_mac "$NET_IFACE" || true)
    if ! tc_prepare_mdns_identity "$iface_mac" "mdns generation"; then
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

    tc_log "mDNS AirPort snapshot generation failed; falling back to mDNS capture"
    set -- "$TC_MDNS_BIN" \
        --save-all-snapshot "$ALL_MDNS_SNAPSHOT" \
        --save-snapshot "$APPLE_MDNS_SNAPSHOT" \
        --ipv4 "$TC_NET_IFACE_IP"
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

    if tc_run_mdns_snapshot_command "capture" "$@"; then
        tc_log "mDNS snapshot capture finished"
    else
        tc_log "mDNS snapshot capture exited with failure; final advertiser will use generated records if needed"
    fi
}

tc_wait_for_mdns_capture() {
    wait_seconds=${MDNS_CAPTURE_WAIT_SECONDS:-75}
    elapsed=0
    capture_status=

    if [ -z "$TC_MDNS_CAPTURE_PID" ]; then
        return 0
    fi

    tc_log "waiting up to ${wait_seconds}s for mDNS snapshot capture pid $TC_MDNS_CAPTURE_PID"
    if ! kill -0 "$TC_MDNS_CAPTURE_PID" >/dev/null 2>&1; then
        TC_MDNS_CAPTURE_PID=
        rm -f "$TC_MDNS_CAPTURE_STATUS_FILE"
        TC_MDNS_CAPTURE_STATUS_FILE=
        return 0
    fi

    while [ "$elapsed" -lt "$wait_seconds" ]; do
        if [ -n "$TC_MDNS_CAPTURE_STATUS_FILE" ] && [ -f "$TC_MDNS_CAPTURE_STATUS_FILE" ]; then
            capture_status=$(/bin/cat "$TC_MDNS_CAPTURE_STATUS_FILE" 2>/dev/null || echo 1)
            wait "$TC_MDNS_CAPTURE_PID" >/dev/null 2>&1 || true
            if [ "$capture_status" = "0" ]; then
                tc_log "mDNS snapshot capture finished"
            else
                tc_log "mDNS snapshot capture exited with failure; final advertiser will use generated records if needed"
            fi
            rm -f "$TC_MDNS_CAPTURE_STATUS_FILE"
            TC_MDNS_CAPTURE_PID=
            TC_MDNS_CAPTURE_STATUS_FILE=
            return 0
        fi
        if ! kill -0 "$TC_MDNS_CAPTURE_PID" >/dev/null 2>&1; then
            wait "$TC_MDNS_CAPTURE_PID" >/dev/null 2>&1 || true
            tc_log "mDNS snapshot capture ended without status; final advertiser will use generated records if needed"
            rm -f "$TC_MDNS_CAPTURE_STATUS_FILE"
            TC_MDNS_CAPTURE_PID=
            TC_MDNS_CAPTURE_STATUS_FILE=
            return 0
        fi
        elapsed=$((elapsed + 1))
        sleep 1
    done

    tc_log "mDNS snapshot capture timed out after ${wait_seconds}s; stopping capture and continuing with generated records if needed"
    kill "$TC_MDNS_CAPTURE_PID" >/dev/null 2>&1 || true
    stop_runtime_process_by_ucomm "$MDNS_PROC_NAME" "$MDNS_PROC_NAME" || true
    sleep 1
    kill -9 "$TC_MDNS_CAPTURE_PID" >/dev/null 2>&1 || true
    wait "$TC_MDNS_CAPTURE_PID" >/dev/null 2>&1 || true
    rm -f "$TC_MDNS_CAPTURE_STATUS_FILE"
    TC_MDNS_CAPTURE_PID=
    TC_MDNS_CAPTURE_STATUS_FILE=
}

tc_launch_mdns_advertiser() {
    context=$1
    wait_for_capture=$2
    kill_prior=$3
    wait_attempts=$4

    iface_ip=${TC_NET_IFACE_IP:-}
    if [ -z "$iface_ip" ]; then
        iface_ip=$(get_iface_ipv4 "$NET_IFACE" || true)
    fi
    iface_mac=$(get_iface_mac "$NET_IFACE" || true)
    if [ -z "$iface_ip" ] || [ "$iface_ip" = "0.0.0.0" ]; then
        tc_log "$context: mdns launch skipped because $NET_IFACE has no IPv4 address"
        return 0
    fi
    if ! tc_prepare_mdns_identity "$iface_mac" "$context"; then
        return 0
    fi

    if [ "$wait_for_capture" = "1" ]; then
        tc_wait_for_mdns_capture
        if tc_log_mdns_snapshot_age "$APPLE_MDNS_SNAPSHOT"; then
            :
        else
            tc_log "mdns advertiser will fall back to generated records"
        fi
    fi

    if [ "$kill_prior" = "1" ]; then
        tc_log "$context: killing prior $MDNS_PROC_NAME processes"
        stop_runtime_process_by_ucomm "$MDNS_PROC_NAME" "$MDNS_PROC_NAME" || true
    fi

    tc_log "$context: starting mdns advertiser for $iface_ip on $NET_IFACE"
    set -- "$TC_MDNS_BIN" \
        --load-snapshot "$APPLE_MDNS_SNAPSHOT" \
        --instance "$MDNS_INSTANCE_NAME" \
        --host "$MDNS_HOST_LABEL" \
        --device-model "$MDNS_DEVICE_MODEL"
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
    if [ -s "$TC_ADISK_TSV" ]; then
        set -- "$@" \
            --adisk-shares-file "$TC_ADISK_TSV" \
            --adisk-sys-wama "$iface_mac"
    fi
    set -- "$@" --ipv4 "$iface_ip"

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
    tc_launch_mdns_advertiser "mdns startup" 0 1 100
}

tc_restart_mdns() {
    tc_launch_mdns_advertiser "watchdog recovery" 0 0 0
}

tc_launch_nbns() {
    context=$1
    wait_attempts=$2

    if [ "$NBNS_ENABLED" != "1" ]; then
        tc_log "$context: nbns responder skipped; disabled in $TC_CONFIG_FILE"
        return 0
    fi

    if [ ! -x "$TC_NBNS_BIN" ]; then
        tc_log "$context: nbns responder launch skipped; missing runtime binary"
        return 0
    fi

    iface_ip=${TC_NET_IFACE_IP:-}
    if [ -z "$iface_ip" ]; then
        iface_ip=$(get_iface_ipv4 "$NET_IFACE" || true)
    fi
    if [ -z "$iface_ip" ] || [ "$iface_ip" = "0.0.0.0" ]; then
        tc_log "$context: nbns launch skipped because $NET_IFACE has no IPv4 address"
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

    tc_log "$context: starting nbns responder for $SMB_NETBIOS_NAME at $iface_ip"
    set -- "$TC_NBNS_BIN" \
        --name "$SMB_NETBIOS_NAME" \
        --ipv4 "$iface_ip"
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

tc_start_nbns() {
    tc_launch_nbns "nbns startup" 10
}

tc_restart_nbns() {
    tc_launch_nbns "watchdog recovery" 0
}

tc_start_smbd() {
    tc_log "starting smbd from $TC_SMBD_BIN with config $TC_SMBD_CONF"
    "$TC_SMBD_BIN" -D -s "$TC_SMBD_CONF"
    if wait_for_process smbd 15; then
        return 0
    fi
    tc_log "smbd process was not observed after launch"
    return 1
}

tc_start_smbd_if_needed() {
    if runtime_process_present_by_ucomm smbd; then
        return 0
    fi

    if [ ! -x "$TC_SMBD_BIN" ] || [ ! -f "$TC_SMBD_CONF" ]; then
        tc_log "watchdog recovery: smbd is not running, but runtime is not staged yet"
        return 0
    fi

    rm -rf "$LOCKS_ROOT"/* >/dev/null 2>&1 || true
    "$TC_SMBD_BIN" -D -s "$TC_SMBD_CONF" >/dev/null 2>&1 || true
    tc_log "watchdog recovery: smbd restart requested"
}

tc_start_watchdog() {
    if runtime_watchdog_present; then
        tc_log "watchdog already running"
        return 0
    fi

    tc_log "starting watchdog"
    /mnt/Flash/watchdog.sh </dev/null >/dev/null 2>&1 &
    watchdog_pid=$!
    tc_log "watchdog launched as pid $watchdog_pid"
}

tc_stop_managed_services() {
    stop_runtime_process_by_ucomm "smbd" "smbd" || true
    stop_runtime_process_by_ucomm "$MDNS_PROC_NAME" "$MDNS_PROC_NAME" || true
    stop_runtime_process_by_ucomm "$NBNS_PROC_NAME" "$NBNS_PROC_NAME" || true
}

tc_current_topology_signature() {
    [ -f "$TC_TOPOLOGY_SIGNATURE" ] || return 1
    /bin/cat "$TC_TOPOLOGY_SIGNATURE"
}

tc_fresh_topology_signature() {
    /mnt/Flash/start-samba.sh --print-topology-signature 2>/dev/null
}

tc_topology_changed() {
    current=$(tc_current_topology_signature || true)
    fresh=$(tc_fresh_topology_signature || true)
    if [ -z "$fresh" ]; then
        tc_log "watchdog recovery: MaSt topology check failed"
        return 1
    fi
    [ "$current" != "$fresh" ]
}

tc_topology_changed_debounced() {
    if ! tc_topology_changed; then
        return 1
    fi

    tc_log "watchdog recovery: MaSt topology changed; debouncing 5s"
    sleep 5
    if tc_topology_changed; then
        return 0
    fi

    tc_log "watchdog recovery: MaSt topology change cleared after debounce"
    return 1
}

tc_exec_start_samba() {
    reason=$1
    tc_log "watchdog recovery: re-execing start-samba.sh: $reason"
    exec /mnt/Flash/start-samba.sh --reload-disk-runtime
}

tc_nbns_enabled() {
    [ "$NBNS_ENABLED" = "1" ]
}

tc_all_managed_services_healthy() {
    if ! runtime_process_present_by_ucomm smbd; then
        return 1
    fi

    if ! runtime_process_present_by_ucomm "$MDNS_PROC_NAME"; then
        return 1
    fi

    if tc_nbns_enabled; then
        if ! runtime_process_present_by_ucomm "$NBNS_PROC_NAME"; then
            return 1
        fi
    fi

    return 0
}

tc_watchdog_iteration() {
    tc_log "watchdog pass: checking topology, payload, active shares, and managed services"

    if tc_topology_changed_debounced; then
        tc_exec_start_samba "MaSt topology changed"
    fi

    if tc_payload_available; then
        tc_log "watchdog pass: payload available at ${TC_PAYLOAD_DIR:-unknown}"
        if tc_mount_active_volumes_from_state; then
            :
        else
            active_mount_status=$?
            if [ "$active_mount_status" -eq 2 ]; then
                tc_exec_start_samba "active share state unavailable"
            fi
            tc_log "watchdog recovery: active share volume unavailable; stopping managed services and retrying"
            tc_stop_managed_services
            return 1
        fi
        tc_start_smbd_if_needed
    else
        tc_log "watchdog recovery: payload unavailable; stopping managed services"
        tc_stop_managed_services
        return 1
    fi

    if runtime_process_present_by_ucomm "$MDNS_PROC_NAME"; then
        :
    else
        tc_restart_mdns
    fi

    if tc_nbns_enabled; then
        if runtime_process_present_by_ucomm "$NBNS_PROC_NAME"; then
            :
        else
            tc_restart_nbns
        fi
    fi

    if tc_all_managed_services_healthy; then
        tc_log "watchdog pass: healthy"
        return 0
    fi

    tc_log "watchdog pass: one or more managed services are unhealthy"
    return 1
}

tc_sleep_with_runtime_checks() {
    total_sleep=$1
    slept=0
    mount_poll_seconds=${MOUNT_POLL_SECONDS:-30}

    while [ "$slept" -lt "$total_sleep" ]; do
        sleep_seconds=$mount_poll_seconds
        remaining=$((total_sleep - slept))
        if [ "$remaining" -lt "$sleep_seconds" ]; then
            sleep_seconds=$remaining
        fi

        sleep "$sleep_seconds"
        slept=$((slept + sleep_seconds))
        if tc_payload_available; then
            :
        else
            tc_log "watchdog steady check: payload unavailable while sleeping"
            return 1
        fi
        if tc_mount_active_volumes_from_state; then
            :
        else
            active_mount_status=$?
            if [ "$active_mount_status" -eq 2 ]; then
                tc_log "watchdog steady check: active share state unavailable while sleeping"
            else
                tc_log "watchdog steady check: one or more active share volumes are unavailable while sleeping"
            fi
            return "$active_mount_status"
        fi
        tc_log "watchdog steady check: healthy after ${slept}s of ${total_sleep}s"
    done
    return 0
}
