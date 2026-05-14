#!/bin/sh
set -eu

PATH=/bin:/sbin:/usr/bin:/usr/sbin

. /mnt/Flash/common.sh
. /mnt/Flash/tcapsulesmb.conf

tc_init_runtime_env
tc_set_log "$RAM_VAR/watchdog.log" "watchdog"
tc_cleanup_watchdog_mast_temp_files
tc_init_runtime_identity

WATCHDOG_DISK_POLL_SECONDS=$(tc_sanitize_positive_integer "${WATCHDOG_DISK_POLL_SECONDS:-10}" 10)
WATCHDOG_SERVICE_POLL_SECONDS=$(tc_sanitize_positive_integer "${WATCHDOG_SERVICE_POLL_SECONDS:-30}" 30)
WATCHDOG_RECOVERY_POLL_SECONDS=$(tc_sanitize_positive_integer "${WATCHDOG_RECOVERY_POLL_SECONDS:-10}" 10)
WATCHDOG_SERVICE_TICKS=$(( (WATCHDOG_SERVICE_POLL_SECONDS + WATCHDOG_DISK_POLL_SECONDS - 1) / WATCHDOG_DISK_POLL_SECONDS ))
if [ "$WATCHDOG_SERVICE_TICKS" -lt 1 ]; then
    WATCHDOG_SERVICE_TICKS=1
fi

tc_log "watchdog startup beginning"
if tc_read_payload_state; then
    :
else
    TC_PAYLOAD_DIR=
    TC_PAYLOAD_VOLUME=
    TC_PAYLOAD_DEVICE=
fi
tc_log "watchdog payload: device=${TC_PAYLOAD_DEVICE:-none} root=${TC_PAYLOAD_VOLUME:-none} dir=${TC_PAYLOAD_DIR:-none}"
tc_log "watchdog intervals: disk=${WATCHDOG_DISK_POLL_SECONDS}s service=${WATCHDOG_SERVICE_POLL_SECONDS}s recovery=${WATCHDOG_RECOVERY_POLL_SECONDS}s service_ticks=$WATCHDOG_SERVICE_TICKS"

watchdog_service_tick=$WATCHDOG_SERVICE_TICKS
while :; do
    watchdog_status=0
    TC_WATCHDOG_RECOVERY_IDENTITY_REFRESHED=0

    if ! tc_watchdog_disk_iteration; then
        watchdog_status=1
    fi

    if [ "$watchdog_status" -eq 0 ]; then
        watchdog_service_tick=$((watchdog_service_tick + 1))
        if [ "$watchdog_service_tick" -ge "$WATCHDOG_SERVICE_TICKS" ]; then
            if ! tc_watchdog_service_iteration; then
                watchdog_status=1
            fi
            watchdog_service_tick=0
        fi
    else
        watchdog_service_tick=$WATCHDOG_SERVICE_TICKS
    fi

    if [ "$watchdog_status" -eq 0 ]; then
        sleep "$WATCHDOG_DISK_POLL_SECONDS"
    else
        sleep "$WATCHDOG_RECOVERY_POLL_SECONDS"
    fi
done
