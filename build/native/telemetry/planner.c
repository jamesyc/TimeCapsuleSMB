#include "planner.h"

void planner_init(struct tc_planner *p, long arm_after) {
    memset(p, 0, sizeof(*p));
    p->spacing = TC_EXTRA_SPACING_SECONDS;
    p->last_extra_at = -1;
    p->arm_after = arm_after;
}

int planner_armed(const struct tc_planner *p, long now) {
    return p->boot_sent || now >= p->arm_after;
}

struct tc_attempt planner_next(const struct tc_planner *p, long now, int requests) {
    struct tc_attempt a = {TC_ATTEMPT_NONE, NULL, 0};
    int scheduled = !p->boot_sent || now >= p->next_scheduled;
    int extra = requests && p->boot_sent && now >= p->next_extra_at;
    if (now < p->retry_at || (!scheduled && !extra)) return a;
    a.kind = !p->boot_sent ? TC_ATTEMPT_BOOT : extra ? TC_ATTEMPT_EXTRA : TC_ATTEMPT_SCHEDULED;
    a.reason = a.kind == TC_ATTEMPT_BOOT ? "boot" : a.kind == TC_ATTEMPT_EXTRA ? "resource_high" : "scheduled";
    /* An extra heartbeat must not fork, unless acp has gone unread for
     * TC_INTERVAL_SECONDS: every delivered heartbeat restarts the schedule,
     * so a chronic health event's extras would otherwise keep the boot-time
     * identity and plan forever. A retry after a local failure never forks. */
    a.refresh = (a.kind != TC_ATTEMPT_EXTRA || now >= p->next_refresh) && !p->last_local;
    return a;
}

enum tc_outcome tc_setup_outcome(int error, int lock) {
    if (error == EMFILE || error == ENFILE || error == ENOMEM) return TC_OUTCOME_LOCAL_RESOURCE;
    return lock ? TC_OUTCOME_BUSY : TC_OUTCOME_SERVER;
}

void planner_finished(struct tc_planner *p, long now, const struct tc_attempt *a, enum tc_outcome outcome,
                      int requests) {
    long cap;
    switch (outcome) {
    case TC_OUTCOME_DELIVERED:
        if (a->kind == TC_ATTEMPT_BOOT) p->boot_sent = 1;
        p->next_scheduled = now + TC_INTERVAL_SECONDS;
        if (a->refresh) p->next_refresh = now + TC_INTERVAL_SECONDS;
        p->retry_at = p->net_delay = 0;
        p->local_count = 0;
        p->last_local = 0;
        p->failed = 0;
        p->failed_since = 0;
        memset(p->failed_by, 0, sizeof(p->failed_by));
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
    p->failed++;
    p->failed_by[outcome]++;
    if (!p->failed_since) p->failed_since = now > 0 ? now : 1;
    if (outcome == TC_OUTCOME_LOCAL_RESOURCE) {
        p->local_count++;
        p->last_local = 1;
        p->retry_at = now + (p->local_count <= TC_PLANNER_LOCAL_RETRIES ? TC_HEARTBEAT_RETRY_SECONDS
                                                                       : TC_PLANNER_LOCAL_LATER_SECONDS);
        return;
    }
    p->local_count = 0;
    p->last_local = 0;
    cap = requests ? p->spacing : TC_INTERVAL_SECONDS;
    p->net_delay = p->net_delay ? p->net_delay * 2 : TC_HEARTBEAT_RETRY_SECONDS;
    if (p->net_delay > cap) p->net_delay = cap;
    p->retry_at = now + p->net_delay;
}
