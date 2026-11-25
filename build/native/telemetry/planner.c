#include "planner.h"

/* The rows of planner.h's facts table. */
static const struct tc_facts_reuse reuse_read = {0, 0};
static const struct tc_facts_reuse reuse_outage = {TC_FACTS_REUSE_SECONDS, 1};
static const struct tc_facts_reuse reuse_extra = {TC_INTERVAL_SECONDS, 0};
static const struct tc_facts_reuse reuse_any = {TC_FACTS_FOREVER, 0};

void planner_init(struct tc_planner *p, long arm_after, long hold_until) {
    memset(p, 0, sizeof(*p));
    p->retry_at = hold_until;
    p->spacing = TC_EXTRA_SPACING_SECONDS;
    p->last_extra_at = -1;
    p->arm_after = arm_after;
}

int planner_armed(const struct tc_planner *p, long now) {
    return p->boot_sent || now >= p->arm_after;
}

struct tc_attempt planner_next(const struct tc_planner *p, long now, int requests) {
    struct tc_attempt a = {TC_ATTEMPT_NONE, NULL, {0, 0}};
    int scheduled = !p->boot_sent || now >= p->next_scheduled;
    int extra = requests && p->boot_sent && now >= p->next_extra_at;
    if (now < p->retry_at || (!scheduled && !extra)) return a;
    a.kind = !p->boot_sent ? TC_ATTEMPT_BOOT : extra ? TC_ATTEMPT_EXTRA : TC_ATTEMPT_SCHEDULED;
    a.reason = a.kind == TC_ATTEMPT_BOOT ? "boot" : a.kind == TC_ATTEMPT_EXTRA ? "resource_high" : "scheduled";
    /* planner.h's facts table, row by row. */
    switch (p->last_failure) {
    case TC_OUTCOME_LOCAL_RESOURCE:
        a.facts = reuse_any;
        break;
    case TC_OUTCOME_ACP_UNAVAILABLE:
        a.facts = reuse_read;
        break;
    case TC_OUTCOME_NETWORK:
    case TC_OUTCOME_SERVER:
        a.facts = a.kind == TC_ATTEMPT_EXTRA ? reuse_extra : reuse_outage;
        break;
    default: /* none: delivered, or nothing sent yet */
        a.facts = a.kind == TC_ATTEMPT_EXTRA ? reuse_extra : reuse_read;
        break;
    }
    return a;
}

enum tc_outcome tc_setup_outcome(int error, int lock) {
    if (error == EMFILE || error == ENFILE || error == ENOMEM) return TC_OUTCOME_LOCAL_RESOURCE;
    return lock ? TC_OUTCOME_BUSY : TC_OUTCOME_SERVER;
}

/* One more counted failure in the delivery record. since is 0 for none, so
 * a failure at monotonic 0 records 1. */
static void count_failure(struct tc_delivery *d, enum tc_outcome outcome, long now) {
    d->failed++;
    if (!d->since) d->since = now > 0 ? now : 1;
    if (outcome == TC_OUTCOME_LOCAL_RESOURCE) d->local_resource++;
    else if (outcome == TC_OUTCOME_ACP_UNAVAILABLE) d->acp_unavailable++;
    else if (outcome == TC_OUTCOME_NETWORK) d->network++;
    else d->server++;
}

void planner_finished(struct tc_planner *p, long now, const struct tc_attempt *a, enum tc_outcome outcome,
                      int requests) {
    long cap;
    switch (outcome) {
    case TC_OUTCOME_DELIVERED:
        if (a->kind == TC_ATTEMPT_BOOT) p->boot_sent = 1;
        p->next_scheduled = now + TC_INTERVAL_SECONDS;
        p->retry_at = p->net_delay = 0;
        p->local_count = 0;
        p->last_failure = TC_OUTCOME_DELIVERED;
        memset(&p->delivery, 0, sizeof(p->delivery));
        if (a->kind == TC_ATTEMPT_EXTRA) {
            if (p->last_extra_at >= 0 && now - p->last_extra_at >= TC_EXTRA_QUIET_SECONDS)
                p->spacing = TC_EXTRA_SPACING_SECONDS;
            p->next_extra_at = now + p->spacing;
            p->spacing *= 2;
            if (p->spacing > TC_EXTRA_SPACING_MAX_SECONDS) p->spacing = TC_EXTRA_SPACING_MAX_SECONDS;
            p->last_extra_at = now;
        }
        return;
    case TC_OUTCOME_CANCELLED:
        return;
    case TC_OUTCOME_BUSY:
        p->retry_at = now + TC_CLEANUP_INTERVAL_SECONDS;
        return;
    default:
        break;
    }
    count_failure(&p->delivery, outcome, now);
    p->last_failure = outcome;
    if (outcome == TC_OUTCOME_LOCAL_RESOURCE) {
        p->local_count++;
        p->retry_at = now + (p->local_count <= TC_PLANNER_LOCAL_RETRIES ? TC_HEARTBEAT_RETRY_SECONDS
                                                                       : TC_PLANNER_LOCAL_LATER_SECONDS);
        return;
    }
    p->local_count = 0;
    cap = requests && p->spacing < TC_PLANNER_NET_RETRY_MAX_SECONDS ? p->spacing : TC_PLANNER_NET_RETRY_MAX_SECONDS;
    p->net_delay = p->net_delay ? p->net_delay * 2 : TC_HEARTBEAT_RETRY_SECONDS;
    if (p->net_delay > cap) p->net_delay = cap;
    p->retry_at = now + p->net_delay;
}
