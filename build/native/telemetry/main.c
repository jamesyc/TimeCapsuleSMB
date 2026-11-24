#include "telemetry.h"
#include "../common/acp.h"
#include "../common/parent.h"
#include "heartbeat_facts.h"
#include "planner.h"
#include "health.h"
#include "../common/report_deadline.h"
volatile sig_atomic_t telemetry_stop = 0;
static int parent_fd = -1;
/* Every decision this process keeps state for, in one place: one-shot
 * modes start from a cleared one. Static, not on the stack: the facts hold
 * two network plans. */
struct telemetry_daemon {
    struct tc_planner planner;
    struct tc_heartbeat_facts facts;
    struct tc_health health;
    time_t next_cleanup, next_sample, next_probe;
};

#ifndef TC_NATIVE_TEST
/* A one-shot heartbeat (the manager's buffer-stall --report) at its worst:
 * one acp collection, one probe round, the HTTP deadline, and 30 s for
 * everything else, inside the manager's limit. Host tests shorten the
 * manager's limit, so only device builds check it. */
typedef char tc_report_fits_manager_deadline[(TC_ACP_COLLECTION_BUDGET_SECONDS * 1000LL +
                                              TC_HEALTH_PROBE_ROUND_MAX_MS + TC_HTTP_DEADLINE_MS + 30000LL <=
                                              TC_BUFSTALL_REPORT_TIMEOUT_MS) ? 1 : -1];
#endif

/* One probe round, unless the table is too full for the probe's child. */
static void probe_round(struct telemetry_daemon *d, time_t now) {
    struct tc_probe_round round;
    if (tc_health_probe_allowed(&d->health)) {
        tc_health_read_probes(&round);
        tc_health_probes(&d->health, &round, (long)now);
    } else {
        tc_health_probes_skipped(&d->health);
    }
}

/* One sample and one probe round, for a one-shot heartbeat (not armed:
 * nothing it sees is a crossing). */
static void sample_once(struct telemetry_daemon *d, time_t now) {
    struct tc_reading reading;
    tc_health_read_sample(&reading, (long)now);
    tc_health_sample(&d->health, &reading, (long)now, 0);
    probe_round(d, now);
}
static struct telemetry_daemon daemon_state;
static int collect_cancelled(void) { return telemetry_stop || !tc_parent_alive(parent_fd); }
static void stop(int sig) { (void)sig; telemetry_stop = 1; acp_stop_requested = 1; }
/* Scheduling runs on the monotonic clock. NTP steps the wall clock after
 * boot; a backward step would otherwise postpone heartbeats by its size. */
static time_t monotonic_seconds(void) {
    long long ms = acp_monotonic_ms();
    return ms < 0 ? -1 : (time_t)(ms / 1000);
}

static void delivery_record(const struct tc_planner *p, struct tc_delivery *out) {
    out->failed = p->failed;
    out->since = p->failed_since;
    out->local_resource = p->failed_by[TC_OUTCOME_LOCAL_RESOURCE];
    out->acp_unavailable = p->failed_by[TC_OUTCOME_ACP_UNAVAILABLE];
    out->network = p->failed_by[TC_OUTCOME_NETWORK];
    out->server = p->failed_by[TC_OUTCOME_SERVER];
}

int tc_telemetry_main(int argc, char **argv) {
    int rc = 0, daemon = 0, cleanup_only = 0, report_only = 0;
    const char *reason = "manual";
    struct telemetry_daemon *d = &daemon_state;
    struct tc_delivery delivery;
    struct tc_payload_context ctx;
    enum tc_outcome outcome;
    memset(d, 0, sizeof(*d));
    memset(&delivery, 0, sizeof(delivery));
    memset(&ctx, 0, sizeof(ctx));
    ctx.facts = &d->facts;
    ctx.refresh = 1;
    ctx.delivery = &delivery;
    ctx.health = &d->health;
    signal(SIGTERM, stop); signal(SIGINT, stop); signal(SIGPIPE, SIG_IGN);
    if (argc == 2 && !strcmp(argv[1], "--version")) { puts(HEARTBEAT_AGENT_VERSION); return 0; }
    if ((argc == 2 || argc == 3) && !strcmp(argv[1], "--print-payload")) {
        char json[HEARTBEAT_MAX_JSON];
        struct tc_payload_result built;
        sample_once(d, monotonic_seconds());
        if (telemetry_payload(json, sizeof(json), argc == 3 ? argv[2] : reason, "", &ctx, &built)) return 1;
        fputs(json, stdout); return 0;
    }
    if (argc == 2 && !strcmp(argv[1], "--daemon")) daemon = 1;
    else if (argc == 2 && !strcmp(argv[1], "--cleanup")) cleanup_only = 1;
    else if ((argc == 2 || argc == 3) && !strcmp(argv[1], "--once")) reason = argc == 3 ? argv[2] : reason;
    else if ((argc == 2 || argc == 3) && !strcmp(argv[1], "--report")) {
        report_only = 1;
        reason = argc == 3 ? argv[2] : reason;
    }
    else {
        fputs("Usage: telemetry --daemon | --once [reason] | --report [reason] | --cleanup | --print-payload [reason] | --version\n", stderr);
        return 2;
    }
    /* Only the daemon plans; one-shot modes go at once. */
    planner_init(&d->planner, TC_ARM_AFTER_SECONDS, daemon ? (long)monotonic_seconds() + TC_BOOT_HOLD_SECONDS : 0);
    if (cleanup_only) {
        rc = telemetry_recover();
        if (rc == TC_EXIT_BUSY) fputs("telemetry: cleanup deferred; workspace is in use\n", stderr);
        return rc;
    }
    if (daemon || report_only) {
        parent_fd = tc_parent_pipe();
        if (parent_fd >= 0 && getpgrp() == getpid())
            acp_set_scope(1, collect_cancelled);
#if defined(__NetBSD__)
        if (daemon) setproctitle("role=telemetry --daemon");
#endif
    }
    if (report_only) {
        /* Reports neither use nor clean the debug workspace. Exit zero only
         * after a successful POST so the manager can acknowledge delivery. */
        if (!telemetry_enabled() || !tc_parent_alive(parent_fd)) return 1;
        sample_once(d, monotonic_seconds());
        return telemetry_cycle(reason, -1, &ctx, &outcome);
    }
    do {
        time_t now = monotonic_seconds();
        struct tc_attempt attempt;
        if (now < 0) {
            fputs("telemetry: cannot read monotonic clock\n", stderr);
            return 1;
        }
        /* Sampling is a few sysctls, no fork; a probe round at most
         * TC_HEALTH_PROBE_ROUND_MAX_MS. The first sample runs before any
         * heartbeat and touches the static buffers, so later reads need no
         * new pages. Both pause while a cycle runs. */
        if (daemon && now >= d->next_sample) {
            struct tc_reading reading;
            tc_health_read_sample(&reading, (long)now);
            tc_health_sample(&d->health, &reading, (long)now, planner_armed(&d->planner, (long)now));
            d->next_sample = now + TC_HEALTH_SAMPLE_SECONDS;
        }
        if (daemon && now >= d->next_probe) {
            probe_round(d, now);
            d->next_probe = monotonic_seconds() + TC_HEALTH_PROBE_SECONDS;
        }
        /* --once is one heartbeat with its own reason, read afresh. */
        if (daemon) {
            attempt = planner_next(&d->planner, (long)now, tc_health_requests(&d->health));
            if (attempt.reuse && tc_heartbeat_facts_fresh(&d->facts, acp_monotonic_ms())) attempt.refresh = 0;
        } else {
            attempt.kind = TC_ATTEMPT_SCHEDULED;
            attempt.reason = reason;
            attempt.refresh = 1;
            attempt.reuse = 0;
        }
        /* Recheck while idle so a running daemon observes a manual opt-out.
         * An active cycle still finishes its normal child/cleanup handling. */
        if (!telemetry_enabled() || !tc_parent_alive(parent_fd)) return 0;
        if (attempt.kind != TC_ATTEMPT_NONE || now >= d->next_cleanup) {
            int lock = telemetry_lock();
            if (lock < 0) {
                int lock_error = errno;
                rc = lock_error == EAGAIN || lock_error == EWOULDBLOCK ? TC_EXIT_BUSY : 1;
                if (rc != TC_EXIT_BUSY) {
                    telemetry_workspace_error("lock", TC_TELEMETRY_WORK_ROOT);
                }
                if (!daemon) return rc;
                if (attempt.kind != TC_ATTEMPT_NONE)
                    planner_finished(&d->planner, (long)now, &attempt, tc_setup_outcome(lock_error, 1),
                                     tc_health_requests(&d->health));
                d->next_cleanup = now + TC_CLEANUP_INTERVAL_SECONDS;
            } else {
                rc = telemetry_cleanup_locked();
                if (rc == 0 && attempt.kind != TC_ATTEMPT_NONE && !telemetry_stop) {
                    int cleanup_rc;
                    ctx.refresh = attempt.refresh;
                    delivery_record(&d->planner, &delivery);
                    if (!daemon) sample_once(d, now);
                    rc = telemetry_cycle(attempt.reason, lock, &ctx, &outcome);
                    if (outcome == TC_OUTCOME_DELIVERED && ctx.health_sent)
                        tc_health_delivered(&d->health, ctx.health_token);
                    if (daemon)
                        planner_finished(&d->planner, (long)monotonic_seconds(), &attempt, outcome,
                                         tc_health_requests(&d->health));
                    /* Closing this reference preserves an inherited child's
                     * lock. Never LOCK_UN a lock shared with a running job. */
                    close(lock);
                    cleanup_rc = telemetry_recover();
                    if (cleanup_rc == 1) rc = 1;
                    /* Busy means a surviving worker or another cycle owns the
                     * files now. Housekeeping retries without deleting them. */
                } else {
                    /* A workspace that cannot be cleaned holds the heartbeat
                     * back like a lock held elsewhere. */
                    if (daemon && rc != 0 && attempt.kind != TC_ATTEMPT_NONE)
                        planner_finished(&d->planner, (long)now, &attempt, TC_OUTCOME_BUSY,
                                         tc_health_requests(&d->health));
                    close(lock);
                }
                d->next_cleanup = monotonic_seconds() + TC_CLEANUP_INTERVAL_SECONDS;
            }
        }
        if (!daemon) return rc;
        /* An unreapable ACP child stops the shared reader; do not keep
         * scheduling collections a kernel cannot finish. */
        if (acp_stop_requested) telemetry_stop = 1;
        if (!telemetry_stop) sleep(1);
    } while (!telemetry_stop);
    return rc;
}
