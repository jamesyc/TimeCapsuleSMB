/* telemetry_payload (payload.c) and its heartbeat facts (heartbeat_facts.c)
 * with acp, the plan builder and the interface table replaced by scripts.
 * Prints the payloads that test_unit.py parses; everything else is asserted
 * here. */
#include "device.h"
#include "heartbeat_facts.h"
#include "errclass.h"
#include "plan.h"
#include <assert.h>
#include <sys/utsname.h>
volatile sig_atomic_t telemetry_stop;
volatile sig_atomic_t acp_stop_requested;

static const char *const plan_keys[ACP_KEY_COUNT] = {"raNA", "raDS", "waNM", "usbF", "laIP", "waIP", "waLL",
                                                     "gnRo", "syNm", "waMA"};
/* One scripted answer per key; unlisted keys answer "1". */
struct answer { const char *key; int status; const char *value; enum acp_abort_reason reason; int error; };
static struct answer script[8];
static size_t script_count;
static int collections, last_flags, plan_builds, built_with_history, validated = 1, have_mac = 1;
/* The interface table's lowest-index address; the real one by default. */
static const unsigned char real_mac[6] = {0x80, 0xea, 0x96, 0xe6, 0x58, 0x68};
static const unsigned char placeholder_mac[6] = {0x00, 0x01, 0x02, 0x03, 0x04, 0x05};
static const unsigned char other_mac[6] = {0xe8, 0x8d, 0x28, 0x58, 0xf1, 0x5c};
static const unsigned char *table_mac = real_mac;
static char keys_asked[512];

static void answer(const char *key, int status, const char *value, enum acp_abort_reason reason, int error) {
    script[script_count].key = key;
    script[script_count].status = status;
    script[script_count].value = value;
    script[script_count].reason = reason;
    script[script_count].error = error;
    script_count++;
}

static void reset(void) {
    script_count = 0;
    collections = plan_builds = built_with_history = 0;
    validated = have_mac = 1;
    table_mac = real_mac;
    telemetry_stop = 0;
    keys_asked[0] = 0;
}

void trim_line(char *v) {
    size_t n;
    char *start = v;
    while (*start == ' ' || *start == '\t') start++;
    memmove(v, start, strlen(start) + 1);
    n = strlen(v);
    while (n && (v[n - 1] == ' ' || v[n - 1] == '\t' || v[n - 1] == '\r' || v[n - 1] == '\n')) v[--n] = 0;
}
int uname(struct utsname *name) {
    memset(name, 0, sizeof(*name));
    strcpy(name->sysname, "NetBSD"); strcpy(name->release, "6.0"); strcpy(name->machine, "evbarm");
    return 0;
}
int read_deploy_release_tag(char *out, size_t cap) { snprintf(out, cap, "v-test"); return 0; }
int read_uptime_seconds(long *out) { *out = 123; return 0; }
long long acp_monotonic_ms(void) { return 50000; }

int acp_collect_run_opts(struct acp_request *requests, size_t count, long long timeout_ms, long long budget_ms,
                         int flags) {
    size_t i, j;
    int stopped = 0;
    (void)timeout_ms; (void)budget_ms;
    collections++;
    last_flags = flags;
    keys_asked[0] = 0;
    for (i = 0; i < count; i++) {
        const char *value = "1";
        int status = ACP_OK;
        enum acp_abort_reason reason = ACP_REASON_NONE;
        int error = 0;
        strcat(keys_asked, requests[i].key);
        strcat(keys_asked, " ");
        if (!strcmp(requests[i].key, "sySN")) value = "serial123";
        if (!strcmp(requests[i].key, "syAP")) value = "0x6a";
        if (!strcmp(requests[i].key, "syAM")) value = "TimeCapsule6,106";
        if (!strcmp(requests[i].key, "syNm")) value = "  Name\n\"quoted\"";
        for (j = 0; j < script_count; j++)
            if (!strcmp(script[j].key, requests[i].key)) {
                value = script[j].value;
                status = script[j].status;
                reason = script[j].reason;
                error = script[j].error;
            }
        /* ACP_STOP_EARLY: nothing is
         * asked after a timeout or a child that could not start. */
        if (stopped) { status = ACP_ABORT; reason = ACP_REASON_SKIPPED; error = 0; }
        requests[i].status = status;
        requests[i].reason = status == ACP_ABORT ? reason : ACP_REASON_NONE;
        requests[i].error = error;
        snprintf(requests[i].output, requests[i].capacity, "%s", status == ACP_OK ? value : "");
        requests[i].length = strlen(requests[i].output);
        if (status == ACP_ABORT && (reason == ACP_REASON_TIMEOUT || reason == ACP_REASON_FORK) &&
            (flags & ACP_STOP_EARLY))
            stopped = 1;
    }
    return 0;
}
void device_facts_requests(struct acp_request *requests, struct device_facts *facts) {
    size_t i;
    for (i = 0; i < ACP_KEY_COUNT; i++) {
        requests[i].key = plan_keys[i];
        requests[i].output = facts->acp[i].text;
        requests[i].capacity = sizeof(facts->acp[i].text);
    }
}
void device_facts_complete(struct device_facts *facts, const struct acp_request *requests) {
    size_t i;
    for (i = 0; i < ACP_KEY_COUNT; i++)
        facts->acp[i].status = requests[i].status == ACP_OK && !requests[i].length ? ACP_UNAVAILABLE : requests[i].status;
}
/* A plan validates when every key answered and the test allows it; an
 * unvalidated one built from history keeps its age, as plan.c does. */
int device_plan_build(struct device_plan *out, const struct device_facts *facts, const struct device_plan *previous,
                      const struct plan_options *options, long long now_ms) {
    size_t i;
    int all = 1;
    assert(options->diskless == 0);
    plan_builds++;
    if (previous) built_with_history++;
    for (i = 0; i < ACP_KEY_COUNT; i++) if (facts->acp[i].status != ACP_OK) all = 0;
    memset(out, 0, sizeof(*out));
    out->status.validated = all && validated;
    if (!out->status.validated) {
        strcpy(out->status.reason, all ? "mode" : "syNm");
        if (previous) out->status.stale_seconds = (unsigned long)((now_ms - previous->validated_at_ms) / 1000);
    } else {
        out->validated_at_ms = now_ms - 7000;
    }
    return 0;
}
void device_plan_history_update(struct device_plan *history, int *have, const struct device_plan *current) {
    if (current->status.validated) { *history = *current; *have = 1; }
}
int iflist_collect(struct if_table *table) { memset(table, 0, sizeof(*table)); return 0; }
int if_table_device_mac(const struct if_table *table, unsigned char out[6]) {
    (void)table;
    if (!have_mac) return -1;
    memcpy(out, table_mac, 6);
    return 0;
}
const char *router_mode_name(enum router_mode value) { (void)value; return "unknown"; }
const char *link_role_name(enum link_role value) { (void)value; assert(0); return ""; }
int addr_is_service_address(const struct if_addr *addr) { (void)addr; assert(0); return 0; }

static const struct tc_delivery *delivery;
static int build(struct tc_heartbeat_facts *f, int refresh, char *json, size_t cap, struct tc_payload_result *r) {
    struct tc_payload_context ctx;
    memset(&ctx, 0, sizeof(ctx));
    ctx.facts = f;
    ctx.refresh = refresh;
    ctx.delivery = delivery;
    return telemetry_payload(json, cap, "manual", "0123456789abcdef0123456789abcdef", &ctx, r);
}

/* tc_mac_parse: waMA as acp prints it, or the table's address; never a
 * boot placeholder. */
static void test_mac_parse(void) {
    char out[18];
    assert(!tc_mac_parse("80:EA:96:E6:58:68", out) && !strcmp(out, "80:ea:96:e6:58:68"));
    assert(!tc_mac_parse(" e8:8d:28:58:f1:5c \r\n", out) && !strcmp(out, "e8:8d:28:58:f1:5c"));
    assert(tc_mac_parse("00:01:02:03:04:05", out) && !out[0]);
    assert(tc_mac_parse("00:0A:AA:BB:CC:DD", out) && tc_mac_parse("00:0a:aa:bb:cc:dd", out));
    assert(tc_mac_parse("00:00:00:00:00:00", out) && tc_mac_parse("01:00:5e:00:00:01", out));
    assert(tc_mac_parse("ff:ff:ff:ff:ff:ff", out));
    assert(tc_mac_parse("80:ea:96:e6:58", out) && tc_mac_parse("80:ea:96:e6:58:68:00", out));
    assert(tc_mac_parse("80-ea-96-e6-58-68", out) && tc_mac_parse("80:ea:96:e6:58:6g", out));
    assert(tc_mac_parse("1", out) && tc_mac_parse("", out) && tc_mac_parse(NULL, out));
}

/* When a retry may keep the facts it has instead of reading acp again. */
static void test_facts_fresh(void) {
    static const struct { const char *key; int status; enum acp_abort_reason reason; int error; } incomplete[] = {
        {"syNm", ACP_ABORT, ACP_REASON_TIMEOUT, 0}, {"waMA", ACP_ABORT, ACP_REASON_BUDGET, 0},
        {"syAP", ACP_ABORT, ACP_REASON_FORK, EAGAIN}, {"raNA", ACP_ABORT, ACP_REASON_EXEC, ENOENT}};
    struct tc_heartbeat_facts f;
    struct tc_facts_round round;
    size_t i;
    const long long limit = (long long)TC_FACTS_REUSE_SECONDS * 1000;
    /* Nothing collected: nothing to keep. */
    reset();
    memset(&f, 0, sizeof(f));
    assert(!tc_heartbeat_facts_fresh(&f, 50000));
    /* Every key answered, "not set" included: fresh until the limit. */
    answer("waMA", ACP_OK, "", ACP_REASON_NONE, 0);
    answer("usbF", ACP_UNAVAILABLE, "", ACP_REASON_NONE, 0);
    tc_heartbeat_facts_update(&f, 1, &round);
    assert(round.collected && f.last_complete);
    assert(tc_heartbeat_facts_fresh(&f, 50000) && tc_heartbeat_facts_fresh(&f, 50000 + limit - 1));
    assert(!tc_heartbeat_facts_fresh(&f, 50000 + limit));
    /* Reusing them reads nothing and does not make them younger. */
    tc_heartbeat_facts_update(&f, 0, &round);
    assert(!round.collected && collections == 1 && !tc_heartbeat_facts_fresh(&f, 50000 + limit));
    /* A key that timed out, ran out of budget or never started (and every
     * key skipped after it): read again. */
    for (i = 0; i < sizeof(incomplete) / sizeof(incomplete[0]); i++) {
        reset();
        memset(&f, 0, sizeof(f));
        answer(incomplete[i].key, incomplete[i].status, "", incomplete[i].reason, incomplete[i].error);
        tc_heartbeat_facts_update(&f, 1, &round);
        assert(round.collected && f.have_last && !f.last_complete && !tc_heartbeat_facts_fresh(&f, 50000));
        /* A complete collection after it is fresh again. */
        reset();
        tc_heartbeat_facts_update(&f, 1, &round);
        assert(f.last_complete && tc_heartbeat_facts_fresh(&f, 50000));
    }
}

/* The facts: where each heartbeat's address comes from. */
static void test_mac_sources(void) {
    char json[8192];
    struct tc_payload_result r;
    /* waMA wins over a different table address. */
    {
        static struct tc_heartbeat_facts f;
        reset();
        table_mac = other_mac;
        answer("waMA", ACP_OK, "80:EA:96:E6:58:68", ACP_REASON_NONE, 0);
        assert(!build(&f, 1, json, sizeof(json), &r));
        assert(strstr(json, "\"mac\":\"80:ea:96:e6:58:68\"") && !strcmp(f.wama, "80:ea:96:e6:58:68"));
        /* Kept through a collection that stops before waMA (syAP times out),
         * one where waMA is "not set", and one that never validates. */
        reset();
        answer("syAP", ACP_ABORT, "", ACP_REASON_TIMEOUT, 0);
        assert(!build(&f, 1, json, sizeof(json), &r) && strstr(json, "\"mac\":\"80:ea:96:e6:58:68\""));
        reset();
        answer("waMA", ACP_UNAVAILABLE, "", ACP_REASON_NONE, 0);
        assert(!build(&f, 1, json, sizeof(json), &r) && strstr(json, "\"mac\":\"80:ea:96:e6:58:68\""));
    }
    /* A plan that never validates still gets waMA's address. */
    {
        static struct tc_heartbeat_facts f;
        reset();
        validated = 0;
        table_mac = other_mac;
        answer("waMA", ACP_OK, "80:EA:96:E6:58:68", ACP_REASON_NONE, 0);
        assert(!build(&f, 1, json, sizeof(json), &r) && strstr(json, "\"mac\":\"80:ea:96:e6:58:68\""));
    }
    /* Boot: both sources hold a placeholder. The serial names the router
     * and no address goes. */
    {
        static struct tc_heartbeat_facts f;
        reset();
        table_mac = placeholder_mac;
        answer("waMA", ACP_OK, "00:01:02:03:04:05", ACP_REASON_NONE, 0);
        assert(!build(&f, 1, json, sizeof(json), &r));
        assert(strstr(json, "\"router_id\":\"serial123\"") && strstr(json, "\"mac\":null") && !f.wama[0]);
        /* The table is read again on every heartbeat until waMA answers. */
        reset();
        table_mac = other_mac;
        assert(!build(&f, 0, json, sizeof(json), &r) && collections == 0);
        assert(strstr(json, "\"mac\":\"e8:8d:28:58:f1:5c\""));
        /* The next collection's waMA is kept, and later heartbeats send it
         * with no acp read, whatever the table holds. */
        reset();
        answer("waMA", ACP_OK, "80:EA:96:E6:58:68\n", ACP_REASON_NONE, 0);
        assert(!build(&f, 1, json, sizeof(json), &r) && strstr(json, "\"mac\":\"80:ea:96:e6:58:68\""));
        reset();
        table_mac = placeholder_mac;
        assert(!build(&f, 0, json, sizeof(json), &r) && collections == 0);
        assert(strstr(json, "\"mac\":\"80:ea:96:e6:58:68\""));
        /* A later placeholder leaves it; a different usable answer replaces it. */
        reset();
        answer("waMA", ACP_OK, "00:01:02:03:04:05", ACP_REASON_NONE, 0);
        assert(!build(&f, 1, json, sizeof(json), &r) && strstr(json, "\"mac\":\"80:ea:96:e6:58:68\""));
        reset();
        answer("waMA", ACP_OK, "e8:8d:28:58:f1:5c", ACP_REASON_NONE, 0);
        assert(!build(&f, 1, json, sizeof(json), &r) && strstr(json, "\"mac\":\"e8:8d:28:58:f1:5c\""));
    }
    /* No serial, no waMA, a placeholder table: nothing names the router, so
     * nothing goes, never under the placeholder; once the table is real, the
     * retry reports under it. */
    {
        static struct tc_heartbeat_facts f;
        reset();
        table_mac = placeholder_mac;
        answer("sySN", ACP_UNAVAILABLE, "", ACP_REASON_NONE, 0);
        answer("waMA", ACP_UNAVAILABLE, "", ACP_REASON_NONE, 0);
        assert(build(&f, 1, json, sizeof(json), &r) && r.failure == TC_OUTCOME_ACP_UNAVAILABLE && !f.mac[0]);
        reset();
        answer("sySN", ACP_UNAVAILABLE, "", ACP_REASON_NONE, 0);
        answer("waMA", ACP_UNAVAILABLE, "", ACP_REASON_NONE, 0);
        assert(!build(&f, 1, json, sizeof(json), &r) && strstr(json, "\"router_id\":\"mac-80:ea:96:e6:58:68\""));
    }
}

int main(void) {
    static struct tc_heartbeat_facts f;
    struct tc_payload_result r;
    struct tc_failure_counts counts;
    char json[8192];

    /* A first heartbeat reads everything in one collection: identity first,
     * stopping at the first timeout. */
    reset();
    assert(!build(&f, 1, json, sizeof(json), &r) && !r.local_resource);
    assert(collections == 1 && last_flags == ACP_STOP_EARLY);
    assert(!strcmp(keys_asked, "sySN syAP syAM raNA raDS waNM usbF laIP waIP waLL gnRo syNm waMA "));
    assert(strstr(json, "\"router_id\":\"serial123\"") && strstr(json, "\"mac\":\"80:ea:96:e6:58:68\""));
    assert(strstr(json, "\"device_syap\":\"106\"") && strstr(json, "\"device_model\":\"TimeCapsule6,106\""));
    /* syNm is captured whole for Bonjour; the heartbeat sends it trimmed. */
    assert(strstr(json, "\"device_name\":\"Name\\n\\\"quoted\\\"\""));
    assert(strstr(json, "\"uptime_sec\":123") && strstr(json, "\"deploy_release_tag\":\"v-test\""));
    assert(!strstr(json, "plan_error") && !strstr(json, "plan_age_sec") && !strstr(json, "plan_stale_sec"));
    assert(strstr(json, "\"delivery\":{\"failed\":0,\"since\":null,"));
    puts(json);

    /* Without refresh nothing is read: the kept identity and plan go, with
     * the plan's age, and the delivery record says what failed before. */
    {
        static struct tc_delivery record = {3, 4000, 2, 0, 1, 0};
        delivery = &record;
    }
    reset();
    assert(!build(&f, 0, json, sizeof(json), &r));
    assert(collections == 0 && plan_builds == 0);
    assert(strstr(json, "\"router_id\":\"serial123\"") && strstr(json, "\"plan_age_sec\":0"));
    assert(strstr(json, "\"delivery\":{\"failed\":3,\"since\":4000,\"classes\":{\"local_resource\":2,"
                        "\"acp_unavailable\":0,\"network\":1,\"server\":0}}"));
    puts(json);
    delivery = NULL;

    /* ACPd hangs at syAP: the kept identity stands in, the plan is built
     * from the validated one (its roles, with plan_error and its age), and
     * the timeout is counted. */
    reset();
    answer("syAP", ACP_ABORT, "", ACP_REASON_TIMEOUT, 0);
    assert(!build(&f, 1, json, sizeof(json), &r) && !r.local_resource);
    assert(f.acp_timeouts == 1 && built_with_history == 1);
    assert(strstr(json, "\"router_id\":\"serial123\"") && strstr(json, "\"device_syap\":\"106\""));
    assert(strstr(json, "\"plan_error\":\"syNm\"") && strstr(json, "\"plan_stale_sec\":7"));
    assert(!strstr(json, "plan_age_sec"));

    /* A model read that fails for want of a process: the kept one goes. */
    reset();
    answer("syAM", ACP_ABORT, "", ACP_REASON_FORK, EAGAIN);
    assert(!build(&f, 1, json, sizeof(json), &r) && r.local_resource);
    assert(strstr(json, "\"device_model\":\"TimeCapsule6,106\""));

    /* A new process whose model read times out still reports, under its
     * serial, with the model empty: the router ID does not depend on it. */
    {
        static struct tc_heartbeat_facts fresh;
        reset();
        answer("syAM", ACP_ABORT, "", ACP_REASON_TIMEOUT, 0);
        assert(!build(&fresh, 1, json, sizeof(json), &r));
        assert(strstr(json, "\"router_id\":\"serial123\"") && strstr(json, "\"device_model\":\"\""));
        /* ... and the name, asked after the timeout, is empty too. */
        assert(strstr(json, "\"device_name\":\"\"") && fresh.acp_timeouts == 1);
    }
    /* A hung ACPd from the first read (ACPd stuck at boot, telemetry
     * restarted during the wedge): the hardware address names it. */
    {
        static struct tc_heartbeat_facts fresh;
        reset();
        answer("sySN", ACP_ABORT, "", ACP_REASON_TIMEOUT, 0);
        assert(!build(&fresh, 1, json, sizeof(json), &r));
        assert(strstr(json, "\"router_id\":\"mac-80:ea:96:e6:58:68\""));
    }
    /* A broken acp binary is worth a heartbeat too. */
    {
        static struct tc_heartbeat_facts fresh;
        reset();
        answer("sySN", ACP_ABORT, "", ACP_REASON_EXEC, 0);
        answer("syAP", ACP_ABORT, "", ACP_REASON_EXEC, 0);
        answer("syAM", ACP_ABORT, "", ACP_REASON_EXEC, 0);
        answer("syNm", ACP_ABORT, "", ACP_REASON_EXEC, 0);
        assert(!build(&fresh, 1, json, sizeof(json), &r) && !r.local_resource);
        assert(strstr(json, "\"router_id\":\"mac-80:ea:96:e6:58:68\"") && strstr(json, "\"device_syap\":\"\""));
    }
    /* A full process table at the first read is the device's trouble, not
     * ACP's: it reports by hardware address. The collection stops at the
     * first failed fork, so it counts once. */
    {
        static struct tc_heartbeat_facts fresh;
        struct tc_failure_counts before;
        tc_failure_counts(&before);
        reset();
        answer("sySN", ACP_ABORT, "", ACP_REASON_FORK, EAGAIN);
        assert(!build(&fresh, 1, json, sizeof(json), &r) && r.local_resource);
        assert(strstr(json, "\"router_id\":\"mac-80:ea:96:e6:58:68\"") && strstr(json, "\"device_model\":\"\""));
        tc_failure_counts(&counts);
        assert(counts.fork_eagain == before.fork_eagain + 1);
    }
    /* No serial ("not set"): the hardware address, never a hash. */
    {
        static struct tc_heartbeat_facts fresh;
        reset();
        answer("sySN", ACP_UNAVAILABLE, "", ACP_REASON_NONE, 0);
        assert(!build(&fresh, 1, json, sizeof(json), &r));
        assert(strstr(json, "\"router_id\":\"mac-80:ea:96:e6:58:68\""));
    }
    /* Neither a serial nor a hardware address: nothing to name it by. */
    {
        static struct tc_heartbeat_facts fresh;
        reset();
        have_mac = 0;
        answer("sySN", ACP_UNAVAILABLE, "", ACP_REASON_NONE, 0);
        assert(build(&fresh, 1, json, sizeof(json), &r) && r.failure == TC_OUTCOME_ACP_UNAVAILABLE);
        assert(!fresh.mac[0]);
    }
    /* TERM during the collection. */
    {
        static struct tc_heartbeat_facts fresh;
        reset();
        telemetry_stop = 1;
        assert(build(&fresh, 1, json, sizeof(json), &r) && r.failure == TC_OUTCOME_CANCELLED);
        telemetry_stop = 0;
    }
    /* The largest delivery record fits its budget (telemetry.h). */
    {
        static struct tc_delivery most = {4294967295u, 2147483647L, 4294967295u, 4294967295u, 4294967295u,
                                          4294967295u};
        const char *start, *end;
        delivery = &most;
        reset();
        assert(!build(&f, 0, json, sizeof(json), &r));
        start = strstr(json, ",\"delivery\":");
        end = strstr(start, "}}");
        assert(start && end && (size_t)(end + 2 - start) <= TC_DELIVERY_JSON_MAX);
        delivery = NULL;
    }
    /* Too small a buffer sends nothing. */
    reset();
    assert(build(&f, 0, json, 12, &r) && r.failure == TC_OUTCOME_SERVER);
    test_mac_parse();
    test_facts_fresh();
    test_mac_sources();
    return 0;
}
