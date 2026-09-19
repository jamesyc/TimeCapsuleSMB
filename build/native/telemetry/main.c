#include "telemetry.h"
#include "../common/acp.h"
#include "../common/ipc.h"
static void stop(int sig) { (void)sig; telemetry_stop = 1; acp_stop_requested = 1; }

int tc_telemetry_main(int argc, char **argv) {
    int rc = 0, daemon = 0, cleanup_only = 0;
    int control_fd = -1, mode_seen = 0, i;
    uint64_t supervisor_instance = 0, control_generation = 0;
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
    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--control-fd") && i + 1 < argc) {
            char *end;
            long parsed = strtol(argv[++i], &end, 10);
            if (!*argv[i] || *end || parsed < 3 || parsed > 1024) return 2;
            control_fd = (int)parsed;
        } else if (!mode_seen && !strcmp(argv[i], "--daemon")) { daemon = mode_seen = 1; }
        else if (!mode_seen && !strcmp(argv[i], "--cleanup")) { cleanup_only = mode_seen = 1; }
        else if (!mode_seen && !strcmp(argv[i], "--once")) {
            mode_seen = 1;
            if (i + 1 < argc && argv[i + 1][0] != '-') reason = argv[++i];
        } else {
            mode_seen = -1;
            break;
        }
    }
    if (mode_seen != 1) {
        fputs("Usage: telemetry --daemon | --once [reason] | --cleanup | --print-payload [reason] | --version\n", stderr);
        return 2;
    }
    if (control_fd >= 0 && tc_ipc_worker_handshake(
            control_fd, TC_ROLE_TELEMETRY, &supervisor_instance, &control_generation) != 0) return 2;
    if (control_fd >= 0) (void)tc_ipc_send(
        control_fd, TC_IPC_READY, TC_ROLE_TELEMETRY,
        supervisor_instance, control_generation, NULL, 0);
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
        /* An unreapable ACP child stops the shared reader; do not keep
         * scheduling collections a kernel cannot finish. */
        if (acp_stop_requested) telemetry_stop = 1;
        if (!telemetry_stop && control_fd >= 0) {
            fd_set reads;
            struct timeval timeout;
            FD_ZERO(&reads); FD_SET(control_fd, &reads);
            timeout.tv_sec = 1; timeout.tv_usec = 0;
            if (select(control_fd + 1, &reads, NULL, NULL, &timeout) > 0 && FD_ISSET(control_fd, &reads)) {
                struct tc_ipc_message message;
                int ipc_rc = tc_ipc_recv(control_fd, &message);
                if (ipc_rc <= 0 || message.instance != supervisor_instance ||
                    message.role != TC_ROLE_TELEMETRY || message.type == TC_IPC_STOP) telemetry_stop = 1;
                else if (message.type == TC_IPC_REFRESH && message.generation > control_generation)
                    control_generation = message.generation;
            }
        } else if (!telemetry_stop) sleep(1);
    } while (!telemetry_stop);
    if (control_fd >= 0) close(control_fd);
    return rc;
}

#ifndef TC_UNIFIED_SERVICE
int main(int argc, char **argv) { return tc_telemetry_main(argc, argv); }
#endif
