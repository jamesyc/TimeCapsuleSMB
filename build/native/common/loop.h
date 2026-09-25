#ifndef TC_LOOP_H
#define TC_LOOP_H
#include "plan.h"

/* Shared daemon loop shape (guide B.7 / C.4): select() with a monotonic
 * deadline, no sleep(). Inputs: a PF_ROUTE socket whose interface/address
 * messages schedule a recollection after a 2 s debounce, the 30 s poll
 * timer, and the non-blocking facts collector's pipe while a collection is
 * running. Collections run one at a time; a slow one never queues another.
 * The loop owns the last validated plan (9.4 B) for its process. */

#ifndef TC_PLAN_POLL_MS
#define TC_PLAN_POLL_MS 30000
#endif
#ifndef TC_PLAN_DEBOUNCE_MS
#define TC_PLAN_DEBOUNCE_MS 2000
#endif

struct plan_loop {
    struct plan_options options;
    int route_fd;
    long long next_poll_ms;
    long long recollect_at_ms;      /* 0 = nothing scheduled */
    int collecting;
    struct facts_collector collector;
    struct device_facts facts;
    struct device_plan last_validated;
    int have_validated;
    struct device_plan current;
    int logged_synthetic;       /* the C.11 parser-gap line is printed once */
#ifdef TC_NATIVE_TEST
    const char *facts_file;         /* test-only replacement for live collection */
#endif
};

void plan_loop_init(struct plan_loop *loop, const struct plan_options *options, const char *facts_file);
void plan_loop_close(struct plan_loop *loop);
/* Adds the loop's fds to `reads`, updates *maxfd, and lowers *deadline_ms
 * to the loop's next wake time. */
void plan_loop_prepare(struct plan_loop *loop, long long now_ms, fd_set *reads, int *maxfd, long long *deadline_ms);
/* Consumes ready fds and timers. Returns 1 when a new plan landed in
 * loop->current, 0 otherwise. */
int plan_loop_dispatch(struct plan_loop *loop, long long now_ms, const fd_set *reads);
/* Request a recollection now (e.g. startup). */
void plan_loop_request(struct plan_loop *loop, long long now_ms);
long long plan_loop_now_ms(void);
/* select() helper: waits until an fd is readable or deadline_ms; returns
 * select's result (EINTR yields 0). */
int plan_loop_wait(fd_set *reads, int maxfd, long long now_ms, long long deadline_ms);
#endif
