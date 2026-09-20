#!/bin/sh

PATH=/bin:/sbin:/usr/bin:/usr/sbin

RAM_ROOT=/mnt/Memory/samba4
RAM_SBIN="$RAM_ROOT/sbin"
RAM_ETC="$RAM_ROOT/etc"
RAM_VAR="$RAM_ROOT/var"
RAM_PRIVATE="$RAM_ROOT/private"
LOCKS_ROOT=/mnt/Locks

DISCOVERY_PROC_NAME=discoveryd
RSYNC_PROC_NAME=rsync

TC_CONFIG_FILE=/mnt/Flash/tcapsulesmb.conf
TC_STATE_DIR="$RAM_VAR"
TC_TAB=$(printf '\t')

TC_LOG_FILE="$TC_STATE_DIR/runtime.log"
TC_LOG_PREFIX=runtime
TC_LOG_MAX_BYTES=32768
TC_DISCOVERY_BIN=/mnt/Flash/discoveryd
TC_SERVICE_BIN="$RAM_SBIN/service"
TC_TELEMETRY_BIN="$RAM_SBIN/telemetry"
TC_RSYNC_BIN="$RAM_SBIN/rsync"
TC_RSYNC_CONF="$RAM_ETC/rsyncd.conf"
TC_SMBD_BIN="$RAM_SBIN/smbd"
TC_SMBD_CONF="$RAM_ETC/smb.conf"
TC_DISCOVERY_LOG_FILE="$RAM_VAR/discovery.log"
TC_RSYNC_LOG_FILE="$RAM_VAR/rsync.log"
TC_PAYLOAD_LOG_DIR=
TC_PAYLOAD_LOG_VOLUME=
TC_RUNTIME_LOG_MAX_BYTES=32768
TC_SMBD_DISK_LOGGING_ENABLED=0
# Publish SMB-only Time Machine flags by default. Enabling AFP advertisement
# below switches generated ADisk rows to the Apple AFP+SMB compatibility flag.
TC_ADISK_DISK_ADVF=0x82
TC_ADISK_TXT_MAX_BYTES=255
TC_ADISK_TXT_ADVF_PREFIX_BYTES=6
TC_ADISK_TXT_ADVN_MID_BYTES=6
TC_ADISK_TXT_ADVU_PREFIX_BYTES=6
TC_SAMBA_VM_BUFCACHE=5
TC_SMB_BIND_INTERFACES=${TC_SMB_BIND_INTERFACES:-}
# The manager owns the last validated Samba bind projection (guide B.9):
# process-local shell state, never a file. `service --print-smb-bind-interfaces`
# reports tokens plus a status line; only `validated` runs may change it.
TC_SMB_BIND_STATUS=
TC_SMB_BIND_REASON=
TC_SMB_BIND_POLICY=
TC_MANAGER_BIND_POLICY=
TC_SMB_BIND_PROBE_TOKENS=
TC_MANAGER_LAST_VALIDATED_BIND_TOKENS=
TC_MANAGER_LAST_VALIDATED_BIND_TIME=
TC_MANAGER_LAST_DISCOVERY_SIGNATURE=
TC_RUNTIME_IDENTITY_READY=
TC_DISCOVERY_PID=
SMB_FRUIT_MODEL=
SMB_SERVER_STRING=
TC_DISKD_USE_VOLUME_MOUNT_TIMEOUT_SECONDS=31
TC_DISKD_USE_VOLUME_MOUNT_POLL_SECONDS=3
TC_RUNTIME_ENV_WARNING_LINES=
TC_RUNTIME_ENV_WARNINGS_LOGGED=0

LEGACY_PREFIX_NETBSD7=/root/tc-netbsd7
LEGACY_PREFIX_NETBSD4=/root/tc-netbsd4
LEGACY_PREFIX_NETBSD4LE=/root/tc-netbsd4le
LEGACY_PREFIX_NETBSD4BE=/root/tc-netbsd4be

tc_add_runtime_env_warning() {
    warning_line=$1

    if [ -n "$TC_RUNTIME_ENV_WARNING_LINES" ]; then
        TC_RUNTIME_ENV_WARNING_LINES="$TC_RUNTIME_ENV_WARNING_LINES
$warning_line"
    else
        TC_RUNTIME_ENV_WARNING_LINES=$warning_line
    fi
}

tc_log_runtime_env_warnings() {
    [ -n "$TC_RUNTIME_ENV_WARNING_LINES" ] || return 0
    [ "$TC_RUNTIME_ENV_WARNINGS_LOGGED" = "1" ] && return 0

    printf '%s\n' "$TC_RUNTIME_ENV_WARNING_LINES" | while IFS= read -r warning_line || [ -n "$warning_line" ]; do
        [ -n "$warning_line" ] || continue
        tc_log "$warning_line"
    done
    TC_RUNTIME_ENV_WARNINGS_LOGGED=1
}

tc_sanitize_unsigned_integer() {
    value=$1
    default_value=$2
    case "$value" in
        ""|*[!0123456789]*) echo "$default_value" ;;
        *) echo "$value" ;;
    esac
}

tc_sanitize_positive_integer() {
    value=$1
    default_value=$2
    case "$value" in
        ""|*[!0123456789]*|0) echo "$default_value" ;;
        *) echo "$value" ;;
    esac
}

tc_is_unsigned_integer() {
    case "$1" in
        ""|*[!0123456789]*) return 1 ;;
        *) return 0 ;;
    esac
}

tc_init_runtime_env() {
    PAYLOAD_DIR_NAME=.samba4
    DISKD_USE_VOLUME_ATTEMPTS=${DISKD_USE_VOLUME_ATTEMPTS:-2}
    DISKD_USE_VOLUME_MOUNT_TIMEOUT_SECONDS=${DISKD_USE_VOLUME_MOUNT_TIMEOUT_SECONDS:-31}
    DISKD_USE_VOLUME_MOUNT_POLL_SECONDS=${DISKD_USE_VOLUME_MOUNT_POLL_SECONDS:-3}
    TC_RUNTIME_ENV_WARNING_LINES=
    TC_RUNTIME_ENV_WARNINGS_LOGGED=0
    TC_DISKD_USE_VOLUME_MOUNT_TIMEOUT_SECONDS=$(tc_sanitize_unsigned_integer "$DISKD_USE_VOLUME_MOUNT_TIMEOUT_SECONDS" 31)
    if [ "$TC_DISKD_USE_VOLUME_MOUNT_TIMEOUT_SECONDS" != "$DISKD_USE_VOLUME_MOUNT_TIMEOUT_SECONDS" ]; then
        tc_add_runtime_env_warning "runtime config: invalid DISKD_USE_VOLUME_MOUNT_TIMEOUT_SECONDS=$DISKD_USE_VOLUME_MOUNT_TIMEOUT_SECONDS; using 31s"
    fi
    TC_DISKD_USE_VOLUME_MOUNT_POLL_SECONDS=$(tc_sanitize_positive_integer "$DISKD_USE_VOLUME_MOUNT_POLL_SECONDS" 3)
    if [ "$TC_DISKD_USE_VOLUME_MOUNT_POLL_SECONDS" != "$DISKD_USE_VOLUME_MOUNT_POLL_SECONDS" ]; then
        tc_add_runtime_env_warning "runtime config: invalid DISKD_USE_VOLUME_MOUNT_POLL_SECONDS=$DISKD_USE_VOLUME_MOUNT_POLL_SECONDS; using 3s"
    fi
    ATA_IDLE_SECONDS=${ATA_IDLE_SECONDS:-300}
    ATA_STANDBY=${ATA_STANDBY:-}
    MAST_DISCOVERY_WAIT_SECONDS=${MAST_DISCOVERY_WAIT_SECONDS:-120}
    MANAGER_TOPOLOGY_DEBOUNCE_SECONDS=${MANAGER_TOPOLOGY_DEBOUNCE_SECONDS:-${WATCHDOG_TOPOLOGY_DEBOUNCE_SECONDS:-5}}
    INTERNAL_SHARE_USE_DISK_ROOT=${INTERNAL_SHARE_USE_DISK_ROOT:-0}
    SMB_BROWSE_COMPATIBILITY=${SMB_BROWSE_COMPATIBILITY:-0}
    MDNS_ADVERTISE_AFP=${MDNS_ADVERTISE_AFP:-0}
    ANY_PROTOCOL=${ANY_PROTOCOL:-0}
    REQUIRE_SMB_ENCRYPTION=${REQUIRE_SMB_ENCRYPTION:-0}
    FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION=${FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION:-0}
    FRUIT_METADATA_NETATALK=${FRUIT_METADATA_NETATALK:-1}
    VFS_AIO_FORK_ENABLED=${VFS_AIO_FORK_ENABLED:-0}
    NBNS_ENABLED=${NBNS_ENABLED:-0}
    RSYNC_ENABLED=${RSYNC_ENABLED:-0}
    TC_SMBD_DISK_LOGGING_ENABLED=${SMBD_DEBUG_LOGGING:-0}

    case "$MDNS_ADVERTISE_AFP" in
        1|true|TRUE|yes|YES)
            MDNS_ADVERTISE_AFP=1
            TC_ADISK_DISK_ADVF=0x83
            ;;
        0|false|FALSE|no|NO)
            MDNS_ADVERTISE_AFP=0
            TC_ADISK_DISK_ADVF=0x82
            ;;
        *)
            tc_add_runtime_env_warning "runtime config: invalid MDNS_ADVERTISE_AFP=$MDNS_ADVERTISE_AFP; using 0"
            MDNS_ADVERTISE_AFP=0
            TC_ADISK_DISK_ADVF=0x82
            ;;
    esac

    case "$RSYNC_ENABLED" in
        1|true|TRUE|yes|YES) RSYNC_ENABLED=1 ;;
        0|false|FALSE|no|NO) RSYNC_ENABLED=0 ;;
        *)
            tc_add_runtime_env_warning "runtime config: invalid RSYNC_ENABLED=$RSYNC_ENABLED; using 0"
            RSYNC_ENABLED=0
            ;;
    esac
}

tc_set_log() {
    TC_LOG_FILE=$1
    TC_LOG_PREFIX=$2
    TC_LOG_MAX_BYTES=$TC_RUNTIME_LOG_MAX_BYTES
}

tc_runtime_logs_unbounded() {
    [ "${SMBD_DEBUG_LOGGING:-0}" = "1" ] || [ "${MDNS_DEBUG_LOGGING:-0}" = "1" ]
}

tc_smbd_max_log_size() {
    if [ "$TC_SMBD_DISK_LOGGING_ENABLED" = "1" ]; then
        echo 0
    else
        echo 128
    fi
}

tc_log_file_size() {
    log_path=$1
    [ -f "$log_path" ] || {
        echo 0
        return 0
    }
    set -- $(/bin/ls -ln "$log_path" 2>/dev/null)
    case "${5:-}" in
        ""|*[!0123456789]*) echo 0 ;;
        *) echo "$5" ;;
    esac
}

tc_replace_log_with_trimmed_copy() {
    trim_log_path=$1
    trim_log_bytes=$2
    trim_log_tmp=$3

    if /usr/bin/tail -c "$trim_log_bytes" "$trim_log_path" >"$trim_log_tmp" 2>/dev/null || /bin/cat "$trim_log_path" >"$trim_log_tmp" 2>/dev/null; then
        # Failed readers can leave an empty temporary file from redirection; never let that erase an existing log.
        if [ -s "$trim_log_tmp" ] || [ ! -s "$trim_log_path" ]; then
            mv "$trim_log_tmp" "$trim_log_path"
            return $?
        fi
    fi

    rm -f "$trim_log_tmp"
    return 0
}

tc_trim_log_file_if_needed() {
    trim_log_path=$1
    trim_log_bytes=$2
    trim_log_tmp="${trim_log_path}.tmp.$$"

    [ "$trim_log_bytes" -gt 0 ] || return 0
    [ -f "$trim_log_path" ] || return 0
    current_size=$(tc_log_file_size "$trim_log_path")
    [ -n "$current_size" ] || current_size=0
    [ "$current_size" -gt "$trim_log_bytes" ] || return 0

    tc_replace_log_with_trimmed_copy "$trim_log_path" "$trim_log_bytes" "$trim_log_tmp"
}

tc_ram_rewrite_log_line() {
    log_path=$1
    line=$2
    log_dir=${log_path%/*}

    [ -d "$log_dir" ] || mkdir -p "$log_dir"
    echo "$line" >>"$log_path"
    tc_trim_log_file_if_needed "$log_path" "$TC_LOG_MAX_BYTES"
}

tc_ensure_parent_dir() {
    target_path=$1
    parent_dir=${target_path%/*}
    if [ -n "$parent_dir" ] && [ "$parent_dir" != "$target_path" ]; then
        mkdir -p "$parent_dir"
    fi
}

tc_prepare_log_file() {
    prepare_log_path=$1
    prepare_log_bytes=${2:-65536}

    tc_ensure_parent_dir "$prepare_log_path"
    if [ -f "$prepare_log_path" ]; then
        tc_trim_log_file_if_needed "$prepare_log_path" "$prepare_log_bytes"
    else
        : >"$prepare_log_path"
    fi
}

tc_now_seconds() {
    now_seconds=$(date '+%s' 2>/dev/null || echo 0)
    case "$now_seconds" in
        ""|*[!0123456789]*) echo 0 ;;
        *) echo "$now_seconds" ;;
    esac
}

# MaSt is needed before RAM service exists. The Flash helper performs this
# bounded read with the shared C collector, without temporary capture files.
TC_ACP_QUERY_SECONDS=${TC_ACP_QUERY_SECONDS:-20}
tc_read_mast() {
    "$TC_DISCOVERY_BIN" --print-mast --timeout-seconds "$TC_ACP_QUERY_SECONDS"
}

tc_elapsed_seconds_since() {
    elapsed_start_seconds=$1
    elapsed_end_seconds=$(tc_now_seconds)
    if ! tc_is_unsigned_integer "$elapsed_start_seconds"; then
        echo 0
        return 0
    fi
    elapsed_seconds=$((elapsed_end_seconds - elapsed_start_seconds))
    [ "$elapsed_seconds" -ge 0 ] || elapsed_seconds=0
    echo "$elapsed_seconds"
}

tc_log_timestamp() {
    date '+%Y-%m-%d %H:%M:%S'
}

tc_log() {
    line="$(tc_log_timestamp) $TC_LOG_PREFIX: $*"
    tc_ram_rewrite_log_line "$TC_LOG_FILE" "$line"
}

tc_smbd_debug_logging_enabled() {
    [ "${SMBD_DEBUG_LOGGING:-0}" = "1" ]
}

tc_smbd_debug_log() {
    if tc_smbd_debug_logging_enabled; then
        tc_log "$@"
    fi
}
