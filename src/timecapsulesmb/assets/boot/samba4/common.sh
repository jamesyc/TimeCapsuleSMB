#!/bin/sh

PATH=/bin:/sbin:/usr/bin:/usr/sbin

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

wait_for_smbd_ready() {
    smbd_log_path=$1
    max_attempts=${2:-15}
    attempt=0
    while [ "$attempt" -lt "$max_attempts" ]; do
        if [ -f "$smbd_log_path" ]; then
            smbd_log=$(/bin/cat "$smbd_log_path" 2>/dev/null || true)
            case "$smbd_log" in
                *daemon_ready*)
                    return 0
                    ;;
            esac
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    return 1
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
