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
if tc_read_payload_state; then
    tc_set_payload_log_dir "$TC_PAYLOAD_DIR" "$TC_PAYLOAD_VOLUME"
    tc_set_payload_append_log "$TC_PAYLOAD_LOG_DIR/watchdog.log" "watchdog" "$TC_PAYLOAD_VOLUME" "$RAM_VAR/watchdog.log"
else
    TC_PAYLOAD_DIR=
    TC_PAYLOAD_VOLUME=
    TC_PAYLOAD_DEVICE=
fi
tc_log "watchdog payload: device=${TC_PAYLOAD_DEVICE:-none} root=${TC_PAYLOAD_VOLUME:-none} dir=${TC_PAYLOAD_DIR:-none}"

while :; do
    if tc_watchdog_iteration; then
        if tc_sleep_with_runtime_checks "$STEADY_POLL_SECONDS"; then
            :
        else
            tc_log "watchdog steady check interrupted; running recovery pass"
        fi
    else
        sleep "$RECOVERY_POLL_SECONDS"
    fi
done
