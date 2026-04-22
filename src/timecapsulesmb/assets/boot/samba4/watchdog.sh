#!/bin/sh
set -eu

PATH=/bin:/sbin:/usr/bin:/usr/sbin

. /mnt/Flash/common.sh

WATCHDOG_LOG="$RAM_VAR/watchdog.log"
MDNS_LOG_ENABLED=__MDNS_LOG_ENABLED__
MDNS_LOG_FILE=__MDNS_LOG_FILE__
SMBD_BIN="$RAM_SBIN/smbd"
SMBD_CONF="$RAM_ETC/smb.conf"
MDNS_BIN=/mnt/Flash/mdns-advertiser
NBNS_BIN="$RAM_SBIN/nbns-advertiser"

NET_IFACE=__NET_IFACE__
SMB_SHARE_NAME=__SMB_SHARE_NAME__
SMB_NETBIOS_NAME=__SMB_NETBIOS_NAME__
MDNS_INSTANCE_NAME=__MDNS_INSTANCE_NAME__
MDNS_HOST_LABEL=__MDNS_HOST_LABEL__
MDNS_DEVICE_MODEL=__MDNS_DEVICE_MODEL__
AIRPORT_SYAP=__AIRPORT_SYAP__
ADISK_DISK_KEY=__ADISK_DISK_KEY__
ADISK_UUID=__ADISK_UUID__

RECOVERY_POLL_SECONDS=10
STEADY_POLL_SECONDS=300
INITIAL_STARTUP_DELAY_SECONDS=30
SNAPSHOT_BOOTSTRAP_GRACE_SECONDS=120
WATCHDOG_START_TS=$(/bin/date +%s)

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

start_smbd_if_needed() {
    if /usr/bin/pkill -0 smbd >/dev/null 2>&1; then
        return 0
    fi

    if [ ! -x "$SMBD_BIN" ] || [ ! -f "$SMBD_CONF" ]; then
        log "smbd missing and runtime not ready"
        return 0
    fi

    rm -rf "$LOCKS_ROOT"/* >/dev/null 2>&1 || true
    "$SMBD_BIN" -D -s "$SMBD_CONF" >/dev/null 2>&1 || true
    log "smbd restart requested"
}

restart_mdns() {
    if [ ! -x "$MDNS_BIN" ]; then
        return 0
    fi

    if [ ! -f "$APPLE_MDNS_SNAPSHOT" ] && [ "$elapsed" -lt "$SNAPSHOT_BOOTSTRAP_GRACE_SECONDS" ]; then
        log "mdns restart deferred; waiting for startup snapshot bootstrap"
        return 0
    fi

    iface_ip=$(get_iface_ipv4 "$NET_IFACE" || true)
    iface_mac=$(get_iface_mac "$NET_IFACE" || true)
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

    set -- "$MDNS_BIN" \
        --instance "$MDNS_INSTANCE_NAME" \
        --host "$MDNS_HOST_LABEL" \
        --device-model "$MDNS_DEVICE_MODEL"
    if [ -f "$APPLE_MDNS_SNAPSHOT" ]; then
        set -- "$@" --load-snapshot "$APPLE_MDNS_SNAPSHOT"
    else
        set -- "$@" \
            --save-all-snapshot "$ALL_MDNS_SNAPSHOT" \
            --save-snapshot "$APPLE_MDNS_SNAPSHOT" \
            --load-snapshot "$APPLE_MDNS_SNAPSHOT"
    fi
    if derive_airport_fields "$iface_mac"; then
        set -- "$@" \
            --airport-wama "$AIRPORT_WAMA" \
            --airport-rama "$AIRPORT_RAMA" \
            --airport-ram2 "$AIRPORT_RAM2" \
            --airport-syvs "$AIRPORT_SYVS" \
            --airport-srcv "$AIRPORT_SRCV"
        if [ -n "$AIRPORT_SYAP" ]; then
            set -- "$@" --airport-syap "$AIRPORT_SYAP"
        else
            log "airport syAP missing; advertising _airport._tcp without syAP"
        fi
    else
        log "airport clone fields incomplete; skipping _airport._tcp advertisement"
    fi
    set -- "$@" \
        --adisk-share "$SMB_SHARE_NAME" \
        --adisk-disk-key "$ADISK_DISK_KEY" \
        --adisk-uuid "$ADISK_UUID" \
        --adisk-sys-wama "$iface_mac" \
        --ipv4 "$iface_ip"
    if [ "$MDNS_LOG_ENABLED" = "1" ]; then
        trim_log_file "$MDNS_LOG_FILE" 131072
        printf '%s watchdog: launching mdns-advertiser\n' "$(date '+%Y-%m-%d %H:%M:%S')" >>"$MDNS_LOG_FILE"
        "$@" >>"$MDNS_LOG_FILE" 2>&1 &
    else
        "$@" >/dev/null 2>&1 &
    fi
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

    iface_ip=$(get_iface_ipv4 "$NET_IFACE" || true)
    if [ -z "$iface_ip" ] || [ "$iface_ip" = "0.0.0.0" ]; then
        log "nbns restart skipped; missing $NET_IFACE IPv4"
        return 0
    fi

    /usr/bin/pkill wcifsnd >/dev/null 2>&1 || true
    /usr/bin/pkill wcifsfs >/dev/null 2>&1 || true
    /usr/bin/pkill "$NBNS_PROC_NAME" >/dev/null 2>&1 || true
    sleep 1

    "$NBNS_BIN" \
        --name "$SMB_NETBIOS_NAME" \
        --ipv4 "$iface_ip" \
        >/dev/null 2>&1 &
    log "nbns restart requested"
}

nbns_enabled() {
    [ -f "$RAM_PRIVATE/nbns.enabled" ]
}

all_managed_services_healthy() {
    if ! /usr/bin/pkill -0 smbd >/dev/null 2>&1; then
        return 1
    fi

    if ! /usr/bin/pkill -0 "$MDNS_PROC_NAME" >/dev/null 2>&1; then
        return 1
    fi

    if nbns_enabled; then
        if ! /usr/bin/pkill -0 "$NBNS_PROC_NAME" >/dev/null 2>&1; then
            return 1
        fi
    fi

    return 0
}

elapsed=0
log "watchdog start"
sleep "$INITIAL_STARTUP_DELAY_SECONDS"

while :; do
    now_ts=$(/bin/date +%s)
    elapsed=$((now_ts - WATCHDOG_START_TS))
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

    if all_managed_services_healthy; then
        sleep "$STEADY_POLL_SECONDS"
    else
        sleep "$RECOVERY_POLL_SECONDS"
    fi
done
