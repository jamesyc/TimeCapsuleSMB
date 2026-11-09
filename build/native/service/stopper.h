#ifndef TC_STOPPER_H
#define TC_STOPPER_H
#include "inspect.h"

/* Foreign copies of the runtime's roles, and Apple's daemons that would take
 * its ports, as the audit (inspect.c) classifies them: each is sent TERM at
 * every audit, then SIGKILL from TC_STALE_KILL_MS after first seen, except
 * telemetry, whose signed jobs must drain. README "Stopping processes" sets
 * this beside the other two stop policies: the manager's own children
 * (process.c) and Apple's stock diskd (diskd.c, TERM once when idle, never
 * KILL), which is never a target here. This machine does no I/O. */
#ifndef TC_STALE_KILL_MS
#define TC_STALE_KILL_MS 10000
#endif

/* What each kind of target holds back until it is gone. */
enum { BLOCK_SMB = 1, BLOCK_RSYNC = 2, BLOCK_DISCOVERY = 4, BLOCK_TELEMETRY = 8 };

/* Who the manager's own roles are, so their processes are never targets. */
struct tc_stop_ours {
    pid_t smb_group, rsync_group, discovery, telemetry;
};
/* A target since it was first seen. A PID reused by another process is
 * another target: the kernel start time tells them apart. */
struct tc_stop_entry {
    struct tc_proc_identity id;
    long long since;
    int killed;
};
struct tc_stopper {
    struct tc_stop_entry entries[TC_PROCESS_MAX];
    size_t count;
};
/* One signal to send. first_kill: the first SIGKILL to this target, which
 * the manager logs once (a TERM is routine cleanup). */
struct tc_stop_signal {
    pid_t pid, group;
    enum tc_process_role role;
    int sig, first_kill;
    long long waited_ms;
};
/* One audit's table in: the signals to send out (returned count, at most
 * TC_PROCESS_MAX) and the block bits. A target the table no longer lists is
 * forgotten. */
size_t tc_stopper_step(struct tc_stopper *, const struct tc_process_table *, const struct tc_stop_ours *, long long now,
                       struct tc_stop_signal *out, unsigned *blocked);
#endif
