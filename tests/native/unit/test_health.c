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
#include "../common/acp.h"
#include <assert.h>

volatile sig_atomic_t telemetry_stop;
static void stop(int sig) { (void)sig; telemetry_stop = 1; }

static const char *json_of(const struct tc_health *h, unsigned *token) {
    static char json[8192];
    unsigned t;
    int n = tc_health_append_json(json, sizeof(json), 0, h, 0, 0, token ? token : &t);
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
}

static void probe_tracks(void) {
    struct tc_probe_track p;
    memset(&p, 0, sizeof(p));
    /* Refused twice in a row while the service runs: one trigger. */
    assert(!tc_probe_track_step(&p, TC_PROBE_OK, 1, 10));
    assert(!tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 20) && p.first_fail_at == 20);
    assert(tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 30));
    assert(!tc_probe_track_step(&p, TC_PROBE_DOWN, 1, 40) && p.streak == 3 && p.last_fail_at == 40);
    /* Only an answer ends the episode; then it can trigger again. */
    assert(!tc_probe_track_step(&p, TC_PROBE_OK, 1, 50) && p.streak == 0 && !p.triggered);
    assert(!tc_probe_track_step(&p, TC_PROBE_DOWN, 1, 60) && tc_probe_track_step(&p, TC_PROBE_DOWN, 1, 70));
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
    assert(!tc_probe_track_step(&p, TC_PROBE_ABSENT, 0, 10) && !tc_probe_track_step(&p, TC_PROBE_ABSENT, 0, 20));
    assert(p.last == TC_PROBE_ABSENT && p.streak == 0 && !p.first_fail_at);
    assert(!tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 30) && !tc_probe_track_step(&p, TC_PROBE_ABSENT, 0, 40));
    assert(p.streak == 0 && !tc_probe_track_step(&p, TC_PROBE_CLOSED, 1, 50));
    /* ... but refused while it runs is down. */
    assert(tc_probe_track_step(&p, TC_PROBE_ABSENT, 1, 60) && p.last == TC_PROBE_DOWN);
    /* A probe that could not run here changes nothing. */
    {
        struct tc_probe_track before = p;
        assert(!tc_probe_track_step(&p, TC_PROBE_SKIPPED, 1, 70) && !memcmp(&before, &p, sizeof(p)));
    }
}

static unsigned scan(struct tc_kmsg_state *k, const char *text, long now) {
    return tc_kmsg_scan(k, text, strlen(text), now);
}

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
    struct tc_kmsg_state k;
    char ring[512], next[512];
    memset(&k, 0, sizeof(k));
    /* The first read is the starting point: what it holds may predate this
     * process. */
    assert(scan(&k, "boot\nproc: table is full\n", 5) == TC_EV_KMSG_AT_START);
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
    /* The ring wraps: the old text slides off the front; only what follows
     * the previous read's tail is new. */
    memset(&k, 0, sizeof(k));
    memset(ring, 'a', 300);
    strcpy(ring + 300, "proc: table is full\n");
    scan(&k, ring, 1);
    memcpy(next, ring + 100, 220);
    strcpy(next + 220, "zzzz\n");
    assert(scan(&k, next, 2) == 0 && !k.seen[0].n);
    /* Wrapped past the tail entirely between two reads: all of it is new. */
    memset(next, 'b', 300);
    strcpy(next + 300, "file: table is full\n");
    assert(scan(&k, next, 3) == TC_EV_KMSG && k.seen[1].n == 1);
    /* The known limit: a buffer of nothing but one repeated line matches
     * its own tail at the end, so a new copy goes unseen. */
    memset(&k, 0, sizeof(k));
    ring[0] = 0;
    while (strlen(ring) < 400) strcat(ring, "proc: table is full\n");
    scan(&k, ring, 1);
    /* One line dropped off the front, one new copy at the end: the same
     * text as before. */
    assert(scan(&k, ring, 2) == 0);
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
    r.snapshot.sshd_up = r.snapshot.smbd_up = 1;
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
    /* The JSON carries them; a probe failure noted while it is in flight is
     * not cleared by its delivery. */
    json_of(&h, &token);
    assert(token == TC_EV_PROCS);
    for (i = 0; i < TC_PROBES; i++) round.result[i] = TC_PROBE_CLOSED;
    tc_health_probes(&h, &round, 50);
    tc_health_probes(&h, &round, 60);
    assert(h.pending & TC_EV_PROBE);
    tc_health_delivered(&h, token);
    assert(h.pending == TC_EV_PROBE && h.news == TC_EV_PROBE);
    /* A refusal is a failure only from a target that answered before, of a
     * service whose listener runs. */
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
        /* IPv4 sshd, which answered, refused twice while sshd runs: down. */
        round.result[TC_PROBE_SSH4] = TC_PROBE_ABSENT;
        tc_health_probes(&off, &round, 40);
        tc_health_probes(&off, &round, 50);
        assert(off.probe[TC_PROBE_SSH4].last == TC_PROBE_DOWN && off.probe[TC_PROBE_SSH4].streak == 2);
        assert(off.pending == TC_EV_PROBE && off.probe[TC_PROBE_SSH4].triggered);
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
    }
    /* No probe round within 2 of kern.maxproc: the probe's own children
     * could take the last slots. */
    {
        struct tc_health full;
        memset(&full, 0, sizeof(full));
        assert(tc_health_probe_allowed(&full)); /* nothing sampled yet */
        r = reading(87, 90);
        tc_health_sample(&full, &r, 10, 1);
        assert(tc_health_probe_allowed(&full));
        r = reading(88, 90);
        tc_health_sample(&full, &r, 20, 1);
        assert(!tc_health_probe_allowed(&full));
        r = reading(89, 90);
        tc_health_sample(&full, &r, 30, 1);
        assert(!tc_health_probe_allowed(&full));
        r.snapshot.read = 0;
        tc_health_sample(&full, &r, 40, 1);
        assert(tc_health_probe_allowed(&full));
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
    /* fork, socket and acp-timeout counts that rose are news. */
    {
        struct tc_health n;
        memset(&n, 0, sizeof(n));
        tc_health_note_failures(&n, 0);
        (void)tc_errno_record(TC_OP_FORK, EAGAIN);
        (void)tc_errno_record(TC_OP_SOCKET, EMFILE);
        tc_health_note_failures(&n, 1);
        assert(n.news == (TC_EV_FORK | TC_EV_SOCKET | TC_EV_ACP_TIMEOUT) && !n.pending);
        n.news = 0;
        tc_health_note_failures(&n, 1);
        assert(!n.news);
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
        h.now.pdscans = h.min_free = 9223372036854775807LL;
    h.now.mbuf_drops = h.now.mbuf_waits = (unsigned long)-1;
    h.near_full = h.procs.events = h.files.events = 4294967295u;
    h.peak_files = 2147483647;
    h.peak_files_at = h.min_free_at = 2147483647L;
    h.kmsg.bytes = 2147483647L;
    for (i = 0; i < TC_KMSG_PATTERNS; i++) {
        h.kmsg.seen[i].first = h.kmsg.seen[i].last = 2147483647L;
        h.kmsg.seen[i].n = 4294967295u;
        h.kmsg.seen[i].at_start = 1;
    }
    for (i = 0; i < TC_PROBES; i++) {
        h.probe[i].last = TC_PROBE_ABSENT;
        h.probe[i].streak = h.probe[i].ok = h.probe[i].closed = h.probe[i].silent = h.probe[i].down = 4294967295u;
        h.probe[i].first_fail_at = h.probe[i].last_fail_at = 2147483647L;
    }
    h.pending = h.news = (1u << TC_EV_COUNT) - 1;
    for (i = 0; i < 1000; i++) {
        (void)tc_errno_record(TC_OP_FORK, EAGAIN);
        (void)tc_errno_record(TC_OP_SOCKET, EMFILE);
    }
    h.probe_rounds_skipped = 4294967295u;
    {
        /* Every counter at its largest, the acp counts too. */
        static char out[TC_HEALTH_JSON_MAX + 2];
        int n = tc_health_append_json(out, sizeof(out), 0, &h, 4294967295u, 4294967295u, &token);
        assert(n > 0 && (size_t)n <= TC_HEALTH_JSON_MAX);
        json = out + 1;
        printf("%zu {%s}\n", strlen(json) + 1, json);
    }
}

int main(int argc, char **argv) {
    signal(SIGTERM, stop);
    if (argc == 2 && !strcmp(argv[1], "detectors")) {
        thresholds();
        probe_tracks();
        connect_failures();
        kmsg();
        ledger_and_sample();
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
        static const char *const names[] = {"none", "ok", "closed", "silent", "down", "absent"};
        for (i = 0; i < rounds; i++) {
            struct tc_probe_round round;
            long long started = acp_monotonic_ms();
            tc_health_read_probes(&round);
            for (j = 0; j < TC_PROBES; j++) printf("%s%s=%s", j ? " " : "", tc_probe_names[j], names[round.result[j]]);
            printf(" elapsed_ms=%lld\n", acp_monotonic_ms() - started);
            fflush(stdout);
        }
        return 0;
    }
    return 2;
}
