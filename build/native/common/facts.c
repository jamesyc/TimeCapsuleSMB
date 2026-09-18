#include "plan.h"
#include "config.h"

const char *const device_acp_keys[ACP_KEY_COUNT] = {
    "raNA", "raDS", "waNM", "usbF", "laIP", "waIP", "waLL", "gnRo",
    "syNm", "waMA"
};

#ifdef TC_NATIVE_TEST
static int acp_key_index(const char *name) {
    int i;
    for (i = 0; i < ACP_KEY_COUNT; i++) {
        if (strcmp(device_acp_keys[i], name) == 0) {
            return i;
        }
    }
    return -1;
}

#endif

/* -1 is unknown, 0/1 are actual settings; missing keys use shell defaults. */
static int read_config_bool(const char *path, const char *key) {
    char value[TC_CONFIG_VALUE_MAX];
    int rc = config_read_value(path, key, value, sizeof(value));
    return rc == 1 ? 0 : rc < 0 ? -1 : config_bool_value(value, -1);
}

int device_facts_read_config(struct device_config *out, const char *path) {
    int smb_debug, mdns_debug;
    memset(out, 0, sizeof(*out));
    out->advertise_afp = read_config_bool(path, "MDNS_ADVERTISE_AFP");
    out->nbns_enabled = read_config_bool(path, "NBNS_ENABLED");
    smb_debug = read_config_bool(path, "SMBD_DEBUG_LOGGING");
    mdns_debug = read_config_bool(path, "MDNS_DEBUG_LOGGING");
    out->debug_logging = smb_debug == 1 || mdns_debug == 1 ? 1 :
        smb_debug < 0 || mdns_debug < 0 ? -1 : 0;
    return 0;
}

static void read_hostname(struct device_facts *facts) {
    if (gethostname(facts->hostname, sizeof(facts->hostname)) != 0) {
        facts->hostname[0] = '\0';
    }
    facts->hostname[sizeof(facts->hostname) - 1] = '\0';
}

/* Both drivers capture directly into the scalar facts buffers. Descriptors
 * must outlive asynchronous collection; no large raw buffer is stored here. */
static void facts_requests(struct acp_request *requests, struct device_facts *facts) {
    size_t i;
    memset(requests, 0, sizeof(*requests) * ACP_KEY_COUNT);
    for (i = 0; i < ACP_KEY_COUNT; i++) {
        requests[i].key = device_acp_keys[i];
        requests[i].trim_whitespace = 1;
        requests[i].output = facts->acp[i].text;
        requests[i].capacity = sizeof(facts->acp[i].text);
    }
}

static void facts_complete(struct device_facts *facts, const struct acp_request *requests) {
    size_t i;
    for (i = 0; i < ACP_KEY_COUNT; i++) {
        facts->acp[i].status = requests[i].status == ACP_OK && !requests[i].length ?
            ACP_UNAVAILABLE : requests[i].status;
    }
    facts->ifs_ok = iflist_collect(&facts->ifs) == 0;
    (void)device_facts_read_config(&facts->config, TC_FLASH_CONFIG_PATH);
    read_hostname(facts);
}

int facts_collect_begin(struct facts_collector *c, struct device_facts *facts) {
    memset(c, 0, sizeof(*c));
    memset(facts, 0, sizeof(*facts));
    c->facts = facts;
    facts_requests(c->requests, facts);
    if (acp_collect_begin(&c->acp, c->requests, ACP_KEY_COUNT,
                          (long long)TC_ACP_TIMEOUT_SECONDS * 1000,
                          (long long)TC_ACP_COLLECTION_BUDGET_SECONDS * 1000) != 0) {
        return facts_collect_pump(c);
    }
    return 0;
}

int facts_collect_fd(const struct facts_collector *c) {
    return c->done ? -1 : acp_collect_fd(&c->acp);
}

long long facts_collect_deadline_ms(const struct facts_collector *c) {
    return c->done ? -1 : acp_collect_deadline_ms(&c->acp);
}

int facts_collect_pump(struct facts_collector *c) {
    if (c->done) {
        return 1;
    }
    if (acp_collect_pump(&c->acp) == 0) {
        return 0;
    }
    facts_complete(c->facts, c->requests);
    c->done = 1;
    return 1;
}

void facts_collect_cancel(struct facts_collector *c) {
    if (!c->done) {
        acp_collect_cancel(&c->acp);
        c->done = 1;
    }
}

int collect_device_facts(struct device_facts *out) {
    struct acp_request requests[ACP_KEY_COUNT];
    memset(out, 0, sizeof(*out));
    facts_requests(requests, out);
    (void)acp_collect_run(requests, ACP_KEY_COUNT, (long long)TC_ACP_TIMEOUT_SECONDS * 1000,
                          (long long)TC_ACP_COLLECTION_BUDGET_SECONDS * 1000);
    facts_complete(out, requests);
    return 0;
}

#ifdef TC_NATIVE_TEST
/* ---- --facts-file: a versioned text form of device_facts (test-only) ---- */

/* Finds "key=" in a line of space-separated key=value pairs; the value ends
 * at the next space unless it is the last field (then it runs to the end,
 * so values with spaces must be written last). */
static const char *field(const char *line, const char *key, char *out, size_t out_len, int last) {
    size_t key_len = strlen(key);
    const char *p = line;
    out[0] = '\0';
    while ((p = strstr(p, key)) != NULL) {
        if ((p == line || p[-1] == ' ') && p[key_len] == '=') {
            const char *value = p + key_len + 1;
            size_t len = last ? strlen(value) : strcspn(value, " ");
            if (len >= out_len) {
                len = out_len - 1;
            }
            memcpy(out, value, len);
            out[len] = '\0';
            return out;
        }
        p += key_len;
    }
    return NULL;
}

int device_facts_parse_file(struct device_facts *out, FILE *fp) {
    char line[1024];
    char a[512], b[64], c[64];
    int version_seen = 0;

    memset(out, 0, sizeof(*out));
    while (fgets(line, sizeof(line), fp) != NULL) {
        line[strcspn(line, "\r\n")] = '\0';
        if (strncmp(line, "facts: ", 7) == 0) {
            if (field(line + 7, "version", a, sizeof(a), 0) == NULL || strcmp(a, "1") != 0) {
                return -1;
            }
            version_seen = 1;
        } else if (strncmp(line, "acp: ", 5) == 0) {
            int index;
            if (field(line + 5, "key", b, sizeof(b), 0) == NULL || (index = acp_key_index(b)) < 0 ||
                field(line + 5, "status", c, sizeof(c), 0) == NULL) {
                return -1;
            }
            out->acp[index].status = !strcmp(c, "ok") ? ACP_OK : !strcmp(c, "abort") ? ACP_ABORT : ACP_UNAVAILABLE;
            if (field(line + 5, "value", a, sizeof(a), 1) != NULL) {
                strncpy(out->acp[index].text, a, sizeof(out->acp[index].text) - 1);
            }
        } else if (strncmp(line, "hostname: ", 10) == 0) {
            strncpy(out->hostname, line + 10, sizeof(out->hostname) - 1);
        } else if (strncmp(line, "config: ", 8) == 0) {
            if (field(line + 8, "advertise_afp", a, sizeof(a), 0)) out->config.advertise_afp = atoi(a);
            if (field(line + 8, "nbns_enabled", a, sizeof(a), 0)) out->config.nbns_enabled = atoi(a);
            if (field(line + 8, "debug_logging", a, sizeof(a), 0)) out->config.debug_logging = atoi(a);
        } else if (strncmp(line, "iflist: ", 8) == 0) {
            if (field(line + 8, "ok", a, sizeof(a), 0)) out->ifs_ok = atoi(a);
            if (field(line + 8, "truncated", a, sizeof(a), 0)) out->ifs.truncated = atoi(a);
        } else if (strncmp(line, "link: ", 6) == 0) {
            struct if_link *link;
            if (out->ifs.link_count >= TC_MAX_LINKS) {
                return -1;
            }
            link = &out->ifs.links[out->ifs.link_count++];
            memset(link, 0, sizeof(*link));
            if (field(line + 6, "name", a, sizeof(a), 0)) strncpy(link->name, a, sizeof(link->name) - 1);
            if (field(line + 6, "index", a, sizeof(a), 0)) link->index = (unsigned)strtoul(a, NULL, 0);
            if (field(line + 6, "flags", a, sizeof(a), 0)) link->flags = (unsigned)strtoul(a, NULL, 0);
        } else if (strncmp(line, "addr: ", 6) == 0) {
            struct if_addr *addr;
            if (out->ifs.addr_count >= TC_MAX_ADDRS) {
                return -1;
            }
            addr = &out->ifs.addrs[out->ifs.addr_count++];
            memset(addr, 0, sizeof(*addr));
            if (field(line + 6, "link", a, sizeof(a), 0)) addr->owner_index = (unsigned)strtoul(a, NULL, 0);
            addr->scope = addr->owner_index;
            if (field(line + 6, "family", b, sizeof(b), 0) == NULL || field(line + 6, "addr", a, sizeof(a), 0) == NULL) {
                return -1;
            }
            if (strcmp(b, "inet") == 0) {
                addr->family = AF_INET;
                if (inet_pton(AF_INET, a, &addr->v4) != 1) return -1;
            } else if (strcmp(b, "inet6") == 0) {
                addr->family = AF_INET6;
                if (inet_pton(AF_INET6, a, &addr->v6) != 1) return -1;
                addr->link_local = addr->v6.s6_addr[0] == 0xfe && (addr->v6.s6_addr[1] & 0xc0) == 0x80;
            } else {
                return -1;
            }
            if (field(line + 6, "scope", a, sizeof(a), 0)) addr->scope = (unsigned)strtoul(a, NULL, 0);
            if (field(line + 6, "prefix", a, sizeof(a), 0)) addr->prefix = (unsigned)strtoul(a, NULL, 0);
        } else if (line[0] == '#' || line[0] == '\0') {
            continue;
        } else {
            return -1;
        }
    }
    if (!version_seen) {
        return -1;
    }
    return 0;
}
#endif
