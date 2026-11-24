#ifndef TC_ACP_H
#define TC_ACP_H
#include "platform.h"

/* ACP readers shared by the helpers. Only `acp -q/-A <key>` is ever run:
 * `acp notelisten` reset a device's AirPort settings once and is forbidden.
 * The child-process discipline (pipe hygiene, 20 s per-key timeout, process
 * group cleanup, bounded output) came from telemetry/device.c.
 * Cancellation is `acp_stop_requested`, separate from telemetry's stop flag. */

enum { ACP_OK = 0, ACP_UNAVAILABLE = -1, ACP_ABORT = -2 };
#ifndef TC_ACP_PATH
#define TC_ACP_PATH "/usr/bin/acp"
#endif
#ifndef TC_ACP_TIMEOUT_SECONDS
#define TC_ACP_TIMEOUT_SECONDS 20
#endif
#ifndef TC_ACP_COLLECTION_BUDGET_SECONDS
#define TC_ACP_COLLECTION_BUDGET_SECONDS 30
#endif
#define ACP_VALUE_MAX 256

extern volatile sig_atomic_t acp_stop_requested;
/* Managed jobs/roles inherit their owner's group. Standalone diagnostics keep
 * an isolated collector group so cancellation never signals their caller. */
void acp_set_scope(int inherited_group, int (*cancelled)(void));

struct acp_value {
    int status;                /* ACP_OK / ACP_UNAVAILABLE / ACP_ABORT */
    char text[ACP_VALUE_MAX];
};

/* Caller-owned buffers keep large MaSt/password captures out of every facts
 * snapshot. First-line scalar reads and raw multiline reads share one child
 * lifecycle; trimming never changes interior whitespace. */
enum acp_read_form { ACP_QUERY, ACP_ARRAY };
/* Why a request ended ACP_ABORT, so a caller can tell an ACPd that stopped
 * answering (TIMEOUT) from a device out of processes (FORK) or descriptors
 * (PIPE), a broken acp binary (EXEC), unusable output (OUTPUT), its own
 * cancellation, the collection budget running out (BUDGET: the key was not
 * asked, or its child was stopped when the budget ran out before its own
 * timeout), or a key not asked because an earlier one timed out or could not
 * start in a collection begun with ACP_STOP_EARLY (SKIPPED). LOCAL covers
 * the collector's own failures: an invalid request, an unreadable clock,
 * select() or waitpid() errors. */
enum acp_abort_reason {
    ACP_REASON_NONE = 0, ACP_REASON_TIMEOUT, ACP_REASON_BUDGET, ACP_REASON_FORK, ACP_REASON_PIPE,
    ACP_REASON_EXEC, ACP_REASON_OUTPUT, ACP_REASON_CANCELLED, ACP_REASON_LOCAL, ACP_REASON_SKIPPED
};
struct acp_request {
    const char *key;
    enum acp_read_form form;
    int multiline;
    int trim_whitespace;
    char *output;
    size_t capacity;
    size_t length;
    int status;
    int exit_status;
    enum acp_abort_reason reason; /* ACP_REASON_NONE unless status is ACP_ABORT */
    int error;                    /* errno for FORK and PIPE, else 0 */
};

struct acp_bool { int available; int value; };
struct acp_u32  { int available; uint32_t value; };
struct acp_ipv4 { int available; uint32_t addr; };   /* network byte order */

void trim_line(char *value);

struct acp_bool acp_bool(const struct acp_value *value);
struct acp_u32 acp_u32(const struct acp_value *value);
struct acp_ipv4 acp_ipv4(const struct acp_value *value);
const char *acp_str(const struct acp_value *value);   /* NULL when unavailable */

/* Non-blocking collection: one child at a time, next key on completion,
 * whole-collection budget after which remaining keys are marked
 * ACP_ABORT with ACP_REASON_BUDGET (never read, so not "not set"). Drive it with acp_collect_fd()/acp_collect_pump() from a
 * select() loop, or synchronously with acp_collect_run(). */
struct acp_collector {
    struct acp_request *requests;
    size_t key_count;
    size_t next;
    long long timeout_ms;
    long long deadline_ms;       /* collection budget */
    long long child_deadline_ms; /* per-key timeout */
    pid_t child;
    int fd;
    size_t used;
    int eof;
    long long eof_ms;            /* when the child's output closed */
    int line_done;
    int active;
    int finished;
    int aborted;
    int inherited_group;
    int stop_early;            /* ACP_STOP_EARLY */
    int stopped;               /* a key timed out or its child could not start */
    int child_at_budget;       /* the running child's deadline is the budget's */
};
/* Flags: ACP_STOP_EARLY asks no more keys (ACP_REASON_SKIPPED) after one
 * times out, or after a child cannot start. An ACPd that has stopped
 * answering one key will not answer the next, and a process table that is
 * full for one fork is full for the next: telemetry would otherwise spend
 * its budget, or a dozen forks, on children that fail one after another. */
#define ACP_STOP_EARLY 1

/* begin/pump return 0 while pending, 1 when complete, -1 if any request
 * aborted. Inspect per-request status: missing values are not batch failures. */
int acp_collect_begin(struct acp_collector *c, struct acp_request *requests, size_t count,
                      long long timeout_ms, long long budget_ms, int flags);
int acp_collect_fd(const struct acp_collector *c);
long long acp_collect_deadline_ms(const struct acp_collector *c);
/* The same deadline as seen at monotonic time now; tests choose the time. */
long long acp_collect_deadline_at(const struct acp_collector *c, long long now);
int acp_collect_pump(struct acp_collector *c);      /* 1 = finished, 0 = in progress, -1 = aborted */
void acp_collect_cancel(struct acp_collector *c);
int acp_collect_run(struct acp_request *requests, size_t count, long long timeout_ms, long long budget_ms);
int acp_collect_run_opts(struct acp_request *requests, size_t count, long long timeout_ms, long long budget_ms,
                         int flags);
long long acp_monotonic_ms(void);
#endif
