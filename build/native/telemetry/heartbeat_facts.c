#include "heartbeat_facts.h"
#include "errclass.h"
#include "../common/acp.h"

#define IDENTITY_REQUESTS 3 /* sySN, syAP, syAM; syNm is one of the plan's keys */
static const char *const identity_keys[IDENTITY_REQUESTS] = {"sySN", "syAP", "syAM"};

/* The addresses Apple's NICs carry until boot sets the real one: NetBSD 6's
 * bcmeth and NetBSD 4's mgi. Every unit of a model has the same one. */
static const unsigned char placeholders[][6] = {
    {0x00, 0x01, 0x02, 0x03, 0x04, 0x05},
    {0x00, 0x0a, 0xaa, 0xbb, 0xcc, 0xdd},
};

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
    for (i = 0; i < sizeof(placeholders) / sizeof(placeholders[0]); i++)
        if (!memcmp(mac, placeholders[i], 6)) return -1;
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
    char text[18];
    if (f->wama[0]) {
        (void)snprintf(f->mac, sizeof(f->mac), "%s", f->wama);
        return;
    }
    f->mac[0] = '\0';
    if (iflist_collect(&table) != 0 || if_table_device_mac(&table, mac) != 0) return;
    (void)snprintf(text, sizeof(text), "%02x:%02x:%02x:%02x:%02x:%02x", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    (void)tc_mac_parse(text, f->mac);
}

/* A child that could not start for want of resources: counted, and the
 * device's state rather than ACP's. */
static int start_failed_for_resources(const struct acp_request *r) {
    if (r->status != ACP_ABORT) return 0;
    if (r->reason == ACP_REASON_FORK) return tc_errno_record(TC_OP_FORK, r->error) == TC_ERR_RESOURCE;
    if (r->reason == ACP_REASON_PIPE) return tc_errno_class(TC_OP_PIPE, r->error) == TC_ERR_RESOURCE;
    return 0;
}

/* Keeps a key's answer: a value, or empty for "not set". A failed read keeps
 * the cached value, or empty if there is none: with the router named by its
 * serial or hardware address, a missing model or name no longer changes who
 * the heartbeat is from. */
static void keep(struct tc_heartbeat_facts *f, int index, int status, const char *value) {
    if (status == ACP_OK || status == ACP_UNAVAILABLE) {
        (void)snprintf(f->id[index], sizeof(f->id[index]), "%s", status == ACP_OK ? value : "");
        trim_line(f->id[index]);
        f->answered[index] = 1;
    }
}

void tc_heartbeat_facts_update(struct tc_heartbeat_facts *f, int refresh, struct tc_facts_round *round) {
    /* Static: device_facts holds the interface table. */
    static struct device_facts facts;
    struct acp_request requests[IDENTITY_REQUESTS + ACP_KEY_COUNT];
    char values[IDENTITY_REQUESTS][ACP_VALUE_MAX];
    struct plan_options options;
    size_t i;
    memset(round, 0, sizeof(*round));
    if (!refresh && f->have_last) {
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
    (void)acp_collect_run_opts(requests, IDENTITY_REQUESTS + ACP_KEY_COUNT, (long long)TC_ACP_TIMEOUT_SECONDS * 1000,
                               (long long)TC_ACP_COLLECTION_BUDGET_SECONDS * 1000,
                               ACP_STOP_EARLY);
    device_facts_complete(&facts, requests + IDENTITY_REQUESTS);
    round->collected = 1;
    f->last_complete = 1;
    for (i = 0; i < IDENTITY_REQUESTS + ACP_KEY_COUNT; i++) {
        if (requests[i].status == ACP_ABORT) f->last_complete = 0;
        if (requests[i].status == ACP_ABORT && requests[i].reason == ACP_REASON_TIMEOUT) round->timed_out = 1;
        if (requests[i].status == ACP_ABORT && requests[i].reason == ACP_REASON_BUDGET) round->budget_spent = 1;
        if (start_failed_for_resources(&requests[i])) round->local_resource = 1;
    }
    if (round->timed_out) f->acp_timeouts++;
    if (round->budget_spent) f->acp_budget_exhausted++;
    for (i = 0; i < IDENTITY_REQUESTS; i++) {
        int status = requests[i].status == ACP_OK && !requests[i].length ? ACP_UNAVAILABLE : requests[i].status;
        keep(f, (int)i, status, values[i]);
    }
    keep(f, TC_ID_SYNM, facts.acp[ACP_KEY_syNm].status, facts.acp[ACP_KEY_syNm].text);
    note_wama(f, facts.acp[ACP_KEY_waMA].status == ACP_OK ? facts.acp[ACP_KEY_waMA].text : NULL);
    choose_mac(f);
    memset(&options, 0, sizeof(options));
    f->last_ms = acp_monotonic_ms();
    (void)device_plan_build(&f->last, &facts, f->have_history ? &f->history : NULL, &options, f->last_ms);
    device_plan_history_update(&f->history, &f->have_history, &f->last);
    f->have_last = 1;
}

int tc_heartbeat_facts_fresh(const struct tc_heartbeat_facts *f, long long now_ms) {
    return f->have_last && f->last_complete && now_ms >= f->last_ms &&
           now_ms - f->last_ms < (long long)TC_FACTS_REUSE_SECONDS * 1000;
}
