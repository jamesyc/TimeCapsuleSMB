#include "health.h"
#include "heartbeat_facts.h"

const char *const tc_health_event_names[TC_EV_COUNT] = {
    "procs", "files", "kmsg", "probe", "kmsg_warning", "ram", "mbuf", "fork", "socket", "acp_timeout", "acp_empty"};
const char *const tc_probe_names[TC_PROBES] = {"ssh4", "smb4", "smb6"};
const char *const tc_probe_result_names[TC_PROBE_RESULTS] = {
    "none", "ok", "closed", "silent", "refused", "timeout", "down", "absent"};
const char *const tc_kmsg_names[TC_KMSG_PATTERNS] = {"proc_full", "file_full", "out_of_swap", "pool_limit"};
static const char *const kmsg_patterns[TC_KMSG_PATTERNS] = {
    "proc: table is full", "file: table is full", "out of swap", "limit reached"};
/* A full process or file table is the issue-429 signal and asks for a
 * heartbeat; swap and pool-limit warnings only ride along, since a pool
 * limit can be routine on a busy NetBSD 4 (mbuf clusters). */
static const unsigned kmsg_events[TC_KMSG_PATTERNS] = {TC_EV_KMSG, TC_EV_KMSG, TC_EV_KMSG_WARNING, TC_EV_KMSG_WARNING};

static void note(struct tc_health *h, unsigned bits) {
    h->news |= bits;
    if (h->armed) h->pending |= bits & TC_EV_REQUESTING;
}

/* ---- threshold ----
 *   below  -- a sample at or past high --> over (count 1)
 *   over   -- another one in a row --> count 2; once armed: high, crossing
 *          -- a sample below high --> below
 *   high   -- a sample at or below rearm --> below; anything else stays high
 * A sample that could not be read is no sample in a row: the caller
 * clears the count (tc_threshold_unread), and high stays as it was.
 * So a burst that ends between two samples asks nothing, and a table that
 * hovers near the limit asks once. Before arming (boot runs many short
 * processes against the stock kern.maxproc), the count saturates, so the
 * first armed sample still past the mark crosses at once. */
void tc_threshold_unread(struct tc_threshold *t) {
    t->over = 0;
}

int tc_threshold_step(struct tc_threshold *t, long value, long high, long rearm, int armed) {
    if (value >= high) {
        if (t->over < TC_HEALTH_HIGH_SAMPLES) t->over++;
    } else {
        t->over = 0;
    }
    if (!t->high && armed && t->over >= TC_HEALTH_HIGH_SAMPLES) {
        t->high = 1;
        t->events++;
        return 1;
    }
    if (t->high && value <= rearm) t->high = 0;
    return 0;
}

/* ---- probe_track ----
 * One target's results. absent becomes refused where the service is
 * expected to listen (tc_health_probes): this target answered before (a
 * daemon that listens on IPv4 only has never answered on IPv6), and, for
 * smbd, its listener runs. sshd is expected whether or not a listener is
 * left, so an sshd that died is a failure; the manager stops smbd on
 * purpose (no payload, an unmounted disk) and reports those itself.
 *
 * Counted failures: closed and silent always (the port accepted, so the
 * service is there: issue 429's sshd accepts and closes); refused, timeout
 * and down only from a target that answered before, since a target that
 * never answered (no ::1 route, a port that never listened) would
 * otherwise fail every round for good. The cost: a service already wedged
 * so that it refuses or never accepts when this process starts (telemetry
 * restarted mid-wedge) is never counted. Issue 429's wedge accepts and
 * closes, which counts from the first round, telemetry normally outlives a
 * wedge (seen_ok is set by then), and a full process table shows in the
 * procs detector anyway. A counted failure adds to streak;
 * all but silent add to hard_streak (slow is not refused).
 *
 * phase, which sets the probe cadence (tc_health_next_probe_delay):
 *   quiet       -- counted failure below the trigger --> confirming
 *               -- counted failure at the trigger --> reported, triggers
 *   confirming  -- counted failure below the trigger --> confirming
 *               -- counted failure at the trigger --> reported, triggers
 *               -- skipped (this target, or the whole round near a full
 *                  table), or an uncounted failure --> quiet, streaks 0:
 *                  nothing confirmed it, and a failure from long ago must
 *                  not join a later one to trigger (a table that full is
 *                  the procs detector's to report). So a service that
 *                  fails while the device is intermittently short of
 *                  sockets or buffers (probes alternating failure and
 *                  skipped) may never trigger: accepted, since those
 *                  shortages are counted as socket failures and go out as
 *                  news, and quick rounds stay bounded
 *   reported    -- any failure or skip --> reported (no new trigger)
 *   any         -- ok or absent --> quiet, streaks 0 (the episode ends)
 *               -- skipped with the latest real result
 *                  TC_HEALTH_PROBE_STALE_SECONDS old or more --> quiet, last
 *                  none, streaks 0: the result has expired, and a
 *                  heartbeat must not keep reporting it for days
 * first_fail_at is when the current or last episode's first counted
 * failure came, last_fail_at its latest: both stay after the episode ends,
 * so a heartbeat after a recovery still says when it failed, and the next
 * episode's first counted failure starts them again.
 * The trigger is hard_streak >= TC_HEALTH_PROBE_HARD_FAILURES (refused
 * twice: ACPd restarting sshd, or the manager restarting smbd, fails one
 * probe) or streak >= TC_HEALTH_PROBE_ANY_FAILURES (an smbd parent that
 * never accepts answers nothing). Each entry to confirming is followed by
 * at most two quick rounds before the episode is reported or dropped, so
 * a lasting failure costs a few extra rounds, not one every few minutes.
 * A table that stays near full skips rounds and so drops every
 * confirmation: after a counted failure the next round then waits the long
 * interval (tc_health_next_probe_delay), so such a device runs at most one
 * quick round per long interval, not one per retry delay. */
static int counts_as_failure(enum tc_probe_result r, int seen_ok) {
    if (r == TC_PROBE_CLOSED || r == TC_PROBE_SILENT) return 1;
    return seen_ok && (r == TC_PROBE_REFUSED || r == TC_PROBE_TIMEOUT || r == TC_PROBE_DOWN);
}

/* A confirmation that nothing confirmed ends, and is forgotten. */
static void drop_confirmation(struct tc_probe_track *p) {
    if (p->phase != TC_PROBE_CONFIRMING) return;
    p->phase = TC_PROBE_QUIET;
    p->streak = p->hard_streak = 0;
}

int tc_probe_track_step(struct tc_probe_track *p, enum tc_probe_result r, int expected, long now) {
    if (r == TC_PROBE_SKIPPED) {
        if (p->probed_at && now - p->probed_at >= TC_HEALTH_PROBE_STALE_SECONDS) {
            p->last = TC_PROBE_SKIPPED;
            p->phase = TC_PROBE_QUIET;
            p->streak = p->hard_streak = 0;
            p->probed_at = 0;
        }
        drop_confirmation(p);
        return 0;
    }
    p->probed_at = now > 0 ? now : 1;
    if (r == TC_PROBE_ABSENT && expected) r = TC_PROBE_REFUSED;
    p->last = r;
    if (r == TC_PROBE_OK || r == TC_PROBE_ABSENT) {
        if (r == TC_PROBE_OK) {
            p->ok++;
            p->seen_ok = 1;
        }
        p->streak = p->hard_streak = 0;
        p->phase = TC_PROBE_QUIET;
        return 0;
    }
    if (r == TC_PROBE_SILENT) p->silent++;
    else if (r == TC_PROBE_CLOSED) p->closed++;
    else if (r == TC_PROBE_REFUSED) p->refused++;
    else if (r == TC_PROBE_TIMEOUT) p->timeout++;
    else p->down++;
    if (!counts_as_failure(r, p->seen_ok)) {
        drop_confirmation(p);
        return 0;
    }
    if (!p->streak) p->first_fail_at = now > 0 ? now : 1;
    p->hard_streak = r == TC_PROBE_SILENT ? 0 : p->hard_streak + 1;
    p->streak++;
    p->last_fail_at = now;
    if (p->phase == TC_PROBE_REPORTED) return 0;
    if (p->hard_streak >= TC_HEALTH_PROBE_HARD_FAILURES || p->streak >= TC_HEALTH_PROBE_ANY_FAILURES) {
        p->phase = TC_PROBE_REPORTED;
        return 1;
    }
    p->phase = TC_PROBE_CONFIRMING;
    return 0;
}

/* ---- kmsg_scan ----
 *   no starting point -- first readable buffer --> baseline: patterns found
 *                        are "at start" (they may predate this process, even
 *                        the boot: the buffer can survive a warm restart),
 *                        no event
 *   baseline          -- each read: the ring drops its oldest bytes and
 *                        appends new ones, so a read begins with the end of
 *                        the previous one. The largest e >= TC_KMSG_OVERLAP
 *                        (or the whole previous read, if shorter) where the
 *                        first e bytes equal the previous read's last e
 *                        marks the old text; what follows is new. No such
 *                        e (the ring wrapped past the previous read between
 *                        samples): all of it is new.
 * Before the ring first wraps it only grows, and e is the whole previous
 * read at once. The scan starts (longest pattern - 1) bytes before the new
 * text and counts only matches that end inside it, so a line split across
 * two reads is caught once. Counts are close, not exact, at two edges. The
 * largest e hides new text that repeats the previous read's own end: in a
 * buffer flooded with one line, new copies of that same line are missed
 * (any other line after the flood is found). And a ring that wrapped so
 * far between reads that fewer than TC_KMSG_OVERLAP bytes overlap is read
 * as all new, so a match in that leftover old text is counted again. */
static const char *first_occurrence(const char *text, size_t from, size_t len, const char *needle, size_t n) {
    size_t i;
    for (i = from; i + n <= len; i++)
        if (text[i] == needle[0] && !memcmp(text + i, needle, n)) return text + i;
    return NULL;
}

/* The largest overlap, longest first; checking the first and last byte
 * before memcmp keeps a 16 KB buffer cheap on the device's CPU. */
static size_t old_part(const struct tc_kmsg_state *k, const char *text, size_t len) {
    size_t e = len < k->prev_len ? len : k->prev_len;
    size_t least = k->prev_len < TC_KMSG_OVERLAP ? k->prev_len : TC_KMSG_OVERLAP;
    const char *end = k->prev + k->prev_len;
    for (; e >= least && e > 0; e--)
        if (text[0] == end[-(long)e] && text[e - 1] == end[-1] && !memcmp(text, end - e, e)) return e;
    return 0;
}

unsigned tc_kmsg_scan(struct tc_kmsg_state *k, const char *text, size_t len, long now) {
    size_t i, start, from, longest = 0, plen;
    unsigned events = 0;
    const char *cursor;
    for (i = 0; i < TC_KMSG_PATTERNS; i++)
        if (strlen(kmsg_patterns[i]) > longest) longest = strlen(kmsg_patterns[i]);
    if (!k->baseline) {
        for (i = 0; i < TC_KMSG_PATTERNS; i++)
            if (first_occurrence(text, 0, len, kmsg_patterns[i], strlen(kmsg_patterns[i]))) k->seen[i].at_start = 1;
        k->baseline = 1;
    } else {
        start = old_part(k, text, len);
        from = start > longest - 1 ? start - (longest - 1) : 0;
        for (i = 0; i < TC_KMSG_PATTERNS; i++) {
            plen = strlen(kmsg_patterns[i]);
            for (cursor = first_occurrence(text, from, len, kmsg_patterns[i], plen); cursor;
                 cursor = first_occurrence(text, (size_t)(cursor - text) + plen, len, kmsg_patterns[i], plen)) {
                if ((size_t)(cursor - text) + plen <= start) continue; /* seen in an earlier read */
                if (!k->seen[i].first) k->seen[i].first = now > 0 ? now : 1;
                k->seen[i].last = now > 0 ? now : 1;
                k->seen[i].n++;
                events |= kmsg_events[i];
            }
        }
    }
    k->prev_len = len < sizeof(k->prev) ? len : sizeof(k->prev);
    memcpy(k->prev, text + len - k->prev_len, k->prev_len);
    return events;
}

/* ---- one sample ---- */
void tc_health_sample(struct tc_health *h, const struct tc_reading *r, long now, int armed) {
    const struct tc_counters *c = &r->counters;
    h->armed = armed;
    h->now = *c;
    h->current = r->snapshot;
    if (h->current.read && (!h->peak.read || h->current.procs > h->peak.procs)) h->peak = h->current;
    if (h->current.read && c->maxproc > 0) {
        long procs = (long)h->current.procs;
        if (tc_threshold_step(&h->procs, procs, c->maxproc - TC_HEALTH_PROC_HIGH_MARGIN,
                              c->maxproc - TC_HEALTH_PROC_REARM_MARGIN, armed)) {
            note(h, TC_EV_PROCS);
            if (!h->have_first_high) {
                h->first_high = h->current;
                h->have_first_high = 1;
            }
        }
    } else {
        tc_threshold_unread(&h->procs);
    }
    if (c->files > h->peak_files) h->peak_files = c->files;
    if (c->files >= 0 && c->maxfiles > 0) {
        /* 90 %, and rearmed below 80 %, in tenths so no rounding moves the
         * marks. */
        if (tc_threshold_step(&h->files, (long)c->files * 10, (long)c->maxfiles * 9, (long)c->maxfiles * 8 - 1, armed))
            note(h, TC_EV_FILES);
    } else {
        tc_threshold_unread(&h->files);
    }
    if (c->have_mem) {
        if (!h->have_min_free || c->free < h->min_free) {
            h->have_min_free = 1;
            h->min_free = c->free;
            h->min_free_at = now;
        }
        /* Faults that had to wait for a free page: memory really ran out. */
        if (h->have_last_mem && c->fltnoram > h->last_fltnoram) note(h, TC_EV_RAM);
        h->last_fltnoram = c->fltnoram;
        h->have_last_mem = 1;
    }
    if (c->have_mbuf) {
        if (h->have_last_mbuf && c->mbuf_drops > h->last_mbuf_drops) note(h, TC_EV_MBUF);
        h->last_mbuf_drops = c->mbuf_drops;
        h->have_last_mbuf = 1;
    }
    h->kmsg.bytes = r->kmsg_len;
    if (r->kmsg_len >= 0) note(h, tc_kmsg_scan(&h->kmsg, r->kmsg, (size_t)r->kmsg_len, now));
}

void tc_health_probes(struct tc_health *h, const struct tc_probe_round *round, long now) {
    size_t i;
    for (i = 0; i < TC_PROBES; i++) {
        struct tc_probe_track *p = &h->probe[i];
        int ssh = i == TC_PROBE_SSH4;
        /* probe_track says why sshd needs no listener (a round runs only
         * when the table was read, so smbd_up is current). */
        int expected = p->seen_ok && (ssh || h->current.smbd_up);
        if (tc_probe_track_step(p, round->result[i], expected, now)) note(h, TC_EV_PROBE);
    }
}

void tc_health_note_failures(struct tc_health *h, const struct tc_acp_counts *acp) {
    struct tc_failure_counts c;
    tc_failure_counts(&c);
    if (c.fork_eagain + c.fork_enomem + c.fork_other !=
        h->failures_seen.fork_eagain + h->failures_seen.fork_enomem + h->failures_seen.fork_other)
        note(h, TC_EV_FORK);
    if (c.socket_emfile + c.socket_enfile + c.socket_enobufs + c.socket_other !=
        h->failures_seen.socket_emfile + h->failures_seen.socket_enfile + h->failures_seen.socket_enobufs +
            h->failures_seen.socket_other)
        note(h, TC_EV_SOCKET);
    if (acp->timeouts != h->acp_timeouts_seen) note(h, TC_EV_ACP_TIMEOUT);
    if (acp->empty != h->acp_empty_seen) note(h, TC_EV_ACP_EMPTY);
    h->failures_seen = c;
    h->acp_timeouts_seen = acp->timeouts;
    h->acp_empty_seen = acp->empty;
}

int tc_health_probe_allowed(const struct tc_health *h) {
    return h->current.read && h->now.maxproc > 0 &&
           (long)h->current.procs + TC_HEALTH_PROC_FULL_MARGIN < h->now.maxproc;
}

long tc_health_next_probe_delay(const struct tc_health *h) {
    size_t i;
    for (i = 0; i < TC_PROBES; i++)
        if (h->probe[i].phase == TC_PROBE_CONFIRMING) return TC_HEALTH_PROBE_RETRY_SECONDS;
    if (tc_health_probe_allowed(h)) return TC_HEALTH_PROBE_SECONDS;
    /* A skipped round leaves last as it was (until it expires). */
    for (i = 0; i < TC_PROBES; i++)
        if (counts_as_failure(h->probe[i].last, h->probe[i].seen_ok)) return TC_HEALTH_PROBE_SECONDS;
    return TC_HEALTH_PROBE_RETRY_SECONDS;
}

void tc_health_probes_skipped(struct tc_health *h, long now) {
    size_t i;
    h->probe_rounds_skipped++;
    for (i = 0; i < TC_PROBES; i++) (void)tc_probe_track_step(&h->probe[i], TC_PROBE_SKIPPED, 0, now);
}

int tc_health_requests(const struct tc_health *h) {
    return h->pending != 0;
}

void tc_health_delivered(struct tc_health *h, unsigned token) {
    h->news &= ~token;
    h->pending &= ~token;
    h->peak = h->current;
    memset(&h->first_high, 0, sizeof(h->first_high));
    h->have_first_high = 0;
    h->peak_files = h->now.files;
    h->have_min_free = h->now.have_mem;
    h->min_free = h->now.free;
    h->min_free_at = h->current.at;
}
