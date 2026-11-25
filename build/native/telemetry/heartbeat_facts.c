#include "heartbeat_facts.h"
#include "errclass.h"
#include "../common/acp.h"

#define IDENTITY_REQUESTS 3 /* sySN, syAP, syAM; syNm is one of the plan's keys */
static const char *const identity_keys[IDENTITY_REQUESTS] = {"sySN", "syAP", "syAM"};

static int hex_digit(int ch) {
    if (ch >= '0' && ch <= '9') return ch - '0';
    if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
    if (ch >= 'A' && ch <= 'F') return ch - 'A' + 10;
    return -1;
}

int tc_mac_parse(const char *text, char out[18]) {
    unsigned char mac[6];
    size_t i, p;
    out[0] = '\0';
    if (!text) return -1;
    while (*text == ' ' || *text == '\t') text++;
    /* XX:XX:XX:XX:XX:XX, as acp prints waMA, with only trailing space after. */
    for (i = 0, p = 0; i < 6; i++, p += 3) {
        int high = hex_digit((unsigned char)text[p]), low = high < 0 ? -1 : hex_digit((unsigned char)text[p + 1]);
        if (low < 0 || (i < 5 && text[p + 2] != ':')) return -1;
        mac[i] = (unsigned char)(high * 16 + low);
    }
    for (p = 17; text[p]; p++)
        if (text[p] != ' ' && text[p] != '\t' && text[p] != '\r' && text[p] != '\n') return -1;
    if (mac[0] & 1) return -1; /* multicast, broadcast */
    for (i = 0; i < 6 && !mac[i]; i++) {}
    if (i == 6) return -1;
    if (addr_mac_is_boot_placeholder(mac)) return -1;
    (void)snprintf(out, 18, "%02x:%02x:%02x:%02x:%02x:%02x", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    return 0;
}

/* A waMA answer (text, or NULL when there was none): a usable one is kept,
 * with a line when it is new. */
static void note_wama(struct tc_heartbeat_facts *f, const char *text) {
    char usable[18];
    if (tc_mac_parse(text, usable) || !strcmp(f->wama, usable)) return;
    (void)snprintf(f->wama, sizeof(f->wama), "%s", usable);
    fprintf(stderr, "telemetry: hardware address %s from ACP waMA\n", usable);
}

/* What this heartbeat sends: the kept waMA, else the interface table's
 * address of the moment when it is not a placeholder, else nothing. */
static void choose_mac(struct tc_heartbeat_facts *f) {
    static struct if_table table;
    unsigned char mac[6];
    if (f->wama[0]) {
        (void)snprintf(f->mac, sizeof(f->mac), "%s", f->wama);
        return;
    }
    f->mac[0] = '\0';
    /* addr_device_mac refuses the boot placeholder itself. */
    if (iflist_collect(&table) != 0 || addr_device_mac(&table, mac) != 0) return;
    (void)snprintf(f->mac, sizeof(f->mac), "%02x:%02x:%02x:%02x:%02x:%02x", mac[0], mac[1], mac[2], mac[3], mac[4],
                   mac[5]);
}

/* A child that could not start for want of resources: counted, and the
 * device's state rather than ACP's. */
static int start_failed_for_resources(const struct acp_request *r) {
    if (r->status != ACP_ABORT) return 0;
    if (r->reason == ACP_REASON_FORK) return tc_errno_record(TC_OP_FORK, r->error) == TC_ERR_RESOURCE;
    if (r->reason == ACP_REASON_PIPE) return tc_errno_class(TC_OP_PIPE, r->error) == TC_ERR_RESOURCE;
    return 0;
}

/* Keeps a key's value. acp prints nothing and exits 0 for any failed get,
 * an ACPd that cannot answer included (README, "Failed gets"), so an empty
 * answer cannot tell "not set" from "no answer" and never replaces a kept
 * value: every unit has a serial, model and name. Returns whether the
 * answer had a value. */
static int keep(struct tc_heartbeat_facts *f, int index, int status, const char *value) {
    char text[HEARTBEAT_MAX_FIELD];
    if (status != ACP_OK) return 0;
    (void)snprintf(text, sizeof(text), "%s", value);
    trim_line(text);
    if (!text[0]) return 0;
    (void)snprintf(f->id[index], sizeof(f->id[index]), "%s", text);
    return 1;
}

void tc_heartbeat_facts_update(struct tc_heartbeat_facts *f, struct tc_facts_reuse reuse, struct tc_facts_round *round) {
    /* Static: device_facts holds the interface table. */
    static struct device_facts facts;
    struct acp_request requests[IDENTITY_REQUESTS + ACP_KEY_COUNT];
    char values[IDENTITY_REQUESTS][ACP_VALUE_MAX];
    struct plan_options options;
    size_t i;
    int empty = 1, aborted = 0, had_serial, got[IDENTITY_REQUESTS];
    memset(round, 0, sizeof(*round));
    if (tc_heartbeat_facts_reusable(f, reuse, acp_monotonic_ms())) {
        choose_mac(f);
        return;
    }
    memset(&facts, 0, sizeof(facts));
    memset(requests, 0, sizeof(requests));
    for (i = 0; i < IDENTITY_REQUESTS; i++) {
        requests[i].key = identity_keys[i];
        requests[i].trim_whitespace = 1;
        requests[i].output = values[i];
        requests[i].capacity = sizeof(values[i]);
    }
    device_facts_requests(requests + IDENTITY_REQUESTS, &facts);
    (void)acp_collect_run(requests, IDENTITY_REQUESTS + ACP_KEY_COUNT, (long long)TC_ACP_TIMEOUT_SECONDS * 1000,
                          (long long)TC_ACP_COLLECTION_BUDGET_SECONDS * 1000, ACP_STOP_EARLY);
    device_facts_complete(&facts, requests + IDENTITY_REQUESTS);
    round->collected = 1;
    for (i = 0; i < IDENTITY_REQUESTS + ACP_KEY_COUNT; i++) {
        if (requests[i].status == ACP_ABORT) aborted = 1;
        if (requests[i].status == ACP_ABORT && requests[i].reason == ACP_REASON_TIMEOUT) round->timed_out = 1;
        if (requests[i].status == ACP_ABORT && requests[i].reason == ACP_REASON_BUDGET) round->budget_spent = 1;
        if (start_failed_for_resources(&requests[i])) round->local_resource = 1;
    }
    if (round->timed_out) f->acp.timeouts++;
    if (round->budget_spent) f->acp.budget_exhausted++;
    /* Every identity key answered, none with a value: an ACPd that cannot
     * answer. A key that timed out or never ran is not an answer. */
    had_serial = f->id[TC_ID_SERIAL][0] != '\0';
    for (i = 0; i < IDENTITY_REQUESTS; i++) {
        got[i] = keep(f, (int)i, requests[i].status, values[i]);
        if (got[i] || requests[i].status == ACP_ABORT) empty = 0;
    }
    if (empty) f->acp.empty++;
    /* Complete, so a retry may keep these facts (tc_heartbeat_facts_reusable):
     * every key answered, not every identity key with nothing (an ACPd
     * that cannot answer), and the serial came back if this process read
     * one before. A unit that never gave a serial still keeps its facts. */
    f->last_complete = !aborted && !empty && (got[TC_ID_SERIAL] || !had_serial);
    keep(f, TC_ID_SYNM, facts.acp[ACP_KEY_syNm].status, facts.acp[ACP_KEY_syNm].text);
    note_wama(f, facts.acp[ACP_KEY_waMA].status == ACP_OK ? facts.acp[ACP_KEY_waMA].text : NULL);
    choose_mac(f);
    memset(&options, 0, sizeof(options));
    f->last_ms = acp_monotonic_ms();
    (void)device_plan_build(&f->last, &facts, f->have_history ? &f->history : NULL, &options, f->last_ms);
    device_plan_history_update(&f->history, &f->have_history, &f->last);
    f->have_last = 1;
}

/* The age is compared in whole seconds, never max_age scaled up to
 * milliseconds: TC_FACTS_FOREVER is LONG_MAX, and long is 32 bits on the
 * devices. floor(age_ms / 1000) < max_age exactly when age_ms < max_age *
 * 1000, so the boundary is the same. */
int tc_heartbeat_facts_reusable(const struct tc_heartbeat_facts *f, struct tc_facts_reuse reuse, long long now_ms) {
    if (!f->have_last || (reuse.complete_only && !f->last_complete) || now_ms < f->last_ms) return 0;
    if (tc_heartbeat_facts_router_id(f, NULL, 0)) return 0;
    return (now_ms - f->last_ms) / 1000 < (long long)reuse.max_age;
}

int tc_heartbeat_facts_router_id(const struct tc_heartbeat_facts *f, char *out, size_t len) {
    if (f->id[TC_ID_SERIAL][0]) {
        (void)snprintf(out, len, "%s", f->id[TC_ID_SERIAL]);
        return 0;
    }
    if (f->mac[0]) {
        (void)snprintf(out, len, "mac-%s", f->mac);
        return 0;
    }
    if (len) out[0] = '\0';
    return -1;
}
