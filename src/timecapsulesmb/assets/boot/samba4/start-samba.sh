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

RAM_LOCKS="$RAM_ROOT/locks"
RAM_LOG="$RAM_VAR/rc.local.log"
SMBD_LOG="$RAM_VAR/log.smbd"
SMBD_DISK_LOGGING_ENABLED=__SMBD_DISK_LOGGING_ENABLED__
MDNS_LOG_ENABLED=__MDNS_LOG_ENABLED__
MDNS_LOG_FILE=__MDNS_LOG_FILE__
SHARE_USE_DISK_ROOT=__SHARE_USE_DISK_ROOT__
APPLE_MOUNT_WAIT_SECONDS=__APPLE_MOUNT_WAIT_SECONDS__
MDNS_BIN=/mnt/Flash/mdns-advertiser
LEGACY_PREFIX_NETBSD7=/root/tc-netbsd7
LEGACY_PREFIX_NETBSD4=/root/tc-netbsd4
LEGACY_PREFIX_NETBSD4LE=/root/tc-netbsd4le
LEGACY_PREFIX_NETBSD4BE=/root/tc-netbsd4be

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
MDNS_CAPTURE_PID=
APPLE_MDNS_SNAPSHOT_START=$(/bin/ls -lnT "$APPLE_MDNS_SNAPSHOT" 2>/dev/null || true)

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

log_mdns_snapshot_age() {
    snapshot_path=$1
    if [ ! -f "$snapshot_path" ]; then
        log "trusted Apple mDNS snapshot missing at $snapshot_path"
        return 1
    fi

    snapshot_current=$(/bin/ls -lnT "$snapshot_path" 2>/dev/null || true)
    if [ -z "$APPLE_MDNS_SNAPSHOT_START" ]; then
        log "trusted Apple mDNS snapshot was created during this boot run: $snapshot_path"
    elif [ "$snapshot_current" != "$APPLE_MDNS_SNAPSHOT_START" ]; then
        log "trusted Apple mDNS snapshot was updated during this boot run: $snapshot_path"
    else
        log "trusted Apple mDNS snapshot predates this boot run; accepting stale snapshot: $snapshot_path"
    fi
    return 0
}

cleanup_old_runtime() {
    log "cleaning old managed runtime processes and RAM state"
    /usr/bin/pkill -f /mnt/Flash/watchdog.sh >/dev/null 2>&1 || true
    /usr/bin/pkill smbd >/dev/null 2>&1 || true
    /usr/bin/pkill "$MDNS_PROC_NAME" >/dev/null 2>&1 || true
    /usr/bin/pkill "$NBNS_PROC_NAME" >/dev/null 2>&1 || true
    sleep 1
    rm -rf /mnt/Memory/samba4
    log "old managed runtime cleanup complete"
}

locks_root_is_mounted() {
    df_line=$(/bin/df -k "$LOCKS_ROOT" 2>/dev/null | /usr/bin/tail -n +2 || true)
    case "$df_line" in
        *" $LOCKS_ROOT")
            return 0
            ;;
    esac
    return 1
}

prepare_locks_ramdisk() {
    mkdir -p "$LOCKS_ROOT"

    if locks_root_is_mounted; then
        rm -rf "$LOCKS_ROOT"/* >/dev/null 2>&1 || true
        log "cleared existing $LOCKS_ROOT mount contents"
        return 0
    fi

    kernel_release=$(/usr/bin/uname -r 2>/dev/null || true)
    case "$kernel_release" in
        6.*)
            if /sbin/mount_tmpfs -s 6m tmpfs "$LOCKS_ROOT" >/dev/null 2>&1; then
                rm -rf "$LOCKS_ROOT"/* >/dev/null 2>&1 || true
                log "mounted $LOCKS_ROOT tmpfs for Samba lock directory"
                return 0
            fi
            log "failed to mount $LOCKS_ROOT tmpfs; using plain directory fallback"
            rm -rf "$LOCKS_ROOT"/* >/dev/null 2>&1 || true
            return 0
            ;;
        *)
            if /sbin/mount_mfs -s 12288 swap "$LOCKS_ROOT" >/dev/null 2>&1; then
                rm -rf "$LOCKS_ROOT"/* >/dev/null 2>&1 || true
                log "mounted $LOCKS_ROOT mfs for Samba lock directory"
                return 0
            fi
            log "failed to mount $LOCKS_ROOT mfs; refusing rootfs fallback"
            return 1
            ;;
    esac
}

prepare_legacy_prefix() {
    mkdir -p /root
    # The checked-in smbd can embed the stage prefix it was built with. Keep
    # compatibility symlinks for both current lane names so the boot-time 
    # runtime path exists regardless of which build lane produced the binary.
    for legacy_prefix in \
        "$LEGACY_PREFIX_NETBSD7" \
        "$LEGACY_PREFIX_NETBSD4" \
        "$LEGACY_PREFIX_NETBSD4LE" \
        "$LEGACY_PREFIX_NETBSD4BE"
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
            log "network interface $NET_IFACE ready with IPv4 $iface_ip"
            echo "127.0.0.1/8 $iface_ip/24"
            return 0
        fi

        attempt=$((attempt + 1))
        sleep 1
    done

    log "timed out waiting for IPv4 on $NET_IFACE"
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
        log "data root match: $volume_root/ShareRoot marker"
        echo "$volume_root/ShareRoot"
        return 0
    fi

    if [ -f "$volume_root/Shared/.com.apple.timemachine.supported" ]; then
        log "data root match: $volume_root/Shared marker"
        echo "$volume_root/Shared"
        return 0
    fi

    if [ -d "$volume_root/ShareRoot" ]; then
        log "data root match: $volume_root/ShareRoot directory"
        echo "$volume_root/ShareRoot"
        return 0
    fi

    if [ -d "$volume_root/Shared" ]; then
        log "data root match: $volume_root/Shared directory"
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
        log "mount candidate skipped; missing block device $dev_path"
        return 1
    fi

    if [ ! -d "$volume_root" ]; then
        mkdir -p "$volume_root"
        created_mountpoint=1
        log "created mountpoint $volume_root for $dev_path"
    fi

    log "launching mount_hfs for $dev_path at $volume_root"
    /sbin/mount_hfs "$dev_path" "$volume_root" >/dev/null 2>&1 &
    mount_pid=$!
    attempt=0
    while kill -0 "$mount_pid" >/dev/null 2>&1; do
        if [ "$attempt" -ge 30 ]; then
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
            log "mount_hfs timed out for $dev_path at $volume_root and volume was not mounted at the immediate re-check, will try manual mount"
            return 1
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    wait "$mount_pid" >/dev/null 2>&1 || true

    if is_volume_root_mounted "$volume_root"; then
        log "mounted $dev_path at $volume_root after ${attempt}s"
        return 0
    fi

    if [ "$created_mountpoint" -eq 1 ]; then
        /bin/rmdir "$volume_root" >/dev/null 2>&1 || true
    fi

    log "mount_hfs exited for $dev_path at $volume_root, but volume is not mounted"
    return 1
}

discover_preexisting_data_root() {
    # Disk-root share mode serves the mounted volume root, so skip Apple's
    # ShareRoot/Shared data-root detection and let the normal mount fallback
    # resolve the volume root.
    if [ "$SHARE_USE_DISK_ROOT" = "true" ]; then
        log "waiting to mount disk root"
        sleep "$APPLE_MOUNT_WAIT_SECONDS"
        return 1
    fi

    if data_root=$(wait_for_existing_data_root); then
        log "found Apple-mounted data root: $data_root"
        echo "$data_root"
        return 0
    fi

    return 1
}

resolve_data_root_on_mounted_volume() {
    volume_root=$1

    if [ "$SHARE_USE_DISK_ROOT" = "true" ]; then
        log "disk-root share mode: using mounted volume root $volume_root"
        echo "$volume_root"
        return 0
    fi

    if data_root=$(find_data_root_under_volume "$volume_root"); then
        log "using existing data root $data_root under $volume_root"
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
    while [ "$attempt" -lt "$APPLE_MOUNT_WAIT_SECONDS" ]; do
        if data_root=$(find_existing_data_root); then
            log "data root was mounted after ${attempt}s"
            echo "$data_root"
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    log "data root was not mounted after ${attempt}s"
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
        log "mount candidate succeeded: $dev_path at $volume_root"
        echo "$volume_root"
        return 0
    fi

    log "mount candidate failed: $dev_path at $volume_root"
    return 1
}

mount_fallback_volume() {
    log "no Apple-mounted data root found; falling back to manual mount"
    attempt=0
    while [ "$attempt" -lt 30 ]; do
        log "manual mount attempt $((attempt + 1)): probing /dev/dk2 and /dev/dk3"
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

    log "manual mount fallback exhausted without mounted data volume"
    return 1
}

find_payload_dir() {
    data_root=$1
    volume_root=${data_root%/*}

    payload_dir="$volume_root/$PAYLOAD_DIR_NAME"
    if [ -d "$payload_dir" ]; then
        log "payload directory found at volume root: $payload_dir"
        echo "$payload_dir"
        return 0
    fi

    # Temporary compatibility fallback for older experiments that placed the
    # payload under the share root instead of at the volume root.
    payload_dir="$data_root/$PAYLOAD_DIR_NAME"
    if [ -d "$payload_dir" ]; then
        log "payload directory found at legacy data-root path: $payload_dir"
        echo "$payload_dir"
        return 0
    fi

    log "payload directory missing; checked $volume_root/$PAYLOAD_DIR_NAME and $data_root/$PAYLOAD_DIR_NAME"
    return 1
}

find_payload_smbd() {
    payload_dir=$1

    if [ -x "$payload_dir/smbd" ]; then
        log "selected smbd binary $payload_dir/smbd"
        echo "$payload_dir/smbd"
        return 0
    fi

    if [ -x "$payload_dir/sbin/smbd" ]; then
        log "selected smbd binary $payload_dir/sbin/smbd"
        echo "$payload_dir/sbin/smbd"
        return 0
    fi

    log "no smbd binary found in $payload_dir"
    return 1
}

find_payload_nbns() {
    payload_dir=$1

    if [ -x "$payload_dir/nbns-advertiser" ]; then
        log "selected nbns binary $payload_dir/nbns-advertiser"
        echo "$payload_dir/nbns-advertiser"
        return 0
    fi

    log "nbns binary not found in $payload_dir"
    return 1
}

prepare_smbd_disk_logging() {
    if [ "$SMBD_DISK_LOGGING_ENABLED" != "1" ]; then
        return 0
    fi

    log_dir="$DATA_ROOT/samba4-logs"
    if mkdir -p "$log_dir"; then
        chmod 777 "$log_dir" >/dev/null 2>&1 || true
        log "smbd debug logging directory ready: $log_dir"
        return 0
    fi

    log "smbd debug logging directory could not be created: $log_dir"
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
        log "staged nbns runtime binary and enabled marker"
    else
        log "nbns runtime staging skipped"
    fi

    if [ -f "$payload_dir/$PAYLOAD_TEMPLATE_NAME" ]; then
        log "rendering smb.conf from payload template $payload_dir/$PAYLOAD_TEMPLATE_NAME"
        sed \
            -e "s#__DATA_ROOT__#$DATA_ROOT#g" \
            -e "s#__PAYLOAD_DIR__#$PAYLOAD_DIR#g" \
            -e "s#__BIND_INTERFACES__#$BIND_INTERFACES#g" \
            "$payload_dir/$PAYLOAD_TEMPLATE_NAME" >"$RAM_ETC/smb.conf"
        return 0
    fi

    log "payload smb.conf template missing; generating fallback smb.conf"
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
    lock directory = $LOCKS_ROOT
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
    fruit:encoding = native
    fruit:time machine = yes
    fruit:posix_rename = yes
    xattr_tdb:file = $PAYLOAD_DIR/private/xattr.tdb
    force user = root
    force group = wheel
    create mask = 0666
    directory mask = 0777
    force create mode = 0666
    force directory mode = 0777
EOF
}

start_smbd() {
    log "starting smbd from $RAM_SBIN/smbd with config $RAM_ETC/smb.conf"
    "$RAM_SBIN/smbd" -D -s "$RAM_ETC/smb.conf"
    if wait_for_process smbd 15; then
        return 0
    fi
    log "smbd process was not observed after launch"
    return 1
}

prepare_mdns_identity() {
    iface_mac=$1

    if [ ! -x "$MDNS_BIN" ]; then
        log "mdns skipped; missing $MDNS_BIN"
        return 1
    fi

    log "mdns startup: interface $NET_IFACE mac=${iface_mac:-missing}"
    if [ -z "$iface_mac" ]; then
        log "mdns skipped; missing $NET_IFACE MAC address"
        return 1
    fi

    if derive_airport_fields "$iface_mac"; then
        log "mdns startup: derived airport fields wama=${AIRPORT_WAMA:-missing} rama=${AIRPORT_RAMA:-missing} ram2=${AIRPORT_RAM2:-missing} syvs=${AIRPORT_SYVS:-missing} srcv=${AIRPORT_SRCV:-missing}"
    else
        log "airport clone fields incomplete; skipping _airport._tcp advertisement"
    fi
    return 0
}

start_mdns_capture() {
    iface_mac=$(get_iface_mac "$NET_IFACE" || true)
    if ! prepare_mdns_identity "$iface_mac"; then
        return 0
    fi

    log "starting mDNS snapshot capture"
    set -- "$MDNS_BIN" \
        --save-all-snapshot "$ALL_MDNS_SNAPSHOT" \
        --save-snapshot "$APPLE_MDNS_SNAPSHOT"
    if [ -n "${AIRPORT_WAMA:-}" ] || [ -n "${AIRPORT_RAMA:-}" ] || [ -n "${AIRPORT_RAM2:-}" ] || [ -n "${AIRPORT_SYVS:-}" ] || [ -n "${AIRPORT_SRCV:-}" ]; then
        set -- "$@" \
            --airport-wama "$AIRPORT_WAMA" \
            --airport-rama "$AIRPORT_RAMA" \
            --airport-ram2 "$AIRPORT_RAM2" \
            --airport-syvs "$AIRPORT_SYVS" \
            --airport-srcv "$AIRPORT_SRCV"
        if [ -n "$AIRPORT_SYAP" ]; then
            set -- "$@" --airport-syap "$AIRPORT_SYAP"
        else
            log "airport syAP missing during mDNS capture"
        fi
    fi

    if [ "$MDNS_LOG_ENABLED" = "1" ]; then
        log "mdns capture: debug logging enabled at $MDNS_LOG_FILE"
        trim_log_file "$MDNS_LOG_FILE" 131072
        printf '%s rc.local: launching mdns-advertiser capture\n' "$(date '+%Y-%m-%d %H:%M:%S')" >>"$MDNS_LOG_FILE"
        "$@" >>"$MDNS_LOG_FILE" 2>&1 &
    else
        log "mdns capture: debug logging disabled"
        "$@" >/dev/null 2>&1 &
    fi
    MDNS_CAPTURE_PID=$!
    log "mDNS snapshot capture launched as pid $MDNS_CAPTURE_PID"
}

wait_for_mdns_capture() {
    if [ -z "$MDNS_CAPTURE_PID" ]; then
        return 0
    fi

    log "waiting for mDNS snapshot capture pid $MDNS_CAPTURE_PID"
    if ! kill -0 "$MDNS_CAPTURE_PID" >/dev/null 2>&1; then
        if [ -f "$APPLE_MDNS_SNAPSHOT" ]; then
            log "mDNS snapshot capture already finished; trusted snapshot is available"
        else
            log "mDNS snapshot capture already finished before wait; no trusted snapshot is available"
        fi
        MDNS_CAPTURE_PID=
        return 0
    fi

    if wait "$MDNS_CAPTURE_PID"; then
        log "mDNS snapshot capture finished"
    else
        log "mDNS snapshot capture exited with failure; final advertiser will use generated records if needed"
    fi
    MDNS_CAPTURE_PID=
}

start_mdns_advertiser() {
    iface_mac=$(get_iface_mac "$NET_IFACE" || true)
    if ! prepare_mdns_identity "$iface_mac"; then
        log "final mdns advertiser skipped; identity preparation failed"
        return 0
    fi

    wait_for_mdns_capture
    if log_mdns_snapshot_age "$APPLE_MDNS_SNAPSHOT"; then
        :
    else
        log "mdns advertiser will fall back to generated records"
    fi

    log "mdns startup: killing prior $MDNS_PROC_NAME processes"
    /usr/bin/pkill "$MDNS_PROC_NAME" >/dev/null 2>&1 || true
    sleep 1

    log "starting mdns advertiser for $BRIDGE0_IP on $NET_IFACE"
    set -- "$MDNS_BIN" \
        --load-snapshot "$APPLE_MDNS_SNAPSHOT" \
        --instance "$MDNS_INSTANCE_NAME" \
        --host "$MDNS_HOST_LABEL" \
        --device-model "$MDNS_DEVICE_MODEL"
    if [ -n "${AIRPORT_WAMA:-}" ] || [ -n "${AIRPORT_RAMA:-}" ] || [ -n "${AIRPORT_RAM2:-}" ] || [ -n "${AIRPORT_SYVS:-}" ] || [ -n "${AIRPORT_SRCV:-}" ]; then
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
    fi
    set -- "$@" \
        --adisk-share "$SMB_SHARE_NAME" \
        --adisk-disk-key "$ADISK_DISK_KEY" \
        --adisk-uuid "$ADISK_UUID" \
        --adisk-sys-wama "$iface_mac" \
        --ipv4 "$BRIDGE0_IP"
    log "mdns startup: final argv prepared"
    if [ "$MDNS_LOG_ENABLED" = "1" ]; then
        log "mdns startup: debug logging enabled at $MDNS_LOG_FILE"
        trim_log_file "$MDNS_LOG_FILE" 131072
        printf '%s rc.local: launching mdns-advertiser\n' "$(date '+%Y-%m-%d %H:%M:%S')" >>"$MDNS_LOG_FILE"
        "$@" >>"$MDNS_LOG_FILE" 2>&1 &
    else
        log "mdns startup: debug logging disabled"
        "$@" >/dev/null 2>&1 &
    fi
    mdns_launch_pid=$!
    log "mdns startup: launched background pid $mdns_launch_pid"
    log "mdns startup: waiting for process match on $MDNS_PROC_NAME"
    if wait_for_process "$MDNS_PROC_NAME" 100; then
        log "mdns startup: wait_for_process succeeded"
        log "mdns advertiser launch requested"
    else
        log "mdns startup: wait_for_process failed"
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
if ! prepare_locks_ramdisk; then
    log "aborting startup because $LOCKS_ROOT is unavailable"
    exit 1
fi
prepare_ram_root
prepare_legacy_prefix
log "managed Samba boot startup beginning"

BIND_INTERFACES=$(wait_for_bind_interfaces) || {
    log "network startup failed: could not determine $NET_IFACE IPv4 address"
    exit 1
}
BRIDGE0_IP=${BIND_INTERFACES#127.0.0.1/8 }
BRIDGE0_IP=${BRIDGE0_IP%%/*}

start_mdns_capture

log "disk discovery: waiting up to ${APPLE_MOUNT_WAIT_SECONDS}s for Apple-mounted data volume before manual mount fallback"

if DATA_ROOT=$(discover_preexisting_data_root); then
    :
else
    VOLUME_ROOT=$(mount_fallback_volume) || {
        log "disk discovery failed: no fallback data volume mounted"
        exit 1
    }
    DATA_ROOT=$(resolve_data_root_on_mounted_volume "$VOLUME_ROOT") || {
        log "data root resolution failed on mounted volume $VOLUME_ROOT"
        exit 1
    }
    log "data root resolved after manual mount: $DATA_ROOT"
fi

PAYLOAD_DIR=$(find_payload_dir "$DATA_ROOT") || {
    log "payload discovery failed: missing payload directory under mounted volume"
    exit 1
}
CACHE_DIRECTORY=__CACHE_DIRECTORY__
log "data root selected: $DATA_ROOT"
prepare_smbd_disk_logging || true

SMBD_SRC=$(find_payload_smbd "$PAYLOAD_DIR") || {
    log "payload discovery failed: missing smbd binary in $PAYLOAD_DIR"
    exit 1
}

NBNS_SRC=
if NBNS_SRC=$(find_payload_nbns "$PAYLOAD_DIR"); then
    :
else
    NBNS_SRC=
fi

stage_runtime "$PAYLOAD_DIR" "$SMBD_SRC" "$NBNS_SRC"
log "runtime staging complete under $RAM_ROOT"

start_smbd || {
    log "smbd startup failed: process was not observed"
    exit 1
}
log "smbd startup complete: process observed"

start_mdns_advertiser
start_nbns

exit 0
