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

tc_smb_bind_token_is_ipv6_cidr() {
    token=$1

    case "$token" in
        ""|/*|*/|*/*/*|*[!0123456789abcdefABCDEF:./]*) return 1 ;;
        *:*) ;;
        *) return 1 ;;
    esac

    prefix_part=${token#*/}
    case "$prefix_part" in
        ""|*[!0123456789]*) return 1 ;;
    esac
    [ "$prefix_part" -le 128 ] 2>/dev/null || return 1

    return 0
}

tc_smb_bind_token_is_cidr() {
    tc_smb_bind_token_is_ipv4_cidr "$1" || tc_smb_bind_token_is_ipv6_cidr "$1"
}

tc_normalize_smb_bind_tokens() {
    bind_tokens=$1
    normalized=

    set -- $bind_tokens
    [ "$#" -gt 0 ] || return 1

    for cidr_token in "$@"; do
        tc_smb_bind_token_is_cidr "$cidr_token" || return 1
        if [ -n "$normalized" ]; then
            normalized="$normalized $cidr_token"
        else
            normalized=$cidr_token
        fi
    done

    printf '%s\n' "$normalized"
}

tc_normalize_mdns_socket_families() {
    families=$1
    saw_ipv4=0
    saw_ipv6=0
    normalized=

    set -- $families
    [ "$#" -gt 0 ] || return 1

    for family_token in "$@"; do
        case "$family_token" in
            ipv4)
                [ "$saw_ipv4" = "0" ] || return 1
                saw_ipv4=1
                ;;
            ipv6)
                [ "$saw_ipv6" = "0" ] || return 1
                saw_ipv6=1
                ;;
            *) return 1 ;;
        esac
    done

    if [ "$saw_ipv4" = "1" ]; then
        normalized=ipv4
    fi
    if [ "$saw_ipv6" = "1" ]; then
        if [ -n "$normalized" ]; then
            normalized="$normalized ipv6"
        else
            normalized=ipv6
        fi
    fi

    [ -n "$normalized" ] || return 1
    printf '%s\n' "$normalized"
}

tc_probe_smb_bind_tokens() {
    [ -x "$TC_MDNS_BIN" ] || return 1
    bind_tokens=$("$TC_MDNS_BIN" --print-smb-bind-interfaces 2>/dev/null) || return $?
    tc_normalize_smb_bind_tokens "$bind_tokens" || return 1
}

tc_probe_mdns_socket_families() {
    [ -x "$TC_MDNS_BIN" ] || return 1
    families=$("$TC_MDNS_BIN" --print-mdns-socket-families 2>/dev/null) || return $?
    tc_normalize_mdns_socket_families "$families" || return 1
}

tc_probe_nbns_socket_families() {
    [ -x "$TC_NBNS_BIN" ] || return 1
    families=$("$TC_NBNS_BIN" --print-nbns-socket-families 2>/dev/null) || return $?
    tc_normalize_mdns_socket_families "$families" || return 1
}

tc_probe_smb_bind_interfaces() {
    bind_tokens=$(tc_probe_smb_bind_tokens) || return $?
    printf '127.0.0.1/8 ::1/128 %s\n' "$bind_tokens"
}

tc_auto_ip_unavailable_status() {
    [ "$1" = "11" ]
}

tc_mark_smb_deferred_no_ip() {
    TC_MANAGER_SMB_DEFERRED_NO_IP=1
    if [ "${TC_SMB_IPV4_WAIT_LOGGED:-0}" != "1" ]; then
        tc_log "Samba bind discovery deferred; no usable address has appeared yet"
        TC_SMB_IPV4_WAIT_LOGGED=1
    fi
}
