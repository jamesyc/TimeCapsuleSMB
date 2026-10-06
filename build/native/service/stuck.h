#ifndef TC_STUCK_H
#define TC_STUCK_H
#include "proctable.h"

/* Processes stuck in the kernel. A process in an uninterruptible sleep cannot
 * be killed; one that stays there without using the CPU is waiting on
 * something that is not completing, usually the disk. This only reports them
 * (log, process title, heartbeat): nothing in user space can wake or kill
 * them, and buffer-cache stalls have their own recovery (bufstall.h).
 *
 * Doctor reports the same thing from one ps snapshot (D state, ps sleep time
 * of 120 s or more); the manager also follows a process across samples, so it
 * sees waits the kernel's sleep time cannot: NetBSD 6 waits for a free buffer
 * with a quarter-second timeout (vfs_bio.c needbuf), and every wake-up resets
 * the sleep time.
 *
 * It reads the manager's process table for the pass (proctable.h) plus one
 * sysctl per multi-threaded process (only Apple's daemons have threads), with
 * static buffers: no fork, no allocation, so it works with a full process
 * table. */

/* Continuously asleep this long, uninterruptibly, is stuck while it uses at
 * most TC_STUCK_CPU_US of CPU in each TC_STUCK_MS window. A process doing slow
 * disk I/O wakes and runs between requests, so it either shows as running in
 * a sample or uses more CPU than this; a disk retrying a bad sector can hold
 * one request for tens of seconds, hence two minutes. The budget is per
 * window, not per episode: a stall that wakes to retry uses a little CPU at
 * every wake, which over hours would add up to the budget. */
#ifndef TC_STUCK_MS
#define TC_STUCK_MS 120000
#endif
#ifndef TC_STUCK_CPU_US
#define TC_STUCK_CPU_US 100000
#endif
/* A heartbeat goes out when an episode has lasted this long, and again when a
 * reported episode clears. A stall that ends only with a power cycle never
 * clears, so waiting for the end would never report it. */
#ifndef TC_STUCK_REPORT_MS
#define TC_STUCK_REPORT_MS 300000
#endif
/* Uninterruptible sleepers tracked at once: processes plus the threads of
 * multi-threaded ones. kern.maxproc is 84 on both kernels. */
#define TC_STUCK_MAX 128
#define TC_STUCK_WMESG TC_PROC_WMESG
#define TC_STUCK_COMM TC_PROC_COMM

struct tc_stuck_thread {
    pid_t pid, group;
    int lid;                   /* 0 for a single-threaded process */
    unsigned slept;            /* seconds in this sleep: the kernel's l_slptime */
    unsigned long long cpu_us; /* the process's CPU time */
    char wmesg[TC_STUCK_WMESG], comm[TC_STUCK_COMM];
};
struct tc_stuck_sample {
    size_t count;
    int truncated; /* more uninterruptible sleepers than TC_STUCK_MAX */
    struct tc_stuck_thread threads[TC_STUCK_MAX];
};
struct tc_stuck_entry {
    struct tc_stuck_thread thread; /* latest sample */
    long long since;               /* asleep since, ms on the manager's clock */
    long long window_at;           /* when the current CPU window began */
    unsigned long long window_cpu; /* CPU time when it began */
    int stuck, reported;
};
struct tc_stuck {
    size_t count;
    struct tc_stuck_entry entries[TC_STUCK_MAX];
};
enum tc_stuck_change { TC_STUCK_STARTED, TC_STUCK_REPORT, TC_STUCK_CLEARED };
struct tc_stuck_event {
    enum tc_stuck_change change;
    struct tc_stuck_entry entry;
    long long duration_ms;
};
typedef void (*tc_stuck_emit_fn)(const struct tc_stuck_event *, void *context);

/* 1 for a sleep a kill cannot interrupt, in a user process: LSSLEEP without
 * L_SINTR, and not a kernel thread (P_SYSTEM), which always sleeps this way. */
int tc_stuck_uninterruptible(int stat, int flag, const char *wmesg, size_t wmesg_length);
/* Follows the sample's sleepers, calling fn for each change: in sample order,
 * then the sleepers that left. Returns the number of calls. */
size_t tc_stuck_step(struct tc_stuck *state, const struct tc_stuck_sample *sample, long long now, tc_stuck_emit_fn fn,
                     void *context);
/* Copies text with every byte that is not a letter, digit, '.', '-' or '_'
 * replaced by '_', so it fits a process title or a heartbeat reason field. */
void tc_stuck_word(char *out, size_t size, const char *text);
/* The uninterruptible sleepers in this pass's process table. */
int tc_stuck_read(struct tc_stuck_sample *out, const struct tc_proctable *table);
/* The manager's process title, which doctor reads from ps:
 *   role=manager started=S[ waiting=hostname][ stuck=PID:COMM:WAIT:SECONDS,...[,+N]]
 * started= is when the manager started, in whole seconds on the kernel's
 * monotonic clock (started_ms; omitted when negative, a failed clock read).
 * Doctor subtracts it from `service --print-monotonic-ms`, the same clock:
 * ps's elapsed time is wall-clock arithmetic, and the wall clock can step
 * when sntpd sets it after boot (by a day in the field). The title
 * is rewritten only on changes, so it carries the start, not the age. Stuck
 * entries come last, longest first, at most limit of them and then +N for
 * the rest (stuck=+N alone when limit is 0): a title cut at size loses stuck
 * entries, never started=. Their seconds are now - since. */
void tc_manager_title(char *out, size_t size, long long started_ms, int waiting, const struct tc_stuck *stuck,
                      size_t limit, long long now);
#endif
