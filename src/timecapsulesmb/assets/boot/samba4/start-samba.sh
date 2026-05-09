#!/bin/sh
set -eu

PATH=/bin:/sbin:/usr/bin:/usr/sbin

. /mnt/Flash/common.sh
. /mnt/Flash/tcapsulesmb.conf

tc_init_runtime_env
tc_set_log "$RAM_VAR/rc.local.log" "rc.local"
TC_MDNS_CAPTURE_PID=
TC_APPLE_MDNS_SNAPSHOT_START=$(/bin/ls -lnT "$APPLE_MDNS_SNAPSHOT" 2>/dev/null || true)

case "${1:-}" in
    --print-topology-signature)
        tc_print_topology_signature
        exit $?
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
tc_log "managed Samba boot startup beginning"

BIND_INTERFACES=$(tc_wait_for_bind_interfaces) || {
    tc_log "network startup failed: could not determine $NET_IFACE IPv4 address"
    exit 1
}
TC_NET_IFACE_IP=${BIND_INTERFACES#127.0.0.1/8 }
TC_NET_IFACE_IP=${TC_NET_IFACE_IP%%/*}
tc_prepare_local_hostname_resolution

if ! tc_wait_for_mast_volumes_to "$TC_VOLUMES_TSV" "$TC_MAST_RAW" "$MAST_DISCOVERY_WAIT_SECONDS"; then
    tc_log "MaSt discovery failed or returned no valid HFS volumes"
    exit 1
fi
/bin/cat "$TC_VOLUMES_TSV" >"$TC_TOPOLOGY_SIGNATURE"

tc_log "pausing 10s before loading share volumes"
sleep 10

if ! tc_build_share_state "$TC_VOLUMES_TSV"; then
    tc_log "no writable MaSt share volumes are available"
    exit 1
fi

if ! tc_resolve_payload "$TC_VOLUMES_TSV"; then
    tc_log "payload discovery failed"
    exit 1
fi
PAYLOAD_DIR=$TC_RESOLVED_PAYLOAD_DIR
tc_write_payload_state "$TC_RESOLVED_PAYLOAD_DIR" "$TC_RESOLVED_PAYLOAD_VOLUME" "$TC_RESOLVED_PAYLOAD_DEVICE"
tc_set_payload_log_dir "$PAYLOAD_DIR" "$TC_RESOLVED_PAYLOAD_VOLUME"
if tc_payload_log_dir_ready; then
    tc_log "payload runtime logs enabled at $TC_PAYLOAD_LOG_DIR"
else
    tc_log "payload runtime log directory unavailable at $TC_PAYLOAD_LOG_DIR"
fi

tc_start_mdns_capture

SMBD_SRC=$(tc_find_payload_smbd "$PAYLOAD_DIR") || {
    tc_log "payload discovery failed: missing smbd binary in $PAYLOAD_DIR"
    exit 1
}

NBNS_SRC=
if NBNS_SRC=$(tc_find_payload_nbns "$PAYLOAD_DIR"); then
    :
else
    NBNS_SRC=
fi

tc_stage_runtime "$PAYLOAD_DIR" "$SMBD_SRC" "$NBNS_SRC"
tc_generate_smb_conf "$PAYLOAD_DIR" "$BIND_INTERFACES"
tc_log "runtime staging complete under $RAM_ROOT"

tc_start_smbd || {
    tc_log "smbd startup failed: process was not observed"
    exit 1
}
tc_log "smbd startup complete: process observed"

tc_start_mdns_advertiser
tc_start_nbns
tc_start_watchdog

exit 0
