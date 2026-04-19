#!/bin/sh
set -eu

# Boot-time Samba launcher for Apple Time Capsule.
#
# Design:
# - wait for the internal HFS data device to appear
# - mount it at Apple's expected mountpoint
# - discover the real data root by preferring Apple-style markers, but recover
#   freshly reset disks by creating ShareRoot when needed
# - copy smbd into /mnt/Memory so it does not execute from a volume that Apple
#   may later unmount
# - run mdns-advertiser from /mnt/Flash to save ramdisk space
# - generate smb.conf with the discovered data root
# - launch smbd from /mnt/Memory
#
# Expected persistent payload layout on the mounted disk:
#   /Volumes/dkX/__PAYLOAD_DIR_NAME__/
#     smbd                  or sbin/smbd
#     smb.conf.template     optional; uses __DATA_ROOT__ and
#                           __BIND_INTERFACES__ tokens

PATH=/bin:/sbin:/usr/bin:/usr/sbin

. /mnt/Flash/common.sh

RAM_ROOT=/mnt/Memory/samba4
RAM_SBIN="$RAM_ROOT/sbin"
RAM_ETC="$RAM_ROOT/etc"
RAM_VAR="$RAM_ROOT/var"
RAM_LOCKS="$RAM_ROOT/locks"
RAM_PRIVATE="$RAM_ROOT/private"
RAM_LOG="$RAM_VAR/rc.local.log"
SMBD_LOG="$RAM_VAR/log.smbd"
MDNS_BIN=/mnt/Flash/mdns-advertiser
MDNS_PROC_NAME=mdns-advertiser
ALL_MDNS_SNAPSHOT=/mnt/Flash/allmdns.txt
APPLE_MDNS_SNAPSHOT=/mnt/Flash/applemdns.txt
LEGACY_PREFIX_NETBSD7=/root/tc-netbsd7
LEGACY_PREFIX_NETBSD4=/root/tc-netbsd4
NBNS_PROC_NAME=nbns-advertiser

PAYLOAD_DIR_NAME=__PAYLOAD_DIR_NAME__
PAYLOAD_TEMPLATE_NAME=smb.conf.template

SMB_SHARE_NAME=__SMB_SHARE_NAME__
SMB_NETBIOS_NAME=__SMB_NETBIOS_NAME__
NET_IFACE=__NET_IFACE__
MDNS_INSTANCE_NAME=__MDNS_INSTANCE_NAME__
MDNS_HOST_LABEL=__MDNS_HOST_LABEL__
MDNS_DEVICE_MODEL=__MDNS_DEVICE_MODEL__
AIRPORT_SYAP=__AIRPORT_SYAP__
ADISK_DISK_KEY=__ADISK_DISK_KEY__
ADISK_UUID=__ADISK_UUID__
MDNS_STARTUP_DELAY_SECONDS=30
SCRIPT_START_TS=$(/bin/date +%s)

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
    /usr/bin/pkill "$MDNS_PROC_NAME" >/dev/null 2>&1 || true
    /usr/bin/pkill "$NBNS_PROC_NAME" >/dev/null 2>&1 || true
    sleep 1
    rm -rf /mnt/Memory/samba4
}

prepare_legacy_prefix() {
    mkdir -p /root
    # The checked-in smbd can embed the stage prefix it was built with. Keep
    # compatibility symlinks for both current lane names so the boot-time 
    # runtime path exists regardless of which build lane produced the binary.
    for legacy_prefix in \
        "$LEGACY_PREFIX_NETBSD7" \
        "$LEGACY_PREFIX_NETBSD4"
    do
        rm -rf "$legacy_prefix"
        ln -s "$RAM_ROOT" "$legacy_prefix"
    done
}

prepare_ram_root() {
    mkdir -p "$RAM_SBIN" "$RAM_ETC" "$RAM_VAR" "$RAM_LOCKS" "$RAM_PRIVATE"
    mkdir -p "$RAM_VAR/run/ncalrpc" "$RAM_VAR/cores"
    chmod 755 "$RAM_ROOT" "$RAM_SBIN" "$RAM_ETC" "$RAM_VAR" "$RAM_LOCKS" "$RAM_PRIVATE"
    chmod 755 "$RAM_VAR/run" "$RAM_VAR/run/ncalrpc"
    chmod 700 "$RAM_VAR/cores"
}

wait_for_bind_interfaces() {
    attempt=0

    sleep 1
    while [ "$attempt" -lt 60 ]; do
        iface_ip=$(get_iface_ipv4 "$NET_IFACE" || true)
        if [ -n "$iface_ip" ] && [ "$iface_ip" != "0.0.0.0" ]; then
            echo "127.0.0.1/8 $iface_ip/24"
            return 0
        fi

        attempt=$((attempt + 1))
        sleep 1
    done

    return 1
}

find_existing_data_root() {
    if is_volume_root_mounted /Volumes/dk2 && data_root=$(find_data_root_under_volume /Volumes/dk2); then
        echo "$data_root"
        return 0
    fi

    if is_volume_root_mounted /Volumes/dk3 && data_root=$(find_data_root_under_volume /Volumes/dk3); then
        echo "$data_root"
        return 0
    fi

    return 1
}

is_volume_root_mounted() {
    volume_root=$1
    df_line=$(/bin/df -k "$volume_root" 2>/dev/null | /usr/bin/tail -n +2 || true)
    case "$df_line" in
        *" $volume_root")
            return 0
            ;;
    esac
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

    if [ -d "$volume_root/ShareRoot" ]; then
        echo "$volume_root/ShareRoot"
        return 0
    fi

    if [ -d "$volume_root/Shared" ]; then
        echo "$volume_root/Shared"
        return 0
    fi

    return 1
}

initialize_data_root_under_volume() {
    volume_root=$1
    data_root="$volume_root/ShareRoot"
    marker="$data_root/.com.apple.timemachine.supported"

    mkdir -p "$data_root"
    : >"$marker"
    echo "$data_root"
}

mount_device_if_possible() {
    dev_path=$1
    volume_root=$2
    created_mountpoint=0

    if [ ! -b "$dev_path" ]; then
        return 1
    fi

    if [ ! -d "$volume_root" ]; then
        mkdir -p "$volume_root"
        created_mountpoint=1
    fi

    /sbin/mount_hfs "$dev_path" "$volume_root" >/dev/null 2>&1 &
    mount_pid=$!
    attempt=0
    while kill -0 "$mount_pid" >/dev/null 2>&1; do
        if [ "$attempt" -ge 10 ]; then
            kill "$mount_pid" >/dev/null 2>&1 || true
            sleep 1
            kill -9 "$mount_pid" >/dev/null 2>&1 || true
            wait "$mount_pid" >/dev/null 2>&1 || true
            log "mount_hfs command did not exit promptly for $dev_path at $volume_root; re-checking mount state"
            if is_volume_root_mounted "$volume_root"; then
                log "mount_hfs command timed out, but volume is mounted"
                return 0
            fi
            if [ "$created_mountpoint" -eq 1 ]; then
                /bin/rmdir "$volume_root" >/dev/null 2>&1 || true
            fi
            log "mount_hfs timed out for $dev_path at $volume_root and volume is still not mounted"
            return 1
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    wait "$mount_pid" >/dev/null 2>&1 || true

    if is_volume_root_mounted "$volume_root"; then
        return 0
    fi

    if [ "$created_mountpoint" -eq 1 ]; then
        /bin/rmdir "$volume_root" >/dev/null 2>&1 || true
    fi

    return 1
}

discover_preexisting_data_root() {
    if data_root=$(wait_for_existing_data_root); then
        log "found Apple-mounted data root: $data_root"
        echo "$data_root"
        return 0
    fi

    return 1
}

resolve_data_root_on_mounted_volume() {
    volume_root=$1

    if data_root=$(find_data_root_under_volume "$volume_root"); then
        echo "$data_root"
        return 0
    fi

    if is_volume_root_mounted "$volume_root"; then
        data_root=$(initialize_data_root_under_volume "$volume_root")
        log "initialized ShareRoot under $volume_root"
        echo "$data_root"
        return 0
    fi

    return 1
}

wait_for_existing_data_root() {
    attempt=0
    while [ "$attempt" -lt 30 ]; do
        if data_root=$(find_existing_data_root); then
            echo "$data_root"
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    return 1
}

try_mount_candidate() {
    dev_path=$1
    volume_root=$2

    if is_volume_root_mounted "$volume_root"; then
        echo "$volume_root"
        return 0
    fi

    mount_device_if_possible "$dev_path" "$volume_root" || true
    if is_volume_root_mounted "$volume_root"; then
        echo "$volume_root"
        return 0
    fi

    return 1
}

mount_fallback_volume() {
    log "no Apple-mounted data root found; falling back to manual mount"
    attempt=0
    while [ "$attempt" -lt 30 ]; do
        if volume_root=$(try_mount_candidate /dev/dk2 /Volumes/dk2); then
            echo "$volume_root"
            return 0
        fi

        if volume_root=$(try_mount_candidate /dev/dk3 /Volumes/dk3); then
            echo "$volume_root"
            return 0
        fi

        attempt=$((attempt + 1))
        sleep 1
    done

    return 1
}

find_payload_dir() {
    data_root=$1
    volume_root=${data_root%/*}

    payload_dir="$volume_root/$PAYLOAD_DIR_NAME"
    if [ -d "$payload_dir" ]; then
        echo "$payload_dir"
        return 0
    fi

    # Temporary compatibility fallback for older experiments that placed the
    # payload under the share root instead of at the volume root.
    payload_dir="$data_root/$PAYLOAD_DIR_NAME"
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
    nbns_src=${3:-}

    cp "$smbd_src" "$RAM_SBIN/smbd"
    chmod 755 "$RAM_SBIN/smbd"

    if [ -f "$payload_dir/private/nbns.enabled" ] && [ -n "$nbns_src" ] && [ -x "$nbns_src" ]; then
        cp "$nbns_src" "$RAM_SBIN/nbns-advertiser"
        chmod 755 "$RAM_SBIN/nbns-advertiser"
        cp "$payload_dir/private/nbns.enabled" "$RAM_PRIVATE/nbns.enabled"
        chmod 600 "$RAM_PRIVATE/nbns.enabled"
    fi

    if [ -f "$payload_dir/$PAYLOAD_TEMPLATE_NAME" ]; then
        sed \
            -e "s#__DATA_ROOT__#$DATA_ROOT#g" \
            -e "s#__PAYLOAD_DIR__#$PAYLOAD_DIR#g" \
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
    passdb backend = smbpasswd:$PAYLOAD_DIR/private/smbpasswd
    username map = $PAYLOAD_DIR/private/username.map
    dos charset = ASCII
    min protocol = SMB2
    max protocol = SMB3
    load printers = no
    disable spoolss = yes
    dfree command = /bin/sh /mnt/Flash/dfree.sh
    pid directory = $RAM_VAR
    lock directory = $RAM_LOCKS
    state directory = $RAM_VAR
    cache directory = $CACHE_DIRECTORY
    private dir = $RAM_PRIVATE
    log file = $SMBD_LOG
    max log size = 256
    smb ports = 445
    deadtime = 60
    reset on zero vc = yes
    fruit:aapl = yes
    fruit:model = MacSamba
    fruit:advertise_fullsync = true
    fruit:nfs_aces = no
    fruit:veto_appledouble = yes
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
    fruit:metadata = stream
    fruit:time machine = yes
    fruit:posix_rename = yes
    xattr_tdb:file = $PAYLOAD_DIR/private/xattr.tdb
    force user = root
    force group = wheel
    create mask = 0644
    directory mask = 0755
EOF
}

start_smbd() {
    smbd_ready_log=$SMBD_LOG
    if configured_smbd_log=$(get_smbd_log_path_from_config "$RAM_ETC/smb.conf" || true); then
        if [ -n "$configured_smbd_log" ]; then
            smbd_ready_log=$configured_smbd_log
        fi
    fi

    "$RAM_SBIN/smbd" -D -s "$RAM_ETC/smb.conf"
    wait_for_smbd_ready "$smbd_ready_log"
}

start_mdns() {
    if [ ! -x "$MDNS_BIN" ]; then
        return 0
    fi

    now_ts=$(/bin/date +%s)
    elapsed=$((now_ts - SCRIPT_START_TS))
    if [ "$elapsed" -lt "$MDNS_STARTUP_DELAY_SECONDS" ]; then
        sleep $((MDNS_STARTUP_DELAY_SECONDS - elapsed))
    fi

    iface_mac=$(get_iface_mac "$NET_IFACE" || true)
    if [ -z "$iface_mac" ]; then
        log "mdns advertiser launch skipped; missing $NET_IFACE MAC address"
        return 0
    fi

    /usr/bin/pkill "$MDNS_PROC_NAME" >/dev/null 2>&1 || true
    sleep 1

    log "starting mdns advertiser for $BRIDGE0_IP on $NET_IFACE"
    set -- "$MDNS_BIN" \
        --save-all-snapshot "$ALL_MDNS_SNAPSHOT" \
        --save-snapshot "$APPLE_MDNS_SNAPSHOT" \
        --load-snapshot "$APPLE_MDNS_SNAPSHOT" \
        --instance "$MDNS_INSTANCE_NAME" \
        --host "$MDNS_HOST_LABEL" \
        --device-model "$MDNS_DEVICE_MODEL"
    if derive_airport_fields "$iface_mac"; then
        set -- "$@" \
            --airport-wama "$AIRPORT_WAMA" \
            --airport-rama "$AIRPORT_RAMA" \
            --airport-ram2 "$AIRPORT_RAM2" \
            --airport-syvs "$AIRPORT_SYVS" \
            --airport-srcv "$AIRPORT_SRCV"
        if [ -n "$AIRPORT_SYAP" ]; then
            set -- "$@" --airport-syap "$AIRPORT_SYAP"
        else
            log "airport syAP missing; advertising _airport._tcp without syAP"
        fi
    else
        log "airport clone fields incomplete; skipping _airport._tcp advertisement"
    fi
    set -- "$@" \
        --adisk-share "$SMB_SHARE_NAME" \
        --adisk-disk-key "$ADISK_DISK_KEY" \
        --adisk-uuid "$ADISK_UUID" \
        --adisk-sys-wama "$iface_mac" \
        --ipv4 "$BRIDGE0_IP"
    "$@" >/dev/null 2>&1 &
    if wait_for_process "$MDNS_PROC_NAME" 90; then
        log "mdns advertiser launch requested"
    else
        log "mdns advertiser failed to stay running"
    fi
}

start_nbns() {
    if [ ! -f "$PAYLOAD_DIR/private/nbns.enabled" ]; then
        log "nbns responder skipped; marker missing"
        return 0
    fi

    if [ ! -x "$RAM_SBIN/nbns-advertiser" ]; then
        log "nbns responder launch skipped; missing runtime binary"
        return 0
    fi

    /usr/bin/pkill wcifsnd >/dev/null 2>&1 || true
    /usr/bin/pkill wcifsfs >/dev/null 2>&1 || true
    /usr/bin/pkill "$NBNS_PROC_NAME" >/dev/null 2>&1 || true
    sleep 1

    log "starting nbns responder for $SMB_NETBIOS_NAME at $BRIDGE0_IP"
    "$RAM_SBIN/nbns-advertiser" \
        --name "$SMB_NETBIOS_NAME" \
        --ipv4 "$BRIDGE0_IP" \
        >/dev/null 2>&1 &
    if wait_for_process "$NBNS_PROC_NAME"; then
        log "nbns responder launch requested"
    else
        log "nbns responder failed to stay running"
    fi
}

cleanup_old_runtime
prepare_ram_root
prepare_legacy_prefix
log "boot start"

BIND_INTERFACES=$(wait_for_bind_interfaces) || {
    log "failed to determine $NET_IFACE IPv4 address"
    exit 1
}
BRIDGE0_IP=${BIND_INTERFACES#127.0.0.1/8 }
BRIDGE0_IP=${BRIDGE0_IP%%/*}

start_mdns

log "waiting for Apple-mounted data volume before manual mount fallback"

if DATA_ROOT=$(discover_preexisting_data_root); then
    :
else
    VOLUME_ROOT=$(mount_fallback_volume) || {
        log "failed to mount fallback data volume"
        exit 1
    }
    DATA_ROOT=$(resolve_data_root_on_mounted_volume "$VOLUME_ROOT") || {
        log "failed to discover or initialize data root on mounted volume"
        exit 1
    }
    log "found data root after manual mount: $DATA_ROOT"
fi

PAYLOAD_DIR=$(find_payload_dir "$DATA_ROOT") || {
    log "missing payload directory under mounted volume"
    exit 1
}
CACHE_DIRECTORY=__CACHE_DIRECTORY__
log "data root: $DATA_ROOT"

SMBD_SRC=$(find_payload_smbd "$PAYLOAD_DIR") || {
    log "missing smbd in payload directory"
    exit 1
}

NBNS_SRC=
if NBNS_SRC=$(find_payload_nbns "$PAYLOAD_DIR"); then
    :
else
    NBNS_SRC=
fi

stage_runtime "$PAYLOAD_DIR" "$SMBD_SRC" "$NBNS_SRC"
log "runtime staged under $RAM_ROOT"

start_nbns

start_smbd || {
    log "smbd did not become ready"
    exit 1
}
log "smbd ready"

exit 0
