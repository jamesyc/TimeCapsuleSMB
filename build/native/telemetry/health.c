#include "health.h"

const char *const tc_health_event_names[TC_EV_COUNT] = {
    "procs", "files", "kmsg", "probe", "kmsg_warning", "ram", "mbuf", "fork", "socket", "acp_timeout", "kmsg_at_start"};
const char *const tc_probe_names[TC_PROBES] = {"ssh4", "smb4", "smb6"};
const char *const tc_kmsg_names[TC_KMSG_PATTERNS] = {"proc_full", "file_full", "out_of_swap", "pool_limit"};
static const char *const kmsg_patterns[TC_KMSG_PATTERNS] = {
    "proc: table is full", "file: table is full", "out of swap", "limit reached"};
/* A full process or file table is the issue-429 signal and asks for a
 * heartbeat; swap and pool-limit warnings only ride along, since a pool
 * limit can be routine on a busy NetBSD 4 (mbuf clusters). */
static const unsigned kmsg_events[TC_KMSG_PATTERNS] = {TC_EV_KMSG, TC_EV_KMSG, TC_EV_KMSG_WARNING, TC_EV_KMSG_WARNING};

static void note(struct tc_health *h, unsigned bits) {
    h->news |= bits;
    h->pending |= bits & TC_EV_REQUESTING;
}

/* ---- threshold ----
 *   below  -- a sample at or past high --> over (count 1)
 *   over   -- another one in a row --> count 2; once armed: high, crossing
 *          -- a sample below high --> below
 *   high   -- a sample at or below rearm --> below; anything else stays high
 * So a burst that ends between two samples asks nothing, and a table that
 * hovers near the limit asks once. Before arming (boot runs many short
 * processes against the stock kern.maxproc), the count saturates, so the
 * first armed sample still past the mark crosses at once. */
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
 *   ok        -- ends any episode (streaks 0, ready to trigger again)
 *   closed,
 *   down      -- streak and hard_streak + 1
 *   silent    -- streak + 1, hard_streak 0 (slow is not refused)
 *   absent    -- expected: as down; not expected: the episode ends without
 *                a trigger. A refusal is expected to be a failure only when
 *                the service's listener is running and this target has
 *                answered before: smbd stopped on purpose is not running,
 *                and a daemon that listens on IPv4 only has never answered
 *                on IPv6. (In issue 429 sshd accepts and closes: closed and
 *                silent count whatever expected says.)
 *   skipped   -- nothing (the probe could not run here)
 * It triggers once per episode, at hard_streak >= 2 (refused twice: ACPd
 * restarting sshd, or the manager restarting smbd, fails one probe) or
 * streak >= 3 (an smbd parent that never accepts answers nothing). */
int tc_probe_track_step(struct tc_probe_track *p, enum tc_probe_result r, int expected, long now) {
    if (r == TC_PROBE_SKIPPED) return 0;
    if (r == TC_PROBE_ABSENT && expected) r = TC_PROBE_DOWN;
    p->last = r;
    if (r == TC_PROBE_OK || r == TC_PROBE_ABSENT) {
        if (r == TC_PROBE_OK) {
            p->ok++;
            p->seen_ok = 1;
        }
        p->streak = p->hard_streak = 0;
        p->triggered = 0;
        return 0;
    }
    if (r == TC_PROBE_SILENT) {
        p->silent++;
        p->hard_streak = 0;
    } else {
        if (r == TC_PROBE_CLOSED) p->closed++;
        else p->down++;
        p->hard_streak++;
    }
    p->streak++;
    if (!p->first_fail_at) p->first_fail_at = now > 0 ? now : 1;
    p->last_fail_at = now;
    if (!p->triggered &&
        (p->hard_streak >= TC_HEALTH_PROBE_HARD_FAILURES || p->streak >= TC_HEALTH_PROBE_ANY_FAILURES)) {
        p->triggered = 1;
        return 1;
    }
    return 0;
}

/* ---- kmsg_scan ----
 *   no starting point -- first readable buffer --> baseline: patterns found
 *                        are "at start" (they may predate this process, even
 *                        the boot: the buffer can survive a warm restart),
 *                        news but no request
 *   baseline          -- each read: the text after the last place the
 *                        previous read's last 128 bytes appear is new; if
 *                        they are gone (the ring wrapped past them between
 *                        samples), all of it is
 * The scan starts (longest pattern - 1) bytes before the new text and counts
 * only matches that end inside it, so a line split across two reads is
 * caught once. A buffer full of one repeated line also matches its tail at
 * the end, hiding new copies of that line: the process-table detector
 * covers that flood, and a second 16 KB copy of the buffer is not worth it. */
static const char *last_occurrence(const char *text, size_t len, const char *needle, size_t n) {
    size_t i;
    if (!n || n > len) return NULL;
    for (i = len - n + 1; i-- > 0;)
        if (text[i] == needle[0] && !memcmp(text + i, needle, n)) return text + i;
    return NULL;
}

static const char *first_occurrence(const char *text, size_t from, size_t len, const char *needle, size_t n) {
    size_t i;
    for (i = from; i + n <= len; i++)
        if (text[i] == needle[0] && !memcmp(text + i, needle, n)) return text + i;
    return NULL;
}

unsigned tc_kmsg_scan(struct tc_kmsg_state *k, const char *text, size_t len, long now) {
    size_t i, start = 0, from, longest = 0, plen;
    unsigned events = 0;
    const char *anchor, *cursor;
    for (i = 0; i < TC_KMSG_PATTERNS; i++)
        if (strlen(kmsg_patterns[i]) > longest) longest = strlen(kmsg_patterns[i]);
    if (!k->baseline) {
        for (i = 0; i < TC_KMSG_PATTERNS; i++)
            if (first_occurrence(text, 0, len, kmsg_patterns[i], strlen(kmsg_patterns[i]))) {
                k->seen[i].at_start = 1;
                events |= TC_EV_KMSG_AT_START;
            }
        k->baseline = 1;
    } else {
        anchor = last_occurrence(text, len, k->tail, k->tail_len);
        start = anchor ? (size_t)(anchor - text) + k->tail_len : 0;
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
    k->tail_len = len < TC_KMSG_TAIL ? len : TC_KMSG_TAIL;
    memcpy(k->tail, text + len - k->tail_len, k->tail_len);
    return events;
}

/* ---- one sample ---- */
void tc_health_sample(struct tc_health *h, const struct tc_reading *r, long now, int armed) {
    const struct tc_counters *c = &r->counters;
    h->now = *c;
    h->current = r->snapshot;
    if (h->current.read && (!h->peak.read || h->current.procs > h->peak.procs)) h->peak = h->current;
    if (h->current.read && c->maxproc > 0) {
        long procs = (long)h->current.procs;
        if (procs + TC_HEALTH_PROC_FULL_MARGIN >= c->maxproc) h->near_full++;
        if (tc_threshold_step(&h->procs, procs, c->maxproc - TC_HEALTH_PROC_HIGH_MARGIN,
                              c->maxproc - TC_HEALTH_PROC_REARM_MARGIN, armed)) {
            note(h, TC_EV_PROCS);
            if (!h->have_first_high) {
                h->first_high = h->current;
                h->have_first_high = 1;
            }
        }
    }
    if (c->files >= 0) {
        if (c->files > h->peak_files) {
            h->peak_files = c->files;
            h->peak_files_at = now;
        }
        /* 90 % and 80 %, in tenths so no rounding moves the marks. */
        if (c->maxfiles > 0 &&
            tc_threshold_step(&h->files, (long)c->files * 10, (long)c->maxfiles * 9, (long)c->maxfiles * 8 - 1, armed))
            note(h, TC_EV_FILES);
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
    h->sampled = 1;
}

void tc_health_probes(struct tc_health *h, const struct tc_probe_round *round, long now) {
    size_t i;
    for (i = 0; i < TC_PROBES; i++) {
        struct tc_probe_track *p = &h->probe[i];
        int ssh = i == TC_PROBE_SSH4;
        /* This target answered before, and (when the table was read) the
         * service's listener is running. */
        int expected = p->seen_ok && (!h->current.read || (ssh ? h->current.sshd_up : h->current.smbd_up));
        if (tc_probe_track_step(p, round->result[i], expected, now)) note(h, TC_EV_PROBE);
    }
    h->probed = 1;
}

void tc_health_note_failures(struct tc_health *h, unsigned acp_timeouts) {
    struct tc_failure_counts c;
    tc_failure_counts(&c);
    if (c.fork_eagain + c.fork_enomem + c.fork_other !=
        h->failures_seen.fork_eagain + h->failures_seen.fork_enomem + h->failures_seen.fork_other)
        note(h, TC_EV_FORK);
    if (c.socket_emfile + c.socket_enfile + c.socket_enobufs + c.socket_other !=
        h->failures_seen.socket_emfile + h->failures_seen.socket_enfile + h->failures_seen.socket_enobufs +
            h->failures_seen.socket_other)
        note(h, TC_EV_SOCKET);
    if (acp_timeouts != h->acp_timeouts_seen) note(h, TC_EV_ACP_TIMEOUT);
    h->failures_seen = c;
    h->acp_timeouts_seen = acp_timeouts;
}

int tc_health_probe_allowed(const struct tc_health *h) {
    return !(h->current.read && h->now.maxproc > 0 &&
             (long)h->current.procs + TC_HEALTH_PROC_FULL_MARGIN >= h->now.maxproc);
}

void tc_health_probes_skipped(struct tc_health *h) {
    h->probe_rounds_skipped++;
}

int tc_health_requests(const struct tc_health *h) {
    return h->pending != 0;
}

void tc_health_delivered(struct tc_health *h, unsigned token) {
    h->news &= ~token;
    h->pending &= ~token;
}
