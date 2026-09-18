tc_normalize_socket_families() {
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

# `service --print-smb-bind-interfaces` (v3.1.0) prints the Samba
# `interfaces =` tokens on line 1 (loopback included, every SVC_SMB link
# address, guide B.4) and `status=validated|incomplete
# reason=<word>` on line 2 (B.9). With --retain-policy the remaining lines
# carry the manager's process-local policy, which the helper validates on
# its next invocation. No state file or shell interpretation of roles.
tc_probe_smb_bind_interfaces() {
    [ -x "$TC_SERVICE_BIN" ] || return 1
    tc_bind_output=$(printf '%s\n' "${TC_MANAGER_BIND_POLICY:-policy none}" | "$TC_SERVICE_BIN" --print-smb-bind-interfaces --retain-policy 2>/dev/null) || return 1
    tc_bind_tokens=$(printf '%s\n' "$tc_bind_output" | sed -n '1p') || return 1
    tc_bind_status=$(printf '%s\n' "$tc_bind_output" | sed -n '2p') || return 1
    tc_bind_policy=$(printf '%s\n' "$tc_bind_output" | sed -n '3,$p') || return 1
    tc_bind_header=$(printf '%s\n' "$tc_bind_policy" | sed -n '1p') || return 1
    # C formats typed addresses. Only check the response boundary here; no
    # duplicate IP parser, and never permit an injected smb.conf directive.
    case "$tc_bind_tokens" in ""|*[!0123456789abcdefABCDEF:./\ ]*) return 1 ;; esac
    case "$tc_bind_status" in
        status=validated) tc_bind_state=validated; tc_bind_reason= ;;
        "status=incomplete reason="?*) tc_bind_state=incomplete; tc_bind_reason=${tc_bind_status#status=incomplete reason=} ;;
        *) return 1 ;;
    esac
    case "$tc_bind_header" in
        "policy "[123]" "[01]) ;;
        "policy none") [ "$tc_bind_state" != validated ] || return 1 ;;
        *) return 1 ;;
    esac
    TC_SMB_BIND_PROBE_TOKENS=$tc_bind_tokens
    TC_SMB_BIND_STATUS=$tc_bind_state
    TC_SMB_BIND_REASON=$tc_bind_reason
    TC_SMB_BIND_POLICY=$tc_bind_policy
}
