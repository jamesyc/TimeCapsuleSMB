#!/bin/sh
set -eu

PATH=/bin:/sbin:/usr/bin:/usr/sbin

. /mnt/Flash/common.sh
. /mnt/Flash/tcapsulesmb.conf

tc_init_runtime_env
tc_set_log "$RAM_VAR/rc.local.log" "rc.local"
TC_START_MODE=${1:-}

case "$TC_START_MODE" in
    --print-topology-signature)
        tc_print_topology_signature
        exit $?
        ;;
    --refresh-disk-state)
        tc_prepare_ram_root
        tc_log "managed Samba disk-state refresh beginning; services will not be restarted"
        tc_refresh_disk_state
        exit $?
        ;;
    ""|--reload-disk-runtime)
        ;;
    *)
        tc_log "unknown start-samba.sh mode: $TC_START_MODE"
        exit 2
        ;;
esac

if ! tc_cleanup_old_runtime; then
    tc_log "aborting startup because old managed runtime could not be stopped safely"
    exit 1
fi
tc_tune_kernel_memory
if ! tc_prepare_locks_ramdisk; then
    tc_log "aborting startup because $LOCKS_ROOT is unavailable"
    exit 1
fi
tc_prepare_ram_root
tc_prepare_legacy_prefix
if [ "$TC_START_MODE" = "--reload-disk-runtime" ]; then
    tc_log "managed Samba disk runtime reload beginning"
else
    tc_log "managed Samba boot startup beginning"
fi

if ! tc_prepare_smb_bind_context; then
    tc_log "aborting startup because IPv4 bind interface discovery failed"
    exit 1
fi

if ! tc_refresh_disk_state; then
    exit 1
fi

tc_init_runtime_identity

if ! tc_stage_disk_runtime; then
    exit 1
fi

tc_start_smbd || {
    tc_log "smbd startup failed: IPv4 TCP 445 listener was not observed"
    exit 1
}
tc_log "smbd startup complete: IPv4 TCP 445 listener observed"

tc_start_watchdog

exit 0
