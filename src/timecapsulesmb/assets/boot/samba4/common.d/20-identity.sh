# Samba names/model come from the same native normalization used by discovery.
# service must be staged in RAM first; never execute the disk copy here.
tc_init_runtime_identity() {
    [ -x "$TC_SERVICE_BIN" ] || return 1
    tc_identity_output=$("$TC_SERVICE_BIN" --print-samba-identity) || return 1
    {
        IFS= read -r tc_identity_header &&
        IFS= read -r tc_identity_netbios &&
        IFS= read -r tc_identity_server &&
        IFS= read -r tc_identity_model &&
        ! IFS= read -r tc_identity_extra
    } <<EOF
$tc_identity_output
EOF
    [ "$?" -eq 0 ] || return 1
    [ "$tc_identity_header" = "samba-identity 1" ] || return 1
    case "$tc_identity_netbios" in ""|*[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-]*) return 1 ;; esac
    case "$tc_identity_model" in ""|*[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789,]*) return 1 ;; esac
    [ -n "$tc_identity_server" ] || return 1
    SMB_NETBIOS_NAME=$tc_identity_netbios
    SMB_SERVER_STRING=$tc_identity_server
    SMB_FRUIT_MODEL=$tc_identity_model
    TC_RUNTIME_IDENTITY_READY=1
    tc_smbd_debug_log "runtime identity: netbios=$SMB_NETBIOS_NAME server_string=$SMB_SERVER_STRING model=$SMB_FRUIT_MODEL"
}

tc_ensure_runtime_identity() {
    [ "${TC_RUNTIME_IDENTITY_READY:-0}" = "1" ] || tc_init_runtime_identity
}
