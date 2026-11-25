/* planner.c on a simulated clock: every transition in planner.h. */
#include "planner.h"
#include <assert.h>

static struct tc_attempt next(struct tc_planner *p, long now, int requests) {
    return planner_next(p, now, requests);
}

/* Whether the attempt reads acp, and whether it may keep fresh facts. */
static int reads(struct tc_attempt a) { return a.facts != TC_FACTS_KEEP; }
static int may_keep(struct tc_attempt a) { return a.facts == TC_FACTS_READ_UNLESS_FRESH; }

/* Finishes an attempt as the daemon does: it read acp when its policy said
 * to (the facts were never fresh enough to keep in these tests). */
static void finish(struct tc_planner *p, long now, const struct tc_attempt *a, enum tc_outcome outcome, int requests) {
    struct tc_attempt_result r;
    r.outcome = outcome;
    r.read_acp = a->facts != TC_FACTS_KEEP;
    planner_finished(p, now, a, &r, requests);
}

/* Runs the attempt due at now with the given outcome; returns its kind. */
static enum tc_attempt_kind run(struct tc_planner *p, long now, int requests, enum tc_outcome outcome) {
    struct tc_attempt a = planner_next(p, now, requests);
    if (a.kind != TC_ATTEMPT_NONE) finish(p, now, &a, outcome, requests);
    return a.kind;
}

static void boot_and_schedule(void) {
    struct tc_planner p;
    struct tc_attempt a;
    planner_init(&p, 900, 0);
    /* The first heartbeat is the boot one, due at once, read afresh. */
    a = next(&p, 100, 0);
    assert(a.kind == TC_ATTEMPT_BOOT && !strcmp(a.reason, "boot") && reads(a));
    /* An undelivered boot heartbeat retries with doubling delays and stays
     * a boot heartbeat. */
    assert(run(&p, 100, 0, TC_OUTCOME_NETWORK) == TC_ATTEMPT_BOOT);
    assert(!p.boot_sent && p.retry_at == 100 + TC_HEARTBEAT_RETRY_SECONDS);
    assert(next(&p, 100 + TC_HEARTBEAT_RETRY_SECONDS - 1, 0).kind == TC_ATTEMPT_NONE);
    assert(next(&p, 100 + TC_HEARTBEAT_RETRY_SECONDS, 0).kind == TC_ATTEMPT_BOOT);
    run(&p, 200, 0, TC_OUTCOME_SERVER);
    assert(p.retry_at == 200 + 2 * TC_HEARTBEAT_RETRY_SECONDS);
    run(&p, 400, 0, TC_OUTCOME_NETWORK);
    assert(p.retry_at == 400 + 4 * TC_HEARTBEAT_RETRY_SECONDS);
    /* The back-off stops at six hours, half the normal interval, so a
     * health event during a long outage waits at most that long. */
    for (int i = 0; i < 40; i++) {
        p.retry_at = 0;
        run(&p, 1000, 0, TC_OUTCOME_NETWORK);
    }
    assert(TC_PLANNER_NET_RETRY_MAX_SECONDS == 6 * 3600 && TC_PLANNER_NET_RETRY_MAX_SECONDS < TC_INTERVAL_SECONDS);
    assert(p.net_delay == TC_PLANNER_NET_RETRY_MAX_SECONDS && p.retry_at == 1000 + TC_PLANNER_NET_RETRY_MAX_SECONDS);
    assert(p.failed == 43 && p.failed_since == 100 && p.failed_by[TC_OUTCOME_NETWORK] == 42);
    /* Delivery marks boot sent, clears the back-off and the failure record,
     * and returns to the normal interval. */
    p.retry_at = 0;
    assert(run(&p, 5000, 0, TC_OUTCOME_DELIVERED) == TC_ATTEMPT_BOOT);
    assert(p.boot_sent && p.retry_at == 0 && p.net_delay == 0 && !p.failed && !p.failed_since);
    assert(next(&p, 5000 + TC_INTERVAL_SECONDS - 1, 0).kind == TC_ATTEMPT_NONE);
    a = next(&p, 5000 + TC_INTERVAL_SECONDS, 0);
    assert(a.kind == TC_ATTEMPT_SCHEDULED && !strcmp(a.reason, "scheduled") && reads(a));
    /* A later failed scheduled heartbeat starts the back-off from the base. */
    run(&p, 50000, 0, TC_OUTCOME_NETWORK);
    assert(p.boot_sent && p.retry_at == 50000 + TC_HEARTBEAT_RETRY_SECONDS);
}

static void local_failures(void) {
    struct tc_planner p;
    struct tc_attempt a;
    int i;
    planner_init(&p, 900, 0);
    run(&p, 0, 0, TC_OUTCOME_DELIVERED);
    /* A full process table: retry every minute, ten times, then every five
     * minutes, without reading acp afresh. */
    for (i = 1; i <= TC_PLANNER_LOCAL_RETRIES + 2; i++) {
        long now = TC_INTERVAL_SECONDS + i * 1000;
        a = next(&p, now, 0);
        assert(a.kind == TC_ATTEMPT_SCHEDULED && reads(a) == (i == 1));
        finish(&p, now, &a, TC_OUTCOME_LOCAL_RESOURCE, 0);
        assert(p.retry_at == now + (i <= TC_PLANNER_LOCAL_RETRIES ? TC_HEARTBEAT_RETRY_SECONDS
                                                                 : TC_PLANNER_LOCAL_LATER_SECONDS));
    }
    assert(p.failed_by[TC_OUTCOME_LOCAL_RESOURCE] == TC_PLANNER_LOCAL_RETRIES + 2 && p.net_delay == 0);
    /* A network failure ends the local run (and reads afresh again) without
     * borrowing its count; the next local failure starts at a minute. */
    run(&p, 100000, 0, TC_OUTCOME_NETWORK);
    assert(p.local_count == 0 && p.net_delay == TC_HEARTBEAT_RETRY_SECONDS && p.last_failure != TC_OUTCOME_LOCAL_RESOURCE);
    assert(reads(next(&p, 100000 + TC_HEARTBEAT_RETRY_SECONDS, 0)));
    run(&p, 100100, 0, TC_OUTCOME_LOCAL_RESOURCE);
    assert(p.retry_at == 100100 + TC_HEARTBEAT_RETRY_SECONDS && p.local_count == 1);
    /* ... and a local failure leaves the network delay where it was. */
    run(&p, 100200, 0, TC_OUTCOME_NETWORK);
    assert(p.net_delay == 2 * TC_HEARTBEAT_RETRY_SECONDS && p.local_count == 0);
}

static void busy_and_cancelled(void) {
    struct tc_planner p;
    planner_init(&p, 900, 0);
    run(&p, 10, 0, TC_OUTCOME_NETWORK);
    /* A held workspace lock retries soon and touches nothing else. */
    run(&p, 100, 0, TC_OUTCOME_BUSY);
    assert(p.retry_at == 100 + TC_CLEANUP_INTERVAL_SECONDS && p.net_delay == TC_HEARTBEAT_RETRY_SECONDS);
    assert(p.failed == 1);
    run(&p, 200, 0, TC_OUTCOME_NETWORK);
    assert(p.net_delay == 2 * TC_HEARTBEAT_RETRY_SECONDS);
    /* Cancelled: nothing changes. */
    {
        struct tc_planner before = p;
        struct tc_attempt a = {TC_ATTEMPT_BOOT, "boot", TC_FACTS_READ};
        finish(&p, 300, &a, TC_OUTCOME_CANCELLED, 0);
        assert(!memcmp(&before, &p, sizeof(p)));
    }
}

static void extra_heartbeats(void) {
    struct tc_planner p;
    struct tc_attempt a;
    long t;
    planner_init(&p, 900, 0);
    /* Nothing extra before the boot heartbeat is in: boot carries it. */
    a = next(&p, 10, 1);
    assert(a.kind == TC_ATTEMPT_BOOT);
    finish(&p, 10, &a, TC_OUTCOME_DELIVERED, 1);
    /* Requested: resource_high at once, never read afresh. */
    a = next(&p, 20, 1);
    assert(a.kind == TC_ATTEMPT_EXTRA && !strcmp(a.reason, "resource_high") && !reads(a));
    finish(&p, 20, &a, TC_OUTCOME_DELIVERED, 0);
    /* Any delivered heartbeat restarts the 12-hour schedule. */
    assert(p.next_scheduled == 20 + TC_INTERVAL_SECONDS);
    /* The next waits an hour, then 2, 4, 8 and at most 12. */
    t = 20;
    for (long spacing = TC_EXTRA_SPACING_SECONDS; spacing <= TC_EXTRA_SPACING_MAX_SECONDS; spacing *= 2) {
        assert(next(&p, t + spacing - 1, 1).kind == TC_ATTEMPT_NONE);
        t += spacing;
        assert(run(&p, t, 1, TC_OUTCOME_DELIVERED) == TC_ATTEMPT_EXTRA);
    }
    assert(p.spacing == TC_EXTRA_SPACING_MAX_SECONDS);
    t += TC_EXTRA_SPACING_MAX_SECONDS;
    assert(run(&p, t, 1, TC_OUTCOME_DELIVERED) == TC_ATTEMPT_EXTRA);
    assert(p.next_extra_at == t + TC_EXTRA_SPACING_MAX_SECONDS);
    /* A device that keeps retriggering never gets a quiet day: still 12 h. */
    assert(p.spacing == TC_EXTRA_SPACING_MAX_SECONDS);
    /* After a quiet day, a new episode starts again at an hour. */
    t += TC_EXTRA_QUIET_SECONDS + 5;
    assert(run(&p, t, 1, TC_OUTCOME_DELIVERED) == TC_ATTEMPT_EXTRA);
    assert(p.next_extra_at == t + TC_EXTRA_SPACING_SECONDS && p.spacing == 2 * TC_EXTRA_SPACING_SECONDS);
    /* While requested, a network back-off is capped at the spacing. */
    t += 2 * TC_EXTRA_SPACING_SECONDS;
    for (int i = 0; i < 20; i++) {
        p.retry_at = 0;
        run(&p, t, 1, TC_OUTCOME_NETWORK);
    }
    assert(p.net_delay == p.spacing);
    /* Spacing past six hours (8, then 12) still retries within six. */
    p.spacing = TC_EXTRA_SPACING_MAX_SECONDS;
    for (int i = 0; i < 5; i++) {
        p.retry_at = 0;
        run(&p, t, 1, TC_OUTCOME_NETWORK);
    }
    assert(p.net_delay == TC_PLANNER_NET_RETRY_MAX_SECONDS && p.retry_at == t + TC_PLANNER_NET_RETRY_MAX_SECONDS);
    p.spacing = 2 * TC_EXTRA_SPACING_SECONDS;
    p.net_delay = 0;
    for (int i = 0; i < 20; i++) {
        p.retry_at = 0;
        run(&p, t, 1, TC_OUTCOME_NETWORK);
    }
    assert(p.net_delay == p.spacing);
    /* A scheduled heartbeat due while requested goes as resource_high: one
     * attempt, which also counts as the scheduled one. */
    p.retry_at = 0;
    p.next_scheduled = t;
    a = next(&p, t, 1);
    /* It reads acp afresh only once acp has gone unread for the interval. */
    assert(a.kind == TC_ATTEMPT_EXTRA && reads(a) == (t >= p.next_refresh));
    p.next_refresh = t;
    assert(reads(next(&p, t, 1)));
    finish(&p, t, &a, TC_OUTCOME_DELIVERED, 0);
    assert(p.next_scheduled == t + TC_INTERVAL_SECONDS && next(&p, t + 1, 0).kind == TC_ATTEMPT_NONE);
}

static void chronic_requests_still_refresh(void) {
    /* A health event that never clears: forty days of extra heartbeats at
     * the 12-hour cap, every one delivered. acp is still read at least
     * every 12 hours. */
    struct tc_planner p;
    long t, last_refresh = 0;
    planner_init(&p, 900, 0);
    run(&p, 0, 1, TC_OUTCOME_DELIVERED);
    for (t = 1; t < 40L * 86400; t++) {
        struct tc_attempt a = next(&p, t, 1);
        if (a.kind == TC_ATTEMPT_NONE) continue;
        if (reads(a)) last_refresh = t;
        assert(t - last_refresh <= TC_INTERVAL_SECONDS);
        finish(&p, t, &a, TC_OUTCOME_DELIVERED, 1);
    }
    assert(last_refresh > 39L * 86400);
}

static void setup_failures(void) {
    /* Out of descriptors or memory before the heartbeat is built is the
     * device short of resources; anything else keeps its old meaning. */
    assert(tc_setup_outcome(ENFILE, 1) == TC_OUTCOME_LOCAL_RESOURCE);
    assert(tc_setup_outcome(EMFILE, 1) == TC_OUTCOME_LOCAL_RESOURCE);
    assert(tc_setup_outcome(ENOMEM, 0) == TC_OUTCOME_LOCAL_RESOURCE);
    assert(tc_setup_outcome(EMFILE, 0) == TC_OUTCOME_LOCAL_RESOURCE);
    assert(tc_setup_outcome(EAGAIN, 1) == TC_OUTCOME_BUSY);
    assert(tc_setup_outcome(EPERM, 1) == TC_OUTCOME_BUSY);
    assert(tc_setup_outcome(ENOENT, 0) == TC_OUTCOME_SERVER);
}

static void one_attempt_per_slot(void) {
    /* After a failure, one stream of attempts: never two in one slot, even
     * with a scheduled heartbeat overdue and an extra one requested. */
    struct tc_planner p;
    long t, attempts = 0, last = -1000;
    planner_init(&p, 900, 0);
    run(&p, 0, 0, TC_OUTCOME_DELIVERED);
    p.next_scheduled = 1000;
    for (t = 1000; t < 1000 + 30 * 60; t++) {
        struct tc_attempt a = next(&p, t, 1);
        if (a.kind == TC_ATTEMPT_NONE) continue;
        assert(t - last >= TC_HEARTBEAT_RETRY_SECONDS);
        last = t;
        attempts++;
        finish(&p, t, &a, TC_OUTCOME_LOCAL_RESOURCE, 1);
    }
    /* In 30 minutes: the first and ten retries a minute apart (to 1600 s),
     * then at 1900, 2200 and 2500. */
    assert(attempts == 14);
}

static void boot_hold(void) {
    /* The daemon starts at 40 and holds its boot heartbeat to 100. */
    struct tc_planner p;
    struct tc_attempt a;
    long t;
    planner_init(&p, 900, 100);
    for (t = 40; t < 100; t++) {
        /* A health request does not cut the hold short: the boot heartbeat
         * carries what it saw. */
        assert(next(&p, t, 0).kind == TC_ATTEMPT_NONE && next(&p, t, 1).kind == TC_ATTEMPT_NONE);
    }
    /* The hold counts as nothing: no failure, no back-off. */
    assert(!p.failed && !p.failed_since && !p.net_delay && !p.boot_sent);
    a = next(&p, 100, 1);
    assert(a.kind == TC_ATTEMPT_BOOT && reads(a));
    /* A failure after it backs off from the base, as without a hold. */
    finish(&p, 100, &a, TC_OUTCOME_NETWORK, 0);
    assert(p.retry_at == 100 + TC_HEARTBEAT_RETRY_SECONDS && p.failed == 1 && p.failed_since == 100);
    assert(run(&p, 100 + TC_HEARTBEAT_RETRY_SECONDS, 0, TC_OUTCOME_DELIVERED) == TC_ATTEMPT_BOOT && p.boot_sent);
    /* No hold: the boot heartbeat goes at once. */
    planner_init(&p, 900, 0);
    assert(next(&p, 0, 0).kind == TC_ATTEMPT_BOOT);
}

static void reuse_after_network_failures(void) {
    struct tc_planner p;
    struct tc_attempt a;
    planner_init(&p, 900, 0);
    /* The first attempt has nothing to reuse. */
    a = next(&p, 0, 0);
    assert(reads(a) && !may_keep(a));
    /* Network and server failures: acp answered, so the retry may reuse. */
    finish(&p, 0, &a, TC_OUTCOME_NETWORK, 0);
    a = next(&p, p.retry_at, 0);
    assert(a.kind == TC_ATTEMPT_BOOT && reads(a) && may_keep(a));
    finish(&p, p.retry_at, &a, TC_OUTCOME_SERVER, 0);
    a = next(&p, p.retry_at, 0);
    assert(may_keep(a));
    /* A held lock leaves the mark: the retry after it still may reuse. */
    finish(&p, p.retry_at, &a, TC_OUTCOME_BUSY, 0);
    assert(may_keep(next(&p, p.retry_at, 0)));
    /* No router ID (acp did not answer well enough) reads afresh. */
    a = next(&p, p.retry_at, 0);
    finish(&p, p.retry_at, &a, TC_OUTCOME_ACP_UNAVAILABLE, 0);
    a = next(&p, p.retry_at, 0);
    assert(reads(a) && !may_keep(a));
    /* A local failure forks nothing on its retry, reuse or not. */
    finish(&p, p.retry_at, &a, TC_OUTCOME_NETWORK, 0);
    a = next(&p, p.retry_at, 0);
    finish(&p, p.retry_at, &a, TC_OUTCOME_LOCAL_RESOURCE, 0);
    a = next(&p, p.retry_at, 0);
    assert(!reads(a) && !may_keep(a));
    /* Delivery clears it; the next scheduled heartbeat reads afresh. */
    finish(&p, 5000, &a, TC_OUTCOME_NETWORK, 0);
    a = next(&p, p.retry_at, 0);
    finish(&p, 6000, &a, TC_OUTCOME_DELIVERED, 0);
    a = next(&p, 6000 + TC_INTERVAL_SECONDS, 0);
    assert(a.kind == TC_ATTEMPT_SCHEDULED && reads(a) && !may_keep(a));
    /* Delivered on kept facts (the collection was fresh, so the attempt
     * read nothing): an extra heartbeat's next read counts from the last
     * real one, not this one. */
    p.next_refresh = 100;
    finish(&p, 6000 + TC_INTERVAL_SECONDS, &a, TC_OUTCOME_NETWORK, 0);
    a = next(&p, p.retry_at, 0);
    assert(may_keep(a));
    {
        struct tc_attempt_result kept = {TC_OUTCOME_DELIVERED, 0};
        planner_finished(&p, 7000 + TC_INTERVAL_SECONDS, &a, &kept, 0);
    }
    assert(p.next_refresh == 100 && p.last_failure == TC_OUTCOME_DELIVERED);
}

static void arming(void) {
    struct tc_planner p;
    planner_init(&p, 900, 0);
    assert(!planner_armed(&p, 899) && planner_armed(&p, 900));
    run(&p, 50, 0, TC_OUTCOME_DELIVERED);
    assert(planner_armed(&p, 51));
}

int main(void) {
    boot_and_schedule();
    local_failures();
    busy_and_cancelled();
    extra_heartbeats();
    one_attempt_per_slot();
    chronic_requests_still_refresh();
    setup_failures();
    boot_hold();
    reuse_after_network_failures();
    arming();
    return 0;
}
