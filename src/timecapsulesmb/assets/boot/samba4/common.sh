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
        if /usr/bin/pkill -0 "$proc_name" >/dev/null 2>&1; then
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
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
