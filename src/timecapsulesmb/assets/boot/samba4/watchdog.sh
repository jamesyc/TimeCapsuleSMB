#!/bin/sh
set -eu

PATH=/bin:/sbin:/usr/bin:/usr/sbin

. /mnt/Flash/common.sh
. /mnt/Flash/tcapsulesmb.conf

tc_init_runtime_env
tc_set_log "$RAM_VAR/watchdog.log" "watchdog"
tc_init_runtime_identity

RECOVERY_POLL_SECONDS=10
WATCHDOG_POLL_SECONDS=30

tc_log "watchdog startup beginning"
if tc_read_payload_state; then
    :
else
    TC_PAYLOAD_DIR=
    TC_PAYLOAD_VOLUME=
    TC_PAYLOAD_DEVICE=
fi
tc_log "watchdog payload: device=${TC_PAYLOAD_DEVICE:-none} root=${TC_PAYLOAD_VOLUME:-none} dir=${TC_PAYLOAD_DIR:-none}"

while :; do
    if tc_watchdog_iteration; then
        sleep "$WATCHDOG_POLL_SECONDS"
    else
        sleep "$RECOVERY_POLL_SECONDS"
    fi
done
