#ifndef TC_PROCESS_H
#define TC_PROCESS_H
#include "platform.h"
#ifndef TC_CHILD_GRACE_MS
#define TC_CHILD_GRACE_MS 10000
#endif

struct tc_child {
    pid_t pid, group;
    int lifetime, output, exited, status, overflow, stopping, allow_kill, nested;
    long long deadline;
    unsigned char *capture;
    size_t capacity, used;
};
/* The callback runs in a forked, isolated process with only standard FDs.
 * A captured result uses stdout; diagnostics use stderr. No result file. */
typedef int (*tc_child_fn)(void *);
int tc_child_fork(struct tc_child *, tc_child_fn, void *, const char *log, void *capture, size_t capacity,
                  long long deadline);
int tc_child_exec(struct tc_child *, char *const argv[], const char *log);
int tc_child_exec_capture(struct tc_child *, char *const argv[], void *, size_t, long long deadline);
/* A utility belongs to its enclosing job group; only its direct PID is reaped
 * here. The outer supervisor keeps the group until every descendant exits. */
int tc_command_exec(struct tc_child *, char *const argv[], void *, size_t);
void tc_child_prepare(const struct tc_child *, fd_set *, int *, long long *);
/* Returns 1 after exit and complete output, 0 while running. */
int tc_child_poll(struct tc_child *, long long now);
void tc_child_stop(struct tc_child *, long long now, int allow_kill);
void tc_child_close(struct tc_child *);
int tc_child_ok(const struct tc_child *);
void tc_close_other_fds(int keep);
#endif
