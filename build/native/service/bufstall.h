#ifndef TC_BUFSTALL_H
#define TC_BUFSTALL_H
#include "../common/platform.h"
#include "inspect.h"

/* Buffer-cache stall recovery (NetBSD kern/60584, fixed upstream in 2026 and
 * not in Apple's NetBSD 4 or 6 kernels). getnewbuf() asks buf_lotsfree()
 * whether to allocate a fresh buffer. When it says no and every cached buffer
 * is busy, the caller sleeps with no timeout, and the thread that would free
 * a buffer may itself be waiting for one. The sleeper cannot be killed. Each
 * new connection or job then adds another stuck process until fork() hits
 * kern.maxproc (84 on both kernels), and the device stops answering SSH and
 * SMB until it is power-cycled.
 *
 * buf_lotsfree() always allocates while vm.bufmem is below
 * vm.bufmem_lowater. The manager raises the low-water mark to the highest
 * value the kernel accepts (vm.bufmem_hiwater - 16), wakes the sleepers, and
 * restores Apple's value (hiwater >> 3, how buf_setwm() sets it on both
 * kernels) once the stall is over. Any low-water mark above vm.bufmem stops
 * the pagedaemon from draining the cache, so the raise is kept short and
 * simply repeated when needed.
 *
 * Nothing on the recovery path forks or starts a process: a stall can come
 * with a full process table. Detection, the raise, the wake and the restore
 * are system calls in the manager itself, on buffers allocated statically. */

/* The manager samples at most this often (its loop wakes every
 * TC_MANAGER_PASS_MS, a second on the device). */
#ifndef TC_BUFSTALL_SAMPLE_MS
#define TC_BUFSTALL_SAMPLE_MS 1000
#endif
/* A process passing through a buffer wait is normal; one still there this
 * long is stalled. A false positive costs a short raise. */
#ifndef TC_BUFSTALL_TRIGGER_MS
#define TC_BUFSTALL_TRIGGER_MS 5000
#endif
/* The stall is over, and Apple's value comes back, after this long without a
 * stalled process. Brief waits under load do not hold the raise. */
#ifndef TC_BUFSTALL_QUIET_MS
#define TC_BUFSTALL_QUIET_MS 10000
#endif
/* A process still stalled this long into a raise means the raise is not
 * helping: restore, and raise again this long later (or, when the cache is at
 * its high-water mark, check again this long later). */
#ifndef TC_BUFSTALL_HOLD_MS
#define TC_BUFSTALL_HOLD_MS 60000
#endif
/* NetBSD 6 wakes one buffer sleeper per released buffer, NetBSD 4 wakes them
 * all. Each pass releases at least one; 84 processes is the kernel limit. */
#ifndef TC_BUFWAKE_PASSES
#define TC_BUFWAKE_PASSES 128
#endif
/* An FFS directory on the RAM root. FFS reads directories with bread(), and
 * every buffer release wakes buffer sleepers, cached or not. It does not
 * depend on the disk layout: NetBSD 6 refuses to open a mounted /dev/dkN. */
#ifndef TC_BUFWAKE_DIR
#define TC_BUFWAKE_DIR "/dev"
#endif
/* sysctl refuses a low-water mark closer than this to the high-water mark. */
#define TC_BUFSTALL_GAP 16

struct tc_bufstall_sample {
    uint64_t bufmem, lowater, hiwater;
    size_t count;
    pid_t pids[TC_PROCESS_MAX];
};
struct tc_bufstall_seen {
    pid_t pid;
    long long since;
};
/* Worst first-to-last outcome of one episode, in rising order of severity. */
enum tc_bufstall_outcome {
    TC_BUFSTALL_NO_EPISODE,
    TC_BUFSTALL_OUTCOME_RESOLVED, /* a raise was in place when it cleared */
    TC_BUFSTALL_OUTCOME_STUCK,    /* a raise did not help for TC_BUFSTALL_HOLD_MS */
    TC_BUFSTALL_OUTCOME_CAPPED,   /* bufmem was at the high-water mark */
    TC_BUFSTALL_OUTCOME_FAILED,   /* the kernel refused the raise */
};
struct tc_bufstall {
    struct tc_bufstall_seen seen[TC_PROCESS_MAX];
    size_t seen_count;
    int raised, capped_logged;
    long long raised_at, retry_at, last_stalled_at;
    enum tc_bufstall_outcome outcome;
    long long longest;
    /* Set when tc_bufstall_step() returns RESOLVED or ENDED. */
    enum tc_bufstall_outcome ended_outcome;
    long long ended_longest;
};
enum tc_bufstall_action {
    TC_BUFSTALL_NONE,
    TC_BUFSTALL_RAISE,    /* write *lowater, then wake */
    TC_BUFSTALL_WAKE,     /* raised and still stalled: wake again */
    TC_BUFSTALL_CAPPED,   /* bufmem is at the high-water mark: log (once per episode) */
    TC_BUFSTALL_STUCK,    /* write *lowater (Apple's value); raise again later */
    TC_BUFSTALL_RESOLVED, /* write *lowater (Apple's value); the episode ended */
    TC_BUFSTALL_ENDED,    /* the episode ended with no raise in place */
    TC_BUFSTALL_RESTORE,  /* not raised but above Apple's value: write *lowater */
};

/* 1 when an 8-byte (not NUL-terminated) kernel wait message is a buffer wait:
 * getnewbuf (NetBSD 4), needbuf (NetBSD 6) or buf_malloc (NetBSD 4). */
int tc_bufstall_wmesg(const char *wmesg, size_t length);
uint64_t tc_bufstall_default_lowater(uint64_t hiwater);
enum tc_bufstall_action tc_bufstall_step(struct tc_bufstall *state, const struct tc_bufstall_sample *sample,
                                         long long now, uint64_t *lowater);
/* The kernel refused the raise step() asked for. */
void tc_bufstall_failed(struct tc_bufstall *state, long long now);
const char *tc_bufstall_outcome_name(enum tc_bufstall_outcome outcome);
/* Kernel access, all without fork or allocation. Host test builds use the
 * file named by TC_TEST_BUFCACHE instead; other builds report ENOSYS. */
int tc_bufstall_read(struct tc_bufstall_sample *out);
int tc_bufstall_set_lowater(uint64_t value);
/* Read TC_BUFWAKE_DIR TC_BUFWAKE_PASSES times. */
int tc_bufstall_wake(void);
#endif
