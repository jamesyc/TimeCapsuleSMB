#include "heartbeat_facts.h"
#include "errclass.h"
#include "../common/acp.h"

#define IDENTITY_REQUESTS 3 /* sySN, syAP, syAM; syNm is one of the plan's keys */
static const char *const identity_keys[IDENTITY_REQUESTS] = {"sySN", "syAP", "syAM"};

static void read_device_mac(char *out, size_t out_len) {
    static struct if_table table;
    unsigned char mac[6];
    out[0] = '\0';
    if (iflist_collect(&table) != 0 || if_table_device_mac(&table, mac) != 0) return;
    (void)snprintf(out, out_len, "%02x:%02x:%02x:%02x:%02x:%02x", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

/* A child that could not start for want of resources: counted, and the
 * device's state rather than ACP's. */
static int start_failed_for_resources(const struct acp_request *r) {
    if (r->status != ACP_ABORT) return 0;
    if (r->reason == ACP_REASON_FORK) return tc_errno_record(TC_OP_FORK, r->error) == TC_ERR_RESOURCE;
    if (r->reason == ACP_REASON_PIPE) return tc_errno_class(TC_OP_PIPE, r->error) == TC_ERR_RESOURCE;
    return 0;
}

/* Keeps a key's answer: a value, or empty for "not set". An abort keeps the
 * cached value; one that is not for want of resources, on a key never
 * answered, means the identity is unknown. */
static void keep(struct tc_heartbeat_facts *f, int index, int status, const char *value, int resources,
                 struct tc_facts_round *round) {
    if (status == ACP_OK || status == ACP_UNAVAILABLE) {
        (void)snprintf(f->id[index], sizeof(f->id[index]), "%s", status == ACP_OK ? value : "");
        trim_line(f->id[index]);
        f->answered[index] = 1;
    } else if (!f->answered[index] && !resources) {
        round->identity_lost = 1;
    }
}

void tc_heartbeat_facts_update(struct tc_heartbeat_facts *f, int refresh, struct tc_facts_round *round) {
    /* Static: device_facts holds the interface table. */
    static struct device_facts facts;
    struct acp_request requests[IDENTITY_REQUESTS + ACP_KEY_COUNT];
    char values[IDENTITY_REQUESTS][ACP_VALUE_MAX];
    int resources[IDENTITY_REQUESTS + ACP_KEY_COUNT];
    struct plan_options options;
    size_t i;
    memset(round, 0, sizeof(*round));
    if (!f->mac[0]) read_device_mac(f->mac, sizeof(f->mac));
    if (!refresh && f->have_last) return;
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
                               (long long)TC_ACP_COLLECTION_BUDGET_SECONDS * 1000, ACP_STOP_ON_TIMEOUT);
    device_facts_complete(&facts, requests + IDENTITY_REQUESTS);
    round->collected = 1;
    for (i = 0; i < IDENTITY_REQUESTS + ACP_KEY_COUNT; i++) {
        if (requests[i].status == ACP_ABORT && requests[i].reason == ACP_REASON_TIMEOUT) round->timed_out = 1;
        resources[i] = start_failed_for_resources(&requests[i]);
        if (resources[i]) round->local_resource = 1;
    }
    if (round->timed_out) f->acp_timeouts++;
    for (i = 0; i < IDENTITY_REQUESTS; i++) {
        int status = requests[i].status == ACP_OK && !requests[i].length ? ACP_UNAVAILABLE : requests[i].status;
        keep(f, (int)i, status, values[i], resources[i], round);
    }
    keep(f, TC_ID_SYNM, facts.acp[ACP_KEY_syNm].status, facts.acp[ACP_KEY_syNm].text,
         resources[IDENTITY_REQUESTS + ACP_KEY_syNm], round);
    memset(&options, 0, sizeof(options));
    f->last_ms = acp_monotonic_ms();
    (void)device_plan_build(&f->last, &facts, f->have_history ? &f->history : NULL, &options, f->last_ms);
    device_plan_history_update(&f->history, &f->have_history, &f->last);
    f->have_last = 1;
}
