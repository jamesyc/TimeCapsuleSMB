tc_fstat_line_matches_socket() {
    family=$1
    sock_type=$2
    proto=$3
    port=$4
    line=$5

    case "$line" in
        *" $family $sock_type $proto "*":$port"*) return 0 ;;
        *) return 1 ;;
    esac
}

tc_runtime_process_table() {
    /bin/ps axww -o pid= -o stat= -o ucomm= -o command= 2>/dev/null
}

tc_runtime_fstat_pid() {
    /usr/bin/fstat -p "$1" 2>/dev/null
}

tc_fstat_line_is_ipv4_tcp_445() {
    tc_fstat_line_matches_socket internet stream tcp 445 "$1"
}

tc_fstat_line_is_ipv6_tcp_445() {
    tc_fstat_line_matches_socket internet6 stream tcp 445 "$1"
}

tc_bind_interfaces_have_ipv6() {
    set -- ${TC_SMB_BIND_INTERFACES:-}
    for token in "$@"; do
        case "$token" in
            ::1/128) ;;
            *:*) return 0 ;;
        esac
    done
    return 1
}

tc_smbd_bound_ipv4_445() {
    tc_process_has_fstat_socket smbd internet stream tcp 445
}

tc_smbd_bound_ipv6_445() {
    tc_process_has_fstat_socket smbd internet6 stream tcp 445
}

tc_process_has_fstat_socket() {
    proc_name=$1
    family=$2
    sock_type=$3
    proto=$4
    port=$5

    if ps_out=$(tc_runtime_process_table); then
        old_ifs=$IFS
        IFS='
'
        for line in $ps_out; do
            [ -n "$line" ] || continue
            line_ifs=$IFS
            IFS=' 	'
            set -- $line
            IFS=$line_ifs
            [ "$#" -ge 3 ] || continue
            case "$2" in
                Z*) continue ;;
            esac
            [ "$3" = "$proc_name" ] || continue

            if fstat_out=$(tc_runtime_fstat_pid "$1"); then
                fstat_ifs=$IFS
                IFS='
'
                for fstat_line in $fstat_out; do
                    if tc_fstat_line_matches_socket "$family" "$sock_type" "$proto" "$port" "$fstat_line"; then
                        IFS=$old_ifs
                        return 0
                    fi
                done
                IFS=$fstat_ifs
            fi
        done
        IFS=$old_ifs
    fi

    return 1
}

tc_smbd_bound_tcp_445() {
    if tc_bind_interfaces_have_nonloopback_ipv4; then
        tc_smbd_bound_ipv4_445 || return 1
    fi
    if tc_bind_interfaces_have_ipv6; then
        tc_smbd_bound_ipv6_445 || return 1
    fi
    if ! tc_bind_interfaces_have_nonloopback_ipv4 && ! tc_bind_interfaces_have_ipv6; then
        tc_smbd_bound_ipv4_445 || return 1
    fi
    return 0
}

tc_fstat_line_is_ipv4_udp_port() {
    tc_fstat_line_matches_socket internet dgram udp "$1" "$2"
}

tc_fstat_line_is_ipv6_udp_port() {
    tc_fstat_line_matches_socket internet6 dgram udp "$1" "$2"
}

tc_process_bound_ipv4_udp_port() {
    proc_name=$1
    port=$2

    tc_process_has_fstat_socket "$proc_name" internet dgram udp "$port"
}

tc_process_bound_ipv6_udp_port() {
    proc_name=$1
    port=$2

    tc_process_has_fstat_socket "$proc_name" internet6 dgram udp "$port"
}

tc_bind_interfaces_have_nonloopback_ipv4() {
    set -- ${TC_SMB_BIND_INTERFACES:-}
    for token in "$@"; do
        case "$token" in
            127.*) ;;
            *.*/*) return 0 ;;
        esac
    done
    return 1
}

tc_nbns_bound_ipv4_udp_137() {
    tc_process_bound_ipv4_udp_port "$NBNS_PROC_NAME" 137
}

tc_mdns_bound_ipv4_udp_5353() {
    tc_process_bound_ipv4_udp_port "$MDNS_PROC_NAME" 5353
}

tc_mdns_bound_ipv6_udp_5353() {
    tc_process_bound_ipv6_udp_port "$MDNS_PROC_NAME" 5353
}

tc_process_bound_required_udp_families() {
    proc_name=$1
    port=$2
    families=$3
    saw_family=0

    set -- $families
    for family in "$@"; do
        case "$family" in
            ipv4)
                saw_family=1
                tc_process_bound_ipv4_udp_port "$proc_name" "$port" || return 1
                ;;
            ipv6)
                saw_family=1
                tc_process_bound_ipv6_udp_port "$proc_name" "$port" || return 1
                ;;
            *) return 1 ;;
        esac
    done

    [ "$saw_family" = "1" ]
}

tc_mdns_bound_udp_5353() {
    families=$(tc_probe_mdns_socket_families) || return $?
    tc_process_bound_required_udp_families "$MDNS_PROC_NAME" 5353 "$families"
}

tc_nbns_bound_udp_137() {
    if ! families=$(tc_probe_nbns_socket_families); then
        tc_nbns_bound_ipv4_udp_137
        return $?
    fi
    tc_process_bound_required_udp_families "$NBNS_PROC_NAME" 137 "$families"
}

tc_wait_for_smbd_ipv4_445() {
    max_attempts=${1:-10}
    attempt=0
    while [ "$attempt" -lt "$max_attempts" ]; do
        if tc_smbd_bound_tcp_445; then
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    return 1
}

tc_log_smbd_socket_diagnostics() {
    if ps_out=$(/bin/ps axww -o pid= -o stat= -o ucomm= -o command= 2>/dev/null); then
        old_ifs=$IFS
        IFS='
'
        for line in $ps_out; do
            [ -n "$line" ] || continue
            line_ifs=$IFS
            IFS=' 	'
            set -- $line
            IFS=$line_ifs
            [ "$#" -ge 3 ] || continue
            case "$2" in
                Z*) continue ;;
            esac
            [ "$3" = "smbd" ] || continue

            if fstat_out=$(/usr/bin/fstat -p "$1" 2>/dev/null); then
                fstat_ifs=$IFS
                IFS='
'
                for fstat_line in $fstat_out; do
                    case "$fstat_line" in
                        *":445"*) tc_log "smbd socket diagnostic: $fstat_line" ;;
                    esac
                done
                IFS=$fstat_ifs
            fi
        done
        IFS=$old_ifs
    fi
}

tc_reload_smbd_config() {
    smbd_pid=$(tc_smbd_parent_pid || true)
    if [ -z "$smbd_pid" ]; then
        tc_log "watchdog recovery: smbd config reload skipped; missing valid $RAM_VAR/smbd.pid"
        return 1
    fi

    # Samba's parent process reloads services on SIGHUP. Signal only the
    # parent pid file instead of pkilling every smbd child, so active SMB
    # sessions can keep running while new share definitions are loaded.
    if kill -HUP "$smbd_pid" >/dev/null 2>&1; then
        tc_log "watchdog recovery: smbd config reload requested with SIGHUP pid $smbd_pid"
        return 0
    fi

    tc_log "watchdog recovery: smbd config reload failed for pid $smbd_pid"
    return 1
}
