#!/bin/sh
set -eu

PATH=/bin:/sbin:/usr/bin:/usr/sbin

. /mnt/Flash/common.sh
. /mnt/Flash/tcapsulesmb.conf

tc_init_runtime_env
tc_set_log "$RAM_VAR/rc.local.log" "rc.local"
TC_MDNS_CAPTURE_PID=
TC_APPLE_MDNS_SNAPSHOT_START=$(/bin/ls -lnT "$APPLE_MDNS_SNAPSHOT" 2>/dev/null || true)
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

BIND_INTERFACES=$(tc_wait_for_bind_interfaces) || {
    tc_log "network startup failed: could not determine $NET_IFACE IPv4 address"
    exit 1
}
TC_NET_IFACE_IP=${BIND_INTERFACES#127.0.0.1/8 }
TC_NET_IFACE_IP=${TC_NET_IFACE_IP%%/*}
tc_prepare_local_hostname_resolution

if ! tc_refresh_disk_state; then
    exit 1
fi

tc_start_mdns_capture

if ! tc_stage_disk_runtime "$BIND_INTERFACES"; then
    exit 1
fi

tc_start_smbd || {
    tc_log "smbd startup failed: process was not observed"
    exit 1
}
tc_log "smbd startup complete: process observed"

tc_start_mdns_advertiser
tc_start_nbns
tc_start_watchdog

exit 0
