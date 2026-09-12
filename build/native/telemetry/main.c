#include "telemetry.h"
volatile sig_atomic_t telemetry_stop = 0;
static void stop(int sig) { (void)sig; telemetry_stop = 1; }

int main(int argc, char **argv) {
    int rc = 0, daemon = 0, cleanup_only = 0;
    const char *reason = "manual";
    struct telemetry_schedule schedule;
    time_t next_cleanup = 0, retry_after = 0;
    memset(&schedule, 0, sizeof(schedule));
    signal(SIGTERM, stop); signal(SIGINT, stop); signal(SIGPIPE, SIG_IGN);
    if (argc == 2 && !strcmp(argv[1], "--version")) { puts(HEARTBEAT_AGENT_VERSION); return 0; }
    if ((argc == 2 || argc == 3) && !strcmp(argv[1], "--print-payload")) {
        char json[HEARTBEAT_MAX_JSON];
        if (telemetry_payload(json, sizeof(json), argc == 3 ? argv[2] : reason, "")) return 1;
        fputs(json, stdout); return 0;
    }
    if (argc == 2 && !strcmp(argv[1], "--daemon")) daemon = 1;
    else if (argc == 2 && !strcmp(argv[1], "--cleanup")) cleanup_only = 1;
    else if ((argc == 2 || argc == 3) && !strcmp(argv[1], "--once")) reason = argc == 3 ? argv[2] : reason;
    else {
        fputs("Usage: telemetry --daemon | --once [reason] | --cleanup | --print-payload [reason] | --version\n", stderr);
        return 2;
    }
    if (cleanup_only) {
        rc = telemetry_recover();
        if (rc == TC_EXIT_BUSY) fputs("telemetry: cleanup deferred; workspace is in use\n", stderr);
        return rc;
    }
    do {
        time_t now = time(NULL);
        int due = !daemon || telemetry_schedule_due(&schedule, now);
        /* Recheck while idle so a running daemon observes a manual opt-out.
         * An active cycle still finishes its normal child/cleanup handling. */
        if (!telemetry_enabled()) return 0;
        if (now >= retry_after && (due || now >= next_cleanup)) {
            int lock = telemetry_lock();
            if (lock < 0) {
                rc = errno == EAGAIN || errno == EWOULDBLOCK ? TC_EXIT_BUSY : 1;
                if (rc != TC_EXIT_BUSY) {
                    telemetry_workspace_error("lock", TC_TELEMETRY_WORK_ROOT);
                }
                if (!daemon) return rc;
                retry_after = now + TC_CLEANUP_INTERVAL_SECONDS;
            } else {
                rc = telemetry_cleanup_locked();
                if (rc == 0 && due && !telemetry_stop) {
                    const char *cycle_reason = daemon ? (schedule.boot_sent ? "scheduled" : "boot") : reason;
                    int cleanup_rc;
                    telemetry_schedule_started(&schedule, now);
                    rc = telemetry_cycle(cycle_reason, lock);
                    /* Closing this reference preserves an inherited child's
                     * lock. Never LOCK_UN a lock shared with a running job. */
                    close(lock);
                    cleanup_rc = telemetry_recover();
                    if (cleanup_rc == 1) rc = 1;
                    /* Busy means a surviving worker or another cycle owns the
                     * files now. Housekeeping retries without deleting them. */
                } else {
                    close(lock);
                }
                next_cleanup = time(NULL) + TC_CLEANUP_INTERVAL_SECONDS;
                retry_after = rc ? next_cleanup : 0;
            }
        }
        if (!daemon) return rc;
        if (!telemetry_stop) sleep(1);
    } while (!telemetry_stop);
    return rc;
}
