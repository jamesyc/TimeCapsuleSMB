#include "device.h"
#include "heartbeat_facts.h"
#include "health.h"
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

/* A new id; with token (the manager's, one per report), the same one for
 * every process that sends that report under this router ID, whatever its
 * reason: a buffer-stall retry carries the episodes merged into it since
 * its first try, and still replaces that try's row. */
static void new_heartbeat_id(char *out, size_t out_len, const char *router_id, const char *reason, const char *token) {
    static unsigned made; /* two ids in one second from one process still differ */
    char input[512];
    char hash_hex[65];
    time_t now;

    now = time(NULL);
    if (token)
        (void)snprintf(input, sizeof(input), "%s\nreport %s\n", router_id, token);
    else
        (void)snprintf(input, sizeof(input), "%s\n%s\n%ld\n%ld\n%u\n", router_id, reason, (long)now, (long)getpid(),
                       made++);
    hash_text_hex(hash_hex, sizeof(hash_hex), input);
    (void)snprintf(out, out_len, "hb1-%s", hash_hex);
}

/* The id of this heartbeat (struct tc_heartbeat_ident): the pending one
 * while the reason and router ID match it, else a new one, kept as pending
 * when there is somewhere to keep it. */
static void heartbeat_id_for(char *out, size_t out_len, struct tc_heartbeat_ident *ident, const char *token,
                             const char *router_id, const char *reason) {
    if (ident && ident->id[0] && !strcmp(ident->reason, reason) && !strcmp(ident->router_id, router_id)) {
        (void)snprintf(out, out_len, "%s", ident->id);
        return;
    }
    new_heartbeat_id(out, out_len, router_id, reason, token);
    if (!ident) return;
    (void)snprintf(ident->id, sizeof(ident->id), "%s", out);
    (void)snprintf(ident->reason, sizeof(ident->reason), "%s", reason);
    (void)snprintf(ident->router_id, sizeof(ident->router_id), "%s", router_id);
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

/* Appends the v2 fields from plan; the caller has already emitted the v1
 * body up to its closing brace position. age_sec >= 0 marks a plan resent
 * without collecting (plan_age_sec). Returns the new length or -1. */
static int append_v2_fields(char *json, size_t cap, size_t used, const struct device_plan *plan, long long age_sec) {
    size_t i, emitted;
    int n;
    n = snprintf(json + used, cap - used,
                 ",\"router_mode\":\"%s\",\"wan_setup_allowed\":%s,\"disks_over_wan\":%s,\"guest_enabled\":%s,"
                 "\"debug_logging\":%s,\"advertise_afp\":%s",
                 router_mode_name(plan->mode),
                 json_bool_or_null(plan->waNM, 1),             /* waNM=1 means setup over WAN disabled */
                 plan->usbF.available ? (plan->wan_disks_allowed ? "true" : "false") : "null",
                 plan->gnRo.available ? "true" : "false",
                 config_bool_json(plan->config.debug_logging),
                 config_bool_json(plan->config.advertise_afp));
    if (n < 0 || (size_t)n >= cap - used) return -1;
    used += (size_t)n;
    if (!plan->status.validated) {
        n = snprintf(json + used, cap - used, ",\"plan_error\":\"%s\"", plan->status.reason);
        if (n < 0 || (size_t)n >= cap - used) return -1;
        used += (size_t)n;
        if (plan->status.stale_seconds) {
            /* The roles above are the last validated plan's, this old. */
            n = snprintf(json + used, cap - used, ",\"plan_stale_sec\":%lu", plan->status.stale_seconds);
            if (n < 0 || (size_t)n >= cap - used) return -1;
            used += (size_t)n;
        }
        if (!strncmp(plan->status.reason, "iflist-", 7)) {
            /* The kernel's own totals: how far past the table a device is. */
            n = snprintf(json + used, cap - used, ",\"iflist_links\":%lu,\"iflist_addrs\":%lu",
                         (unsigned long)plan->kernel_link_count, (unsigned long)plan->kernel_addr_count);
            if (n < 0 || (size_t)n >= cap - used) return -1;
            used += (size_t)n;
        }
    }
    n = snprintf(json + used, cap - used, ",\"links\":[");
    if (n < 0 || (size_t)n >= cap - used) return -1;
    used += (size_t)n;
    for (i = 0, emitted = 0; i < plan->link_count; i++) {
        const struct link_plan *link = &plan->links[i];
        int v4 = 0, v6 = 0;
        size_t j;
        /* Every byte of a name may need a six-byte \u escape. */
        char name[IFNAMSIZ * 6 + 1];
        for (j = 0; j < link->addr_count; j++) {
            if (!addr_is_service_address(&link->addrs[j])) continue;
            if (link->addrs[j].family == AF_INET) v4 = 1; else v6 = 1;
        }
        /* The server keeps only links with a role or a service address;
         * sending the rest (bridge members, extenders) only costs bytes. */
        if (link->role == LINK_ROLE_ISOLATED && !v4 && !v6) continue;
        if (json_escape(name, sizeof(name), link->link.name) != 0) return -1;
        n = snprintf(json + used, cap - used, "%s{\"name\":\"%s\",\"role\":\"%s\",\"families\":[%s%s%s]}",
                     emitted ? "," : "", name, link_role_name(link->role),
                     v4 ? "\"ipv4\"" : "", v4 && v6 ? "," : "", v6 ? "\"ipv6\"" : "");
        if (n < 0 || (size_t)n >= cap - used) return -1;
        used += (size_t)n;
        emitted++;
    }
    n = snprintf(json + used, cap - used, "]");
    if (n < 0 || (size_t)n >= cap - used) return -1;
    used += (size_t)n;
    if (age_sec >= 0) {
        n = snprintf(json + used, cap - used, ",\"plan_age_sec\":%lld", age_sec);
        if (n < 0 || (size_t)n >= cap - used) return -1;
        used += (size_t)n;
    }
    return (int)used;
}

/* The router ID is the serial (ACP sySN) once read. A device whose ACP never
 * gave one sends "mac-" and its hardware address, so it still reports;
 * every heartbeat also carries "mac", which joins the two IDs. */
/* The largest payload fits its buffer, with room for the closing brace. */
typedef char tc_payload_fits[(TC_PAYLOAD_BASE_MAX + TC_DELIVERY_JSON_MAX + TC_HEALTH_JSON_MAX + 3 <= HEARTBEAT_MAX_JSON)
                             ? 1 : -1];

/* ,"delivery":{...} from the planner's record. */
static int append_delivery(char *json, size_t cap, size_t used, const struct tc_delivery *d) {
    static const struct tc_delivery none;
    int n;
    if (!d) d = &none;
    n = d->since ? snprintf(json + used, cap - used, ",\"delivery\":{\"failed\":%u,\"since\":%ld", d->failed, d->since)
                 : snprintf(json + used, cap - used, ",\"delivery\":{\"failed\":%u,\"since\":null", d->failed);
    if (n < 0 || (size_t)n >= cap - used) return -1;
    used += (size_t)n;
    n = snprintf(json + used, cap - used,
                 ",\"classes\":{\"local_resource\":%u,\"acp_unavailable\":%u,\"network\":%u,\"server\":%u}}",
                 d->local_resource, d->acp_unavailable, d->network, d->server);
    if (n < 0 || (size_t)n >= cap - used) return -1;
    return (int)(used + (size_t)n);
}

int telemetry_payload(char *json, size_t cap, const char *reason, const char *nonce,
                      struct tc_payload_context *ctx, struct tc_payload_result *result) {
    struct tc_heartbeat_facts *f = ctx->facts;
    char syap[HEARTBEAT_MAX_FIELD] = "";
    struct utsname system_name;
    char os_version[HEARTBEAT_MAX_FIELD * 3];
    char deploy_release_tag[HEARTBEAT_MAX_FIELD] = "";
    char router_id[HEARTBEAT_MAX_FIELD + 8];
    char heartbeat_id[96];
    char esc_router_id[192];
    char esc_heartbeat_id[192];
    char esc_reason[192];
    char esc_syam[512];
    char esc_synm[512];
    char esc_syap[128];
    char esc_os[512];
    char esc_deploy_release_tag[HEARTBEAT_MAX_FIELD * 6 + 1];
    char uptime_json[32], mac_json[24];
    long uptime_sec = 0;
    int have_uptime;
    struct tc_facts_round round;

    memset(result, 0, sizeof(*result));
    result->failure = TC_OUTCOME_SERVER;
    memset(&ctx->sent, 0, sizeof(ctx->sent));
    tc_heartbeat_facts_update(f, ctx->reuse, &round);
    result->local_resource = round.local_resource;
    if (telemetry_stop) {
        result->failure = TC_OUTCOME_CANCELLED;
        return 1;
    }
    if (tc_heartbeat_facts_router_id(f, router_id, sizeof(router_id))) {
        fputs("telemetry: no router serial or hardware address\n", stderr);
        result->failure = TC_OUTCOME_ACP_UNAVAILABLE;
        return 1;
    }
    (void)snprintf(syap, sizeof(syap), "%s", f->id[TC_ID_SYAP]);
    normalize_decimal_acp(syap);
    if (f->mac[0]) (void)snprintf(mac_json, sizeof(mac_json), "\"%s\"", f->mac);
    else (void)snprintf(mac_json, sizeof(mac_json), "null");
    os_version[0] = '\0';
    if (uname(&system_name) == 0)
        (void)snprintf(os_version, sizeof(os_version), "%s %s %s", system_name.sysname, system_name.release, system_name.machine);
    trim_line(os_version);
    have_uptime = read_uptime_seconds(&uptime_sec) == 0;
    format_uptime_json(uptime_json, sizeof(uptime_json), uptime_sec, have_uptime);
    (void)read_deploy_release_tag(deploy_release_tag, sizeof(deploy_release_tag));

    heartbeat_id_for(heartbeat_id, sizeof(heartbeat_id), ctx->ident, ctx->report_token, router_id, reason);

    if (json_escape(esc_router_id, sizeof(esc_router_id), router_id) != 0 ||
        json_escape(esc_heartbeat_id, sizeof(esc_heartbeat_id), heartbeat_id) != 0 ||
        json_escape(esc_reason, sizeof(esc_reason), reason) != 0 ||
        json_escape(esc_syam, sizeof(esc_syam), f->id[TC_ID_SYAM]) != 0 ||
        json_escape(esc_synm, sizeof(esc_synm), f->id[TC_ID_SYNM]) != 0 ||
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
                 "\"mac\":%s,"
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
                 mac_json,
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
        written = append_v2_fields(json, cap, used, &f->last,
                                   round.collected ? -1 : (acp_monotonic_ms() - f->last_ms) / 1000);
        if (written >= 0) written = append_delivery(json, cap, (size_t)written, ctx->delivery);
        if (written >= 0 && ctx->health) {
            /* Health is diagnostics: when it does not fit (with room left
             * for the closing brace), the heartbeat still goes, saying so,
             * and its events wait for the next one. */
            unsigned token;
            int with_health;
            tc_health_note_failures(ctx->health, &f->acp);
            with_health = tc_health_append_json(json, cap - 3, (size_t)written, ctx->health, &f->acp, &token);
            if (with_health >= 0) {
                written = with_health;
                ctx->sent.health_token = token;
                ctx->sent.health = 1;
            } else {
                int n = snprintf(json + written, cap - (size_t)written, ",\"health\":null");
                written = n < 0 || (size_t)n >= cap - (size_t)written ? -1 : written + n;
            }
        }
        if (written < 0 || (size_t)written + 3 > cap) {
            fprintf(stderr, "heartbeat: JSON payload too large\n");
            return 1;
        }
        strcpy(json + written, "}\n");
    }

    return 0;
}
