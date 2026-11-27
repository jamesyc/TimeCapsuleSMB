/* telemetry_payload (payload.c) and its heartbeat facts (heartbeat_facts.c),
 * with the planner (planner.c) choosing how each heartbeat gets them, and
 * acp, the plan builder and the interface table replaced by scripts.
 * Prints the payloads that test_unit.py parses; everything else is asserted
 * here. */
#include "device.h"
#include "heartbeat_facts.h"
#include "planner.h"
#include "health.h"
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
/* The facts' clock; test_planned_reads moves it with the planner's. */
static long long now_ms = 50000;
long long acp_monotonic_ms(void) { return now_ms; }

int acp_collect_run(struct acp_request *requests, size_t count, long long timeout_ms, long long budget_ms,
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
        /* ACP_STOP_EARLY, as acp.c: nothing is asked after a timeout or a
         * child that could not start (no process, or no pipe). */
        if (stopped) { status = ACP_ABORT; reason = ACP_REASON_SKIPPED; error = 0; }
        requests[i].status = status;
        requests[i].reason = status == ACP_ABORT ? reason : ACP_REASON_NONE;
        requests[i].error = error;
        snprintf(requests[i].output, requests[i].capacity, "%s", status == ACP_OK ? value : "");
        requests[i].length = strlen(requests[i].output);
        if (status == ACP_ABORT &&
            (reason == ACP_REASON_TIMEOUT || reason == ACP_REASON_FORK || reason == ACP_REASON_PIPE) &&
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
/* addr.c's two, as there (its own tests, through iflist_parse, cover the
 * real list); the stubbed table refuses them the way addr.c's does. */
int addr_mac_is_boot_placeholder(const unsigned char mac[6]) {
    static const unsigned char netbsd6[6] = {0x00, 0x01, 0x02, 0x03, 0x04, 0x05};
    static const unsigned char netbsd4[6] = {0x00, 0x0a, 0xaa, 0xbb, 0xcc, 0xdd};
    return !memcmp(mac, netbsd6, 6) || !memcmp(mac, netbsd4, 6);
}
int addr_device_mac(const struct if_table *table, unsigned char out[6]) {
    (void)table;
    if (!have_mac || addr_mac_is_boot_placeholder(table_mac)) return -1;
    memcpy(out, table_mac, 6);
    return 0;
}
const char *router_mode_name(enum router_mode value) { (void)value; return "unknown"; }
const char *link_role_name(enum link_role value) { (void)value; assert(0); return ""; }
int addr_is_service_address(const struct if_addr *addr) { (void)addr; assert(0); return 0; }

static const struct tc_delivery *delivery;
static struct tc_heartbeat_ident *build_ident; /* the daemon's pending id, or NULL as one-shot modes */
static const char *build_reason = "manual";
static struct tc_health *build_health; /* NULL: no health object */
static int last_health_sent;
/* The two ends of struct tc_facts_reuse: read afresh, or keep any facts. */
static const struct tc_facts_reuse READ = {0, 0}, KEEP = {TC_FACTS_FOREVER, 0};
/* A boot or scheduled retry after an outage (planner.h's table). */
static const struct tc_facts_reuse OUTAGE = {TC_FACTS_REUSE_SECONDS, 1};
static int outage_keeps(const struct tc_heartbeat_facts *f, long long at_ms) {
    return tc_heartbeat_facts_reusable(f, OUTAGE, at_ms);
}
static int build(struct tc_heartbeat_facts *f, struct tc_facts_reuse how, char *json, size_t cap,
                 struct tc_payload_result *r) {
    struct tc_payload_context ctx;
    int rc;
    memset(&ctx, 0, sizeof(ctx));
    ctx.facts = f;
    ctx.reuse = how;
    ctx.delivery = delivery;
    ctx.ident = build_ident;
    ctx.health = build_health;
    rc = telemetry_payload(json, cap, build_reason, "0123456789abcdef0123456789abcdef", &ctx, r);
    last_health_sent = ctx.sent.health;
    return rc;
}

/* tc_mac_parse: waMA as acp prints it, or the table's address; never a
 * boot placeholder. */
/* The payload's heartbeat_id, copied into out. */
static const char *heartbeat_id(const char *json, char out[80]) {
    const char *start = strstr(json, "\"heartbeat_id\":\""), *end;
    assert(start);
    start += strlen("\"heartbeat_id\":\"");
    end = strchr(start, '"');
    assert(end && end - start < 80);
    memcpy(out, start, (size_t)(end - start));
    out[end - start] = 0;
    return out;
}

/* One heartbeat keeps its id through every retry (the server keeps the
 * latest attempt under it); another reason or router ID is another
 * heartbeat, and a delivered one leaves none pending. */
static void test_heartbeat_ident(void) {
    static struct tc_heartbeat_facts f;
    struct tc_heartbeat_ident ident;
    struct tc_payload_result r;
    char json[8192], first[80], again[80];
    memset(&ident, 0, sizeof(ident));
    build_ident = &ident;
    /* No serial yet: the router is named by its hardware address. */
    reset();
    answer("sySN", ACP_OK, "", ACP_REASON_NONE, 0);
    build_reason = "boot";
    assert(!build(&f, READ, json, sizeof(json), &r) && strstr(json, "\"router_id\":\"mac-"));
    heartbeat_id(json, first);
    assert(!strcmp(ident.id, first) && !strcmp(ident.reason, "boot"));
    reset();
    answer("sySN", ACP_OK, "", ACP_REASON_NONE, 0);
    assert(!build(&f, READ, json, sizeof(json), &r) && !strcmp(heartbeat_id(json, again), first));
    /* The serial arrives on a retry: another router ID, so another id. */
    reset();
    assert(!build(&f, READ, json, sizeof(json), &r) && strstr(json, "\"router_id\":\"serial123\""));
    assert(strcmp(heartbeat_id(json, first), again) && !strcmp(ident.id, first));
    /* A scheduled retry that became resource_high is another heartbeat. */
    build_reason = "resource_high";
    reset();
    assert(!build(&f, KEEP, json, sizeof(json), &r) && strcmp(heartbeat_id(json, again), first));
    /* Delivered: the next heartbeat, even for the same reason, is new. */
    tc_heartbeat_ident_clear(&ident);
    reset();
    assert(!build(&f, KEEP, json, sizeof(json), &r) && strcmp(heartbeat_id(json, first), again));
    /* With nowhere to keep it (one-shot modes) each payload gets a new id. */
    build_ident = NULL;
    reset();
    assert(!build(&f, KEEP, json, sizeof(json), &r) && strcmp(heartbeat_id(json, again), first));
    reset();
    assert(!build(&f, KEEP, json, sizeof(json), &r) && strcmp(heartbeat_id(json, first), again));
    build_reason = "manual";
}

/* Health is diagnostics: an object that does not fit goes as null, the
 * heartbeat still goes, and its events are not marked sent, so they wait
 * for the next one. One that fits is marked sent with its events. */
static void test_health_that_does_not_fit(void) {
    static struct tc_heartbeat_facts f;
    static struct tc_health h;
    struct tc_payload_result r;
    char json[HEARTBEAT_MAX_JSON];
    size_t base;
    reset();
    assert(!build(&f, READ, json, sizeof(json), &r));
    base = strlen(json);
    memset(&h, 0, sizeof(h));
    h.pending = h.news = TC_EV_PROCS;
    build_health = &h;
    reset();
    assert(!build(&f, KEEP, json, base + 64, &r));
    assert(strstr(json, "\"health\":null}") && !last_health_sent);
    /* (Building it also notes the fork failures earlier cases counted.) */
    assert(h.pending == TC_EV_PROCS && (h.news & TC_EV_PROCS));
    reset();
    assert(!build(&f, KEEP, json, sizeof(json), &r));
    assert(strstr(json, "\"triggers\":[\"procs\"]") && last_health_sent);
    build_health = NULL;
}

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

/* When a heartbeat may keep the facts it has instead of reading acp again. */
static void test_facts_reusable(void) {
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
    assert(!outage_keeps(&f, 50000));
    /* Every key answered, "not set" included: fresh until the limit. */
    answer("waMA", ACP_OK, "", ACP_REASON_NONE, 0);
    answer("usbF", ACP_UNAVAILABLE, "", ACP_REASON_NONE, 0);
    tc_heartbeat_facts_update(&f, READ, &round);
    assert(round.collected && f.last_complete);
    assert(outage_keeps(&f, 50000) && outage_keeps(&f, 50000 + limit - 1));
    assert(!outage_keeps(&f, 50000 + limit));
    /* Reusing them reads nothing and does not make them younger. */
    tc_heartbeat_facts_update(&f, KEEP, &round);
    assert(!round.collected && collections == 1 && !outage_keeps(&f, 50000 + limit));
    /* The age counts whole seconds: facts read 900 ms into a second are a
     * second old 100 ms later and, under a one-second limit, too old 999 ms
     * after the read, as the planner's clock sees it. */
    {
        const struct tc_facts_reuse one_second = {1, 0};
        reset();
        memset(&f, 0, sizeof(f));
        now_ms = 50900;
        tc_heartbeat_facts_update(&f, READ, &round);
        now_ms = 50000;
        assert(tc_heartbeat_facts_reusable(&f, one_second, 50999));
        assert(!tc_heartbeat_facts_reusable(&f, one_second, 51000));
        assert(!tc_heartbeat_facts_reusable(&f, one_second, 51899));
    }
    /* A key that timed out, ran out of budget or never started (and every
     * key skipped after it): read again. */
    for (i = 0; i < sizeof(incomplete) / sizeof(incomplete[0]); i++) {
        reset();
        memset(&f, 0, sizeof(f));
        answer(incomplete[i].key, incomplete[i].status, "", incomplete[i].reason, incomplete[i].error);
        tc_heartbeat_facts_update(&f, READ, &round);
        assert(round.collected && f.have_last && !f.last_complete && !outage_keeps(&f, 50000));
        /* A complete collection after it is fresh again. */
        reset();
        tc_heartbeat_facts_update(&f, READ, &round);
        assert(f.last_complete && outage_keeps(&f, 50000));
    }
    /* A retry after a network failure: fresh complete facts are kept,
     * incomplete ones read again. */
    reset();
    tc_heartbeat_facts_update(&f, OUTAGE, &round);
    assert(!round.collected && collections == 0);
    answer("syNm", ACP_ABORT, "", ACP_REASON_TIMEOUT, 0);
    tc_heartbeat_facts_update(&f, READ, &round);
    assert(!f.last_complete);
    reset();
    tc_heartbeat_facts_update(&f, OUTAGE, &round);
    assert(round.collected && collections == 1 && f.last_complete);
    /* A dead ACPd from this process's first read: every key answers empty
     * at once. Counted, and not complete, so a retry reads acp again
     * instead of keeping no identity for an hour. */
    {
        static struct tc_heartbeat_facts dead;
        reset();
        answer("sySN", ACP_OK, "", ACP_REASON_NONE, 0);
        answer("syAP", ACP_OK, "", ACP_REASON_NONE, 0);
        answer("syAM", ACP_OK, "", ACP_REASON_NONE, 0);
        tc_heartbeat_facts_update(&dead, READ, &round);
        assert(dead.acp.empty == 1 && !dead.last_complete && !outage_keeps(&dead, 50000));
        tc_heartbeat_facts_update(&dead, OUTAGE, &round);
        assert(round.collected && collections == 2 && dead.acp.empty == 2);
    }
    /* The rule's edges, on an incomplete collection at 50 s. */
    {
        static struct tc_heartbeat_facts g;
        const struct tc_facts_reuse second = {1, 0}, any_complete = {TC_FACTS_FOREVER, 1};
        reset();
        assert(!tc_heartbeat_facts_reusable(&g, KEEP, 50000)); /* nothing collected */
        answer("syNm", ACP_ABORT, "", ACP_REASON_TIMEOUT, 0);
        tc_heartbeat_facts_update(&g, READ, &round);
        assert(round.collected && !g.last_complete);
        /* {0, 0} never keeps, not even facts collected this millisecond. */
        assert(!tc_heartbeat_facts_reusable(&g, READ, 50000));
        /* Age is whole seconds on both clocks, as the planner's: under one
         * second means the same second. */
        assert(tc_heartbeat_facts_reusable(&g, second, 50999) && !tc_heartbeat_facts_reusable(&g, second, 51000));
        /* Forever is forever, even 40 years on, without overflowing a
         * 32-bit long; complete_only still refuses an incomplete one. */
        assert(tc_heartbeat_facts_reusable(&g, KEEP, 50000 + 40LL * 365 * 86400 * 1000));
        assert(!tc_heartbeat_facts_reusable(&g, any_complete, 50000));
        /* A clock that went back keeps nothing. */
        assert(!tc_heartbeat_facts_reusable(&g, KEEP, 49999));
        /* Keeping updates the address sent but collects nothing. */
        reset();
        tc_heartbeat_facts_update(&g, KEEP, &round);
        assert(!round.collected && collections == 0 && !strcmp(g.mac, "80:ea:96:e6:58:68"));
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
        assert(!build(&f, READ, json, sizeof(json), &r));
        assert(strstr(json, "\"mac\":\"80:ea:96:e6:58:68\"") && !strcmp(f.wama, "80:ea:96:e6:58:68"));
        /* Kept through a collection that stops before waMA (syAP times out),
         * one where waMA is "not set", and one that never validates. */
        reset();
        answer("syAP", ACP_ABORT, "", ACP_REASON_TIMEOUT, 0);
        assert(!build(&f, READ, json, sizeof(json), &r) && strstr(json, "\"mac\":\"80:ea:96:e6:58:68\""));
        reset();
        answer("waMA", ACP_UNAVAILABLE, "", ACP_REASON_NONE, 0);
        assert(!build(&f, READ, json, sizeof(json), &r) && strstr(json, "\"mac\":\"80:ea:96:e6:58:68\""));
    }
    /* A plan that never validates still gets waMA's address. */
    {
        static struct tc_heartbeat_facts f;
        reset();
        validated = 0;
        table_mac = other_mac;
        answer("waMA", ACP_OK, "80:EA:96:E6:58:68", ACP_REASON_NONE, 0);
        assert(!build(&f, READ, json, sizeof(json), &r) && strstr(json, "\"mac\":\"80:ea:96:e6:58:68\""));
    }
    /* Boot: both sources hold a placeholder. The serial names the router
     * and no address goes. */
    {
        static struct tc_heartbeat_facts f;
        reset();
        table_mac = placeholder_mac;
        answer("waMA", ACP_OK, "00:01:02:03:04:05", ACP_REASON_NONE, 0);
        assert(!build(&f, READ, json, sizeof(json), &r));
        assert(strstr(json, "\"router_id\":\"serial123\"") && strstr(json, "\"mac\":null") && !f.wama[0]);
        /* The table is read again on every heartbeat until waMA answers. */
        reset();
        table_mac = other_mac;
        assert(!build(&f, KEEP, json, sizeof(json), &r) && collections == 0);
        assert(strstr(json, "\"mac\":\"e8:8d:28:58:f1:5c\""));
        /* The next collection's waMA is kept, and later heartbeats send it
         * with no acp read, whatever the table holds. */
        reset();
        answer("waMA", ACP_OK, "80:EA:96:E6:58:68\n", ACP_REASON_NONE, 0);
        assert(!build(&f, READ, json, sizeof(json), &r) && strstr(json, "\"mac\":\"80:ea:96:e6:58:68\""));
        reset();
        table_mac = placeholder_mac;
        assert(!build(&f, KEEP, json, sizeof(json), &r) && collections == 0);
        assert(strstr(json, "\"mac\":\"80:ea:96:e6:58:68\""));
        /* A later placeholder leaves it; a different usable answer replaces it. */
        reset();
        answer("waMA", ACP_OK, "00:01:02:03:04:05", ACP_REASON_NONE, 0);
        assert(!build(&f, READ, json, sizeof(json), &r) && strstr(json, "\"mac\":\"80:ea:96:e6:58:68\""));
        reset();
        answer("waMA", ACP_OK, "e8:8d:28:58:f1:5c", ACP_REASON_NONE, 0);
        assert(!build(&f, READ, json, sizeof(json), &r) && strstr(json, "\"mac\":\"e8:8d:28:58:f1:5c\""));
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
        assert(build(&f, READ, json, sizeof(json), &r) && r.failure == TC_OUTCOME_ACP_UNAVAILABLE && !f.mac[0]);
        reset();
        answer("sySN", ACP_UNAVAILABLE, "", ACP_REASON_NONE, 0);
        answer("waMA", ACP_UNAVAILABLE, "", ACP_REASON_NONE, 0);
        assert(!build(&f, READ, json, sizeof(json), &r) && strstr(json, "\"router_id\":\"mac-80:ea:96:e6:58:68\""));
    }
    /* A complete collection that names no router (no serial, waMA not set,
     * a placeholder table) is never kept, under any row: each retry reads
     * acp again instead of failing acp_unavailable on kept facts. */
    {
        static struct tc_heartbeat_facts f, g;
        const struct tc_facts_reuse extra = {TC_INTERVAL_SECONDS, 0};
        char id[64];
        reset();
        table_mac = placeholder_mac;
        answer("sySN", ACP_OK, "", ACP_REASON_NONE, 0);
        answer("waMA", ACP_OK, "", ACP_REASON_NONE, 0);
        assert(build(&f, READ, json, sizeof(json), &r) && r.failure == TC_OUTCOME_ACP_UNAVAILABLE);
        assert(f.have_last && f.last_complete && tc_heartbeat_facts_router_id(&f, id, sizeof(id)) && !id[0]);
        assert(!tc_heartbeat_facts_reusable(&f, KEEP, 50000) && !tc_heartbeat_facts_reusable(&f, OUTAGE, 50000) &&
               !tc_heartbeat_facts_reusable(&f, extra, 50000));
        reset();
        table_mac = placeholder_mac;
        answer("sySN", ACP_OK, "", ACP_REASON_NONE, 0);
        answer("waMA", ACP_OK, "", ACP_REASON_NONE, 0);
        assert(build(&f, KEEP, json, sizeof(json), &r) && collections == 1);
        /* An address from the table names it: kept again. */
        reset();
        answer("sySN", ACP_OK, "", ACP_REASON_NONE, 0);
        answer("waMA", ACP_OK, "", ACP_REASON_NONE, 0);
        assert(!build(&f, READ, json, sizeof(json), &r));
        assert(!tc_heartbeat_facts_router_id(&f, id, sizeof(id)) && !strcmp(id, "mac-80:ea:96:e6:58:68"));
        assert(tc_heartbeat_facts_reusable(&f, KEEP, 50000) && tc_heartbeat_facts_reusable(&f, OUTAGE, 50000) &&
               tc_heartbeat_facts_reusable(&f, extra, 50000));
        /* So does a serial with no address at all. */
        reset();
        table_mac = placeholder_mac;
        answer("waMA", ACP_OK, "", ACP_REASON_NONE, 0);
        assert(!build(&g, READ, json, sizeof(json), &r) && !g.mac[0]);
        assert(!tc_heartbeat_facts_router_id(&g, id, sizeof(id)) && !strcmp(id, "serial123"));
        assert(tc_heartbeat_facts_reusable(&g, KEEP, 50000) && tc_heartbeat_facts_reusable(&g, extra, 50000));
    }
}

/* The planner's choice and the facts together, by what the device sees:
 * whether a heartbeat forks acp. Each case is one row of planner.h's table;
 * the facts' clock (now_ms) follows the planner's seconds. */
/* How far into the planner's second each attempt collects, in ms. */
static long long into_second;

static int attempt_at(struct tc_planner *p, struct tc_heartbeat_facts *f, long now, int requests,
                      enum tc_outcome outcome, int line) {
    struct tc_attempt a = planner_next(p, now, requests);
    if (a.kind == TC_ATTEMPT_NONE) fprintf(stderr, "no attempt at %ld (line %d)\n", now, line);
    struct tc_facts_round round;
    assert(a.kind != TC_ATTEMPT_NONE);
    now_ms = now * 1000LL + into_second;
    tc_heartbeat_facts_update(f, a.facts, &round);
    planner_finished(p, now, &a, outcome, requests);
    return round.collected;
}

#define attempt(p, f, now, requests, outcome) attempt_at(p, f, now, requests, outcome, __LINE__)

static void incomplete_next(void) {
    reset();
    answer("syNm", ACP_ABORT, "", ACP_REASON_TIMEOUT, 0);
}

static void test_planned_reads(void) {
    static struct tc_heartbeat_facts f;
    struct tc_planner p;
    long t, end, last_fork;
    /* Boot, then network and server failures: a complete collection under
     * an hour old serves the retries; an older one is read again. */
    reset(); memset(&f, 0, sizeof(f)); planner_init(&p, 900, 0);
    assert(attempt(&p, &f, 1000, 0, TC_OUTCOME_NETWORK));
    assert(!attempt(&p, &f, p.retry_at, 0, TC_OUTCOME_SERVER));
    assert(!attempt(&p, &f, 1000 + TC_FACTS_REUSE_SECONDS - 1, 0, TC_OUTCOME_NETWORK));
    assert(p.retry_at > 1000 + TC_FACTS_REUSE_SECONDS);
    incomplete_next();
    assert(attempt(&p, &f, p.retry_at, 0, TC_OUTCOME_NETWORK) && !f.last_complete);
    /* An incomplete one is read again on the next retry. */
    reset();
    assert(attempt(&p, &f, p.retry_at, 0, TC_OUTCOME_DELIVERED));
    /* A scheduled heartbeat reads, however fresh the facts are. */
    t = p.next_scheduled;
    assert(attempt(&p, &f, t, 0, TC_OUTCOME_DELIVERED));
    /* Extra heartbeats keep what they have, incomplete or not, until acp
     * has gone unread for the interval; a health event that never clears
     * still reads every 12 hours. */
    incomplete_next();
    assert(attempt(&p, &f, p.next_scheduled, 0, TC_OUTCOME_DELIVERED) && !f.last_complete);
    reset();
    last_fork = p.next_scheduled - TC_INTERVAL_SECONDS;
    end = last_fork + 3 * TC_INTERVAL_SECONDS;
    for (t = last_fork + 1; t < end; t++) {
        if (planner_next(&p, t, 1).kind == TC_ATTEMPT_NONE) continue;
        if (attempt(&p, &f, t, 1, TC_OUTCOME_DELIVERED)) {
            assert(t - last_fork >= TC_INTERVAL_SECONDS);
            last_fork = t;
        } else {
            assert(t - last_fork < TC_INTERVAL_SECONDS);
        }
    }
    assert(t - last_fork <= TC_INTERVAL_SECONDS);
    /* An extra that failed on the network keeps its facts on the retry. */
    reset(); memset(&f, 0, sizeof(f)); planner_init(&p, 900, 0);
    assert(attempt(&p, &f, 1000, 0, TC_OUTCOME_DELIVERED));
    assert(!attempt(&p, &f, 1100, 1, TC_OUTCOME_NETWORK));
    assert(!attempt(&p, &f, p.retry_at, 1, TC_OUTCOME_DELIVERED));
    /* One that found no router ID reads, extra or not. */
    t = p.next_extra_at;
    assert(!attempt(&p, &f, t, 1, TC_OUTCOME_ACP_UNAVAILABLE));
    assert(attempt(&p, &f, p.retry_at, 1, TC_OUTCOME_ACP_UNAVAILABLE));
    /* A device short of processes forks nothing while it has facts, however
     * old or incomplete they are ... */
    reset(); memset(&f, 0, sizeof(f)); planner_init(&p, 900, 0);
    incomplete_next();
    assert(attempt(&p, &f, 1000, 0, TC_OUTCOME_LOCAL_RESOURCE));
    reset();
    assert(!attempt(&p, &f, p.retry_at, 0, TC_OUTCOME_LOCAL_RESOURCE));
    assert(!attempt(&p, &f, p.retry_at + 3 * TC_INTERVAL_SECONDS, 0, TC_OUTCOME_LOCAL_RESOURCE));
    /* ... and still tries once with none. */
    memset(&f, 0, sizeof(f));
    assert(attempt(&p, &f, p.retry_at, 0, TC_OUTCOME_LOCAL_RESOURCE));
    now_ms = 50000;
}

/* An extra heartbeat's 12 hours count from the last collection, whichever
 * attempt made it: one made by a scheduled heartbeat that then failed on
 * the network serves the extra retries after it, complete or not, and the
 * first retry 12 hours on reads again. */
static void test_extra_age_counts_from_any_collection(void) {
    static struct tc_heartbeat_facts f;
    struct tc_planner p;
    long collected;
    reset(); memset(&f, 0, sizeof(f)); planner_init(&p, 900, 0);
    assert(attempt(&p, &f, 1000, 0, TC_OUTCOME_DELIVERED));
    collected = p.next_scheduled;
    incomplete_next();
    assert(attempt(&p, &f, collected, 0, TC_OUTCOME_NETWORK) && !f.last_complete);
    reset();
    while (p.retry_at < collected + TC_INTERVAL_SECONDS)
        assert(!attempt(&p, &f, p.retry_at, 1, TC_OUTCOME_NETWORK));
    assert(attempt(&p, &f, p.retry_at, 1, TC_OUTCOME_DELIVERED) && f.last_complete);
    now_ms = 50000;
}

/* An extra due 12 h after the collection on the planner's clock reads acp,
 * wherever in their seconds the collection and the pass fell: collected
 * 100 ms into a second, the extra's pass only 50 ms into its own is under
 * 12 h on a millisecond clock, and keeping the facts there would hold them
 * another 12 h. */
static void test_extra_age_ignores_where_in_the_second(void) {
    static struct tc_heartbeat_facts f;
    struct tc_planner p;
    long t;
    reset(); memset(&f, 0, sizeof(f)); planner_init(&p, 900, 0);
    into_second = 100;
    assert(attempt(&p, &f, 1000, 1, TC_OUTCOME_DELIVERED));
    /* Extras up to the cap keep the boot collection. */
    into_second = 50;
    for (t = 1001; t < 1000 + TC_INTERVAL_SECONDS; t = p.next_extra_at)
        assert(!attempt(&p, &f, t, 1, TC_OUTCOME_DELIVERED));
    /* The first one 12 h on reads, 50 ms into its second. */
    p.next_extra_at = 1000 + TC_INTERVAL_SECONDS;
    assert(attempt(&p, &f, 1000 + TC_INTERVAL_SECONDS, 1, TC_OUTCOME_DELIVERED));
    into_second = 0;
    now_ms = 50000;
}

int main(void) {
    static struct tc_heartbeat_facts f;
    struct tc_payload_result r;
    struct tc_failure_counts counts;
    char json[8192];

    /* A first heartbeat reads everything in one collection: identity first,
     * stopping at the first timeout. */
    reset();
    assert(!build(&f, READ, json, sizeof(json), &r) && !r.local_resource);
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
    assert(!build(&f, KEEP, json, sizeof(json), &r));
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
    assert(!build(&f, READ, json, sizeof(json), &r) && !r.local_resource);
    assert(f.acp.timeouts == 1 && built_with_history == 1);
    assert(!f.acp.empty); /* a timeout is no answer, not an empty one */
    assert(strstr(json, "\"router_id\":\"serial123\"") && strstr(json, "\"device_syap\":\"106\""));
    assert(strstr(json, "\"plan_error\":\"syNm\"") && strstr(json, "\"plan_stale_sec\":7"));
    assert(!strstr(json, "plan_age_sec"));

    /* A model read that fails for want of a process: the kept one goes. */
    reset();
    answer("syAM", ACP_ABORT, "", ACP_REASON_FORK, EAGAIN);
    assert(!build(&f, READ, json, sizeof(json), &r) && r.local_resource);
    assert(strstr(json, "\"device_model\":\"TimeCapsule6,106\""));

    /* An ACPd that cannot answer: acp prints nothing and exits 0 for every
     * get, so each key reads empty at once (no timeout). The kept serial,
     * model, model number and name still go, under the same router ID, and
     * the collection is not complete, so a retry reads acp again. */
    reset();
    answer("sySN", ACP_OK, "", ACP_REASON_NONE, 0);
    answer("syAP", ACP_OK, "", ACP_REASON_NONE, 0);
    answer("syAM", ACP_OK, " \t", ACP_REASON_NONE, 0);
    answer("syNm", ACP_OK, "", ACP_REASON_NONE, 0);
    answer("waMA", ACP_OK, "", ACP_REASON_NONE, 0);
    assert(!build(&f, READ, json, sizeof(json), &r) && collections == 1);
    assert(strstr(json, "\"router_id\":\"serial123\"") && strstr(json, "\"device_syap\":\"106\""));
    assert(strstr(json, "\"device_model\":\"TimeCapsule6,106\"") && strstr(json, "\"device_name\":\"Name\\n"));
    assert(strstr(json, "\"mac\":\"80:ea:96:e6:58:68\"") && f.acp.timeouts == 1 && f.acp.empty == 1);
    assert(!f.last_complete && !outage_keeps(&f, 50000));
    /* acp exiting nonzero ("not set") does not clear them either; a new
     * value replaces the kept one. */
    reset();
    answer("sySN", ACP_UNAVAILABLE, "", ACP_REASON_NONE, 0);
    answer("syAM", ACP_OK, "TimeCapsule8,119", ACP_REASON_NONE, 0);
    assert(!build(&f, READ, json, sizeof(json), &r));
    assert(strstr(json, "\"router_id\":\"serial123\"") && strstr(json, "\"device_model\":\"TimeCapsule8,119\""));
    assert(!f.last_complete);
    reset();
    assert(!build(&f, READ, json, sizeof(json), &r) && f.last_complete && outage_keeps(&f, 50000));

    /* A new process whose model read times out still reports, under its
     * serial, with the model empty: the router ID does not depend on it. */
    {
        static struct tc_heartbeat_facts fresh;
        reset();
        answer("syAM", ACP_ABORT, "", ACP_REASON_TIMEOUT, 0);
        assert(!build(&fresh, READ, json, sizeof(json), &r));
        assert(strstr(json, "\"router_id\":\"serial123\"") && strstr(json, "\"device_model\":\"\""));
        /* ... and the name, asked after the timeout, is empty too. */
        assert(strstr(json, "\"device_name\":\"\"") && fresh.acp.timeouts == 1);
    }
    /* A hung ACPd from the first read (ACPd stuck at boot, telemetry
     * restarted during the wedge): the hardware address names it. */
    {
        static struct tc_heartbeat_facts fresh;
        reset();
        answer("sySN", ACP_ABORT, "", ACP_REASON_TIMEOUT, 0);
        assert(!build(&fresh, READ, json, sizeof(json), &r));
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
        assert(!build(&fresh, READ, json, sizeof(json), &r) && !r.local_resource);
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
        assert(!build(&fresh, READ, json, sizeof(json), &r) && r.local_resource);
        assert(strstr(json, "\"router_id\":\"mac-80:ea:96:e6:58:68\"") && strstr(json, "\"device_model\":\"\""));
        tc_failure_counts(&counts);
        assert(counts.fork_eagain == before.fork_eagain + 1);
    }
    /* No serial ("not set"): the hardware address, never a hash. */
    {
        static struct tc_heartbeat_facts fresh;
        reset();
        answer("sySN", ACP_UNAVAILABLE, "", ACP_REASON_NONE, 0);
        assert(!build(&fresh, READ, json, sizeof(json), &r));
        assert(strstr(json, "\"router_id\":\"mac-80:ea:96:e6:58:68\""));
        /* A unit that never gave a serial still keeps complete facts, so a
         * retry after a network failure need not fork acp again ... */
        assert(fresh.last_complete && outage_keeps(&fresh, 50000) && !fresh.acp.empty);
        /* ... and once one comes, a later empty serial makes it incomplete. */
        reset();
        assert(!build(&fresh, READ, json, sizeof(json), &r) && fresh.last_complete);
        reset();
        answer("sySN", ACP_OK, "", ACP_REASON_NONE, 0);
        assert(!build(&fresh, READ, json, sizeof(json), &r) && !fresh.last_complete);
        assert(strstr(json, "\"router_id\":\"serial123\""));
    }
    /* Neither a serial nor a hardware address: nothing to name it by. */
    {
        static struct tc_heartbeat_facts fresh;
        reset();
        have_mac = 0;
        answer("sySN", ACP_UNAVAILABLE, "", ACP_REASON_NONE, 0);
        assert(build(&fresh, READ, json, sizeof(json), &r) && r.failure == TC_OUTCOME_ACP_UNAVAILABLE);
        assert(!fresh.mac[0]);
    }
    /* TERM during the collection. */
    {
        static struct tc_heartbeat_facts fresh;
        reset();
        telemetry_stop = 1;
        assert(build(&fresh, READ, json, sizeof(json), &r) && r.failure == TC_OUTCOME_CANCELLED);
        telemetry_stop = 0;
    }
    /* The largest delivery record fits its budget (telemetry.h). */
    {
        static struct tc_delivery most = {4294967295u, 2147483647L, 4294967295u, 4294967295u, 4294967295u,
                                          4294967295u};
        const char *start, *end;
        delivery = &most;
        reset();
        assert(!build(&f, KEEP, json, sizeof(json), &r));
        start = strstr(json, ",\"delivery\":");
        end = strstr(start, "}}");
        assert(start && end && (size_t)(end + 2 - start) <= TC_DELIVERY_JSON_MAX);
        delivery = NULL;
    }
    /* Too small a buffer sends nothing. */
    reset();
    assert(build(&f, KEEP, json, 12, &r) && r.failure == TC_OUTCOME_SERVER);
    test_mac_parse();
    test_facts_reusable();
    test_planned_reads();
    test_extra_age_counts_from_any_collection();
    test_extra_age_ignores_where_in_the_second();
    test_mac_sources();
    test_heartbeat_ident();
    test_health_that_does_not_fit();
    return 0;
}
