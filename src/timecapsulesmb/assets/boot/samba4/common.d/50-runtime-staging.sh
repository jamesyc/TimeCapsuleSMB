tc_refresh_disk_state() {
    volumes_file="$TC_STATE_DIR/mast-volumes.$$"
    raw_file="$TC_STATE_DIR/mast.raw.$$"
    shares_file="$TC_STATE_DIR/shares.tsv.$$"
    adisk_file="$TC_STATE_DIR/adisk.tsv.$$"
    payload_file="$TC_STATE_DIR/payload.tsv.$$"
    TC_DISK_REFRESH_RESULT=failed

    tc_log "disk-state refresh: discovering MaSt HFS volumes"
    rm -f "$volumes_file" "$raw_file" "$shares_file" "$adisk_file" "$payload_file"
    if ! tc_read_mast_volumes_to "$volumes_file" "$raw_file"; then
        tc_log "MaSt discovery failed"
        TC_DISK_REFRESH_RESULT=mast_failed
        rm -f "$volumes_file" "$raw_file" "$shares_file" "$adisk_file" "$payload_file"
        return 1
    fi
    tc_log "disk-state refresh: MaSt discovery complete"
    tc_log_mast_volume_state "$volumes_file"

    if [ ! -s "$volumes_file" ]; then
        tc_log "disk-state refresh: MaSt reports zero managed HFS volumes; writing diskless runtime state"
        : >"$shares_file"
        : >"$adisk_file"
        : >"$payload_file"
        mv -f "$volumes_file" "$TC_TOPOLOGY_SIGNATURE"
        mv -f "$shares_file" "$TC_SHARES_TSV"
        mv -f "$adisk_file" "$TC_ADISK_TSV"
        mv -f "$payload_file" "$TC_PAYLOAD_TSV"
        TC_DISK_REFRESH_RESULT=no_payload
        tc_log "disk-state refresh complete: diskless runtime state written"
        rm -f "$raw_file" "$volumes_file" "$shares_file" "$adisk_file" "$payload_file"
        return 0
    fi

    tc_log "disk-state refresh: activating discovered MaSt volumes"
    tc_mount_mast_volumes_for_boot "$volumes_file"

    tc_log "disk-state refresh: resolving payload directory"
    if ! tc_resolve_payload "$volumes_file"; then
        tc_log "payload discovery failed; writing no-payload runtime state"
        : >"$shares_file"
        : >"$adisk_file"
        : >"$payload_file"
        mv -f "$volumes_file" "$TC_TOPOLOGY_SIGNATURE"
        mv -f "$shares_file" "$TC_SHARES_TSV"
        mv -f "$adisk_file" "$TC_ADISK_TSV"
        mv -f "$payload_file" "$TC_PAYLOAD_TSV"
        TC_DISK_REFRESH_RESULT=no_payload
        tc_log "disk-state refresh complete: no-payload runtime state written"
        rm -f "$raw_file" "$volumes_file" "$shares_file" "$adisk_file" "$payload_file"
        return 0
    fi

    tc_log "disk-state refresh: building share state from mounted writable MaSt volumes"
    if ! tc_build_share_state "$volumes_file" "$shares_file" "$adisk_file"; then
        tc_log "no writable MaSt share volumes are available; writing no-payload runtime state"
        : >"$shares_file"
        : >"$adisk_file"
        : >"$payload_file"
        mv -f "$volumes_file" "$TC_TOPOLOGY_SIGNATURE"
        mv -f "$shares_file" "$TC_SHARES_TSV"
        mv -f "$adisk_file" "$TC_ADISK_TSV"
        mv -f "$payload_file" "$TC_PAYLOAD_TSV"
        TC_DISK_REFRESH_RESULT=no_payload
        tc_log "disk-state refresh complete: no-payload runtime state written"
        rm -f "$raw_file" "$volumes_file" "$shares_file" "$adisk_file" "$payload_file"
        return 0
    fi
    tc_log "disk-state refresh: share state ready"

    tc_log "disk-state refresh: applying ATA drive settings after payload and share-state build"
    tc_configure_ata_drive_settings_for_mast_disks "$volumes_file" || true

    tc_write_payload_state "$TC_RESOLVED_PAYLOAD_DIR" "$TC_RESOLVED_PAYLOAD_VOLUME" "$TC_RESOLVED_PAYLOAD_DEVICE" "$payload_file"
    mv -f "$volumes_file" "$TC_TOPOLOGY_SIGNATURE"
    mv -f "$shares_file" "$TC_SHARES_TSV"
    mv -f "$adisk_file" "$TC_ADISK_TSV"
    mv -f "$payload_file" "$TC_PAYLOAD_TSV"
    tc_log "disk-state refresh: payload state written: dir=$TC_RESOLVED_PAYLOAD_DIR volume=$TC_RESOLVED_PAYLOAD_VOLUME device=$TC_RESOLVED_PAYLOAD_DEVICE"
    tc_set_payload_log_dir "$TC_RESOLVED_PAYLOAD_DIR" "$TC_RESOLVED_PAYLOAD_VOLUME"
    if tc_payload_log_dir_ready; then
        tc_log "payload smbd log directory ready at $TC_PAYLOAD_LOG_DIR"
    else
        tc_log "payload smbd log directory unavailable at $TC_PAYLOAD_LOG_DIR"
    fi

    tc_log "disk-state refresh complete: runtime state written"
    TC_DISK_REFRESH_RESULT=ready
    rm -f "$volumes_file" "$raw_file" "$shares_file" "$adisk_file" "$payload_file"
    return 0
}

tc_stage_disk_runtime() {
    if ! tc_load_payload_state; then
        tc_log "payload discovery failed: payload state is unavailable"
        return 1
    fi
    tc_set_payload_log_dir "$TC_PAYLOAD_DIR" "$TC_PAYLOAD_VOLUME"

    SMBD_SRC=$(tc_find_payload_smbd "$TC_PAYLOAD_DIR") || {
        tc_log "payload discovery failed: missing smbd binary in $TC_PAYLOAD_DIR"
        return 1
    }

    NBNS_SRC=
    if [ "$NBNS_ENABLED" = "1" ]; then
        if NBNS_SRC=$(tc_find_payload_nbns "$TC_PAYLOAD_DIR"); then
            :
        else
            NBNS_SRC=
        fi
    fi

    tc_stage_runtime "$TC_PAYLOAD_DIR" "$SMBD_SRC" "$NBNS_SRC"
    if [ -z "$TC_SMB_BIND_INTERFACES" ]; then
        tc_refresh_smb_bind_interfaces || {
            tc_log "runtime staging failed: no usable bind address is available"
            return 1
        }
    fi
    tc_generate_smb_conf "$TC_PAYLOAD_DIR"
    tc_log "runtime staging complete under $RAM_ROOT"
}

tc_find_payload_smbd() {
    payload_dir=$1

    if [ -x "$payload_dir/smbd" ]; then
        tc_log "selected smbd binary $payload_dir/smbd"
        echo "$payload_dir/smbd"
        return 0
    fi

    if [ -x "$payload_dir/sbin/smbd" ]; then
        tc_log "selected smbd binary $payload_dir/sbin/smbd"
        echo "$payload_dir/sbin/smbd"
        return 0
    fi

    tc_log "no smbd binary found in $payload_dir"
    return 1
}

tc_find_payload_nbns() {
    payload_dir=$1

    if [ -x "$payload_dir/nbns-advertiser" ]; then
        tc_log "selected nbns binary $payload_dir/nbns-advertiser"
        echo "$payload_dir/nbns-advertiser"
        return 0
    fi

    tc_log "nbns binary not found in $payload_dir"
    return 1
}

tc_select_cache_directory() {
    payload_dir=$1
    kernel_release=$(/usr/bin/uname -r 2>/dev/null || true)
    case "$kernel_release" in
        4.*) echo "$payload_dir/cache" ;;
        *) echo "$RAM_VAR" ;;
    esac
}

tc_set_payload_log_dir() {
    payload_dir=$1
    payload_volume=$2

    TC_PAYLOAD_LOG_DIR="$payload_dir/logs"
    TC_PAYLOAD_LOG_VOLUME="$payload_volume"
    TC_MDNS_LOG_FILE="$TC_PAYLOAD_LOG_DIR/mdns.log"
    TC_NBNS_LOG_FILE="$TC_PAYLOAD_LOG_DIR/nbns.log"
}

tc_payload_log_dir_ready() {
    [ -n "$TC_PAYLOAD_LOG_DIR" ] || return 1
    [ -n "$TC_PAYLOAD_LOG_VOLUME" ] || return 1
    is_volume_root_mounted "$TC_PAYLOAD_LOG_VOLUME" || return 1
    mkdir -p "$TC_PAYLOAD_LOG_DIR" || return 1
    chmod 755 "$TC_PAYLOAD_LOG_DIR" >/dev/null 2>&1 || true
    tc_prepare_smbd_core_dir "$TC_PAYLOAD_LOG_DIR" || return 1
}

tc_prepare_smbd_core_dir() {
    log_dir=$1

    [ -n "$log_dir" ] || return 1
    # Samba derives its core path from the log directory as cores/smbd.
    # Prepare it on the payload disk so panic dumps do not target RAM.
    mkdir -p "$log_dir/cores/smbd" || return 1
    chmod 700 "$log_dir/cores" "$log_dir/cores/smbd" >/dev/null 2>&1 || true
}

tc_prepare_runtime_log_file() {
    log_path=$1
    max_bytes=$TC_RUNTIME_LOG_MAX_BYTES

    case "$log_path" in
        "$TC_PAYLOAD_LOG_DIR"/*)
            if [ -n "$TC_PAYLOAD_LOG_DIR" ]; then
                tc_payload_log_dir_ready || return 1
            else
                tc_ensure_parent_dir "$log_path"
            fi
            ;;
        *)
            tc_ensure_parent_dir "$log_path"
            ;;
    esac
    : >>"$log_path" || return 1
    tc_trim_log_file_if_needed "$log_path" "$max_bytes"
}

tc_stage_runtime() {
    payload_dir=$1
    smbd_src=$2
    nbns_src=${3:-}

    cp "$smbd_src" "$TC_SMBD_BIN"
    chmod 755 "$TC_SMBD_BIN"

    for required_file in smbpasswd username.map; do
        if [ ! -f "$payload_dir/private/$required_file" ]; then
            tc_log "required Samba private file missing: $payload_dir/private/$required_file"
            return 1
        fi
        cp "$payload_dir/private/$required_file" "$RAM_PRIVATE/$required_file"
    done
    chmod 600 "$RAM_PRIVATE/smbpasswd" "$RAM_PRIVATE/username.map"
    tc_log "staged Samba auth files into RAM private directory"

    if [ "$NBNS_ENABLED" = "1" ] && [ -n "$nbns_src" ] && [ -x "$nbns_src" ]; then
        cp "$nbns_src" "$TC_NBNS_BIN"
        chmod 755 "$TC_NBNS_BIN"
        tc_log "staged nbns runtime binary"
    else
        tc_log "nbns runtime staging skipped"
    fi
}

tc_smbd_fruit_model() {
    if [ -n "${MDNS_DEVICE_MODEL:-}" ]; then
        printf '%s\n' "$MDNS_DEVICE_MODEL"
        return 0
    fi
    airport_syap=${AIRPORT_SYAP:-}
    if [ -z "$airport_syap" ]; then
        airport_syap=$(get_airport_syap 2>/dev/null || true)
    fi
    if detected_model=$(get_airport_mdns_model "$airport_syap" 2>/dev/null); then
        if [ -n "$detected_model" ]; then
            printf '%s\n' "$detected_model"
            return 0
        fi
    fi
    echo MacSamba
}

tc_generate_smb_conf() {
    payload_dir=$1
    tc_ensure_runtime_identity
    if [ -z "$TC_SMB_BIND_INTERFACES" ]; then
        tc_log "smb.conf generation failed: TC_SMB_BIND_INTERFACES is empty"
        return 1
    fi
    cache_directory=$(tc_select_cache_directory "$payload_dir")
    smbd_log="$payload_dir/logs/log.smbd"
    smbd_max_log_size=$(tc_smbd_max_log_size)
    smbd_log_level_line=
    smbd_protocol_lines=
    smbd_fruit_model=$(tc_smbd_fruit_model)

    mkdir -p "$payload_dir/logs"
    chmod 755 "$payload_dir/logs" >/dev/null 2>&1 || true
    tc_prepare_smbd_core_dir "$payload_dir/logs" || true
    if [ "$TC_SMBD_DISK_LOGGING_ENABLED" = "1" ]; then
        smbd_log_level_line="    log level = 5 vfs:8 fruit:8"
        : >>"$smbd_log" || true
        tc_log "smbd debug logging enabled at $smbd_log"
    else
        tc_prepare_log_file "$smbd_log" "$TC_RUNTIME_LOG_MAX_BYTES"
    fi
    if [ "$ANY_PROTOCOL" != "1" ]; then
        smbd_protocol_lines="    min protocol = SMB2
    max protocol = SMB3
"
    fi

    {
        cat <<EOF
[global]
    netbios name = $SMB_NETBIOS_NAME
    workgroup = WORKGROUP
    # Samba's interface enumeration can race boot networking on Time Capsule.
    # Bind to explicit IPv4/IPv6 CIDRs discovered immediately before rendering config.
    interfaces = $TC_SMB_BIND_INTERFACES
    bind interfaces only = yes
    server string = $SMB_SERVER_STRING
    security = user
    map to guest = Never
    restrict anonymous = 2
    guest account = nobody
    null passwords = no
    ea support = yes
    passdb backend = smbpasswd:$RAM_PRIVATE/smbpasswd
    username map = $RAM_PRIVATE/username.map
    dos charset = ASCII
${smbd_protocol_lines}    server multi channel support = no
    load printers = no
    disable spoolss = yes
    dfree command = /mnt/Flash/dfree.sh
    pid directory = $RAM_VAR
    lock directory = $LOCKS_ROOT
    state directory = $RAM_VAR
    cache directory = $cache_directory
    private dir = $RAM_PRIVATE
    dbwrap_tdb_max_dead:* = 0
    log file = $smbd_log
    max log size = $smbd_max_log_size
$smbd_log_level_line
    smb ports = 445
    deadtime = 60
    max open files = 512
    max smbd processes = 16
    reset on zero vc = yes
    fruit:aapl = yes
    fruit:model = $smbd_fruit_model
    fruit:advertise_fullsync = true
    fruit:nfs_aces = no
    fruit:veto_appledouble = yes
    fruit:wipe_intentionally_left_blank_rfork = yes
    fruit:delete_empty_adfiles = yes
EOF

        while IFS="$TC_TAB" read -r share_name share_path part_device builtin part_uuid; do
            [ -n "$share_name" ] || continue
            cat <<EOF

[$share_name]
    path = $share_path
    browseable = yes
    read only = no
    guest ok = no
    valid users = root
    veto files = /$PAYLOAD_DIR_NAME/
    vfs objects = catia fruit streams_xattr acl_xattr xattr_tdb
    acl_xattr:ignore system acls = yes
    streams_xattr:max xattrs per stream = 2
    fruit:resource = file
    fruit:metadata = stream
    fruit:encoding = native
    fruit:time machine = yes
    fruit:posix_rename = yes
    xattr_tdb:file = $payload_dir/private/xattr.tdb
    force user = root
    force group = wheel
    create mask = 0666
    directory mask = 0777
    force create mode = 0666
    force directory mode = 0777
EOF
        done <"$TC_SHARES_TSV"
    } >"$TC_SMBD_CONF"
}
