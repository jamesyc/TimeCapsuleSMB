/* The health layers (telemetry/health*.c):
 *   test_health detectors   every detector and the ledger, on constructed
 *                           readings and a simulated clock (asserts only)
 *   test_health sample      one reader sample from the host fixtures
 *                           (TC_TEST_PROCS, TC_TEST_HEALTH, TC_TEST_KMSG);
 *                           prints the health object
 *   test_health worst       the largest health object; prints it
 *   test_health probes N    N probe rounds against the test's servers;
 *                           prints each round's results and elapsed ms
 * SIGTERM sets telemetry_stop, as the daemon's handler does. */
#include "health.h"
#include "heartbeat_facts.h"
#include "../common/acp.h"
#include <assert.h>

volatile sig_atomic_t telemetry_stop;
static void stop(int sig) { (void)sig; telemetry_stop = 1; }

static const char *json_of(const struct tc_health *h, unsigned *token) {
    static char json[8192];
    unsigned t;
    static const struct tc_acp_counts none;
    int n = tc_health_append_json(json, sizeof(json), 0, h, &none, token ? token : &t);
    assert(n > 0);
    return json + 1; /* past the leading comma */
}

static void thresholds(void) {
    struct tc_threshold t;
    memset(&t, 0, sizeof(t));
    /* One sample past the mark is a burst: nothing. */
    assert(!tc_threshold_step(&t, 85, 80, 70, 1));
    assert(!tc_threshold_step(&t, 60, 80, 70, 1) && t.over == 0);
    /* Two in a row cross, once. */
    assert(!tc_threshold_step(&t, 85, 80, 70, 1));
    assert(tc_threshold_step(&t, 85, 80, 70, 1) && t.high && t.events == 1);
    assert(!tc_threshold_step(&t, 90, 80, 70, 1));
    /* Hovering between the marks never rearms or asks again. */
    assert(!tc_threshold_step(&t, 75, 80, 70, 1) && t.high);
    assert(!tc_threshold_step(&t, 85, 80, 70, 1) && !tc_threshold_step(&t, 85, 80, 70, 1));
    /* At the rearm mark it rearms, and the next crossing asks again. */
    assert(!tc_threshold_step(&t, 70, 80, 70, 1) && !t.high);
    assert(!tc_threshold_step(&t, 81, 80, 70, 1) && tc_threshold_step(&t, 81, 80, 70, 1) && t.events == 2);
    /* Not armed (boot): nothing, however long it stays high; the first
     * armed sample still past the mark crosses at once. */
    memset(&t, 0, sizeof(t));
    assert(!tc_threshold_step(&t, 85, 80, 70, 0) && !tc_threshold_step(&t, 85, 80, 70, 0));
    assert(!tc_threshold_step(&t, 85, 80, 70, 0) && !t.high);
    assert(tc_threshold_step(&t, 85, 80, 70, 1));
    /* An unreadable sample between two high ones breaks the run: they are
     * not two in a row. The next two readable ones are. */
    memset(&t, 0, sizeof(t));
    assert(!tc_threshold_step(&t, 85, 80, 70, 1));
    tc_threshold_unread(&t);
    assert(!tc_threshold_step(&t, 85, 80, 70, 1) && !t.high);
    assert(tc_threshold_step(&t, 85, 80, 70, 1) && t.high);
    /* ... and leaves a high threshold high: no new crossing after it. */
    tc_threshold_unread(&t);
    assert(!tc_threshold_step(&t, 85, 80, 70, 1) && !tc_threshold_step(&t, 85, 80, 70, 1) && t.events == 1);
}

static void probe_tracks(void) {
    struct tc_probe_track p;
    memset(&p, 0, sizeof(p));
    /* Refused twice in a row while the service runs: one trigger. */
    assert(!tc_probe_track_step(&p, TC_PROBE_OK, 1, 10) && p.phase == TC_PROBE_QUIET);
    assert(!tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 20) && p.first_fail_at == 20);
    assert(p.phase == TC_PROBE_CONFIRMING);
    assert(tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 30) && p.phase == TC_PROBE_REPORTED);
    /* Reported: later failures count but do not trigger again. */
    assert(!tc_probe_track_step(&p, TC_PROBE_DOWN, 1, 40) && p.streak == 3 && p.last_fail_at == 40);
    assert(p.phase == TC_PROBE_REPORTED);
    /* Only an answer ends the episode; then it can trigger again. */
    assert(!tc_probe_track_step(&p, TC_PROBE_OK, 1, 50) && p.streak == 0 && p.phase == TC_PROBE_QUIET);
    assert(!tc_probe_track_step(&p, TC_PROBE_DOWN, 1, 60) && tc_probe_track_step(&p, TC_PROBE_DOWN, 1, 70));
    assert(p.closed == 2 && p.down == 3 && !p.refused && !p.timeout && p.ok == 2);
    /* A target that never answered (no ::1 route, a port that never
     * listened): its timeouts, connect errors and refusals are reported in
     * last and the counters, but never count, so it stays quiet for good
     * instead of failing every round. */
    memset(&p, 0, sizeof(p));
    assert(!tc_probe_track_step(&p, TC_PROBE_TIMEOUT, 0, 10) && !tc_probe_track_step(&p, TC_PROBE_DOWN, 0, 20));
    assert(!tc_probe_track_step(&p, TC_PROBE_TIMEOUT, 1, 30) && !tc_probe_track_step(&p, TC_PROBE_DOWN, 1, 40));
    assert(p.timeout == 2 && p.down == 2 && p.last == TC_PROBE_DOWN && !p.streak && !p.hard_streak);
    assert(p.phase == TC_PROBE_QUIET && !p.first_fail_at);
    /* ... but closed and silent count from the first round: the port
     * accepted, so the service is there (sshd wedged before telemetry
     * started still triggers). */
    assert(!tc_probe_track_step(&p, TC_PROBE_CLOSED, 0, 50) && p.phase == TC_PROBE_CONFIRMING);
    assert(tc_probe_track_step(&p, TC_PROBE_CLOSED, 0, 60) && p.phase == TC_PROBE_REPORTED);
    /* Once a target answered, a connect timeout (a full listen queue) is a
     * hard failure even where no listener is seen: something holds the
     * port. */
    memset(&p, 0, sizeof(p));
    assert(!tc_probe_track_step(&p, TC_PROBE_OK, 0, 5));
    assert(!tc_probe_track_step(&p, TC_PROBE_TIMEOUT, 0, 10) && p.hard_streak == 1);
    assert(tc_probe_track_step(&p, TC_PROBE_TIMEOUT, 0, 20) && p.timeout == 2 && p.last == TC_PROBE_TIMEOUT);
    /* Refused, then timed out, while the service runs: two hard failures. */
    memset(&p, 0, sizeof(p));
    assert(!tc_probe_track_step(&p, TC_PROBE_OK, 1, 10));
    assert(!tc_probe_track_step(&p, TC_PROBE_ABSENT, 1, 20) && p.last == TC_PROBE_REFUSED && p.refused == 1);
    assert(tc_probe_track_step(&p, TC_PROBE_TIMEOUT, 1, 30) && p.timeout == 1 && !p.down);
    /* Silent is slower to count: three in a row, and it resets the hard
     * streak, so closed, silent, closed is three of any kind. */
    memset(&p, 0, sizeof(p));
    assert(!tc_probe_track_step(&p, TC_PROBE_SILENT, 1, 10) && !tc_probe_track_step(&p, TC_PROBE_SILENT, 1, 20));
    assert(tc_probe_track_step(&p, TC_PROBE_SILENT, 1, 30) && p.silent == 3 && p.hard_streak == 0);
    memset(&p, 0, sizeof(p));
    assert(!tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 10) && !tc_probe_track_step(&p, TC_PROBE_SILENT, 1, 20));
    assert(p.hard_streak == 0 && tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 30));
    /* Refused while the service is not running (smbd stopped on purpose,
     * SSH off): it is off, not failing, and that ends an episode. */
    memset(&p, 0, sizeof(p));
    assert(!tc_probe_track_step(&p, TC_PROBE_OK, 1, 5));
    assert(!tc_probe_track_step(&p, TC_PROBE_ABSENT, 0, 10) && !tc_probe_track_step(&p, TC_PROBE_ABSENT, 0, 20));
    assert(p.last == TC_PROBE_ABSENT && p.streak == 0 && !p.first_fail_at);
    assert(!tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 30) && !tc_probe_track_step(&p, TC_PROBE_ABSENT, 0, 40));
    assert(p.streak == 0 && !tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 50));
    /* ... but refused while it runs is refused, a hard failure. */
    assert(tc_probe_track_step(&p, TC_PROBE_ABSENT, 1, 60) && p.last == TC_PROBE_REFUSED && p.refused == 1);
    /* A probe that could not run changes nothing once reported ... */
    {
        struct tc_probe_track before = p;
        assert(p.phase == TC_PROBE_REPORTED);
        assert(!tc_probe_track_step(&p, TC_PROBE_SKIPPED, 1, 70) && !memcmp(&before, &p, sizeof(p)));
    }
    /* ... and ends a confirmation, forgetting it: nothing confirmed it, so
     * the cadence goes back to the long interval, and a later failure starts
     * a new count instead of joining one from long ago. */
    memset(&p, 0, sizeof(p));
    assert(!tc_probe_track_step(&p, TC_PROBE_OK, 1, 10) && !tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 20));
    assert(p.phase == TC_PROBE_CONFIRMING);
    assert(!tc_probe_track_step(&p, TC_PROBE_SKIPPED, 1, 30) && p.phase == TC_PROBE_QUIET);
    assert(!p.streak && !p.hard_streak && p.closed == 1 && p.last == TC_PROBE_CLOSED);
    assert(!tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 40) && p.phase == TC_PROBE_CONFIRMING && p.streak == 1);
    assert(p.first_fail_at == 40);
    assert(tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 50) && p.phase == TC_PROBE_REPORTED);
    /* An uncounted failure ends a confirmation the same way: a target that
     * accepted once but never answered, then timed out. */
    memset(&p, 0, sizeof(p));
    assert(!tc_probe_track_step(&p, TC_PROBE_SILENT, 1, 10) && p.phase == TC_PROBE_CONFIRMING);
    assert(!tc_probe_track_step(&p, TC_PROBE_TIMEOUT, 1, 20) && p.phase == TC_PROBE_QUIET && !p.streak);
    assert(p.timeout == 1 && p.last == TC_PROBE_TIMEOUT);
    /* Episode times: the first counted failure of each episode starts them,
     * and they stay after it ends, until the next one begins. */
    memset(&p, 0, sizeof(p));
    assert(!tc_probe_track_step(&p, TC_PROBE_OK, 1, 10));
    assert(!tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 100) && tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 200));
    assert(!tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 300));
    assert(p.first_fail_at == 100 && p.last_fail_at == 300);
    assert(!tc_probe_track_step(&p, TC_PROBE_OK, 1, 400) && p.first_fail_at == 100 && p.last_fail_at == 300);
    assert(!tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 500000));
    assert(p.first_fail_at == 500000 && p.last_fail_at == 500000);
    assert(tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 500300));
    assert(p.first_fail_at == 500000 && p.last_fail_at == 500300);
    /* A result expires after TC_HEALTH_PROBE_STALE_SECONDS without a probe
     * of the target: a reported episode whose rounds keep being skipped
     * reports none from then on, not its last result for days. */
    memset(&p, 0, sizeof(p));
    assert(!tc_probe_track_step(&p, TC_PROBE_OK, 1, 100) && p.probed_at == 100);
    assert(!tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 200) && tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 300));
    assert(p.probed_at == 300 && p.phase == TC_PROBE_REPORTED);
    assert(!tc_probe_track_step(&p, TC_PROBE_SKIPPED, 1, 300 + TC_HEALTH_PROBE_STALE_SECONDS - 1));
    assert(p.phase == TC_PROBE_REPORTED && p.last == TC_PROBE_CLOSED && p.streak == 2);
    assert(!tc_probe_track_step(&p, TC_PROBE_SKIPPED, 1, 300 + TC_HEALTH_PROBE_STALE_SECONDS));
    assert(p.phase == TC_PROBE_QUIET && p.last == TC_PROBE_SKIPPED && !p.streak && !p.hard_streak);
    assert(!p.probed_at && p.closed == 2 && p.first_fail_at == 200 && p.last_fail_at == 300);
    /* Expired once: later skips change nothing; the next real result
     * starts over. */
    {
        struct tc_probe_track before = p;
        assert(!tc_probe_track_step(&p, TC_PROBE_SKIPPED, 1, 300 + 3 * TC_HEALTH_PROBE_STALE_SECONDS));
        assert(!memcmp(&before, &p, sizeof(p)));
    }
    assert(!tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 900000) && p.phase == TC_PROBE_CONFIRMING);
    assert(p.probed_at == 900000 && p.first_fail_at == 900000);
}

static unsigned scan(struct tc_kmsg_state *k, const char *text, long now) {
    return tc_kmsg_scan(k, text, strlen(text), now);
}

/* Appends text to a ring of cap bytes as the kernel does: the oldest bytes
 * drop off the front. ring has room for cap + the longest line + 1. */
static void ring_add(char *ring, size_t cap, const char *text) {
    size_t have = strlen(ring), n = strlen(text);
    memcpy(ring + have, text, n + 1);
    if (have + n > cap) memmove(ring, ring + have + n - cap, cap + 1);
}

#define NB4_RING 4080 /* NetBSD 4's message buffer */
static const char wifi[] = "wl0: deauth from 00:11:22:33:44:55 reason 2\n";
static const char full[] = "proc: table is full\n";

/* connect_failure: what a connect error says about the target. */
enum tc_probe_result tc_health_connect_failure(int error);
static void connect_failures(void) {
    assert(tc_health_connect_failure(ECONNREFUSED) == TC_PROBE_ABSENT);
    /* A server that accepts and resets at once: when the reset lands before
     * the probe checks its connect, the connect reports it, and it is still
     * closed, as when the probe's read sees it. */
    assert(tc_health_connect_failure(ECONNRESET) == TC_PROBE_CLOSED);
    assert(tc_health_connect_failure(ECONNABORTED) == TC_PROBE_CLOSED);
    assert(tc_health_connect_failure(EADDRNOTAVAIL) == TC_PROBE_SKIPPED);
    assert(tc_health_connect_failure(ENETUNREACH) == TC_PROBE_SKIPPED);
    assert(tc_health_connect_failure(EHOSTUNREACH) == TC_PROBE_DOWN);
}

static void kmsg(void) {
    static struct tc_kmsg_state k;
    static char ring[TC_KMSG_MAX + 128];
    char line[64];
    int i, j;
    memset(&k, 0, sizeof(k));
    /* The first read is the starting point: what it holds may predate this
     * process. It asks nothing and only marks the pattern. */
    assert(scan(&k, "boot\nproc: table is full\n", 5) == 0);
    assert(k.seen[0].at_start && !k.seen[0].n && !k.seen[0].first);
    /* Nothing new: nothing. */
    assert(scan(&k, "boot\nproc: table is full\n", 10) == 0);
    /* The same message again, later: a new sighting, even though it was
     * there at the start. */
    assert(scan(&k, "boot\nproc: table is full\nproc: table is full\n", 20) == TC_EV_KMSG);
    assert(k.seen[0].n == 1 && k.seen[0].first == 20 && k.seen[0].last == 20);
    assert(scan(&k, "boot\nproc: table is full\nproc: table is full\nfile: table is full\n", 30) == TC_EV_KMSG);
    assert(k.seen[0].n == 1 && k.seen[1].n == 1);
    /* A second episode days later counts again, last moves, first stays. */
    assert(scan(&k, "boot\nproc: table is full\nproc: table is full\nfile: table is full\nx\nproc: table is full\n",
                400000) == TC_EV_KMSG);
    assert(k.seen[0].n == 2 && k.seen[0].first == 20 && k.seen[0].last == 400000);
    /* Pool limits and swap are warnings: news, no request. */
    memset(&k, 0, sizeof(k));
    scan(&k, "boot\n", 1);
    assert(scan(&k, "boot\nmclpool limit reached\n", 2) == TC_EV_KMSG_WARNING);
    /* A line split across two reads is counted once, in the read that
     * completes it. */
    memset(&k, 0, sizeof(k));
    scan(&k, "boot\n", 1);
    assert(scan(&k, "boot\nproc: table is fu", 2) == 0);
    assert(scan(&k, "boot\nproc: table is full\n", 3) == TC_EV_KMSG && k.seen[0].n == 1);
    assert(scan(&k, "boot\nproc: table is full\nok\n", 4) == 0 && k.seen[0].n == 1);
    /* Before the first wrap the buffer only grows, even when it ends in
     * copies of one line: each new copy counts. */
    memset(&k, 0, sizeof(k));
    ring[0] = 0;
    ring_add(ring, NB4_RING, full);
    ring_add(ring, NB4_RING, full);
    scan(&k, ring, 1);
    for (i = 0; i < 3; i++) {
        ring_add(ring, NB4_RING, full);
        assert(scan(&k, ring, 2 + i) == TC_EV_KMSG && k.seen[0].n == (unsigned)i + 1);
    }

    /* A buffer flooded with one Wi-Fi line, wrapped many times: a full
     * table between the repeats is found, and only once. */
    memset(&k, 0, sizeof(k));
    ring[0] = 0;
    for (i = 0; i < 300; i++) ring_add(ring, NB4_RING, wifi);
    assert(strlen(ring) == NB4_RING && !strstr(ring, "boot"));
    scan(&k, ring, 1);
    for (i = 0; i < 3; i++) ring_add(ring, NB4_RING, wifi);
    ring_add(ring, NB4_RING, full);
    for (i = 0; i < 3; i++) ring_add(ring, NB4_RING, wifi);
    assert(scan(&k, ring, 2) == TC_EV_KMSG && k.seen[0].n == 1);
    for (i = 0; i < 5; i++) {
        ring_add(ring, NB4_RING, wifi);
        assert(scan(&k, ring, 3 + i) == 0 && k.seen[0].n == 1);
    }
    /* The same with older, different lines still at the front. */
    memset(&k, 0, sizeof(k));
    ring[0] = 0;
    for (i = 0; i < 40; i++) {
        snprintf(line, sizeof(line), "boot line %d\n", i);
        ring_add(ring, NB4_RING, line);
    }
    for (i = 0; i < 90; i++) ring_add(ring, NB4_RING, wifi);
    assert(strlen(ring) == NB4_RING && strstr(ring, "boot line 39"));
    scan(&k, ring, 1);
    ring_add(ring, NB4_RING, wifi);
    ring_add(ring, NB4_RING, full);
    ring_add(ring, NB4_RING, wifi);
    assert(scan(&k, ring, 2) == TC_EV_KMSG && k.seen[0].n == 1);

    /* Full tables logged again and again after varied text, across wraps:
     * every copy counts while the varied text is still in the buffer. */
    memset(&k, 0, sizeof(k));
    ring[0] = 0;
    for (i = 0; i < 300; i++) {
        snprintf(line, sizeof(line), "sd%d: line %d\n", i % 7, i);
        ring_add(ring, NB4_RING, line);
    }
    assert(strlen(ring) == NB4_RING);
    scan(&k, ring, 1);
    for (i = 0; i < 20; i++) {
        for (j = 0; j < 2; j++) ring_add(ring, NB4_RING, full);
        assert(scan(&k, ring, 2 + i) == TC_EV_KMSG && k.seen[0].n == 2 * (unsigned)i + 2);
    }

    /* Wrapped past the whole previous read between two reads: all of it is
     * new, and a full table in it counts. */
    memset(&k, 0, sizeof(k));
    ring[0] = 0;
    for (i = 0; i < 300; i++) ring_add(ring, NB4_RING, wifi);
    scan(&k, ring, 1);
    for (i = 0; i < 1000; i++) {
        snprintf(line, sizeof(line), "after %d\n", i);
        ring_add(ring, NB4_RING, line);
        if (i == 900) ring_add(ring, NB4_RING, "file: table is full\n");
    }
    assert(!strstr(ring, "wl0"));
    assert(scan(&k, ring, 2) == TC_EV_KMSG && k.seen[1].n == 1);

    /* NetBSD 6's 16 KB buffer, flooded and read many times. */
    memset(&k, 0, sizeof(k));
    ring[0] = 0;
    for (i = 0; i < 400; i++) ring_add(ring, 16368, wifi);
    scan(&k, ring, 1);
    for (i = 0; i < 200; i++) {
        ring_add(ring, 16368, wifi);
        if (i == 150) ring_add(ring, 16368, full);
        scan(&k, ring, 2 + i);
    }
    assert(k.seen[0].n == 1);

    /* The documented limit: in a buffer of nothing but one line, new copies
     * of that same line look like the old text, so counts are a lower
     * bound there. */
    memset(&k, 0, sizeof(k));
    ring[0] = 0;
    for (i = 0; i < 300; i++) ring_add(ring, NB4_RING, full);
    scan(&k, ring, 1);
    ring_add(ring, NB4_RING, full);
    assert(scan(&k, ring, 2) == 0 && k.seen[0].n == 0);
}

static struct tc_reading reading(unsigned procs, int maxproc) {
    struct tc_reading r;
    memset(&r, 0, sizeof(r));
    r.counters.maxproc = maxproc;
    r.counters.maxfiles = 1000;
    r.counters.files = 100;
    r.snapshot.read = 1;
    r.snapshot.procs = procs;
    r.snapshot.smbd_children = -1;
    r.snapshot.smbd_up = 1;
    r.kmsg = "";
    r.kmsg_len = 0;
    return r;
}

static void ledger_and_sample(void) {
    struct tc_health h;
    struct tc_reading r;
    struct tc_probe_round round;
    unsigned token;
    size_t i;
    memset(&h, 0, sizeof(h));
    /* Boot: high, but not armed. */
    r = reading(85, 90);
    tc_health_sample(&h, &r, 10, 0);
    tc_health_sample(&h, &r, 25, 0);
    assert(!h.pending && !h.news && !tc_health_requests(&h));
    /* Armed and still high: a crossing, and the first-high snapshot. */
    tc_health_sample(&h, &r, 40, 1);
    assert(h.pending == TC_EV_PROCS && h.news == TC_EV_PROCS && tc_health_requests(&h));
    assert(h.have_first_high && h.first_high.procs == 85 && h.peak.procs == 85);
    /* The JSON carries them as its token. A heartbeat that fails leaves
     * them for the next try, which carries them again; a delivered one
     * clears them. */
    assert(strstr(json_of(&h, &token), "\"triggers\":[\"procs\"],\"news\":[\"procs\"]"));
    assert(token == TC_EV_PROCS && tc_health_requests(&h));
    assert(strstr(json_of(&h, &token), "\"triggers\":[\"procs\"]") && token == TC_EV_PROCS);
    /* An event noted after the heartbeat was built (here a file table
     * crossing) was not in it: delivery clears only the token's bits. */
    {
        struct tc_reading files = r;
        files.counters.files = 950;
        tc_health_sample(&h, &files, 41, 1);
        tc_health_sample(&h, &files, 42, 1);
        assert(h.pending == (TC_EV_PROCS | TC_EV_FILES));
    }
    tc_health_delivered(&h, token);
    assert(h.pending == TC_EV_FILES && h.news == TC_EV_FILES && tc_health_requests(&h));
    assert(strstr(json_of(&h, &token), "\"triggers\":[\"files\"],\"news\":[\"files\"]") && token == TC_EV_FILES);
    tc_health_delivered(&h, token);
    assert(!h.pending && !h.news && !tc_health_requests(&h));
    assert(strstr(json_of(&h, &token), "\"triggers\":[],\"news\":[]") && !token);
    /* Still high: no second crossing. Probes closed twice: a probe event. */
    tc_health_sample(&h, &r, 45, 1);
    for (i = 0; i < TC_PROBES; i++) round.result[i] = TC_PROBE_CLOSED;
    tc_health_probes(&h, &round, 50);
    tc_health_probes(&h, &round, 60);
    assert(h.pending == TC_EV_PROBE && h.news == TC_EV_PROBE);
    /* A refusal is a failure only from a target that answered before, and
     * for smbd only while its listener runs. */
    {
        struct tc_health off;
        memset(&off, 0, sizeof(off));
        r = reading(40, 90);
        r.snapshot.smbd_up = 0;
        tc_health_sample(&off, &r, 10, 1);
        /* sshd answers; smbd is not running, so neither family of it
         * answers. */
        for (i = 0; i < TC_PROBES; i++) round.result[i] = TC_PROBE_OK;
        round.result[TC_PROBE_SMB4] = round.result[TC_PROBE_SMB6] = TC_PROBE_ABSENT;
        tc_health_probes(&off, &round, 20);
        tc_health_probes(&off, &round, 30);
        assert(!off.pending && off.probe[TC_PROBE_SMB6].last == TC_PROBE_ABSENT && off.probe[TC_PROBE_SMB6].streak == 0);
        assert(off.probe[TC_PROBE_SMB4].last == TC_PROBE_ABSENT && off.probe[TC_PROBE_SMB4].streak == 0);
        /* An smbd that listens on IPv4 only: once it runs and answers there,
         * its IPv6 refusals are still no failure, since it never answered on
         * IPv6. */
        {
            struct tc_health v4;
            memset(&v4, 0, sizeof(v4));
            r = reading(40, 90);
            tc_health_sample(&v4, &r, 10, 1);
            for (i = 0; i < TC_PROBES; i++) round.result[i] = TC_PROBE_OK;
            round.result[TC_PROBE_SMB6] = TC_PROBE_ABSENT;
            tc_health_probes(&v4, &round, 20);
            tc_health_probes(&v4, &round, 30);
            tc_health_probes(&v4, &round, 40);
            assert(!v4.pending && v4.probe[TC_PROBE_SMB6].last == TC_PROBE_ABSENT && !v4.probe[TC_PROBE_SMB6].streak);
            assert(v4.probe[TC_PROBE_SMB4].last == TC_PROBE_OK && v4.probe[TC_PROBE_SMB4].ok == 3);
            for (i = 0; i < TC_PROBES; i++) round.result[i] = TC_PROBE_OK;
            round.result[TC_PROBE_SMB4] = round.result[TC_PROBE_SMB6] = TC_PROBE_ABSENT;
        }
        /* IPv4 sshd, which answered, refused twice while sshd runs. */
        round.result[TC_PROBE_SSH4] = TC_PROBE_ABSENT;
        tc_health_probes(&off, &round, 40);
        tc_health_probes(&off, &round, 50);
        assert(off.probe[TC_PROBE_SSH4].last == TC_PROBE_REFUSED && off.probe[TC_PROBE_SSH4].streak == 2);
        assert(off.pending == TC_EV_PROBE && off.probe[TC_PROBE_SSH4].phase == TC_PROBE_REPORTED);
        /* smbd answered once, then stopped on purpose (not in the table):
         * its refusals are not failures. */
        {
            struct tc_health stop;
            memset(&stop, 0, sizeof(stop));
            r = reading(40, 90);
            tc_health_sample(&stop, &r, 10, 1);
            for (i = 0; i < TC_PROBES; i++) round.result[i] = TC_PROBE_OK;
            tc_health_probes(&stop, &round, 20);
            r.snapshot.smbd_up = 0;
            tc_health_sample(&stop, &r, 30, 1);
            round.result[TC_PROBE_SMB4] = round.result[TC_PROBE_SMB6] = TC_PROBE_ABSENT;
            tc_health_probes(&stop, &round, 40);
            tc_health_probes(&stop, &round, 50);
            assert(!stop.pending && stop.probe[TC_PROBE_SMB4].last == TC_PROBE_ABSENT);
        }
        /* sshd answered, then died (nothing listens and no sshd is left in
         * the table, which the reading no longer records): refused, and it
         * triggers on the second round. */
        {
            struct tc_health gone;
            memset(&gone, 0, sizeof(gone));
            r = reading(40, 90);
            tc_health_sample(&gone, &r, 10, 1);
            for (i = 0; i < TC_PROBES; i++) round.result[i] = TC_PROBE_OK;
            tc_health_probes(&gone, &round, 20);
            round.result[TC_PROBE_SSH4] = TC_PROBE_ABSENT;
            tc_health_probes(&gone, &round, 30);
            assert(!gone.pending);
            assert(gone.probe[TC_PROBE_SSH4].last == TC_PROBE_REFUSED &&
                   gone.probe[TC_PROBE_SSH4].phase == TC_PROBE_CONFIRMING);
            tc_health_probes(&gone, &round, 330);
            assert(gone.pending == TC_EV_PROBE && gone.probe[TC_PROBE_SSH4].phase == TC_PROBE_REPORTED &&
                   gone.probe[TC_PROBE_SSH4].streak == 2);
        }
    }
    /* No probe round within 2 of kern.maxproc, where the probe's own
     * children could take the last slots, nor while the table or the limit
     * is unknown. */
    {
        struct tc_health full;
        memset(&full, 0, sizeof(full));
        assert(!tc_health_probe_allowed(&full)); /* nothing sampled yet */
        r = reading(87, 90);
        tc_health_sample(&full, &r, 10, 1);
        assert(tc_health_probe_allowed(&full));
        r = reading(88, 90);
        tc_health_sample(&full, &r, 20, 1);
        assert(!tc_health_probe_allowed(&full));
        r = reading(89, 90);
        tc_health_sample(&full, &r, 30, 1);
        assert(!tc_health_probe_allowed(&full));
        /* The table read failed after a full one: still no round. */
        r.snapshot.read = 0;
        tc_health_sample(&full, &r, 40, 1);
        assert(!tc_health_probe_allowed(&full));
        /* ... and after a roomy one too: the last sample is what counts. */
        r = reading(40, 90);
        tc_health_sample(&full, &r, 50, 1);
        assert(tc_health_probe_allowed(&full));
        r.snapshot.read = 0;
        tc_health_sample(&full, &r, 60, 1);
        assert(!tc_health_probe_allowed(&full));
        r = reading(40, -1); /* kern.maxproc unreadable */
        tc_health_sample(&full, &r, 70, 1);
        assert(!tc_health_probe_allowed(&full));
    }
    /* Memory and mbuf deltas are news only. */
    {
        struct tc_health m;
        memset(&m, 0, sizeof(m));
        r = reading(40, 90);
        r.counters.have_mem = r.counters.have_mbuf = 1;
        r.counters.free = 100;
        tc_health_sample(&m, &r, 10, 1);
        assert(!m.news);
        r.counters.fltnoram = 1;
        r.counters.mbuf_drops = 2;
        r.counters.free = 50;
        tc_health_sample(&m, &r, 20, 1);
        assert(m.news == (TC_EV_RAM | TC_EV_MBUF) && !m.pending && m.min_free == 50 && m.min_free_at == 20);
    }
    /* Peaks and lows cover the time since the last delivered heartbeat: a
     * lower sample and a failed heartbeat keep them, a delivered one starts
     * the next window from the latest sample. */
    {
        struct tc_health w;
        unsigned token;
        memset(&w, 0, sizeof(w));
        r = reading(40, 90);
        r.counters.have_mem = 1;
        r.counters.free = 30;
        r.counters.files = 700;
        r.snapshot.at = 10;
        tc_health_sample(&w, &r, 10, 1);
        r = reading(85, 90);
        r.counters.have_mem = 1;
        r.counters.free = 80;
        r.snapshot.at = 20;
        tc_health_sample(&w, &r, 20, 1);
        r.snapshot.at = 30;
        tc_health_sample(&w, &r, 30, 1); /* the second sample past the mark crosses */
        r = reading(50, 90);
        r.counters.have_mem = 1;
        r.counters.free = 60;
        r.snapshot.at = 40;
        tc_health_sample(&w, &r, 40, 1);
        assert(w.peak.procs == 85 && w.have_first_high && w.first_high.at == 30);
        assert(w.min_free == 30 && w.min_free_at == 10 && w.peak_files == 700);
        (void)json_of(&w, &token); /* built, but this heartbeat fails: nothing changes */
        r.snapshot.at = 50;
        tc_health_sample(&w, &r, 50, 1);
        assert(w.peak.procs == 85 && w.have_first_high && w.min_free == 30 && w.peak_files == 700);
        tc_health_delivered(&w, token);
        assert(w.peak.procs == 50 && w.peak.at == 50 && !w.have_first_high && !w.first_high.read);
        assert(w.min_free == 60 && w.min_free_at == 50 && w.peak_files == 100);
        assert(strstr(json_of(&w, &token), "\"procs_first_high\":null"));
    }
    /* Kernel messages through a sample. */
    {
        struct tc_health k;
        memset(&k, 0, sizeof(k));
        r = reading(40, 90);
        r.kmsg = "boot\n";
        r.kmsg_len = 5;
        tc_health_sample(&k, &r, 10, 1);
        r.kmsg = "boot\nproc: table is full\n";
        r.kmsg_len = (long)strlen(r.kmsg);
        tc_health_sample(&k, &r, 20, 1);
        assert(k.pending == TC_EV_KMSG);
        /* Unreadable: bytes -1, nothing scanned. */
        r.kmsg_len = -1;
        tc_health_sample(&k, &r, 30, 1);
        assert(k.kmsg.bytes == -1);
    }
    /* Files at 90 %, rearming below 80 %. */
    {
        struct tc_health f;
        memset(&f, 0, sizeof(f));
        r = reading(40, 90);
        r.counters.files = 900;
        tc_health_sample(&f, &r, 10, 1);
        tc_health_sample(&f, &r, 20, 1);
        assert(f.pending == TC_EV_FILES && f.peak_files == 900);
        r.counters.files = 800;
        tc_health_sample(&f, &r, 30, 1);
        assert(f.files.high);
        r.counters.files = 799;
        tc_health_sample(&f, &r, 40, 1);
        assert(!f.files.high);
    }
    /* fork, socket, acp-timeout and empty-acp counts that rose are news,
     * never a request. */
    {
        struct tc_health n;
        struct tc_acp_counts acp = {0, 0, 0};
        memset(&n, 0, sizeof(n));
        tc_health_note_failures(&n, &acp);
        (void)tc_errno_record(TC_OP_FORK, EAGAIN);
        (void)tc_errno_record(TC_OP_SOCKET, EMFILE);
        acp.timeouts = 1;
        tc_health_note_failures(&n, &acp);
        assert(n.news == (TC_EV_FORK | TC_EV_SOCKET | TC_EV_ACP_TIMEOUT) && !n.pending);
        n.news = 0;
        tc_health_note_failures(&n, &acp);
        assert(!n.news);
        /* An ACPd that answers every identity key with nothing. */
        acp.empty = 1;
        acp.budget_exhausted = 3; /* not news on its own */
        tc_health_note_failures(&n, &acp);
        assert(n.news == TC_EV_ACP_EMPTY && !n.pending);
        assert(strstr(json_of(&n, NULL), "\"news\":[\"acp_empty\"]"));
        n.news = 0;
        tc_health_note_failures(&n, &acp);
        assert(!n.news);
    }
}

/* The daemon's probe schedule on a simulated clock: each round is followed
 * by the next after tc_health_next_probe_delay, as main.c schedules it. */
static long rounds_until_trigger(struct tc_health *h, long fail_from, enum tc_probe_result failure, long *rounds) {
    struct tc_probe_round round;
    long now, next;
    size_t i;
    *rounds = 0;
    for (now = 0; now < 10L * TC_HEALTH_PROBE_SECONDS; now = next) {
        for (i = 0; i < TC_PROBES; i++) round.result[i] = TC_PROBE_OK;
        if (now >= fail_from) round.result[TC_PROBE_SSH4] = failure;
        tc_health_probes(h, &round, now);
        (*rounds)++;
        if (h->pending & TC_EV_PROBE) return now;
        next = now + tc_health_next_probe_delay(h);
    }
    return -1;
}

/* A week of the daemon's schedule with one target giving the same result
 * every round (the others answer): how many rounds ran and triggered. */
static long week_of(struct tc_health *h, size_t target, enum tc_probe_result result, unsigned *triggers) {
    struct tc_probe_round round;
    long now, rounds = 0;
    size_t i;
    *triggers = 0;
    for (now = 0; now < 7L * 86400; now += tc_health_next_probe_delay(h)) {
        for (i = 0; i < TC_PROBES; i++) round.result[i] = TC_PROBE_OK;
        round.result[target] = result;
        h->pending = 0;
        tc_health_probes(h, &round, now);
        if (h->pending & TC_EV_PROBE) (*triggers)++;
        rounds++;
    }
    return rounds;
}

static void probe_cadence(void) {
    struct tc_health h;
    struct tc_reading r = reading(40, 90);
    struct tc_probe_round round;
    long rounds;
    size_t i;
    /* A clean round waits the long interval. */
    memset(&h, 0, sizeof(h));
    tc_health_sample(&h, &r, 0, 1);
    for (i = 0; i < TC_PROBES; i++) round.result[i] = TC_PROBE_OK;
    tc_health_probes(&h, &round, 0);
    assert(tc_health_next_probe_delay(&h) == TC_HEALTH_PROBE_SECONDS);
    /* A counted failure below the trigger: the next round comes after the
     * retry delay, until the target answers ... */
    round.result[TC_PROBE_SMB6] = TC_PROBE_SILENT;
    tc_health_probes(&h, &round, 10);
    assert(tc_health_next_probe_delay(&h) == TC_HEALTH_PROBE_RETRY_SECONDS);
    round.result[TC_PROBE_SMB6] = TC_PROBE_OK;
    tc_health_probes(&h, &round, 20);
    assert(tc_health_next_probe_delay(&h) == TC_HEALTH_PROBE_SECONDS);
    /* ... or its probe can no longer run (nothing to confirm then) ... */
    round.result[TC_PROBE_SMB6] = TC_PROBE_SILENT;
    tc_health_probes(&h, &round, 30);
    round.result[TC_PROBE_SMB6] = TC_PROBE_SKIPPED;
    tc_health_probes(&h, &round, 31);
    assert(tc_health_next_probe_delay(&h) == TC_HEALTH_PROBE_SECONDS);
    /* ... or the episode is reported: then the long interval again. The
     * skip dropped the silent one's count, so two closed in a row (the
     * hard streak) trigger it. */
    round.result[TC_PROBE_SMB6] = TC_PROBE_CLOSED;
    tc_health_probes(&h, &round, 32);
    assert(h.probe[TC_PROBE_SMB6].phase == TC_PROBE_CONFIRMING && !(h.pending & TC_EV_PROBE));
    assert(tc_health_next_probe_delay(&h) == TC_HEALTH_PROBE_RETRY_SECONDS);
    tc_health_probes(&h, &round, 33);
    assert(h.probe[TC_PROBE_SMB6].phase == TC_PROBE_REPORTED && (h.pending & TC_EV_PROBE));
    assert(tc_health_next_probe_delay(&h) == TC_HEALTH_PROBE_SECONDS);
    round.result[TC_PROBE_SMB6] = TC_PROBE_OK;
    tc_health_probes(&h, &round, 34);
    /* A round that may not run (the table within 2 of the limit, or not
     * read) is retried soon too. */
    r = reading(88, 90);
    tc_health_sample(&h, &r, 40, 1);
    assert(tc_health_next_probe_delay(&h) == TC_HEALTH_PROBE_RETRY_SECONDS);
    r.snapshot.read = 0;
    tc_health_sample(&h, &r, 50, 1);
    assert(tc_health_next_probe_delay(&h) == TC_HEALTH_PROBE_RETRY_SECONDS);
    /* A whole round skipped near a full table ends a confirmation as a
     * skipped probe does, forgetting it; a reported episode stays. */
    memset(&h, 0, sizeof(h));
    r = reading(40, 90);
    tc_health_sample(&h, &r, 0, 1);
    for (i = 0; i < TC_PROBES; i++) round.result[i] = TC_PROBE_OK;
    tc_health_probes(&h, &round, 0);
    round.result[TC_PROBE_SSH4] = TC_PROBE_CLOSED;
    round.result[TC_PROBE_SMB4] = TC_PROBE_CLOSED;
    tc_health_probes(&h, &round, 10);
    round.result[TC_PROBE_SMB4] = TC_PROBE_OK;
    tc_health_probes(&h, &round, 20);
    assert(h.probe[TC_PROBE_SSH4].phase == TC_PROBE_REPORTED && h.probe[TC_PROBE_SMB4].phase == TC_PROBE_QUIET);
    round.result[TC_PROBE_SMB4] = TC_PROBE_CLOSED;
    tc_health_probes(&h, &round, 30);
    assert(h.probe[TC_PROBE_SMB4].phase == TC_PROBE_CONFIRMING);
    tc_health_probes_skipped(&h, 40);
    assert(h.probe_rounds_skipped == 1);
    assert(h.probe[TC_PROBE_SMB4].phase == TC_PROBE_QUIET && !h.probe[TC_PROBE_SMB4].streak);
    assert(h.probe[TC_PROBE_SSH4].phase == TC_PROBE_REPORTED && h.probe[TC_PROBE_SSH4].streak == 3);
    /* A day near the limit: rounds skipped every retry delay, no fork, and
     * the failure from before it does not join the next one. */
    for (i = 0; i < 288; i++) tc_health_probes_skipped(&h, 40 + (long)(i + 1) * TC_HEALTH_PROBE_RETRY_SECONDS);
    assert(h.probe_rounds_skipped == 289);
    round.result[TC_PROBE_SSH4] = TC_PROBE_OK;
    tc_health_probes(&h, &round, 90000);
    assert(h.probe[TC_PROBE_SMB4].phase == TC_PROBE_CONFIRMING && h.probe[TC_PROBE_SMB4].streak == 1);
    assert(h.probe[TC_PROBE_SSH4].phase == TC_PROBE_QUIET && !h.probe[TC_PROBE_SSH4].streak);
    /* Twelve hours of rounds skipped near a full table: every target's
     * result expires and goes out as none. */
    tc_health_probes(&h, &round, 100000);
    for (i = 1; i <= TC_HEALTH_PROBE_STALE_SECONDS / TC_HEALTH_PROBE_RETRY_SECONDS; i++)
        tc_health_probes_skipped(&h, 100000 + (long)i * TC_HEALTH_PROBE_RETRY_SECONDS);
    for (i = 0; i < TC_PROBES; i++) assert(h.probe[i].last == TC_PROBE_SKIPPED && !h.probe[i].probed_at);
    assert(strstr(json_of(&h, NULL), "\"ssh4\":{\"last\":\"none\""));
    /* sshd starts closing connections just after a round: the next long
     * interval finds it, the retry confirms it. */
    memset(&h, 0, sizeof(h));
    r = reading(40, 90);
    tc_health_sample(&h, &r, 0, 1);
    assert(rounds_until_trigger(&h, 1, TC_PROBE_CLOSED, &rounds) ==
               TC_HEALTH_PROBE_SECONDS + TC_HEALTH_PROBE_RETRY_SECONDS &&
           rounds == 3);
    /* Silent needs three in a row: two retries. */
    memset(&h, 0, sizeof(h));
    tc_health_sample(&h, &r, 0, 1);
    assert(rounds_until_trigger(&h, 1, TC_PROBE_SILENT, &rounds) ==
               TC_HEALTH_PROBE_SECONDS + 2 * TC_HEALTH_PROBE_RETRY_SECONDS &&
           rounds == 4);
    /* A week, every round failing the same way: a target that never
     * answered (an ::1 that errors) costs no extra rounds; a wedged sshd
     * that closes every connection triggers once and costs one extra; a
     * silent smbd two. The long interval alone is 112 rounds. */
    {
        unsigned triggers;
        const long base = (7L * 86400 + TC_HEALTH_PROBE_SECONDS - 1) / TC_HEALTH_PROBE_SECONDS;
        memset(&h, 0, sizeof(h));
        tc_health_sample(&h, &r, 0, 1);
        assert(week_of(&h, TC_PROBE_SMB6, TC_PROBE_DOWN, &triggers) == base && triggers == 0);
        assert(h.probe[TC_PROBE_SMB6].down == base && h.probe[TC_PROBE_SMB6].phase == TC_PROBE_QUIET);
        memset(&h, 0, sizeof(h));
        tc_health_sample(&h, &r, 0, 1);
        assert(week_of(&h, TC_PROBE_SSH4, TC_PROBE_CLOSED, &triggers) == base + 1 && triggers == 1);
        memset(&h, 0, sizeof(h));
        tc_health_sample(&h, &r, 0, 1);
        assert(week_of(&h, TC_PROBE_SMB4, TC_PROBE_SILENT, &triggers) == base + 2 && triggers == 1);
        assert(h.probe[TC_PROBE_SMB4].streak == h.probe[TC_PROBE_SMB4].silent);
    }
}

static void worst(void) {
    static struct tc_health h;
    struct tc_snapshot s;
    size_t i;
    unsigned token;
    const char *json;
    memset(&s, 0, sizeof(s));
    s.at = 2147483647L;
    s.read = 1;
    s.procs = s.system = s.zombies = s.blocked = 4294967295u;
    s.smbd_children = 2147483647;
    s.groups = TC_TOP_GROUPS;
    s.waits = TC_TOP_WAITS;
    s.largest = TC_TOP_LARGEST;
    s.most_fds = TC_TOP_FDS;
    for (i = 0; i < TC_TOP_GROUPS; i++) {
        memset(s.group[i].comm, '"', TC_PROC_COMM - 1);
        s.group[i].depth = s.group[i].count = 4294967295u;
    }
    for (i = 0; i < TC_TOP_WAITS; i++) {
        memset(s.wait[i].wmesg, '\\', TC_PROC_WMESG - 1);
        s.wait[i].count = 4294967295u;
    }
    for (i = 0; i < TC_TOP_LARGEST; i++) {
        memset(s.large[i].comm, 'x', TC_PROC_COMM - 1);
        s.large[i].rss_pages = 4294967295u;
    }
    for (i = 0; i < TC_TOP_FDS; i++) {
        memset(s.fd[i].comm, 'y', TC_PROC_COMM - 1);
        s.fd[i].fds = 4294967295u;
    }
    h.current = h.peak = h.first_high = s;
    h.now.maxproc = h.now.maxfiles = h.now.files = h.now.nmbclusters = 2147483647;
    h.now.have_mem = h.now.have_mbuf = 1;
    h.now.pagesize = 65536;
    h.now.free = h.now.active = h.now.inactive = h.now.freetarg = h.now.anon = h.now.file = h.now.fltnoram =
        h.min_free = 9223372036854775807LL;
    h.now.mbuf_drops = (unsigned long)-1;
    h.procs.events = h.files.events = 4294967295u;
    h.peak_files = 2147483647;
    h.min_free_at = 2147483647L;
    h.kmsg.bytes = 2147483647L;
    for (i = 0; i < TC_KMSG_PATTERNS; i++) {
        h.kmsg.seen[i].first = h.kmsg.seen[i].last = 2147483647L;
        h.kmsg.seen[i].n = 4294967295u;
        h.kmsg.seen[i].at_start = 1;
    }
    for (i = 0; i < TC_PROBES; i++) {
        h.probe[i].last = TC_PROBE_TIMEOUT; /* the longest name */
        h.probe[i].streak = h.probe[i].ok = h.probe[i].closed = h.probe[i].silent = h.probe[i].refused =
            h.probe[i].timeout = h.probe[i].down = 4294967295u;
        h.probe[i].first_fail_at = h.probe[i].last_fail_at = h.probe[i].probed_at = 2147483647L;
    }
    h.pending = h.news = (1u << TC_EV_COUNT) - 1;
    {
        struct tc_failure_counts most;
        memset(&most, 0xff, sizeof(most));
        tc_failure_counts_set(&most);
    }
    h.probe_rounds_skipped = 4294967295u;
    {
        /* Every counter at its largest: fork, socket and acp counts too. */
        static const struct tc_acp_counts acp = {4294967295u, 4294967295u, 4294967295u};
        static char out[TC_HEALTH_JSON_MAX + 2];
        int n = tc_health_append_json(out, sizeof(out), 0, &h, &acp, &token);
        assert(n > 0 && (size_t)n <= TC_HEALTH_JSON_MAX);
        json = out + 1;
        printf("%zu {%s}\n", strlen(json) + 1, json);
    }
}

int main(int argc, char **argv) {
    signal(SIGTERM, stop);
    signal(SIGPIPE, SIG_IGN); /* as the daemon: a probe may write to a closed socket */
    if (argc == 2 && !strcmp(argv[1], "detectors")) {
        thresholds();
        probe_tracks();
        connect_failures();
        kmsg();
        ledger_and_sample();
        probe_cadence();
        return 0;
    }
    if (argc == 2 && !strcmp(argv[1], "worst")) {
        worst();
        return 0;
    }
    if (argc == 2 && !strcmp(argv[1], "sample")) {
        static struct tc_health h;
        struct tc_reading r;
        tc_health_read_sample(&r, 42);
        tc_health_sample(&h, &r, 42, 1);
        printf("{%s}\n", json_of(&h, NULL));
        return 0;
    }
    if (argc == 3 && !strcmp(argv[1], "probes")) {
        int rounds = atoi(argv[2]), i, j;
        for (i = 0; i < rounds; i++) {
            struct tc_probe_round round;
            long long started = acp_monotonic_ms();
            tc_health_read_probes(&round);
            for (j = 0; j < TC_PROBES; j++) printf("%s%s=%s", j ? " " : "", tc_probe_names[j], tc_probe_result_names[round.result[j]]);
            printf(" elapsed_ms=%lld\n", acp_monotonic_ms() - started);
            fflush(stdout);
        }
        return 0;
    }
    return 2;
}
