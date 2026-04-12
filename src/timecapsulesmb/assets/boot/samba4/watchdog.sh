#!/bin/sh
set -eu

PATH=/bin:/sbin:/usr/bin:/usr/sbin

RAM_ROOT=/mnt/Memory/samba4
RAM_SBIN="$RAM_ROOT/sbin"
RAM_ETC="$RAM_ROOT/etc"
RAM_VAR="$RAM_ROOT/var"
RAM_PRIVATE="$RAM_ROOT/private"
WATCHDOG_LOG="$RAM_VAR/watchdog.log"
SMBD_BIN="$RAM_SBIN/smbd"
SMBD_CONF="$RAM_ETC/smb.conf"
MDNS_BIN="$RAM_SBIN/mdns-smbd-advertiser"
MDNS_PROC_NAME=mdns-smbd-advert
NBNS_BIN="$RAM_SBIN/nbns-advertiser"
NBNS_PROC_NAME=nbns-advertiser

NET_IFACE=__NET_IFACE__
SMB_SHARE_NAME=__SMB_SHARE_NAME__
SMB_NETBIOS_NAME=__SMB_NETBIOS_NAME__
MDNS_INSTANCE_NAME=__MDNS_INSTANCE_NAME__
MDNS_HOST_LABEL=__MDNS_HOST_LABEL__
MDNS_DEVICE_MODEL=__MDNS_DEVICE_MODEL__
ADISK_DISK_KEY=__ADISK_DISK_KEY__
ADISK_UUID=__ADISK_UUID__

POLL_SECONDS=300

log() {
    log_dir=${WATCHDOG_LOG%/*}
    tmp_log="$WATCHDOG_LOG.tmp.$$"
    line="$(date '+%Y-%m-%d %H:%M:%S') watchdog: $*"

    [ -d "$log_dir" ] || mkdir -p "$log_dir"
    {
        if [ -f "$WATCHDOG_LOG" ]; then
            /usr/bin/tail -n 255 "$WATCHDOG_LOG" 2>/dev/null || true
        fi
        echo "$line"
    } >"$tmp_log"
    mv "$tmp_log" "$WATCHDOG_LOG"
}

get_iface_ipv4() {
    /sbin/ifconfig "$NET_IFACE" 2>/dev/null | sed -n 's/^[[:space:]]*inet[[:space:]]\([0-9.]*\).*/\1/p' | sed -n '1p'
}

get_iface_mac() {
    /sbin/ifconfig "$NET_IFACE" 2>/dev/null \
        | sed -n \
            -e 's/^[[:space:]]*ether[[:space:]]\([0-9A-Fa-f:]*\).*/\1/p' \
            -e 's/^[[:space:]]*address[[:space:]]\([0-9A-Fa-f:]*\).*/\1/p' \
        | sed -n '1p'
}

start_smbd_if_needed() {
    if /usr/bin/pkill -0 smbd >/dev/null 2>&1; then
        return 0
    fi

    if [ ! -x "$SMBD_BIN" ] || [ ! -f "$SMBD_CONF" ]; then
        log "smbd missing and runtime not ready"
        return 0
    fi

    "$SMBD_BIN" -D -s "$SMBD_CONF" >/dev/null 2>&1 || true
    log "smbd restart requested"
}

restart_mdns() {
    if [ ! -x "$MDNS_BIN" ]; then
        return 0
    fi

    iface_ip=$(get_iface_ipv4 || true)
    iface_mac=$(get_iface_mac || true)
    if [ -z "$iface_ip" ] || [ "$iface_ip" = "0.0.0.0" ]; then
        log "mdns restart skipped; missing $NET_IFACE IPv4"
        return 0
    fi
    if [ -z "$iface_mac" ]; then
        log "mdns restart skipped; missing $NET_IFACE MAC address"
        return 0
    fi

    /usr/bin/pkill "$MDNS_PROC_NAME" >/dev/null 2>&1 || true
    sleep 1

    "$MDNS_BIN" \
        --instance "$MDNS_INSTANCE_NAME" \
        --host "$MDNS_HOST_LABEL" \
        --device-model "$MDNS_DEVICE_MODEL" \
        --adisk-share "$SMB_SHARE_NAME" \
        --adisk-disk-key "$ADISK_DISK_KEY" \
        --adisk-uuid "$ADISK_UUID" \
        --adisk-sys-wama "$iface_mac" \
        --ipv4 "$iface_ip" \
        >/dev/null 2>&1 &
    log "mdns restart requested"
}

restart_nbns() {
    if [ ! -f "$RAM_PRIVATE/nbns.enabled" ]; then
        return 0
    fi

    if [ ! -x "$NBNS_BIN" ]; then
        log "nbns restart skipped; missing runtime binary"
        return 0
    fi

    iface_ip=$(get_iface_ipv4 || true)
    if [ -z "$iface_ip" ] || [ "$iface_ip" = "0.0.0.0" ]; then
        log "nbns restart skipped; missing $NET_IFACE IPv4"
        return 0
    fi

    /usr/bin/pkill "$NBNS_PROC_NAME" >/dev/null 2>&1 || true
    sleep 1

    "$NBNS_BIN" \
        --name "$SMB_NETBIOS_NAME" \
        --ipv4 "$iface_ip" \
        >/dev/null 2>&1 &
    log "nbns restart requested"
}

elapsed=0
log "watchdog start"

while :; do
    start_smbd_if_needed

    if /usr/bin/pkill -0 "$MDNS_PROC_NAME" >/dev/null 2>&1; then
        :
    else
        restart_mdns
    fi

    if /usr/bin/pkill -0 "$NBNS_PROC_NAME" >/dev/null 2>&1; then
        :
    else
        restart_nbns
    fi

    sleep "$POLL_SECONDS"
done
