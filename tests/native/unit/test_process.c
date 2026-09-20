#include "common/process.h"
#include "common/parent.h"
#include <assert.h>

static volatile sig_atomic_t parent_signals;
static void parent_term(int sig) {
    (void)sig;
    parent_signals++;
}
static long long now_ms(void) {
    struct timespec t;
    assert(clock_gettime(CLOCK_MONOTONIC, &t) == 0);
    return (long long)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
static void finish(struct tc_child *child, int successful) {
    long long deadline = now_ms() + 3000;
    while (!tc_child_poll(child, now_ms()) && now_ms() < deadline)
        usleep(1000);
    assert(child->exited && child->output < 0);
    assert(tc_child_ok(child) == successful);
}
static int lifetime(void *unused) {
    int fd = tc_parent_pipe();
    (void)unused;
    alarm(10);
    assert(fd == STDIN_FILENO);
    while (tc_parent_alive(fd))
        usleep(1000);
    return 0;
}
static int output(void *unused) {
    unsigned char bytes[4096];
    unsigned i, chunk;
    (void)unused;
    for (i = 0; i < sizeof(bytes); i++)
        bytes[i] = (unsigned char)i;
    for (chunk = 0; chunk < 12; chunk++) {
        size_t used = 0;
        while (used < sizeof(bytes)) {
            ssize_t n = write(STDOUT_FILENO, bytes + used, sizeof(bytes) - used);
            if (n < 0 && errno == EINTR)
                continue;
            if (n <= 0)
                return 1;
            used += n;
        }
    }
    return 0;
}
static int samba_group_exit(void *unused) {
    (void)unused;
    /* Samba's real parent uses this group signal in its atexit handler. */
    kill(0, SIGTERM);
    return 9;
}
static int stubborn(void *unused) {
    (void)unused;
    signal(SIGTERM, SIG_IGN);
    alarm(5);
    assert(write(STDOUT_FILENO, "R", 1) == 1);
    for (;;)
        pause();
}
static int orphan_worker(void *unused) {
    pid_t worker;
    (void)unused;
    worker = fork();
    if (worker < 0)
        return 1;
    if (worker == 0) {
        close(STDOUT_FILENO);
        signal(SIGTERM, SIG_IGN);
        alarm(5);
        for (;;)
            pause();
    }
    return write(STDOUT_FILENO, &worker, sizeof(worker)) == sizeof(worker) ? 0 : 1;
}

int main(int argc, char **argv) {
    struct tc_child a = {0}, b = {0};
    unsigned char capture[49152];
    size_t i;
    assert(argc == 2);
    /* Also isolate when this driver is launched over the device's SSH shell. */
    if (getpgrp() != getpid())
        assert(setpgid(0, 0) == 0);
    signal(SIGTERM, parent_term);
    alarm(15);
    if (!strcmp(argv[1], "lifetime")) {
        assert(tc_child_fork(&a, lifetime, NULL, NULL, NULL, 0, 0) == 0);
        assert(tc_child_fork(&b, lifetime, NULL, NULL, NULL, 0, 0) == 0);
        /* B must not inherit A's lifetime writer. No daemon PID file or
         * special cleanup message is needed when the supervisor disappears. */
        close(a.lifetime);
        a.lifetime = -1;
        finish(&a, 1);
        assert(kill(b.pid, 0) == 0);
        close(b.lifetime);
        b.lifetime = -1;
        finish(&b, 1);
        tc_child_close(&a);
        tc_child_close(&b);
    } else if (!strcmp(argv[1], "group")) {
        assert(tc_child_fork(&a, samba_group_exit, NULL, NULL, NULL, 0, 0) == 0);
        finish(&a, 0);
        assert(WIFSIGNALED(a.status) && WTERMSIG(a.status) == SIGTERM);
        assert(parent_signals == 0);
        tc_child_close(&a);
    } else if (!strcmp(argv[1], "capture")) {
        assert(tc_child_fork(&a, output, NULL, NULL, capture, sizeof(capture), now_ms() + 3000) == 0);
        finish(&a, 1);
        assert(a.used == sizeof(capture));
        for (i = 0; i < a.used; i++)
            assert(capture[i] == (unsigned char)i);
        tc_child_close(&a);
    } else if (!strcmp(argv[1], "overflow")) {
        assert(tc_child_fork(&a, output, NULL, NULL, capture, 3, now_ms() + 3000) == 0);
        finish(&a, 0);
        assert(a.overflow);
        tc_child_close(&a);
    } else if (!strcmp(argv[1], "exec_failure")) {
        char *args[] = {"/nonexistent/timecapsulesmb-test-program", NULL};
        assert(tc_child_exec(&a, args, NULL) == 0);
        finish(&a, 0);
        assert(WEXITSTATUS(a.status) == 127);
        tc_child_close(&a);
    } else if (!strcmp(argv[1], "orphan")) {
        pid_t worker;
        long long deadline = now_ms() + 2000;
        int done = 0;
        assert(tc_child_fork(&a, orphan_worker, NULL, NULL, capture, sizeof(capture), 0) == 0);
        while (!a.exited && now_ms() < deadline) {
            done = tc_child_poll(&a, now_ms());
            usleep(1000);
        }
        assert(a.exited && !done && a.used == sizeof(worker));
        memcpy(&worker, capture, sizeof(worker));
        assert(kill(worker, 0) == 0);
        /* Losing the direct parent must not authorize replacing binaries or
         * wiping locks while its old worker still owns this process group. */
        tc_child_poll(&a, a.deadline + 1);
        deadline = now_ms() + 3000;
        while (!(done = tc_child_poll(&a, now_ms())) && now_ms() < deadline)
            usleep(1000);
        assert(done);
        tc_child_close(&a);
    } else if (!strcmp(argv[1], "stop") || !strcmp(argv[1], "drain")) {
        long long started = now_ms(), limit = started + 2000;
        int escalate = !strcmp(argv[1], "stop");
        assert(tc_child_fork(&a, stubborn, NULL, NULL, capture, sizeof(capture), 0) == 0);
        while (!a.used && now_ms() < limit) {
            tc_child_poll(&a, now_ms());
            usleep(1000);
        }
        assert(a.used == 1);
        tc_child_stop(&a, started, escalate);
        tc_child_poll(&a, started + 10001);
        if (!escalate) {
            /* Signed telemetry work is allowed to drain; a supervisor's
             * ordinary timeout may not SIGKILL its worker group. */
            assert(kill(a.pid, 0) == 0);
            kill(-a.group, SIGKILL); /* Test-owned fixture cleanup only. */
        }
        finish(&a, 0);
        assert(WTERMSIG(a.status) == SIGKILL);
        tc_child_close(&a);
    } else
        return 2;
    return 0;
}
