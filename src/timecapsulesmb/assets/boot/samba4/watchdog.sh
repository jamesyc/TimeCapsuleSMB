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
ADISK_DISK_ADVF=0x82
ADISK_UUID=__ADISK_UUID__
VOLUME_DEVICE=${1:-}
VOLUME_ROOT=${2:-}
DATA_ROOT=${3:-}

RECOVERY_POLL_SECONDS=10
MOUNT_POLL_SECONDS=30
STEADY_POLL_SECONDS=300
INITIAL_STARTUP_DELAY_SECONDS=${4:-0}

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

ensure_data_volume_mounted() {
    if [ -z "$VOLUME_DEVICE" ] || [ -z "$VOLUME_ROOT" ]; then
        return 0
    fi

    if is_volume_root_mounted "$VOLUME_ROOT"; then
        return 0
    fi

    log "watchdog recovery: data volume $VOLUME_ROOT is unmounted; mounting $VOLUME_DEVICE"
    if mount_hfs_bounded "$VOLUME_DEVICE" "$VOLUME_ROOT" 30 "watchdog recovery"; then
        log "watchdog recovery: mounted $VOLUME_DEVICE at $VOLUME_ROOT"
        return 0
    else
        log "watchdog recovery: failed to mount $VOLUME_DEVICE at $VOLUME_ROOT"
        return 1
    fi
}

sleep_with_mount_checks() {
    total_sleep=$1
    slept=0

    while [ "$slept" -lt "$total_sleep" ]; do
        sleep_seconds=$MOUNT_POLL_SECONDS
        remaining=$((total_sleep - slept))
        if [ "$remaining" -lt "$sleep_seconds" ]; then
            sleep_seconds=$remaining
        fi

        sleep "$sleep_seconds"
        slept=$((slept + sleep_seconds))
        ensure_data_volume_mounted || true
    done
}

start_smbd_if_needed() {
    if runtime_process_present smbd false; then
        return 0
    fi

    if [ ! -x "$SMBD_BIN" ] || [ ! -f "$SMBD_CONF" ]; then
        log "watchdog recovery: smbd is not running, but runtime is not staged yet"
        return 0
    fi

    rm -rf "$LOCKS_ROOT"/* >/dev/null 2>&1 || true
    "$SMBD_BIN" -D -s "$SMBD_CONF" >/dev/null 2>&1 || true
    log "watchdog recovery: smbd restart requested"
}

restart_mdns() {
    if [ ! -x "$MDNS_BIN" ]; then
        return 0
    fi

    iface_ip=$(get_iface_ipv4 "$NET_IFACE" || true)
    iface_mac=$(get_iface_mac "$NET_IFACE" || true)
    if [ -z "$iface_ip" ] || [ "$iface_ip" = "0.0.0.0" ]; then
        log "watchdog recovery: mdns restart skipped because $NET_IFACE has no IPv4 address"
        return 0
    fi
    if [ -z "$iface_mac" ]; then
        log "watchdog recovery: mdns restart skipped because $NET_IFACE has no MAC address"
        return 0
    fi

    /usr/bin/pkill "$MDNS_PROC_NAME" >/dev/null 2>&1 || true
    sleep 1

    set -- "$MDNS_BIN" \
        --load-snapshot "$APPLE_MDNS_SNAPSHOT" \
        --instance "$MDNS_INSTANCE_NAME" \
        --host "$MDNS_HOST_LABEL" \
        --device-model "$MDNS_DEVICE_MODEL"
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
        --adisk-disk-advf "$ADISK_DISK_ADVF" \
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
    log "watchdog recovery: mdns restart requested"
}

restart_nbns() {
    if [ ! -f "$RAM_PRIVATE/nbns.enabled" ]; then
        return 0
    fi

    if [ ! -x "$NBNS_BIN" ]; then
        log "watchdog recovery: nbns restart skipped because runtime binary is missing"
        return 0
    fi

    iface_ip=$(get_iface_ipv4 "$NET_IFACE" || true)
    if [ -z "$iface_ip" ] || [ "$iface_ip" = "0.0.0.0" ]; then
        log "watchdog recovery: nbns restart skipped because $NET_IFACE has no IPv4 address"
        return 0
    fi

    if ! stop_nbns_conflicts; then
        log "watchdog recovery: nbns restart skipped because conflicting Apple CIFS/NBNS processes still running"
        return 0
    fi

    "$NBNS_BIN" \
        --name "$SMB_NETBIOS_NAME" \
        --ipv4 "$iface_ip" \
        >/dev/null 2>&1 &
    log "watchdog recovery: nbns restart requested"
}

nbns_enabled() {
    [ -f "$RAM_PRIVATE/nbns.enabled" ]
}

all_managed_services_healthy() {
    if ! runtime_process_present smbd false; then
        return 1
    fi

    if ! runtime_process_present "$MDNS_PROC_NAME" false; then
        return 1
    fi

    if nbns_enabled; then
        if ! runtime_process_present "$NBNS_PROC_NAME" false; then
            return 1
        fi
    fi

    return 0
}

watchdog_iteration() {
    if ensure_data_volume_mounted; then
        start_smbd_if_needed
    else
        log "watchdog recovery: smbd restart skipped because data volume is unavailable"
    fi

    if runtime_process_present "$MDNS_PROC_NAME" false; then
        :
    else
        restart_mdns
    fi

    if runtime_process_present "$NBNS_PROC_NAME" false; then
        :
    else
        restart_nbns
    fi

    all_managed_services_healthy
}

log "watchdog startup beginning; initial recovery delay ${INITIAL_STARTUP_DELAY_SECONDS}s"
log "watchdog mount context: device=${VOLUME_DEVICE:-none} root=${VOLUME_ROOT:-none} data=${DATA_ROOT:-none}"
sleep "$INITIAL_STARTUP_DELAY_SECONDS"

while :; do
    if watchdog_iteration; then
        sleep_with_mount_checks "$STEADY_POLL_SECONDS"
    else
        sleep "$RECOVERY_POLL_SECONDS"
    fi
done
