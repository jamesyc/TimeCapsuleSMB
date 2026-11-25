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
    struct tc_heartbeat_ident ident; /* the pending heartbeat's id (telemetry.h) */
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
        tc_health_probes_skipped(&d->health, (long)now);
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

/* The workspace lock, or -1 with the exit status for the failure in *rc
 * (busy for another holder) and errno kept for tc_setup_outcome. */
static int take_lock(int *rc) {
    int lock = telemetry_lock(), error;
    if (lock >= 0) return lock;
    error = errno;
    *rc = error == EAGAIN || error == EWOULDBLOCK ? TC_EXIT_BUSY : 1;
    if (*rc != TC_EXIT_BUSY) telemetry_workspace_error("lock", TC_TELEMETRY_WORK_ROOT);
    errno = error;
    return -1;
}

/* Under the workspace lock, which it releases: housekeeping, then one
 * heartbeat for reason (NULL: housekeeping only). Returns the exit status;
 * *done says how the heartbeat ended (busy when the workspace could not be
 * cleaned, which holds the heartbeat back like a lock held elsewhere;
 * cancelled when the process is stopping). */
static int locked_cycle(struct tc_payload_context *ctx, int lock, const char *reason, enum tc_outcome *done) {
    int rc = telemetry_cleanup_locked();
    *done = rc ? TC_OUTCOME_BUSY : TC_OUTCOME_CANCELLED;
    if (rc || !reason || telemetry_stop) {
        close(lock);
        return rc;
    }
    rc = telemetry_cycle(reason, lock, ctx, done);
    /* Closing this reference preserves an inherited child's lock. Never
     * LOCK_UN a lock shared with a running job. */
    close(lock);
    /* Busy means a surviving worker or another cycle owns the files now.
     * Housekeeping retries without deleting them. */
    if (telemetry_recover() == 1) rc = 1;
    return rc;
}

/* --once: one heartbeat with its own reason, read afresh, after one sample
 * and probe round. */
static int run_once(struct telemetry_daemon *d, struct tc_payload_context *ctx, const char *reason) {
    enum tc_outcome done;
    int rc = 0, lock;
    if (!telemetry_enabled() || !tc_parent_alive(parent_fd)) return 0;
    lock = take_lock(&rc);
    if (lock < 0) return rc;
    sample_once(d, monotonic_seconds());
    memset(&ctx->reuse, 0, sizeof(ctx->reuse));
    return locked_cycle(ctx, lock, reason, &done);
}

/* A planned heartbeat attempt, from planner_next to here:
 *   planned   daemon_pass clears the attempt record (ctx->sent) and sets
 *             the facts it may keep (ctx->reuse);
 *   built     telemetry_payload fills the record in, under the workspace
 *             lock; an attempt that finds the lock held or gets no nonce
 *             never gets here and its record stays empty;
 *   finished  here, once, whatever ended it.
 * Finishing is the only transition out: a delivered heartbeat's health
 * events are marked reported (only those its payload carried; events
 * noted since stay pending) and its id is done, so the next heartbeat
 * takes a new one; then the planner learns the outcome (planner.h), which
 * also keeps the delivery record the next payload sends. Any other outcome
 * leaves the events and the id for the retry. */
static void heartbeat_finished(struct telemetry_daemon *d, const struct tc_payload_context *ctx,
                               const struct tc_attempt *attempt, enum tc_outcome outcome) {
    if (outcome == TC_OUTCOME_DELIVERED) {
        if (ctx->sent.health) tc_health_delivered(&d->health, ctx->sent.health_token);
        tc_heartbeat_ident_clear(&d->ident);
    }
    planner_finished(&d->planner, (long)monotonic_seconds(), attempt, outcome, tc_health_requests(&d->health));
}

/* One pass of the daemon at now, each step only when it is due: a sample
 * (a few sysctls, no fork), a probe round (at most
 * TC_HEALTH_PROBE_ROUND_MAX_MS), then the heartbeat the planner allows or,
 * with none, housekeeping. Sampling pauses while a heartbeat runs. The
 * first sample comes before any heartbeat and touches the static buffers,
 * so later reads need no new pages. Returns the exit status of the last
 * locked step (rc when this pass took none). */
static int daemon_pass(struct telemetry_daemon *d, struct tc_payload_context *ctx, time_t now, int rc) {
    struct tc_attempt attempt;
    enum tc_outcome done;
    int lock;
    if (now >= d->next_sample) {
        struct tc_reading reading;
        tc_health_read_sample(&reading, (long)now);
        tc_health_sample(&d->health, &reading, (long)now, planner_armed(&d->planner, (long)now));
        d->next_sample = now + TC_HEALTH_SAMPLE_SECONDS;
    }
    if (now >= d->next_probe) {
        probe_round(d, now);
        d->next_probe = monotonic_seconds() + tc_health_next_probe_delay(&d->health);
    }
    attempt = planner_next(&d->planner, (long)now, tc_health_requests(&d->health));
    if (attempt.kind == TC_ATTEMPT_NONE && now < d->next_cleanup) return rc;
    memset(&ctx->sent, 0, sizeof(ctx->sent));
    ctx->reuse = attempt.facts;
    lock = take_lock(&rc);
    if (lock < 0)
        done = tc_setup_outcome(errno, 1);
    else
        rc = locked_cycle(ctx, lock, attempt.kind != TC_ATTEMPT_NONE ? attempt.reason : NULL, &done);
    if (attempt.kind != TC_ATTEMPT_NONE) heartbeat_finished(d, ctx, &attempt, done);
    d->next_cleanup = monotonic_seconds() + TC_CLEANUP_INTERVAL_SECONDS;
    return rc;
}

/* --daemon: a pass every second until TERM, opt-out or the parent's exit.
 * The boot heartbeat and the first probe round wait out the same hold
 * (planner.h): at daemon start smbd is often not listening yet, and a pass
 * probes before it plans, so the boot heartbeat carries that round. */
static int run_daemon(struct telemetry_daemon *d, struct tc_payload_context *ctx) {
    long hold_until = (long)monotonic_seconds() + TC_BOOT_HOLD_SECONDS;
    int rc = 0;
    ctx->ident = &d->ident;
    ctx->delivery = &d->planner.delivery;
    planner_init(&d->planner, TC_ARM_AFTER_SECONDS, hold_until);
    d->next_probe = (time_t)hold_until;
    while (!telemetry_stop) {
        time_t now = monotonic_seconds();
        if (now < 0) {
            fputs("telemetry: cannot read monotonic clock\n", stderr);
            return 1;
        }
        /* Recheck while idle so a running daemon observes a manual opt-out.
         * An active cycle still finishes its normal child/cleanup handling. */
        if (!telemetry_enabled() || !tc_parent_alive(parent_fd)) return 0;
        rc = daemon_pass(d, ctx, now, rc);
        /* An unreapable ACP child stops the shared reader; do not keep
         * scheduling collections a kernel cannot finish. */
        if (acp_stop_requested) telemetry_stop = 1;
        if (!telemetry_stop) sleep(1);
    }
    return rc;
}

int tc_telemetry_main(int argc, char **argv) {
    int rc, daemon = 0, cleanup_only = 0, report_only = 0;
    const char *reason = "manual";
    struct telemetry_daemon *d = &daemon_state;
    struct tc_payload_context ctx;
    enum tc_outcome outcome;
    memset(d, 0, sizeof(*d));
    memset(&ctx, 0, sizeof(ctx));
    /* One-shot modes: reuse zeroed reads afresh, and no delivery record or
     * pending id (run_daemon sets both). */
    ctx.facts = &d->facts;
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
    else if (argc >= 2 && argc <= 4 && !strcmp(argv[1], "--report")) {
        /* The manager's token names the report: its retries, each a new
         * process, send the same heartbeat id, under a changed reason too. */
        report_only = 1;
        reason = argc >= 3 ? argv[2] : reason;
        ctx.report_token = argc == 4 ? argv[3] : NULL;
    }
    else {
        fputs("Usage: telemetry --daemon | --once [reason] | --report [reason [token]] | --cleanup | --print-payload [reason] | --version\n", stderr);
        return 2;
    }
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
    return daemon ? run_daemon(d, &ctx) : run_once(d, &ctx, reason);
}
