#!/bin/sh
set -eu

PATH=/bin:/sbin:/usr/bin:/usr/sbin

. /mnt/Flash/common.sh
. /mnt/Flash/tcapsulesmb.conf

tc_init_runtime_env
tc_set_log "$RAM_VAR/watchdog.log" "watchdog"

RECOVERY_POLL_SECONDS=10
MOUNT_POLL_SECONDS=30
STEADY_POLL_SECONDS=300

tc_log "watchdog startup beginning"
tc_read_payload_state || true
tc_log "watchdog payload: device=${TC_PAYLOAD_DEVICE:-none} root=${TC_PAYLOAD_VOLUME:-none} dir=${TC_PAYLOAD_DIR:-none}"

while :; do
    if tc_watchdog_iteration; then
        tc_sleep_with_runtime_checks "$STEADY_POLL_SECONDS"
    else
        sleep "$RECOVERY_POLL_SECONDS"
    fi
done
