get_iface_mac() {
    iface=$1
    /sbin/ifconfig "$iface" 2>/dev/null \
        | sed -n \
            -e 's/^[[:space:]]*ether[[:space:]]\([0-9A-Fa-f:]*\).*/\1/p' \
            -e 's/^[[:space:]]*address:*[[:space:]]\([0-9A-Fa-f:]*\).*/\1/p' \
        | sed -n '1p'
}

get_radio_mac() {
    radio_iface=$1
    /sbin/ifconfig "$radio_iface" 2>/dev/null \
        | sed -n \
            -e 's/^[[:space:]]*ether[[:space:]]\([0-9A-Fa-f:]*\).*/\1/p' \
            -e 's/^[[:space:]]*address:*[[:space:]]\([0-9A-Fa-f:]*\).*/\1/p' \
        | sed -n '1p'
}

get_airport_srcv() {
    /usr/bin/uname -a 2>/dev/null | sed -n 's/.*AirPortFW-\([0-9][0-9.]*\).*/\1/p' | sed -n '1p'
}

get_airport_acp_value() {
    acp_key=$1
    acp_value=$(/usr/bin/acp -q "$acp_key" 2>/dev/null | sed -n '1p')
    [ -n "$acp_value" ] || return 1
    printf '%s\n' "$acp_value"
}

tc_get_airport_acp_mac() {
    acp_key=$1
    acp_value=$(get_airport_acp_value "$acp_key" || true)
    [ -n "$acp_value" ] || return 1
    acp_mac=$(printf '%s\n' "$acp_value" | sed -n \
        -e 's/-/:/g' \
        -e 's/^\([0-9A-Fa-f][0-9A-Fa-f]:[0-9A-Fa-f][0-9A-Fa-f]:[0-9A-Fa-f][0-9A-Fa-f]:[0-9A-Fa-f][0-9A-Fa-f]:[0-9A-Fa-f][0-9A-Fa-f]:[0-9A-Fa-f][0-9A-Fa-f]\).*$/\1/p' \
        -e 's/^.*[^0-9A-Fa-f]\([0-9A-Fa-f][0-9A-Fa-f]:[0-9A-Fa-f][0-9A-Fa-f]:[0-9A-Fa-f][0-9A-Fa-f]:[0-9A-Fa-f][0-9A-Fa-f]:[0-9A-Fa-f][0-9A-Fa-f]:[0-9A-Fa-f][0-9A-Fa-f]\).*$/\1/p' \
        | sed -n '1p')
    case "$acp_mac" in
        ??:??:??:??:??:??) printf '%s\n' "$acp_mac" ;;
        *) return 1 ;;
    esac
}

tc_select_live_iface_mac() {
    /sbin/ifconfig -a 2>/dev/null \
        | sed -n \
            -e 's/^[[:space:]]*ether[[:space:]]\([0-9A-Fa-f:]*\).*/\1/p' \
            -e 's/^[[:space:]]*address:*[[:space:]]\([0-9A-Fa-f:]*\).*/\1/p' \
        | sed -n '1p'
}

tc_select_advertise_mac() {
    for mac_key in laMA waMA; do
        advertise_mac=$(tc_get_airport_acp_mac "$mac_key" || true)
        if [ -n "$advertise_mac" ]; then
            printf '%s\n' "$advertise_mac"
            return 0
        fi
    done

    tc_select_live_iface_mac
}

airport_acp_decimal_value() {
    acp_value=$1
    case "$acp_value" in
        0x*|0X*)
            if acp_decimal=$(printf '%d' "$acp_value" 2>/dev/null); then
                printf '%s\n' "$acp_decimal"
                return 0
            fi
            return 1
            ;;
        *)
            printf '%s\n' "$acp_value"
            ;;
    esac
}

airport_acp_hex_value() {
    acp_value=$1
    case "$acp_value" in
        0x*|0X*)
            if acp_decimal=$(printf '%d' "$acp_value" 2>/dev/null); then
                printf '0x%X\n' "$acp_decimal"
                return 0
            fi
            return 1
            ;;
        *)
            printf '%s\n' "$acp_value"
            ;;
    esac
}

airport_acp_bool_decimal_value() {
    acp_value=$1
    case "$acp_value" in
        false|False|FALSE)
            echo 0
            ;;
        true|True|TRUE)
            echo 1
            ;;
        *)
            airport_acp_decimal_value "$acp_value"
            ;;
    esac
}

get_airport_system_name() {
    get_airport_acp_value syNm
}

get_airport_host_label() {
    /bin/hostname 2>/dev/null | sed -n '1p'
}

tc_identity_first_label() {
    printf '%s\n' "$1" | sed -n '1p' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//;s/\..*$//'
}

tc_normalize_mdns_instance_name() {
    printf '%s\n' "$1" \
        | sed -n '1p' \
        | sed 's/[[:cntrl:].]/-/g;s/^[[:space:]]*//;s/[[:space:]]*$//' \
        | sed 's/^\(.\{1,63\}\).*/\1/'
}

tc_normalize_mdns_host_label() {
    tc_identity_first_label "$1" \
        | sed 'y/ABCDEFGHIJKLMNOPQRSTUVWXYZ/abcdefghijklmnopqrstuvwxyz/' \
        | sed 's/[^abcdefghijklmnopqrstuvwxyz0123456789-]/-/g;s/^-*//;s/-*$//' \
        | sed 's/^\(.\{1,63\}\).*/\1/' \
        | sed 's/^-*//;s/-*$//'
}

tc_normalize_netbios_name() {
    netbios_name=$(tc_identity_first_label "$1" \
        | sed 's/[^ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-]//g' \
        | sed 's/^\(.\{1,15\}\).*/\1/')
    case "$netbios_name" in
        *[ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789]*)
            printf '%s\n' "$netbios_name"
            ;;
        *)
            return 1
            ;;
    esac
}

tc_normalize_server_string() {
    printf '%s\n' "$1" \
        | sed -n '1p' \
        | sed 's/[[:cntrl:]]/-/g;s/^[[:space:]]*//;s/[[:space:]]*$//' \
        | sed 's/^\(.\{1,255\}\).*/\1/'
}

tc_init_runtime_identity() {
    runtime_system_name=$(get_airport_system_name || true)
    runtime_hostname=$(/bin/hostname 2>/dev/null | sed -n '1p' || true)

    runtime_host_label=$(tc_normalize_mdns_host_label "$runtime_hostname" || true)
    if [ -z "$runtime_host_label" ]; then
        runtime_host_label=$(tc_normalize_mdns_host_label "$runtime_system_name" || true)
    fi
    [ -n "$runtime_host_label" ] || runtime_host_label=timecapsule

    runtime_instance_name=$(tc_normalize_mdns_instance_name "$runtime_system_name" || true)
    [ -n "$runtime_instance_name" ] || runtime_instance_name=$runtime_host_label

    runtime_netbios_name=$(tc_normalize_netbios_name "$runtime_hostname" || true)
    if [ -z "$runtime_netbios_name" ]; then
        runtime_netbios_name=$(tc_normalize_netbios_name "$runtime_system_name" || true)
    fi
    [ -n "$runtime_netbios_name" ] || runtime_netbios_name=TimeCapsule

    runtime_server_string=$(tc_normalize_server_string "$runtime_system_name" || true)
    [ -n "$runtime_server_string" ] || runtime_server_string=$runtime_instance_name

    MDNS_INSTANCE_NAME=$runtime_instance_name
    MDNS_HOST_LABEL=$runtime_host_label
    SMB_NETBIOS_NAME=$runtime_netbios_name
    SMB_SERVER_STRING=$runtime_server_string
    TC_RUNTIME_IDENTITY_READY=1
    tc_log "runtime identity: mdns_instance=$MDNS_INSTANCE_NAME mdns_host=$MDNS_HOST_LABEL netbios=$SMB_NETBIOS_NAME server_string=$SMB_SERVER_STRING"
}

tc_ensure_runtime_identity() {
    if [ "${TC_RUNTIME_IDENTITY_READY:-0}" != "1" ]; then
        tc_init_runtime_identity
    fi
}

get_airport_rast() {
    /usr/bin/acp -A WiFi 2>/dev/null | sed -n 's/^[[:space:]]*raSt=\([^[:space:]]*\).*/\1/p' | sed -n '1p'
}

get_airport_rana() {
    acp_value=$(get_airport_acp_value raNA) || return 1
    airport_acp_bool_decimal_value "$acp_value"
}

get_airport_syfl() {
    acp_value=$(get_airport_acp_value syFl) || return 1
    airport_acp_hex_value "$acp_value"
}

get_airport_syap() {
    acp_value=$(get_airport_acp_value syAP) || return 1
    airport_acp_decimal_value "$acp_value"
}

airport_model_from_syap() {
    case "$1" in
        104) echo AirPort5,104 ;;
        105) echo AirPort5,105 ;;
        106) echo TimeCapsule6,106 ;;
        108) echo AirPort5,108 ;;
        109) echo TimeCapsule6,109 ;;
        113) echo TimeCapsule6,113 ;;
        114) echo AirPort5,114 ;;
        116) echo TimeCapsule6,116 ;;
        117) echo AirPort5,117 ;;
        119) echo TimeCapsule8,119 ;;
        120) echo AirPort7,120 ;;
        *) return 1 ;;
    esac
}

airport_model_from_syam_text() {
    syam_text=$1
    for airport_model in \
        AirPort5,104 AirPort5,105 TimeCapsule6,106 AirPort5,108 \
        TimeCapsule6,109 TimeCapsule6,113 AirPort5,114 TimeCapsule6,116 \
        AirPort5,117 TimeCapsule8,119 AirPort7,120; do
        case "$syam_text" in
            *"$airport_model"*)
                echo "$airport_model"
                return 0
                ;;
        esac
    done
    return 1
}

get_airport_mdns_model() {
    airport_syap=${1:-}
    if [ -n "$airport_syap" ]; then
        airport_model_from_syap "$airport_syap" && return 0
    fi
    airport_syam=$(get_airport_acp_value syAM || true)
    [ -n "$airport_syam" ] || return 1
    airport_model_from_syam_text "$airport_syam"
}

get_airport_syvs() {
    if airport_syvs=$(get_airport_acp_value syVs); then
        printf '%s\n' "$airport_syvs"
        return 0
    fi
    airport_srcv=$1
    digits=$(printf '%s' "$airport_srcv" | sed -n 's/^\([0-9]\)\([0-9]\)\([0-9]\).*/\1.\2.\3/p')
    if [ -n "$digits" ]; then
        echo "$digits"
        return 0
    fi
    return 1
}

get_airport_bjsd() {
    acp_value=$(get_airport_acp_value bjSd) || return 1
    airport_acp_decimal_value "$acp_value"
}

