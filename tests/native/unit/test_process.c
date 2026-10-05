#include "common/process.h"
#include "common/parent.h"
#include <assert.h>

/* The driver build renames process.c's setpgid() to this hook (see
 * test_process.py). term_before_reset holds a new child inside it, before the
 * child resets the handlers it inherited from this driver. */
#undef setpgid
int setpgid(pid_t pid, pid_t group);
static pid_t driver;
static int hold_child_setup;
int tc_test_setpgid(pid_t pid, pid_t group) {
    if (hold_child_setup && getpid() != driver)
        usleep(300000);
    return setpgid(pid, group);
}

static volatile sig_atomic_t parent_signals;
static void parent_term(int sig) {
    (void)sig;
    parent_signals++;
}
static void interrupt_wait(int sig) {
    (void)sig;
}
static long long now_ms(void) {
    struct timespec t;
    assert(clock_gettime(CLOCK_MONOTONIC, &t) == 0);
    return (long long)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
/* Bounds on how long a child may take under a loaded host, not timing claims:
 * main()'s alarm(15) caps a whole case. */
static void finish(struct tc_child *child, int successful) {
    long long deadline = now_ms() + 5000;
    while (!tc_child_poll(child, now_ms()) && now_ms() < deadline)
        usleep(1000);
    if (!child->exited || child->output >= 0 || tc_child_ok(child) != successful)
        fprintf(stderr, "child %ld: exited=%d output=%d overflow=%d status=%#x\n", (long)child->pid,
                child->exited, child->output, child->overflow, child->status);
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
        alarm(12); /* Outlives both of the case's 5 s waits; main's alarm is 15 s. */
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
    driver = getpid();
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
        long long deadline = now_ms() + 5000;
        int done = 0;
        assert(tc_child_fork(&a, orphan_worker, NULL, NULL, capture, sizeof(capture), 0) == 0);
        /* The worker closes its copy of the output pipe after it starts. On a
         * loaded host the direct child can exit first; only once the output
         * has closed too does the remaining group start the owner's stop. */
        while (!(a.exited && a.output < 0) && now_ms() < deadline) {
            done = tc_child_poll(&a, now_ms());
            usleep(1000);
        }
        assert(a.exited && a.output < 0 && a.stopping && !done && a.used == sizeof(worker));
        memcpy(&worker, capture, sizeof(worker));
        assert(kill(worker, 0) == 0);
        /* Losing the direct parent must not authorize replacing binaries or
         * wiping locks while its old worker still owns this process group. */
        tc_child_poll(&a, a.deadline + 1);
        deadline = now_ms() + 5000;
        while (!(done = tc_child_poll(&a, now_ms())) && now_ms() < deadline)
            usleep(1000);
        assert(done);
        tc_child_close(&a);
    } else if (!strcmp(argv[1], "term_before_reset")) {
        /* A stop that reaches the child before its handler reset must still
         * stop it, not run this driver's handler there and be lost while the
         * child execs a long-running program. */
        char *args[] = {"/bin/sleep", "10", NULL};
        hold_child_setup = 1;
        assert(tc_child_exec(&a, args, NULL) == 0);
        assert(kill(a.pid, SIGTERM) == 0);
        finish(&a, 0);
        assert(WIFSIGNALED(a.status) && WTERMSIG(a.status) == SIGTERM);
        assert(parent_signals == 0);
        tc_child_close(&a);
    } else if (!strcmp(argv[1], "stop") || !strcmp(argv[1], "drain")) {
        long long started = now_ms(), limit = started + 5000;
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
    } else if (!strcmp(argv[1], "wait_until")) {
        /* The supervisor loops' select(). Lower bounds only where the wait
         * is the point; upper bounds are loose for a loaded host. */
        int fds[2];
        fd_set reads;
        long long start;
        struct sigaction action;
        struct itimerval soon = {{0, 0}, {0, 100000}};
        assert(pipe(fds) == 0);
        /* A quiet descriptor waits out the deadline and is not reported. */
        FD_ZERO(&reads);
        FD_SET(fds[0], &reads);
        start = now_ms();
        assert(tc_wait_until(&reads, fds[0], start, start + 50) == 0);
        assert(now_ms() - start >= 40 && !FD_ISSET(fds[0], &reads));
        /* A readable one answers at once. */
        assert(write(fds[1], "x", 1) == 1);
        FD_ZERO(&reads);
        FD_SET(fds[0], &reads);
        start = now_ms();
        assert(tc_wait_until(&reads, fds[0], start, start + 10000) == 1);
        assert(FD_ISSET(fds[0], &reads) && now_ms() - start < 5000);
        /* A deadline already past polls without blocking. */
        FD_ZERO(&reads);
        start = now_ms();
        assert(tc_wait_until(&reads, -1, start, start - 5000) == 0);
        assert(now_ms() - start < 5000);
        /* A signal ends the wait with 0 and nothing reported ready. */
        assert(read(fds[0], capture, 1) == 1);
        alarm(0);
        memset(&action, 0, sizeof(action));
        action.sa_handler = interrupt_wait;
        sigemptyset(&action.sa_mask);
        assert(sigaction(SIGALRM, &action, NULL) == 0);
        assert(setitimer(ITIMER_REAL, &soon, NULL) == 0);
        FD_ZERO(&reads);
        FD_SET(fds[0], &reads);
        start = now_ms();
        assert(tc_wait_until(&reads, fds[0], start, start + 10000) == 0);
        assert(now_ms() - start < 5000 && !FD_ISSET(fds[0], &reads));
        signal(SIGALRM, SIG_DFL);
        alarm(15);
        /* A closed descriptor is an error, with select's errno. */
        close(fds[0]);
        FD_ZERO(&reads);
        FD_SET(fds[0], &reads);
        errno = 0;
        start = now_ms();
        assert(tc_wait_until(&reads, fds[0], start, start + 10000) == -1 && errno == EBADF);
        close(fds[1]);
    } else
        return 2;
    return 0;
}
