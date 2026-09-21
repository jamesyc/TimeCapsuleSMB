#include "acp.h"
#include <syslog.h>
#include <limits.h>

volatile sig_atomic_t acp_stop_requested = 0;
static int inherited_scope;
static int (*owner_cancelled)(void);

void acp_set_scope(int inherited_group, int (*cancelled)(void)) {
    inherited_scope = inherited_group;
    owner_cancelled = cancelled;
}
static int cancelled(void) {
    if (owner_cancelled && owner_cancelled()) acp_stop_requested = 1;
    return acp_stop_requested;
}

void trim_line(char *value) {
    char *start;
    size_t len;
    if (value == NULL) return;
    start = value;
    while (*start && isspace((unsigned char)*start)) start++;
    len = strlen(start);
    while (len && isspace((unsigned char)start[len - 1])) len--;
    memmove(value, start, len);
    value[len] = '\0';
}

long long acp_monotonic_ms(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) return -1;
    return (long long)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static void acp_log_failure(const char *key, const char *failure) {
    fprintf(stderr, "acp: %s %s\n", key, failure);
    syslog(LOG_DAEMON | LOG_ERR, "acp: %s %s", key, failure);
}

static void stop_acp_child(pid_t child, int inherited_group) {
    int status, i;
    pid_t result;
    /* Keep the direct child unreaped until group signalling is complete, so
     * its PID cannot be reused while descendants still hold the pipe open.
     * Signal it directly too: cancellation may precede its setpgid call. */
    kill(child, SIGTERM);
    if (!inherited_group) kill(-child, SIGTERM);
    for (i = 0; i < 10; i++) usleep(100000);
    kill(child, SIGKILL);
    if (!inherited_group) kill(-child, SIGKILL);
    for (i = 0; i < 20; i++) {
        result = waitpid(child, &status, WNOHANG);
        if (result == child || (result < 0 && errno == ECHILD)) return;
        if (result < 0 && errno != EINTR) break;
        usleep(50000);
    }
    /* Even reaping must not hang the caller. A kernel-stuck child cannot be
     * recovered here; stop starting collectors instead. */
    acp_stop_requested = 1;
    fputs("acp: child could not be reaped after SIGKILL\n", stderr);
    syslog(LOG_DAEMON | LOG_ERR, "acp: child could not be reaped after SIGKILL");
}

/* Returns 0 with c->child/c->fd set, or -1 with *failure describing why. */
static int start_acp_child(struct acp_collector *c, const struct acp_request *request, const char **failure) {
    int output[2], flags;
    pid_t child;

    if (pipe(output)) { *failure = "cannot create output pipe"; return -1; }
    flags = fcntl(output[0], F_GETFL);
    if (flags < 0 || fcntl(output[0], F_SETFL, flags | O_NONBLOCK) ||
        fcntl(output[0], F_SETFD, FD_CLOEXEC) || fcntl(output[1], F_SETFD, FD_CLOEXEC)) {
        close(output[0]); close(output[1]);
        *failure = "cannot configure output pipe"; return -1;
    }
    child = fork();
    if (child == 0) {
        int null_fd;
        /* Only the child creates its group. Concurrent parent/child setpgid
         * calls can fail with EPERM on Darwin even for the same target group.
         * Cleanup signals both this PID and its group to cover early cancel. */
        if (!c->inherited_group && setpgid(0, 0)) _exit(126);
        signal(SIGTERM, SIG_DFL); signal(SIGINT, SIG_DFL); signal(SIGPIPE, SIG_DFL);
        if (dup2(output[1], STDOUT_FILENO) < 0 || fcntl(STDOUT_FILENO, F_SETFD, 0)) _exit(126);
        if (output[0] != STDOUT_FILENO) close(output[0]);
        if (output[1] != STDOUT_FILENO) close(output[1]);
        null_fd = open("/dev/null", O_RDWR);
        if (null_fd < 0 || dup2(null_fd, STDIN_FILENO) < 0 || dup2(null_fd, STDERR_FILENO) < 0) _exit(126);
        if (null_fd > STDERR_FILENO) close(null_fd);
        /* Raw ACP fork paths must not keep another role's lifetime writer or
         * the manager's singleton/result descriptors alive across exec. */
        {
            long limit = sysconf(_SC_OPEN_MAX);
            int fd;
            if (limit < 0) limit = 1024;
            for (fd = 3; fd < limit; fd++) close(fd);
        }
        execl(TC_ACP_PATH, "acp", request->form == ACP_ARRAY ? "-A" : "-q", request->key, (char *)NULL);
        _exit(127);
    }
    close(output[1]);
    if (child < 0) {
        close(output[0]); *failure = "cannot fork"; return -1;
    }
    c->child = child;
    c->fd = output[0];
    c->used = 0;
    c->eof = 0;
    c->line_done = 0;
    c->active = 1;
    return 0;
}

static void finish_key(struct acp_collector *c, int status) {
    struct acp_request *value = &c->requests[c->next];
    if (status != ACP_OK) {
        if (value->output && value->capacity) value->output[0] = '\0';
        value->length = 0;
    }
    value->status = status;
    if (status == ACP_ABORT) {
        c->aborted = 1;
    }
    c->active = 0;
    c->child = 0;
    c->fd = -1;
    c->next++;
}

static void abort_current_child(struct acp_collector *c, const char *failure) {
    stop_acp_child(c->child, c->inherited_group);
    if (c->inherited_group) {
        char discard[4096];
        int eof = 0, attempts;
        /* An inherited collector cannot kill its group: it contains its owner
         * and potentially protected telemetry work. If a descendant retains
         * stdout after the direct child is reaped, stop this owner/operation
         * and leave whole-group draining to the outer supervisor. */
        for (attempts = 0; attempts < 16; attempts++) {
            ssize_t n = read(c->fd, discard, sizeof(discard));
            if (!n) { eof = 1; break; }
            if (n < 0 && errno != EINTR) break;
        }
        if (!eof) {
            acp_stop_requested = 1;
            acp_log_failure(c->requests[c->next].key, "descendant output did not close; stopping collector owner");
        }
    }
    close(c->fd);
    acp_log_failure(c->requests[c->next].key, failure);
    finish_key(c, ACP_ABORT);
}

/* Start the next key's child, or mark it unavailable when the budget or a
 * cancellation forbids starting more children. Returns 1 when everything
 * is finished. */
static int advance(struct acp_collector *c) {
    while (!c->active && c->next < c->key_count) {
        const char *failure;
        long long now;
        struct acp_request *value = &c->requests[c->next];
        value->output[0] = '\0';
        if (cancelled()) {
            finish_key(c, ACP_ABORT);
            continue;
        }
        now = acp_monotonic_ms();
        if (now < 0) {
            acp_log_failure(c->requests[c->next].key, "cannot read monotonic clock");
            finish_key(c, ACP_ABORT);
            continue;
        }
        if (now >= c->deadline_ms) {
            /* Budget spent: the key was never asked, so nothing is known
             * about it. UNAVAILABLE means acp answered "not set", which
             * the planner may act on; this must not look like that. */
            acp_log_failure(c->requests[c->next].key, "collection budget exhausted");
            finish_key(c, ACP_ABORT);
            continue;
        }
        if (start_acp_child(c, value, &failure) != 0) {
            acp_log_failure(c->requests[c->next].key, failure);
            finish_key(c, ACP_ABORT);
            continue;
        }
        /* Clamp before adding so even a large caller timeout cannot wrap. */
        c->child_deadline_ms = c->deadline_ms;
        if (c->timeout_ms < c->deadline_ms - now) c->child_deadline_ms = now + c->timeout_ms;
    }
    if (!c->active && c->next >= c->key_count) {
        c->finished = 1;
        return 1;
    }
    return 0;
}

int acp_collect_begin(struct acp_collector *c, struct acp_request *requests, size_t count,
                      long long timeout_ms, long long budget_ms) {
    long long now;
    size_t i;
    int invalid = timeout_ms <= 0 || budget_ms < 0;

    memset(c, 0, sizeof(*c));
    c->inherited_group = inherited_scope;
    c->fd = -1;
    c->requests = requests;
    c->key_count = count;
    c->timeout_ms = timeout_ms;
    for (i = 0; i < count; i++) {
        struct acp_request *r = &requests[i];
        r->status = ACP_ABORT;
        r->exit_status = -1;
        r->length = 0;
        if (r->output && r->capacity) r->output[0] = '\0';
        if (!r->output || !r->capacity || !r->key || strlen(r->key) != 4 ||
            strspn(r->key, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") != 4 ||
            (r->form != ACP_QUERY && r->form != ACP_ARRAY)) invalid = 1;
    }
    now = acp_monotonic_ms();
    /* Validate arithmetic before forking; deadlines must not wrap. */
    if (invalid || now < 0 || budget_ms > LLONG_MAX - now || timeout_ms > LLONG_MAX - now) {
        acp_log_failure("read", "invalid request or monotonic deadline");
        c->next = count;
        c->finished = c->aborted = 1;
        return -1;
    }
    c->deadline_ms = now + budget_ms;
    return advance(c) ? (c->aborted ? -1 : 1) : 0;
}

int acp_collect_fd(const struct acp_collector *c) {
    return c->active && !c->eof ? c->fd : -1;
}

long long acp_collect_deadline_ms(const struct acp_collector *c) {
    if (c->finished) {
        return -1;
    }
    /* Poll the child at least every 100 ms: exit is observed via WNOHANG. */
    if (c->active) {
        long long poll = acp_monotonic_ms() + 100;
        return poll < c->child_deadline_ms ? poll : c->child_deadline_ms;
    }
    return c->deadline_ms;
}

int acp_collect_pump(struct acp_collector *c) {
    if (c->finished) {
        return c->aborted ? -1 : 1;
    }
    if (c->active) {
        long long now = acp_monotonic_ms();
        if (cancelled()) {
            abort_current_child(c, "cancelled");
        } else if (now < 0) {
            abort_current_child(c, "cannot read monotonic clock");
        } else if (now >= c->child_deadline_ms) {
            abort_current_child(c, "timed out");
        } else if (c->eof) {
            int status;
            pid_t waited = waitpid(c->child, &status, WNOHANG);
            if (waited == c->child) {
                close(c->fd);
                c->active = 0;
                if (WIFEXITED(status)) c->requests[c->next].exit_status = WEXITSTATUS(status);
                if (acp_stop_requested) {
                    finish_key(c, ACP_ABORT);
                } else if (!WIFEXITED(status) || WEXITSTATUS(status) == 126 || WEXITSTATUS(status) == 127) {
                    acp_log_failure(c->requests[c->next].key, "collector setup, exec, or signal failure");
                    finish_key(c, ACP_ABORT);
                } else if (WEXITSTATUS(status) != 0) {
                    finish_key(c, ACP_UNAVAILABLE);
                } else {
                    struct acp_request *value = &c->requests[c->next];
                    value->output[c->used] = '\0';
                    if (value->trim_whitespace) trim_line(value->output);
                    value->length = strlen(value->output);
                    finish_key(c, ACP_OK);
                }
            } else if (waited < 0 && errno != EINTR) {
                close(c->fd);
                acp_log_failure(c->requests[c->next].key, "cannot reap collector");
                finish_key(c, ACP_ABORT);
            }
        } else {
            char chunk[256];
            ssize_t n = read(c->fd, chunk, sizeof(chunk));
            if (n == 0) {
                c->eof = 1;
            } else if (n < 0) {
                if (errno != EINTR && errno != EAGAIN) {
                    abort_current_child(c, "cannot read output");
                }
            } else {
                /* Capture the first line, but drain later output so it cannot
                 * block the child. The same deadline covers reading and exit. */
                struct acp_request *value = &c->requests[c->next];
                size_t i;
                for (i = 0; i < (size_t)n && !c->line_done; i++) {
                    if (!value->multiline && chunk[i] == '\n') {
                        c->line_done = 1;
                    } else if (!chunk[i] || c->used + 1 >= value->capacity) {
                        abort_current_child(c, "invalid or oversized output");
                        break;
                    } else {
                        value->output[c->used++] = chunk[i];
                    }
                }
            }
        }
    }
    if (advance(c)) {
        return c->aborted ? -1 : 1;
    }
    return 0;
}

void acp_collect_cancel(struct acp_collector *c) {
    if (c->active) {
        abort_current_child(c, "cancelled");
    }
    while (c->next < c->key_count) {
        finish_key(c, ACP_ABORT);
    }
    c->finished = 1;
}

/* Synchronous driver: the daemons use the fd/pump pair; one-shot helpers
 * and telemetry use this. */
int acp_collect_run(struct acp_request *requests, size_t count, long long timeout_ms, long long budget_ms) {
    struct acp_collector c;
    int rc = acp_collect_begin(&c, requests, count, timeout_ms, budget_ms);
    if (rc != 0) {
        return rc;
    }
    for (;;) {
        fd_set reads;
        struct timeval timeout;
        int fd = acp_collect_fd(&c);
        int ready;

        FD_ZERO(&reads);
        if (fd >= 0) FD_SET(fd, &reads);
        timeout.tv_sec = 0; timeout.tv_usec = 100000;
        ready = select(fd >= 0 ? fd + 1 : 0, &reads, NULL, NULL, &timeout);
        if (ready < 0 && errno != EINTR) {
            if (c.active) {
                abort_current_child(&c, "cannot wait for output");
            }
        }
        rc = acp_collect_pump(&c);
        if (rc != 0) {
            return rc;
        }
    }
}

int read_acp_value(const char *key, char *out, size_t out_len) {
    struct acp_request request;
    memset(&request, 0, sizeof(request));
    request.key = key;
    request.trim_whitespace = 1;
    request.output = out;
    request.capacity = out_len < ACP_VALUE_MAX ? out_len : ACP_VALUE_MAX;
    (void)acp_collect_run(&request, 1, (long long)TC_ACP_TIMEOUT_SECONDS * 1000,
                         (long long)TC_ACP_COLLECTION_BUDGET_SECONDS * 1000);
    return request.status == ACP_OK && !request.length ? ACP_UNAVAILABLE : request.status;
}


struct acp_bool acp_bool(const struct acp_value *value) {
    struct acp_bool out;
    out.available = 0;
    out.value = 0;
    if (value->status != ACP_OK) {
        return out;
    }
    if (!strcasecmp(value->text, "true")) {
        out.available = 1;
        out.value = 1;
    } else if (!strcasecmp(value->text, "false")) {
        out.available = 1;
        out.value = 0;
    } else {
        /* ACP also prints booleans as 0/1 or 0x0/0x1. */
        struct acp_u32 number = acp_u32(value);
        if (number.available && number.value <= 1) {
            out.available = 1;
            out.value = (int)number.value;
        }
    }
    return out;
}

struct acp_u32 acp_u32(const struct acp_value *value) {
    struct acp_u32 out;
    char *end = NULL;
    unsigned long parsed;
    out.available = 0;
    out.value = 0;
    if (value->status != ACP_OK || value->text[0] == '\0') {
        return out;
    }
    errno = 0;
    parsed = strtoul(value->text, &end, 0);
    if (errno != 0 || end == value->text || end == NULL || *end != '\0' || parsed > 0xffffffffUL) {
        return out;
    }
    out.available = 1;
    out.value = (uint32_t)parsed;
    return out;
}

struct acp_ipv4 acp_ipv4(const struct acp_value *value) {
    struct acp_ipv4 out;
    struct in_addr parsed;
    out.available = 0;
    out.addr = 0;
    if (value->status != ACP_OK || inet_pton(AF_INET, value->text, &parsed) != 1) {
        return out;
    }
    out.available = 1;
    out.addr = parsed.s_addr;
    return out;
}

const char *acp_str(const struct acp_value *value) {
    return value->status == ACP_OK ? value->text : NULL;
}
