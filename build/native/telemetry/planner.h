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
 * the extra one is wanted, else "scheduled".
 *
 * Which acp facts it may keep instead of reading acp (tc_attempt.facts,
 * struct tc_facts_reuse: a maximum age, and whether only a complete
 * collection counts) follows from last_failure, the latest counted failure
 * since a delivery, and the kind:
 *
 *   last failure      boot, scheduled         resource_high
 *   none              read                    under 12 h
 *   acp_unavailable   read                    read
 *   network, server   under 1 h, complete     under 12 h
 *   local_resource    any                     any
 *
 *   read             {0, 0}: a heartbeat on its own schedule reads afresh.
 *   acp_unavailable  the facts it kept name no router, so keeping them
 *                    fails the same way, whatever the kind.
 *   1 h, complete    {TC_FACTS_REUSE_SECONDS, 1}: acp is not what failed
 *                    (the POST, the server's answer, the nonce or building
 *                    the payload did), so a complete collection under an
 *                    hour old serves the retry instead of forking acp for
 *                    every retry of an outage; an outage that outlasts it
 *                    reads again on its next retry.
 *   12 h             {TC_INTERVAL_SECONDS, 0}: resource_high usually means
 *                    the device is short of processes, so it forks no acp
 *                    while the last collection, complete or not, is under
 *                    the interval old. Every delivered heartbeat restarts
 *                    the schedule, so without the limit a chronic health
 *                    event's extras would keep the boot-time identity and
 *                    plan forever.
 *   any              {TC_FACTS_FOREVER, 0}: a device short of processes
 *                    forks no acp child while it has facts at all; with
 *                    none it still tries once per retry.
 * Ages count from the last collection, whichever attempt made it, so the
 * planner keeps no clock of its own for them.
 *
 * retry_at starts at hold_until: the daemon holds its boot heartbeat
 * TC_BOOT_HOLD_SECONDS from its start. Early in boot ACPd's waMA holds a
 * placeholder and the network is often not up yet; a minute later the one
 * collection the boot heartbeat makes usually finds the hardware address
 * and its first POST goes through. The hold is not a failure: nothing is counted, and health
 * events noted meanwhile go out with the boot heartbeat. A restart that is
 * not a boot waits too; a minute does not matter there.
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
 *                    and never more than TC_PLANNER_NET_RETRY_MAX_SECONDS, so
 *                    a health event that comes during a long outage waits at
 *                    most that long once the network is back.
 * Every counted failure sets last_failure; delivery clears it; busy and
 * cancelled leave it.
 * The two back-offs keep their own counters: a local failure does not reset
 * the network delay, any other failure ends the local run. Every failure but
 * busy and cancelled is counted, by kind, until a heartbeat is delivered;
 * the heartbeat carries the count ("delivery"). Health crossings count only
 * once armed: after the boot heartbeat, or TC_ARM_AFTER_SECONDS of uptime
 * when the server cannot be reached. */
#ifndef TC_FACTS_REUSE_SECONDS
#define TC_FACTS_REUSE_SECONDS 3600
#endif
#define TC_PLANNER_LOCAL_RETRIES 10
#define TC_PLANNER_LOCAL_LATER_SECONDS 300
#ifndef TC_PLANNER_NET_RETRY_MAX_SECONDS
#define TC_PLANNER_NET_RETRY_MAX_SECONDS 21600
#endif
#ifndef TC_EXTRA_SPACING_SECONDS
#define TC_EXTRA_SPACING_SECONDS 3600
#endif
#define TC_EXTRA_SPACING_MAX_SECONDS 43200
#define TC_EXTRA_QUIET_SECONDS 86400
#ifndef TC_BOOT_HOLD_SECONDS
#define TC_BOOT_HOLD_SECONDS 60
#endif
#ifndef TC_ARM_AFTER_SECONDS
#define TC_ARM_AFTER_SECONDS 900
#endif

enum tc_attempt_kind { TC_ATTEMPT_NONE, TC_ATTEMPT_BOOT, TC_ATTEMPT_SCHEDULED, TC_ATTEMPT_EXTRA };
struct tc_attempt {
    enum tc_attempt_kind kind;
    const char *reason;
    struct tc_facts_reuse facts;
};

struct tc_planner {
    int boot_sent;
    enum tc_outcome last_failure; /* the latest counted failure since a delivery; DELIVERED: none */
    long next_scheduled, retry_at, net_delay;
    unsigned local_count;
    long spacing, next_extra_at, last_extra_at; /* last_extra_at < 0: none yet */
    long arm_after;
    struct tc_delivery delivery; /* the counted failures since the last delivery; every payload sends it */
};

/* hold_until: no attempt before it (0: none). */
void planner_init(struct tc_planner *p, long arm_after, long hold_until);
struct tc_attempt planner_next(const struct tc_planner *p, long now, int requests);
/* requests: health still requests a heartbeat (it sets the back-off's cap). */
void planner_finished(struct tc_planner *p, long now, const struct tc_attempt *a, enum tc_outcome outcome,
                      int requests);
int planner_armed(const struct tc_planner *p, long now);
/* The outcome of a failure before the heartbeat is built: taking the
 * workspace lock (lock) or reading the nonce. Out of descriptors or memory
 * is local_resource; any other lock failure stays busy (another holder, or
 * a workspace the log already names), any other nonce failure server. */
enum tc_outcome tc_setup_outcome(int error, int lock);
#endif
