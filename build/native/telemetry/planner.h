#ifndef TC_TELEMETRY_PLANNER_H
#define TC_TELEMETRY_PLANNER_H
#include "telemetry.h"

/* When the daemon sends a heartbeat, and why: one machine for the boot
 * heartbeat, the scheduled one every TC_INTERVAL_SECONDS, and the extra
 * "resource_high" one that health events request. Times are seconds on the
 * monotonic clock (NetBSD's starts at boot). Pure: tests drive it with their
 * own clock.
 *
 * A heartbeat is wanted while the boot one is not delivered, once the next
 * scheduled one is due, or (after boot) while health requests one and the
 * extra spacing has passed. It is allowed once retry_at has passed. Its
 * reason is "boot" until the boot heartbeat is in, then "resource_high" when
 * the extra one is wanted, else "scheduled". Only boot and scheduled
 * heartbeats read acp afresh, and not right after a local failure.
 *
 * After an attempt:
 *   delivered        clear every retry and the failure record; the next
 *                    scheduled heartbeat is TC_INTERVAL_SECONDS away, after
 *                    any heartbeat (the server hears from the device at
 *                    least that often). After resource_high: the spacing
 *                    goes back to TC_EXTRA_SPACING_SECONDS when a full
 *                    TC_EXTRA_QUIET_SECONDS passed since the last one; the
 *                    next waits the spacing, which then doubles up to
 *                    TC_EXTRA_SPACING_MAX_SECONDS.
 *   cancelled        nothing changes (the process is stopping)
 *   busy             retry after TC_CLEANUP_INTERVAL_SECONDS (workspace lock)
 *   local_resource   the device is short of processes, descriptors or
 *                    memory, which free up within minutes: retry after
 *                    TC_HEARTBEAT_RETRY_SECONDS, TC_PLANNER_LOCAL_RETRIES
 *                    times in a row, then every TC_PLANNER_LOCAL_LATER_SECONDS
 *   acp_unavailable,
 *   network, server  back off: TC_HEARTBEAT_RETRY_SECONDS doubling, at most
 *                    the extra spacing while health requests a heartbeat,
 *                    else TC_INTERVAL_SECONDS
 * The two back-offs keep their own counters: a local failure does not reset
 * the network delay, any other failure ends the local run. Every failure but
 * busy and cancelled is counted, by kind, until a heartbeat is delivered;
 * the heartbeat carries the count ("delivery"). Health crossings count only
 * once armed: after the boot heartbeat, or TC_ARM_AFTER_SECONDS of uptime
 * when the server cannot be reached. */
#define TC_PLANNER_LOCAL_RETRIES 10
#define TC_PLANNER_LOCAL_LATER_SECONDS 300
#ifndef TC_EXTRA_SPACING_SECONDS
#define TC_EXTRA_SPACING_SECONDS 3600
#endif
#define TC_EXTRA_SPACING_MAX_SECONDS 43200
#define TC_EXTRA_QUIET_SECONDS 86400
#ifndef TC_ARM_AFTER_SECONDS
#define TC_ARM_AFTER_SECONDS 900
#endif

enum tc_attempt_kind { TC_ATTEMPT_NONE, TC_ATTEMPT_BOOT, TC_ATTEMPT_SCHEDULED, TC_ATTEMPT_EXTRA };
struct tc_attempt {
    enum tc_attempt_kind kind;
    const char *reason;
    int refresh; /* read acp afresh */
};

#define TC_OUTCOME_COUNT (TC_OUTCOME_SERVER + 1)
struct tc_planner {
    int boot_sent, last_local;
    long next_scheduled, retry_at, net_delay;
    unsigned local_count;
    long spacing, next_extra_at, last_extra_at; /* last_extra_at < 0: none yet */
    long arm_after;
    unsigned failed;                    /* failed attempts since the last delivered one */
    long failed_since;                  /* when the first of them failed, or 0 */
    unsigned failed_by[TC_OUTCOME_COUNT];
};

void planner_init(struct tc_planner *p, long arm_after);
struct tc_attempt planner_next(const struct tc_planner *p, long now, int requests);
/* requests: health still requests a heartbeat (it sets the back-off's cap). */
void planner_finished(struct tc_planner *p, long now, const struct tc_attempt *a, enum tc_outcome outcome,
                      int requests);
int planner_armed(const struct tc_planner *p, long now);
#endif
