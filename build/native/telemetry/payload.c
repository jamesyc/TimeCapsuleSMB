#include "device.h"
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

static void build_router_id(char *out, size_t out_len, const char *syap, const char *syam, const char *synm) {
    char sysn[HEARTBEAT_MAX_FIELD];
    char wama[HEARTBEAT_MAX_FIELD];
    char rama[HEARTBEAT_MAX_FIELD];
    char input[1024];
    char hash_hex[65];

    memset(input, 0, sizeof(input));
    if (read_acp_value("sySN", sysn, sizeof(sysn)) == 0) {
        (void)snprintf(out, out_len, "%s", sysn);
        return;
    }

    append_hash_input(input, sizeof(input), "namespace", "timecapsulesmb-router-heartbeat-v1");
    append_hash_input(input, sizeof(input), "syAP", syap);
    append_hash_input(input, sizeof(input), "syAM", syam);
    append_hash_input(input, sizeof(input), "syNm", synm);
    if (read_acp_value("waMA", wama, sizeof(wama)) == 0) {
        append_hash_input(input, sizeof(input), "waMA", wama);
    }
    if (read_acp_value("raMA", rama, sizeof(rama)) == 0) {
        append_hash_input(input, sizeof(input), "raMA", rama);
    }

    hash_text_hex(hash_hex, sizeof(hash_hex), input);
    (void)snprintf(out, out_len, "tc1-%s", hash_hex);
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

int telemetry_payload(char *json, size_t cap, const char *reason, const char *nonce) {
    char syap[HEARTBEAT_MAX_FIELD] = "";
    char syam[HEARTBEAT_MAX_FIELD] = "";
    char synm[HEARTBEAT_MAX_FIELD] = "";
    char uname_s[HEARTBEAT_MAX_FIELD] = "";
    char uname_r[HEARTBEAT_MAX_FIELD] = "";
    char uname_m[HEARTBEAT_MAX_FIELD] = "";
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

    (void)read_acp_value("syAP", syap, sizeof(syap));
    normalize_decimal_acp(syap);
    (void)read_acp_value("syAM", syam, sizeof(syam));
    (void)read_acp_value("syNm", synm, sizeof(synm));
    (void)read_first_line_command("/usr/bin/uname -s 2>/dev/null", uname_s, sizeof(uname_s));
    (void)read_first_line_command("/usr/bin/uname -r 2>/dev/null", uname_r, sizeof(uname_r));
    (void)read_first_line_command("/usr/bin/uname -m 2>/dev/null", uname_m, sizeof(uname_m));
    (void)snprintf(os_version, sizeof(os_version), "%s %s %s", uname_s, uname_r, uname_m);
    trim_line(os_version);
    have_uptime = read_uptime_seconds(&uptime_sec) == 0;
    format_uptime_json(uptime_json, sizeof(uptime_json), uptime_sec, have_uptime);
    (void)read_deploy_release_tag(deploy_release_tag, sizeof(deploy_release_tag));

    build_router_id(router_id, sizeof(router_id), syap, syam, synm);
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
                 "\"schema_version\":1,"
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

    return 0;
}
