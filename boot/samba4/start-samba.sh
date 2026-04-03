#!/bin/sh
set -eu

# Boot-time Samba launcher for Apple Time Capsule.
#
# Design:
# - wait for the internal HFS data device to appear
# - mount it at Apple's expected mountpoint
# - discover the real data root by looking for .com.apple.timemachine.supported
# - copy the Samba runtime into /mnt/Memory so smbd does not execute from a
#   volume that Apple may later unmount
# - generate smb.conf with the discovered data root
# - launch smbd from /mnt/Memory
#
# Expected persistent payload layout on the mounted disk:
#   /Volumes/dkX/samba4/
#     smbd                  or sbin/smbd
#     smb.conf.template     optional; uses __DATA_ROOT__ and
#                           __BIND_INTERFACES__ tokens

PATH=/bin:/sbin:/usr/bin:/usr/sbin

RAM_ROOT=/mnt/Memory/samba4
RAM_SBIN="$RAM_ROOT/sbin"
RAM_ETC="$RAM_ROOT/etc"
RAM_VAR="$RAM_ROOT/var"
RAM_LOCKS="$RAM_ROOT/locks"
RAM_PRIVATE="$RAM_ROOT/private"
RAM_LOG="$RAM_VAR/rc.local.log"
SMBD_LOG="$RAM_VAR/log.smbd"
LEGACY_PREFIX=/root/tc-stage4

PAYLOAD_DIR_NAME=samba4
PAYLOAD_TEMPLATE_NAME=smb.conf.template

SMB_SHARE_NAME=Data
SMB_NETBIOS_NAME=TimeCapsule

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') rc.local: $*" >>"$RAM_LOG"
}

cleanup_old_runtime() {
    /usr/bin/pkill smbd >/dev/null 2>&1 || true
    sleep 1
    rm -rf /mnt/Memory/samba3 /mnt/Memory/samba4
}

prepare_ram_root() {
    mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_VAR" "$RAM_LOCKS" "$RAM_PRIVATE"
    mkdir -p "$RAM_VAR/run/ncalrpc" "$RAM_VAR/cores"
    chmod 755 "$RAM_ROOT" "$RAM_SBIN" "$RAM_ETC" "$RAM_VAR" "$RAM_LOCKS" "$RAM_PRIVATE"
    chmod 755 "$RAM_VAR/run" "$RAM_VAR/run/ncalrpc"
    chmod 700 "$RAM_VAR/cores"
    : >"$RAM_LOG"
}

prepare_legacy_prefix() {
    [ -d /root ] || mkdir -p /root
    rm -rf "$LEGACY_PREFIX"
    ln -s "$RAM_ROOT" "$LEGACY_PREFIX"
}

get_bridge0_ipv4() {
    /sbin/ifconfig bridge0 2>/dev/null | sed -n 's/^[[:space:]]*inet[[:space:]]\([0-9.]*\).*/\1/p' | sed -n '1p'
}

wait_for_bind_interfaces() {
    attempt=0

    sleep 5
    while [ "$attempt" -lt 15 ]; do
        bridge0_ip=$(get_bridge0_ipv4 || true)
        if [ -n "$bridge0_ip" ] && [ "$bridge0_ip" != "0.0.0.0" ]; then
            echo "127.0.0.1/8 $bridge0_ip/24"
            return 0
        fi

        attempt=$((attempt + 1))
        sleep 1
    done

    return 1
}

find_data_root_under_volume() {
    volume_root=$1

    if [ -f "$volume_root/ShareRoot/.com.apple.timemachine.supported" ]; then
        echo "$volume_root/ShareRoot"
        return 0
    fi

    if [ -f "$volume_root/Shared/.com.apple.timemachine.supported" ]; then
        echo "$volume_root/Shared"
        return 0
    fi

    return 1
}

mount_device_if_possible() {
    dev_path=$1
    volume_root=$2

    if [ ! -b "$dev_path" ]; then
        log "device missing: $dev_path"
        return 1
    fi

    [ -d "$volume_root" ] || mkdir -p "$volume_root"
    log "mount attempt: $dev_path -> $volume_root"

    /sbin/mount_hfs "$dev_path" "$volume_root" >/dev/null 2>&1 || true

    if [ -d "$volume_root" ]; then
        log "mountpoint present after attempt: $volume_root"
    fi
}

ensure_data_root() {
    attempt=0
    while [ "$attempt" -lt 120 ]; do
        log "ensure_data_root attempt $attempt"

        if data_root=$(find_data_root_under_volume /Volumes/dk2); then
            log "found data root under /Volumes/dk2"
            echo "$data_root"
            return 0
        fi

        if data_root=$(find_data_root_under_volume /Volumes/dk3); then
            log "found data root under /Volumes/dk3"
            echo "$data_root"
            return 0
        fi

        mount_device_if_possible /dev/dk2 /Volumes/dk2
        if data_root=$(find_data_root_under_volume /Volumes/dk2); then
            log "found data root under /Volumes/dk2 after mount"
            echo "$data_root"
            return 0
        fi

        mount_device_if_possible /dev/dk3 /Volumes/dk3
        if data_root=$(find_data_root_under_volume /Volumes/dk3); then
            log "found data root under /Volumes/dk3 after mount"
            echo "$data_root"
            return 0
        fi

        attempt=$((attempt + 1))
        sleep 1
    done

    return 1
}

find_payload_dir() {
    data_root=$1
    case "$data_root" in
        /Volumes/dk2/*)
            volume_root=/Volumes/dk2
            ;;
        /Volumes/dk3/*)
            volume_root=/Volumes/dk3
            ;;
        *)
            return 1
            ;;
    esac

    payload_dir="$volume_root/$PAYLOAD_DIR_NAME"
    if [ -d "$payload_dir" ]; then
        echo "$payload_dir"
        return 0
    fi

    return 1
}

find_payload_smbd() {
    payload_dir=$1

    if [ -x "$payload_dir/smbd" ]; then
        echo "$payload_dir/smbd"
        return 0
    fi

    if [ -x "$payload_dir/sbin/smbd" ]; then
        echo "$payload_dir/sbin/smbd"
        return 0
    fi

    return 1
}

stage_runtime() {
    payload_dir=$1
    smbd_src=$2

    cp "$smbd_src" "$RAM_SBIN/smbd"
    chmod 755 "$RAM_SBIN/smbd"

    if [ -f "$payload_dir/$PAYLOAD_TEMPLATE_NAME" ]; then
        sed \
            -e "s#__DATA_ROOT__#$DATA_ROOT#g" \
            -e "s#__BIND_INTERFACES__#$BIND_INTERFACES#g" \
            "$payload_dir/$PAYLOAD_TEMPLATE_NAME" >"$RAM_ETC/smb.conf"
        return 0
    fi

    cat >"$RAM_ETC/smb.conf" <<EOF
[global]
    netbios name = $SMB_NETBIOS_NAME
    workgroup = WORKGROUP
    server string = Time Capsule Samba 4
    interfaces = $BIND_INTERFACES
    bind interfaces only = yes
    security = user
    map to guest = Bad User
    guest account = root
    load printers = no
    disable spoolss = yes
    pid directory = $RAM_VAR
    lock directory = $RAM_LOCKS
    state directory = $RAM_VAR
    cache directory = $RAM_VAR
    private dir = $RAM_PRIVATE
    log file = $SMBD_LOG
    max log size = 256
    smb ports = 445
    deadtime = 15

[$SMB_SHARE_NAME]
    path = $DATA_ROOT
    browseable = yes
    read only = no
    guest ok = yes
    guest only = yes
    force user = root
    force group = wheel
    create mask = 0644
    directory mask = 0755
EOF
}

start_smbd() {
    "$RAM_SBIN/smbd" -D -s "$RAM_ETC/smb.conf"
}

cleanup_old_runtime
prepare_ram_root
prepare_legacy_prefix
log "boot start"

DATA_ROOT=$(ensure_data_root) || {
    log "failed to discover data root"
    exit 1
}
log "data root: $DATA_ROOT"

BIND_INTERFACES=$(wait_for_bind_interfaces) || {
    log "failed to determine bridge0 IPv4 address"
    exit 1
}
log "bind interfaces: $BIND_INTERFACES"

PAYLOAD_DIR=$(find_payload_dir "$DATA_ROOT") || {
    log "missing payload directory under mounted volume"
    exit 1
}
log "payload dir: $PAYLOAD_DIR"

SMBD_SRC=$(find_payload_smbd "$PAYLOAD_DIR") || {
    log "missing smbd in payload directory"
    exit 1
}
log "smbd src: $SMBD_SRC"

stage_runtime "$PAYLOAD_DIR" "$SMBD_SRC"
log "runtime staged under $RAM_ROOT"

start_smbd
log "smbd launch requested"

exit 0
