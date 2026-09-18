#include "device.h"
#include "../common/plan.h"
#include <sys/utsname.h>
#if defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__)
#include <sys/sysctl.h>
#endif
static void normalize_decimal_acp(char *value) {
    char *end = NULL;
    unsigned long parsed;

    if (value == NULL || value[0] == '\0') {
        return;
    }
    errno = 0;
    parsed = strtoul(value, &end, 0);
    if (errno == 0 && end != value && end != NULL && *end == '\0') {
        (void)snprintf(value, HEARTBEAT_MAX_FIELD, "%lu", parsed);
    }
}

static void append_hash_input(char *buf, size_t buf_len, const char *label, const char *value) {
    size_t used;

    if (value == NULL || value[0] == '\0') {
        return;
    }
    used = strlen(buf);
    if (used >= buf_len) {
        return;
    }
    (void)snprintf(buf + used, buf_len - used, "%s=%s\n", label, value);
}

static void bytes_to_hex(char *out, size_t out_len, const unsigned char *bytes, size_t byte_count) {
    static const char hex[] = "0123456789abcdef";
    size_t i;

    if (out_len == 0) {
        return;
    }
    for (i = 0; i < byte_count && (i * 2 + 1) < out_len; i++) {
        out[i * 2] = hex[(bytes[i] >> 4) & 0x0f];
        out[i * 2 + 1] = hex[bytes[i] & 0x0f];
    }
    out[i * 2] = '\0';
}

static void hash_text_hex(char *out, size_t out_len, const char *text) {
    unsigned char digest[64];

    crypto_hash(digest, (const unsigned char *)text, (unsigned long long)strlen(text));
    bytes_to_hex(out, out_len, digest, 32);
}

static int build_router_id(char *out, size_t out_len, const char *syap, const char *syam, const char *synm) {
    char sysn[HEARTBEAT_MAX_FIELD];
    char wama[HEARTBEAT_MAX_FIELD];
    char rama[HEARTBEAT_MAX_FIELD];
    char input[1024];
    char hash_hex[65];
    int result;

    memset(input, 0, sizeof(input));
    result = read_acp_value("sySN", sysn, sizeof(sysn));
    if (result == ACP_ABORT) return 1;
    if (result == ACP_OK) {
        (void)snprintf(out, out_len, "%s", sysn);
        return 0;
    }

    append_hash_input(input, sizeof(input), "namespace", "timecapsulesmb-router-heartbeat-v1");
    append_hash_input(input, sizeof(input), "syAP", syap);
    append_hash_input(input, sizeof(input), "syAM", syam);
    append_hash_input(input, sizeof(input), "syNm", synm);
    result = read_acp_value("waMA", wama, sizeof(wama));
    if (result == ACP_ABORT) return 1;
    if (result == ACP_OK) {
        append_hash_input(input, sizeof(input), "waMA", wama);
    }
    result = read_acp_value("raMA", rama, sizeof(rama));
    if (result == ACP_ABORT) return 1;
    if (result == ACP_OK) {
        append_hash_input(input, sizeof(input), "raMA", rama);
    }

    hash_text_hex(hash_hex, sizeof(hash_hex), input);
    (void)snprintf(out, out_len, "tc1-%s", hash_hex);
    return 0;
}

static void build_heartbeat_id(char *out, size_t out_len, const char *router_id, const char *reason) {
    char input[512];
    char hash_hex[65];
    time_t now;

    now = time(NULL);
    (void)snprintf(input, sizeof(input), "%s\n%s\n%ld\n%ld\n", router_id, reason, (long)now, (long)getpid());
    hash_text_hex(hash_hex, sizeof(hash_hex), input);
    (void)snprintf(out, out_len, "hb1-%s", hash_hex);
}

static int json_escape(char *out, size_t out_len, const char *value) {
    size_t used = 0;
    const unsigned char *cursor = (const unsigned char *)value;

    if (out_len == 0) {
        return -1;
    }
    out[0] = '\0';
    while (*cursor != '\0') {
        unsigned char ch = *cursor++;
        const char *replacement = NULL;
        char escaped[7];
        if (ch == '"' || ch == '\\') {
            escaped[0] = '\\';
            escaped[1] = (char)ch;
            escaped[2] = '\0';
            replacement = escaped;
        } else if (ch == '\b') {
            replacement = "\\b";
        } else if (ch == '\f') {
            replacement = "\\f";
        } else if (ch == '\n') {
            replacement = "\\n";
        } else if (ch == '\r') {
            replacement = "\\r";
        } else if (ch == '\t') {
            replacement = "\\t";
        } else if (ch < 0x20) {
            (void)snprintf(escaped, sizeof(escaped), "\\u%04x", (unsigned int)ch);
            replacement = escaped;
        }
        if (replacement != NULL) {
            size_t repl_len = strlen(replacement);
            if (used + repl_len >= out_len) {
                return -1;
            }
            memcpy(out + used, replacement, repl_len);
            used += repl_len;
        } else {
            if (used + 1 >= out_len) {
                return -1;
            }
            out[used++] = (char)ch;
        }
    }
    out[used] = '\0';
    return 0;
}

static void format_uptime_json(char *out, size_t out_len, long uptime_sec, int have_uptime) {
    if (out_len == 0) {
        return;
    }
    if (have_uptime && uptime_sec >= 0) {
        (void)snprintf(out, out_len, "%ld", uptime_sec);
    } else {
        (void)snprintf(out, out_len, "null");
    }
}

/* Schema v2 uses the same fresh facts/config snapshot as the plan. No ps,
 * daemon IPC, or history: plan_error reports failed facts, not service death. */
static const char *config_bool_json(int value) {
    return value < 0 ? "null" : value ? "true" : "false";
}

static const char *json_bool_or_null(struct acp_bool value, int invert) {
    if (!value.available) return "null";
    return (value.value ? 1 : 0) != invert ? "true" : "false";
}

/* Appends the v2 fields; the caller has already emitted the v1 body up to
 * its closing brace position. Returns the number of bytes written or -1. */
static int append_v2_fields(char *json, size_t cap, size_t used) {
    struct device_plan plan;
    struct plan_options options;
    size_t i;
    int n;
    memset(&options, 0, sizeof(options));
    if (device_plan_collect(&plan, NULL, &options) != 0) return -1;
    n = snprintf(json + used, cap - used,
                 ",\"router_mode\":\"%s\",\"wan_setup_allowed\":%s,\"disks_over_wan\":%s,\"guest_enabled\":%s,"
                 "\"nbns_enabled\":%s,\"debug_logging\":%s,\"advertise_afp\":%s",
                 router_mode_name(plan.mode),
                 json_bool_or_null(plan.waNM, 1),             /* waNM=1 means setup over WAN disabled */
                 plan.usbF.available ? (plan.wan_disks_allowed ? "true" : "false") : "null",
                 plan.gnRo.available ? "true" : "false",
                 config_bool_json(plan.config.nbns_enabled),
                 config_bool_json(plan.config.debug_logging),
                 config_bool_json(plan.config.advertise_afp));
    if (n < 0 || (size_t)n >= cap - used) return -1;
    used += (size_t)n;
    if (!plan.status.validated) {
        n = snprintf(json + used, cap - used, ",\"plan_error\":\"%s\"", plan.status.reason);
        if (n < 0 || (size_t)n >= cap - used) return -1;
        used += (size_t)n;
    }
    n = snprintf(json + used, cap - used, ",\"links\":[");
    if (n < 0 || (size_t)n >= cap - used) return -1;
    used += (size_t)n;
    for (i = 0; i < plan.link_count; i++) {
        const struct link_plan *link = &plan.links[i];
        int v4 = 0, v6 = 0;
        size_t j;
        char name[IFNAMSIZ * 2];
        if (json_escape(name, sizeof(name), link->link.name) != 0) return -1;
        for (j = 0; j < link->addr_count; j++) {
            if (!addr_is_service_address(&link->addrs[j])) continue;
            if (link->addrs[j].family == AF_INET) v4 = 1; else v6 = 1;
        }
        n = snprintf(json + used, cap - used, "%s{\"name\":\"%s\",\"role\":\"%s\",\"families\":[%s%s%s]}",
                     i ? "," : "", name, link_role_name(link->role),
                     v4 ? "\"ipv4\"" : "", v4 && v6 ? "," : "", v6 ? "\"ipv6\"" : "");
        if (n < 0 || (size_t)n >= cap - used) return -1;
        used += (size_t)n;
    }
    n = snprintf(json + used, cap - used, "]");
    if (n < 0 || (size_t)n >= cap - used) return -1;
    return (int)(used + (size_t)n);
}

int telemetry_payload(char *json, size_t cap, const char *reason, const char *nonce) {
    char syap[HEARTBEAT_MAX_FIELD] = "";
    char syam[HEARTBEAT_MAX_FIELD] = "";
    char synm[HEARTBEAT_MAX_FIELD] = "";
    struct utsname system_name;
    char os_version[HEARTBEAT_MAX_FIELD * 3];
    char deploy_release_tag[HEARTBEAT_MAX_FIELD] = "";
    char router_id[96];
    char heartbeat_id[96];
    char esc_router_id[192];
    char esc_heartbeat_id[192];
    char esc_reason[192];
    char esc_syam[512];
    char esc_synm[512];
    char esc_syap[128];
    char esc_os[512];
    char esc_deploy_release_tag[HEARTBEAT_MAX_FIELD * 6 + 1];
    char uptime_json[32];
    long uptime_sec = 0;
    int have_uptime;

    if (read_acp_value("syAP", syap, sizeof(syap)) == ACP_ABORT) return 1;
    normalize_decimal_acp(syap);
    if (read_acp_value("syAM", syam, sizeof(syam)) == ACP_ABORT ||
        read_acp_value("syNm", synm, sizeof(synm)) == ACP_ABORT) return 1;
    os_version[0] = '\0';
    if (uname(&system_name) == 0)
        (void)snprintf(os_version, sizeof(os_version), "%s %s %s", system_name.sysname, system_name.release, system_name.machine);
    trim_line(os_version);
    have_uptime = read_uptime_seconds(&uptime_sec) == 0;
    format_uptime_json(uptime_json, sizeof(uptime_json), uptime_sec, have_uptime);
    (void)read_deploy_release_tag(deploy_release_tag, sizeof(deploy_release_tag));

    if (build_router_id(router_id, sizeof(router_id), syap, syam, synm) || telemetry_stop) return 1;
    build_heartbeat_id(heartbeat_id, sizeof(heartbeat_id), router_id, reason);

    if (json_escape(esc_router_id, sizeof(esc_router_id), router_id) != 0 ||
        json_escape(esc_heartbeat_id, sizeof(esc_heartbeat_id), heartbeat_id) != 0 ||
        json_escape(esc_reason, sizeof(esc_reason), reason) != 0 ||
        json_escape(esc_syam, sizeof(esc_syam), syam) != 0 ||
        json_escape(esc_synm, sizeof(esc_synm), synm) != 0 ||
        json_escape(esc_syap, sizeof(esc_syap), syap) != 0 ||
        json_escape(esc_os, sizeof(esc_os), os_version) != 0 ||
        json_escape(esc_deploy_release_tag, sizeof(esc_deploy_release_tag), deploy_release_tag) != 0) {
        fprintf(stderr, "heartbeat: JSON escaping failed\n");
        return 1;
    }

    if (snprintf(json,
                 cap,
                 "{"
                 "\"schema_version\":2,"
                 "\"target_lane\":\"" TC_TELEMETRY_LANE "\","
                 "\"debug_nonce\":\"%s\","
                 "\"heartbeat_id\":\"%s\","
                 "\"router_id\":\"%s\","
                 "\"agent_version\":\"%s\","
                 "\"deploy_release_tag\":\"%s\","
                 "\"device_model\":\"%s\","
                 "\"device_syap\":\"%s\","
                 "\"device_name\":\"%s\","
                 "\"device_os_version\":\"%s\","
                 "\"uptime_sec\":%s,"
                 "\"reason\":\"%s\""
                 "}\n",
                 nonce,
                 esc_heartbeat_id,
                 esc_router_id,
                 HEARTBEAT_AGENT_VERSION,
                 esc_deploy_release_tag,
                 esc_syam,
                 esc_syap,
                 esc_synm,
                 esc_os,
                 uptime_json,
                 esc_reason) >= (int)cap) {
        fprintf(stderr, "heartbeat: JSON payload too large\n");
        return 1;
    }
    {
        /* Drop the closing "}\n", append the v2 fields, close again. */
        size_t used = strlen(json);
        int written;
        if (used < 2) return 1;
        used -= 2;
        written = append_v2_fields(json, cap, used);
        if (written < 0 || (size_t)written + 3 > cap) {
            fprintf(stderr, "heartbeat: JSON payload too large\n");
            return 1;
        }
        strcpy(json + written, "}\n");
    }

    return 0;
}
