#!/bin/sh
set -eu

PATH=/bin:/sbin:/usr/bin:/usr/sbin

. /mnt/Flash/common.sh
. /mnt/Flash/tcapsulesmb.conf

tc_init_runtime_env
tc_set_log "$RAM_VAR/rc.local.log" "rc.local"

case "${1:-}" in
    "")
        ;;
    *)
        tc_log "unknown boot.sh mode: $1"
        exit 2
        ;;
esac

tc_log "managed Samba boot preparation beginning"
if ! tc_cleanup_old_runtime; then
    tc_log "aborting boot preparation because old managed runtime could not be stopped safely"
    exit 1
fi
tc_tune_kernel_memory
tc_log "kernel memory tuned for Samba"
if ! tc_prepare_locks_ramdisk; then
    tc_log "aborting boot preparation because $LOCKS_ROOT is unavailable"
    exit 1
fi
tc_prepare_ram_root
tc_prepare_legacy_prefix
tc_log "managed Samba boot preparation complete; starting manager"
if runtime_manager_present; then
    tc_log "manager already running"
else
    tc_log "starting manager"
    /mnt/Flash/manager.sh </dev/null >/dev/null 2>&1 &
    manager_pid=$!
    tc_log "manager launched as pid $manager_pid"
fi

exit 0
