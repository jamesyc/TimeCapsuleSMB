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
NBNS_PROC_NAME=nbns-advertiser

PAYLOAD_DIR_NAME=__PAYLOAD_DIR_NAME__
PAYLOAD_TEMPLATE_NAME=smb.conf.template

SMB_SHARE_NAME=__SMB_SHARE_NAME__
SMB_NETBIOS_NAME=__SMB_NETBIOS_NAME__
NET_IFACE=__NET_IFACE__
MDNS_INSTANCE_NAME=__MDNS_INSTANCE_NAME__
MDNS_HOST_LABEL=__MDNS_HOST_LABEL__
MDNS_DEVICE_MODEL=__MDNS_DEVICE_MODEL__
ADISK_DISK_KEY=__ADISK_DISK_KEY__
ADISK_UUID=__ADISK_UUID__

log() {
    log_dir=${RAM_LOG%/*}
    tmp_log="$RAM_LOG.tmp.$$"
    line="$(date '+%Y-%m-%d %H:%M:%S') rc.local: $*"

    [ -d "$log_dir" ] || mkdir -p "$log_dir"
    {
        if [ -f "$RAM_LOG" ]; then
            /usr/bin/tail -n 255 "$RAM_LOG" 2>/dev/null || true
        fi
        echo "$line"
    } >"$tmp_log"
    mv "$tmp_log" "$RAM_LOG"
}

cleanup_old_runtime() {
    /usr/bin/pkill smbd >/dev/null 2>&1 || true
    /usr/bin/pkill mdns-smbd-advertiser >/dev/null 2>&1 || true
    /usr/bin/pkill "$NBNS_PROC_NAME" >/dev/null 2>&1 || true
    sleep 1
    rm -rf /mnt/Memory/samba4
}

prepare_legacy_prefix() {
    mkdir -p /root
    rm -rf "$LEGACY_PREFIX"
    ln -s "$RAM_ROOT" "$LEGACY_PREFIX"
}

prepare_ram_root() {
    mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_VAR" "$RAM_LOCKS" "$RAM_PRIVATE"
    mkdir -p "$RAM_VAR/run/ncalrpc" "$RAM_VAR/cores"
    chmod 755 "$RAM_ROOT" "$RAM_SBIN" "$RAM_ETC" "$RAM_VAR" "$RAM_LOCKS" "$RAM_PRIVATE"
    chmod 755 "$RAM_VAR/run" "$RAM_VAR/run/ncalrpc"
    chmod 700 "$RAM_VAR/cores"
}

get_iface_ipv4() {
    /sbin/ifconfig "$NET_IFACE" 2>/dev/null | sed -n 's/^[[:space:]]*inet[[:space:]]\([0-9.]*\).*/\1/p' | sed -n '1p'
}

get_iface_mac() {
    /sbin/ifconfig "$NET_IFACE" 2>/dev/null \
        | sed -n \
            -e 's/^[[:space:]]*ether[[:space:]]\([0-9A-Fa-f:]*\).*/\1/p' \
            -e 's/^[[:space:]]*address[[:space:]]\([0-9A-Fa-f:]*\).*/\1/p' \
        | sed -n '1p'
}

wait_for_bind_interfaces() {
    attempt=0

    sleep 5
    while [ "$attempt" -lt 15 ]; do
        iface_ip=$(get_iface_ipv4 || true)
        if [ -n "$iface_ip" ] && [ "$iface_ip" != "0.0.0.0" ]; then
            echo "127.0.0.1/8 $iface_ip/24"
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
        return 1
    fi

    [ -d "$volume_root" ] || mkdir -p "$volume_root"

    /sbin/mount_hfs "$dev_path" "$volume_root" >/dev/null 2>&1 || true
}

ensure_data_root() {
    attempt=0
    while [ "$attempt" -lt 120 ]; do
        if data_root=$(find_data_root_under_volume /Volumes/dk2); then
            echo "$data_root"
            return 0
        fi

        if data_root=$(find_data_root_under_volume /Volumes/dk3); then
            echo "$data_root"
            return 0
        fi

        mount_device_if_possible /dev/dk2 /Volumes/dk2
        if data_root=$(find_data_root_under_volume /Volumes/dk2); then
            echo "$data_root"
            return 0
        fi

        mount_device_if_possible /dev/dk3 /Volumes/dk3
        if data_root=$(find_data_root_under_volume /Volumes/dk3); then
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

find_payload_mdns() {
    payload_dir=$1

    if [ -x "$payload_dir/mdns-smbd-advertiser" ]; then
        echo "$payload_dir/mdns-smbd-advertiser"
        return 0
    fi

    return 1
}

find_payload_nbns() {
    payload_dir=$1

    if [ -x "$payload_dir/nbns-advertiser" ]; then
        echo "$payload_dir/nbns-advertiser"
        return 0
    fi

    return 1
}

stage_runtime() {
    payload_dir=$1
    smbd_src=$2
    mdns_src=${3:-}
    nbns_src=${4:-}

    cp "$smbd_src" "$RAM_SBIN/smbd"
    chmod 755 "$RAM_SBIN/smbd"

    if [ -n "$mdns_src" ] && [ -x "$mdns_src" ]; then
        cp "$mdns_src" "$RAM_SBIN/mdns-smbd-advertiser"
        chmod 755 "$RAM_SBIN/mdns-smbd-advertiser"
    fi

    if [ -f "$payload_dir/private/nbns.enabled" ] && [ -n "$nbns_src" ] && [ -x "$nbns_src" ]; then
        cp "$nbns_src" "$RAM_SBIN/nbns-advertiser"
        chmod 755 "$RAM_SBIN/nbns-advertiser"
        cp "$payload_dir/private/nbns.enabled" "$RAM_PRIVATE/nbns.enabled"
        chmod 600 "$RAM_PRIVATE/nbns.enabled"
    fi

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
    map to guest = Never
    restrict anonymous = 2
    guest account = nobody
    null passwords = no
    ea support = yes
    passdb backend = smbpasswd:$DATA_ROOT/../$PAYLOAD_DIR_NAME/private/smbpasswd
    username map = $DATA_ROOT/../$PAYLOAD_DIR_NAME/private/username.map
    dos charset = ASCII
    min protocol = SMB2
    max protocol = SMB3
    load printers = no
    disable spoolss = yes
    dfree command = /bin/sh /mnt/Flash/dfree.sh
    pid directory = $RAM_VAR
    lock directory = $RAM_LOCKS
    state directory = $RAM_VAR
    cache directory = $RAM_VAR
    private dir = $RAM_PRIVATE
    log file = $SMBD_LOG
    max log size = 256
    smb ports = 445
    deadtime = 15
    fruit:aapl = yes
    fruit:model = MacSamba
    fruit:advertise_fullsync = true
    fruit:nfs_aces = no
    fruit:encoding = native
    fruit:veto_appledouble = no
    fruit:wipe_intentionally_left_blank_rfork = yes
    fruit:delete_empty_adfiles = yes

[$SMB_SHARE_NAME]
    path = $DATA_ROOT
    browseable = yes
    read only = no
    guest ok = no
    valid users = __SMB_SAMBA_USER__ root
    vfs objects = catia fruit streams_xattr acl_xattr xattr_tdb
    acl_xattr:ignore system acls = yes
    fruit:resource = file
    fruit:metadata = netatalk
    fruit:time machine = yes
    fruit:posix_rename = yes
    fruit:locking = none
    xattr_tdb:file = $DATA_ROOT/../$PAYLOAD_DIR_NAME/private/xattr.tdb
    force user = root
    force group = wheel
    create mask = 0644
    directory mask = 0755
EOF
}

start_smbd() {
    "$RAM_SBIN/smbd" -D -s "$RAM_ETC/smb.conf"
}

start_mdns() {
    if [ ! -x "$RAM_SBIN/mdns-smbd-advertiser" ]; then
        return 0
    fi

    iface_mac=$(get_iface_mac || true)
    if [ -z "$iface_mac" ]; then
        log "mdns advertiser launch skipped; missing $NET_IFACE MAC address"
        return 0
    fi

    "$RAM_SBIN/mdns-smbd-advertiser" \
        --instance "$MDNS_INSTANCE_NAME" \
        --host "$MDNS_HOST_LABEL" \
        --device-model "$MDNS_DEVICE_MODEL" \
        --adisk-share "$SMB_SHARE_NAME" \
        --adisk-disk-key "$ADISK_DISK_KEY" \
        --adisk-uuid "$ADISK_UUID" \
        --adisk-sys-wama "$iface_mac" \
        --ipv4 "$BRIDGE0_IP" \
        >/dev/null 2>&1 &
    log "mdns advertiser launch requested"
}

start_nbns() {
    if [ ! -f "$PAYLOAD_DIR/private/nbns.enabled" ]; then
        return 0
    fi

    if [ ! -x "$RAM_SBIN/nbns-advertiser" ]; then
        log "nbns responder launch skipped; missing runtime binary"
        return 0
    fi

    /usr/bin/pkill "$NBNS_PROC_NAME" >/dev/null 2>&1 || true
    sleep 1

    "$RAM_SBIN/nbns-advertiser" \
        --name "$SMB_NETBIOS_NAME" \
        --ipv4 "$BRIDGE0_IP" \
        >/dev/null 2>&1 &
    log "nbns responder launch requested"
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
    log "failed to determine $NET_IFACE IPv4 address"
    exit 1
}
BRIDGE0_IP=${BIND_INTERFACES#127.0.0.1/8 }
BRIDGE0_IP=${BRIDGE0_IP%%/*}

PAYLOAD_DIR=$(find_payload_dir "$DATA_ROOT") || {
    log "missing payload directory under mounted volume"
    exit 1
}

SMBD_SRC=$(find_payload_smbd "$PAYLOAD_DIR") || {
    log "missing smbd in payload directory"
    exit 1
}

MDNS_SRC=
if MDNS_SRC=$(find_payload_mdns "$PAYLOAD_DIR"); then
    :
else
    MDNS_SRC=
fi

NBNS_SRC=
if NBNS_SRC=$(find_payload_nbns "$PAYLOAD_DIR"); then
    :
else
    NBNS_SRC=
fi

stage_runtime "$PAYLOAD_DIR" "$SMBD_SRC" "$MDNS_SRC" "$NBNS_SRC"
log "runtime staged under $RAM_ROOT"

start_mdns
start_nbns
start_smbd
log "smbd launch requested"

exit 0
