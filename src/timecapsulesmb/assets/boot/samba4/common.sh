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

get_airport_syvs() {
    airport_srcv=$1
    digits=$(printf '%s' "$airport_srcv" | sed -n 's/^\([0-9]\)\([0-9]\)\([0-9]\).*/\1.\2.\3/p')
    if [ -n "$digits" ]; then
        echo "$digits"
        return 0
    fi
    return 1
}

wait_for_process() {
    proc_name=$1
    max_attempts=${2:-10}
    attempt=0
    while [ "$attempt" -lt "$max_attempts" ]; do
        if runtime_process_present "$proc_name" false; then
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    return 1
}

runtime_process_present() {
    pattern=$1
    full_match=${2:-false}

    if ps_out=$(/bin/ps axww -o stat= -o ucomm= -o command= 2>/dev/null); then
        old_ifs=$IFS
        IFS='
'
        for line in $ps_out; do
            [ -n "$line" ] || continue
            line_ifs=$IFS
            IFS=' 	'
            set -- $line
            IFS=$line_ifs
            [ "$#" -ge 2 ] || continue

            # NetBSD leaves killed Apple CIFS helpers as zombies until init
            # reaps them. Zombies do not hold UDP 137, so they must not block
            # the managed NBNS responder takeover path.
            case "$1" in
                Z*)
                    continue
                    ;;
            esac

            if [ "$full_match" = "true" ]; then
                case "$line" in
                    *"$pattern"*)
                        IFS=$old_ifs
                        return 0
                        ;;
                esac
            else
                if [ "$2" = "$pattern" ]; then
                    IFS=$old_ifs
                    return 0
                fi
            fi
        done
        IFS=$old_ifs
    fi

    return 1
}

wait_for_runtime_process_absent() {
    pattern=$1
    full_match=${2:-false}
    max_attempts=${3:-5}
    attempt=0

    while runtime_process_present "$pattern" "$full_match"; do
        if [ "$attempt" -ge "$max_attempts" ]; then
            return 1
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    return 0
}

stop_runtime_process() {
    label=$1
    pattern=$2
    full_match=${3:-false}

    log "stopping old $label"
    if [ "$full_match" = "true" ]; then
        /usr/bin/pkill -f "$pattern" >/dev/null 2>&1 || true
    else
        /usr/bin/pkill "$pattern" >/dev/null 2>&1 || true
    fi

    if wait_for_runtime_process_absent "$pattern" "$full_match" 5; then
        return 0
    fi

    log "old $label still running after TERM; sending KILL"
    if [ "$full_match" = "true" ]; then
        /usr/bin/pkill -9 -f "$pattern" >/dev/null 2>&1 || true
    else
        /usr/bin/pkill -9 "$pattern" >/dev/null 2>&1 || true
    fi

    if wait_for_runtime_process_absent "$pattern" "$full_match" 5; then
        return 0
    fi

    log "old $label survived KILL"
    return 1
}

stop_nbns_conflicts() {
    cleanup_status=0

    stop_runtime_process "wcifsnd" "wcifsnd" false || cleanup_status=1
    stop_runtime_process "wcifsfs" "wcifsfs" false || cleanup_status=1
    stop_runtime_process "$NBNS_PROC_NAME" "$NBNS_PROC_NAME" false || cleanup_status=1

    return "$cleanup_status"
}

is_volume_root_mounted() {
    volume_root=$1
    df_line=$(/bin/df -k "$volume_root" 2>/dev/null | /usr/bin/tail -n +2 || true)
    case "$df_line" in
        *" $volume_root")
            return 0
            ;;
    esac
    return 1
}

append_disk_candidate() {
    candidate=$1
    case " $DISK_CANDIDATES " in
        *" $candidate "*)
            ;;
        *)
            DISK_CANDIDATES="$DISK_CANDIDATES $candidate"
            ;;
    esac
}

disk_name_candidates() {
    # Keep this candidate order in sync with src/timecapsulesmb/device/util.py.
    DISK_CANDIDATES=""
    dmesg_disk_lines=$(/sbin/dmesg 2>/dev/null | /usr/bin/sed -n '/^dk[0-9][0-9]* at /p' || true)
    metadata_wedges=""
    for dev in $(echo "$dmesg_disk_lines" | /usr/bin/sed -n 's/^\(dk[0-9][0-9]*\) at .*: APconfig$/\1/p;s/^\(dk[0-9][0-9]*\) at .*: APswap$/\1/p'); do
        metadata_wedges="$metadata_wedges $dev"
    done

    for dev in $(echo "$dmesg_disk_lines" | /usr/bin/sed -n 's/^\(dk[0-9][0-9]*\) at .*: APdata$/\1/p'); do
        append_disk_candidate "$dev"
    done

    for dev in $(/sbin/sysctl -n hw.disknames 2>/dev/null); do
        case "$dev" in
            dk[0-9]*)
                case " $metadata_wedges " in
                    *" $dev "*)
                        ;;
                    *)
                        append_disk_candidate "$dev"
                        ;;
                esac
                ;;
        esac
    done

    if [ -z "$DISK_CANDIDATES" ]; then
        DISK_CANDIDATES=" dk2 dk3"
    fi

    echo "$DISK_CANDIDATES"
}

volume_root_candidates() {
    roots=""
    for dev in "$@"; do
        roots="$roots /Volumes/$dev"
    done
    echo "$roots"
}

mount_candidates() {
    candidates=""
    for dev in "$@"; do
        candidates="$candidates /dev/$dev:/Volumes/$dev"
    done
    echo "$candidates"
}

log_disk_discovery_state() {
    disk_candidates=$1

    disk_names=$(/sbin/sysctl -n hw.disknames 2>/dev/null || true)
    if [ -n "$disk_names" ]; then
        log "disk discovery: hw.disknames=$disk_names"
    else
        log "disk discovery: hw.disknames unavailable"
    fi

    disk_lines=$(/sbin/dmesg 2>/dev/null | /usr/bin/sed -n '/^wd[0-9]/p;/^sd[0-9]/p;/^ld[0-9]/p;/^dk[0-9]/p' || true)
    if [ -n "$disk_lines" ]; then
        old_ifs=$IFS
        IFS='
'
        for disk_line in $disk_lines; do
            log "disk discovery: dmesg: $disk_line"
        done
        IFS=$old_ifs
    else
        log "disk discovery: no wd/sd/ld/dk dmesg lines available"
    fi

    volume_candidates=$(volume_root_candidates $disk_candidates)
    mount_candidate_list=$(mount_candidates $disk_candidates)
    log "disk discovery: disk candidates:${disk_candidates:- none}"
    log "disk discovery: volume root candidates:${volume_candidates:- none}"
    log "disk discovery: mount candidates:${mount_candidate_list:- none}"
}

find_existing_data_root() {
    disk_candidates=$1
    for volume_root in $(volume_root_candidates $disk_candidates); do
        if is_volume_root_mounted "$volume_root" && data_root=$(find_data_root_under_volume "$volume_root"); then
            echo "$data_root"
            return 0
        fi
    done

    return 1
}

find_existing_volume_root() {
    disk_candidates=$1
    for volume_root in $(volume_root_candidates $disk_candidates); do
        if is_volume_root_mounted "$volume_root"; then
            echo "$volume_root"
            return 0
        fi
    done

    return 1
}

find_data_root_under_volume() {
    volume_root=$1

    if [ -f "$volume_root/ShareRoot/.com.apple.timemachine.supported" ]; then
        log "data root match: $volume_root/ShareRoot marker"
        echo "$volume_root/ShareRoot"
        return 0
    fi

    if [ -f "$volume_root/Shared/.com.apple.timemachine.supported" ]; then
        log "data root match: $volume_root/Shared marker"
        echo "$volume_root/Shared"
        return 0
    fi

    if [ -d "$volume_root/ShareRoot" ]; then
        log "data root match: $volume_root/ShareRoot directory"
        echo "$volume_root/ShareRoot"
        return 0
    fi

    if [ -d "$volume_root/Shared" ]; then
        log "data root match: $volume_root/Shared directory"
        echo "$volume_root/Shared"
        return 0
    fi

    return 1
}

mount_hfs_bounded() {
    dev_path=$1
    volume_root=$2
    timeout_seconds=${3:-30}
    mount_context=${4:-mount candidate}
    created_mountpoint=0

    if [ ! -b "$dev_path" ]; then
        log "$mount_context skipped; missing block device $dev_path"
        return 1
    fi

    if [ ! -d "$volume_root" ]; then
        mkdir -p "$volume_root"
        created_mountpoint=1
        log "created mountpoint $volume_root for $dev_path"
    fi

    log "launching mount_hfs for $dev_path at $volume_root"
    /sbin/mount_hfs "$dev_path" "$volume_root" >/dev/null 2>&1 &
    mount_pid=$!
    attempt=0
    while kill -0 "$mount_pid" >/dev/null 2>&1; do
        if [ "$attempt" -ge "$timeout_seconds" ]; then
            kill "$mount_pid" >/dev/null 2>&1 || true
            sleep 1
            kill -9 "$mount_pid" >/dev/null 2>&1 || true
            wait "$mount_pid" >/dev/null 2>&1 || true
            log "mount_hfs command did not exit promptly for $dev_path at $volume_root; re-checking mount state"
            if is_volume_root_mounted "$volume_root"; then
                log "mount_hfs command timed out, but volume is mounted"
                return 0
            fi
            if [ "$created_mountpoint" -eq 1 ]; then
                /bin/rmdir "$volume_root" >/dev/null 2>&1 || true
            fi
            log "mount_hfs timed out for $dev_path at $volume_root and volume was not mounted at the immediate re-check"
            return 1
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    wait "$mount_pid" >/dev/null 2>&1 || true

    if is_volume_root_mounted "$volume_root"; then
        log "mounted $dev_path at $volume_root after ${attempt}s"
        return 0
    fi

    if [ "$created_mountpoint" -eq 1 ]; then
        /bin/rmdir "$volume_root" >/dev/null 2>&1 || true
    fi

    log "mount_hfs exited for $dev_path at $volume_root, but volume is not mounted"
    return 1
}

try_mount_candidate() {
    dev_path=$1
    volume_root=$2

    if is_volume_root_mounted "$volume_root"; then
        echo "$volume_root"
        return 0
    fi

    mount_hfs_bounded "$dev_path" "$volume_root" 30 "mount candidate" || true
    if is_volume_root_mounted "$volume_root"; then
        log "mount candidate succeeded: $dev_path at $volume_root"
        echo "$volume_root"
        return 0
    fi

    log "mount candidate failed: $dev_path at $volume_root"
    return 1
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

    AIRPORT_WAMA=
    AIRPORT_RAMA=$(get_radio_mac bwl0 || true)
    AIRPORT_RAM2=$(get_radio_mac bwl1 || true)
    AIRPORT_SRCV=$(get_airport_srcv || true)
    AIRPORT_SYVS=$(get_airport_syvs "$AIRPORT_SRCV" || true)
    AIRPORT_WAMA=$iface_mac

    if [ -n "$AIRPORT_WAMA" ] || [ -n "$AIRPORT_RAMA" ] || [ -n "$AIRPORT_RAM2" ] || [ -n "$AIRPORT_SRCV" ] || [ -n "$AIRPORT_SYVS" ]; then
        return 0
    fi
    return 1
}
