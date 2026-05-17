tc_smb_bind_token_is_ipv4_cidr() {
    token=$1

    case "$token" in
        ""|/*|*/|*/*/*|*[!0123456789./]*) return 1 ;;
    esac

    ip_part=${token%/*}
    prefix_part=${token#*/}
    case "$prefix_part" in
        ""|*[!0123456789]*) return 1 ;;
    esac
    [ "$prefix_part" -le 32 ] 2>/dev/null || return 1

    old_ifs=$IFS
    IFS=.
    set -- $ip_part
    IFS=$old_ifs
    [ "$#" -eq 4 ] || return 1

    for octet in "$@"; do
        case "$octet" in
            ""|*[!0123456789]*) return 1 ;;
        esac
        [ "$octet" -le 255 ] 2>/dev/null || return 1
    done

    return 0
}

tc_normalize_smb_bind_cidrs() {
    cidrs=$1
    normalized=

    set -- $cidrs
    [ "$#" -gt 0 ] || return 1

    for cidr_token in "$@"; do
        tc_smb_bind_token_is_ipv4_cidr "$cidr_token" || return 1
        if [ -n "$normalized" ]; then
            normalized="$normalized $cidr_token"
        else
            normalized=$cidr_token
        fi
    done

    printf '%s\n' "$normalized"
}

tc_probe_auto_ip_cidrs() {
    [ -x "$TC_MDNS_BIN" ] || return 1
    cidrs=$("$TC_MDNS_BIN" --print-auto-ip-cidrs 2>/dev/null) || return $?
    tc_normalize_smb_bind_cidrs "$cidrs" || return 1
}

tc_probe_smb_bind_interfaces() {
    cidrs=$(tc_probe_auto_ip_cidrs) || return $?
    printf '127.0.0.1/8 %s\n' "$cidrs"
}

tc_auto_ip_unavailable_status() {
    [ "$1" = "11" ]
}

tc_mark_smb_deferred_no_ip() {
    TC_WATCHDOG_SMB_DEFERRED_NO_IP=1
    if [ "${TC_SMB_IPV4_WAIT_LOGGED:-0}" != "1" ]; then
        tc_log "Samba IPv4 bind discovery deferred; no usable IPv4 has appeared yet"
        TC_SMB_IPV4_WAIT_LOGGED=1
    fi
}

tc_wait_for_smb_ipv4() {
    if [ ! -x "$TC_MDNS_BIN" ]; then
        tc_log "Samba IPv4 bind discovery failed; missing $TC_MDNS_BIN"
        return 1
    fi

    while :; do
        if cidrs=$(tc_probe_auto_ip_cidrs); then
            tc_log "Samba IPv4 bind discovery: first usable IPv4 observed: $cidrs"
            return 0
        else
            probe_status=$?
        fi

        if ! tc_auto_ip_unavailable_status "$probe_status"; then
            tc_log "Samba IPv4 bind discovery failed with exit code $probe_status"
            return 1
        fi

        tc_mark_smb_deferred_no_ip
        sleep "$TC_SMB_IPV4_STARTUP_POLL_SECONDS"
    done
}

tc_refresh_smb_bind_interfaces() {
    if bind_interfaces=$(tc_probe_smb_bind_interfaces); then
        TC_SMB_BIND_INTERFACES=$bind_interfaces
        TC_WATCHDOG_SMB_DEFERRED_NO_IP=0
        tc_log "Samba IPv4 bind interfaces: $TC_SMB_BIND_INTERFACES"
        return 0
    else
        probe_status=$?
    fi

    if tc_auto_ip_unavailable_status "$probe_status"; then
        tc_mark_smb_deferred_no_ip
    else
        tc_log "Samba IPv4 bind interface probe failed with exit code $probe_status"
    fi
    return 1
}

tc_prepare_smb_bind_context() {
    tc_wait_for_smb_ipv4 || return 1
    tc_log "Samba IPv4 bind discovery: waiting ${TC_SMB_IPV4_SETTLE_SECONDS}s for network stabilization"
    sleep "$TC_SMB_IPV4_SETTLE_SECONDS"
    tc_refresh_smb_bind_interfaces
}
