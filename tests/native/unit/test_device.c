#include "device_faults.h"
#include <assert.h>
#include <syslog.h>

static const char *scenario;
static pid_t owner, collector;
static int injected, forks, pipe_calls, fd_sets, clocks, waits;
/* The deadline scenarios run the production 20 s ACP timeout on a clock that
 * only the collector's own select() waits move, so no test waits 20 s and a
 * loaded host cannot move the deadline. */
static struct timespec virtual_base;
static long long virtual_ms;
static int virtual_clock(void) {
    return getpid() == owner && !strncmp(scenario, "deadline_", 9);
}

static int once(const char *name) {
    if (getpid() == owner && !injected && !strcmp(scenario, name)) {
        injected = 1;
        return 1;
    }
    return 0;
}

int test_pipe(int fds[2]) {
    pipe_calls++;
    if (once("pipe")) { errno = EMFILE; return -1; }
    return pipe(fds);
}

pid_t test_fork(void) {
    forks++;
    if (once("fork")) { errno = EAGAIN; return -1; }
    collector = fork();
    if (collector > 0 && (once("cancel_after_fork") || once("cancel_before_group"))) acp_stop_requested = 1;
    return collector;
}

int test_fcntl(int fd, int command, ...) {
    int argument;
    va_list args;
    if (command == F_GETFL) return fcntl(fd, command);
    va_start(args, command); argument = va_arg(args, int); va_end(args);
    if (command == F_SETFL && once("nonblock")) { errno = EIO; return -1; }
    if (getpid() == owner && command == F_SETFD) {
        fd_sets++;
        if (fd_sets == 1 && once("read_cloexec")) { errno = EIO; return -1; }
        if (fd_sets == 2 && once("write_cloexec")) { errno = EIO; return -1; }
    }
    return fcntl(fd, command, argument);
}

int test_setpgid(pid_t pid, pid_t group) {
    /* The parent must not race the child's group creation. */
    assert(getpid() != owner && pid == 0 && group == 0);
    if (!strcmp(scenario, "child_group")) { errno = EPERM; return -1; }
    /* Exercise cancellation while the child still shares the guard's group. */
    if (!strcmp(scenario, "cancel_before_group")) sleep(5);
    return setpgid(pid, group);
}

int test_clock_gettime(clockid_t clock, struct timespec *value) {
    assert(clock == CLOCK_MONOTONIC);
    clocks++;
    if (clocks == 1 && once("initial_clock")) { errno = EIO; return -1; }
    if (clocks == 2 && once("running_clock")) { errno = EIO; return -1; }
    if (!virtual_clock()) return clock_gettime(clock, value);
    if (!virtual_base.tv_sec && clock_gettime(clock, &virtual_base)) return -1;
    value->tv_sec = virtual_base.tv_sec + virtual_ms / 1000;
    value->tv_nsec = virtual_base.tv_nsec + (virtual_ms % 1000) * 1000000;
    if (value->tv_nsec >= 1000000000) { value->tv_sec++; value->tv_nsec -= 1000000000; }
    return 0;
}

int test_select(int count, fd_set *readable, fd_set *writable, fd_set *errors, struct timeval *timeout) {
    if (once("select_error")) { errno = EBADF; return -1; }
    if (once("select_eintr")) { errno = EINTR; return -1; }
    if (virtual_clock() && !strcmp(scenario, "deadline_success")) {
        /* The reply arrives 18 s into the wait. Real waits for the child to
         * write or exit leave the clock alone. */
        int ready = select(count, readable, writable, errors, timeout);
        if (ready > 0 && !injected) {
            injected = 1;
            virtual_ms += 18000;
        }
        return ready;
    } else if (virtual_clock() && !strcmp(scenario, "deadline_timeout")) {
        /* A hung ACP never becomes ready; each wait takes its full timeout. */
        struct timeval now = {0, 0};
        int ready = select(count, readable, writable, errors, &now);
        if (ready) return ready;
        injected = 1;
        virtual_ms += timeout->tv_sec * 1000LL + timeout->tv_usec / 1000;
        return 0;
    }
    return select(count, readable, writable, errors, timeout);
}

ssize_t test_read(int fd, void *buffer, size_t count) {
    if (once("read_error")) { errno = EIO; return -1; }
    if (once("read_eintr")) { errno = EINTR; return -1; }
    if (once("read_eagain")) { errno = EAGAIN; return -1; }
    if (!strcmp(scenario, "byte_reads")) count = 1;
    return read(fd, buffer, count);
}

pid_t test_waitpid(pid_t child, int *status, int options) {
    assert(child == collector);
    assert(options & WNOHANG); /* No blocking wait is allowed even after KILL. */
    waits++;
    if (!strcmp(scenario, "reap_stuck")) { injected = 1; return 0; }
    if (once("wait_eintr")) { errno = EINTR; return -1; }
    return waitpid(child, status, options);
}

static int open_fds(void) {
    int fd, count = 0;
    for (fd = 0; fd < 128; fd++) if (fcntl(fd, F_GETFD) >= 0) count++;
    return count;
}

int main(int argc, char **argv) {
    int before, result, status, success, repeat, timed;
    long long started, elapsed;
    char output[256];
    pid_t guard;
    assert(argc == 2);
    scenario = argv[1]; owner = getpid();
    openlog("acp-collector-test", LOG_NDELAY, LOG_USER);
    guard = fork();
    assert(guard >= 0);
    if (!guard) {
        /* Do not hide a driver assertion failure by keeping pytest's capture
         * pipes open after the driver exits. */
        close(STDIN_FILENO); close(STDOUT_FILENO); close(STDERR_FILENO);
        for (;;) pause();
    }
    /* The guard shares telemetry's group, outside the collector's group. */
    before = open_fds();
    success = !strcmp(scenario, "normal") || !strcmp(scenario, "byte_reads") ||
              !strcmp(scenario, "select_eintr") || !strcmp(scenario, "read_eintr") ||
              !strcmp(scenario, "read_eagain") || !strcmp(scenario, "wait_eintr") ||
              !strcmp(scenario, "deadline_success");
    if (!strcmp(scenario, "cancel_before_fork")) acp_stop_requested = 1;
    /* Repeated calls catch descriptors retained within a live scheduler. */
    for (repeat = 0; repeat < 3; repeat++) {
        injected = forks = pipe_calls = fd_sets = clocks = waits = 0;
        collector = 0;
        strcpy(output, "stale value must not escape");
        /* Only the deadline scenarios read the clock here: the clock faults
         * count the collector's own reads. */
        timed = !strncmp(scenario, "deadline_", 9);
        started = timed ? acp_monotonic_ms() : 0;
        result = read_acp_value("syAP", output, sizeof(output));
        elapsed = timed ? acp_monotonic_ms() - started : 0;
        assert(result == (success ? ACP_OK : ACP_ABORT));
        /* The production timeout: an 18 s reply is kept, and a hung ACP is
         * stopped at 20 s, not before and not after (the collector waits in
         * 100 ms steps). */
        if (!strcmp(scenario, "deadline_success")) assert(elapsed == 18000);
        if (!strcmp(scenario, "deadline_timeout")) assert(elapsed >= 20000 && elapsed <= 20100);
        assert(success ? !strcmp(output, "0x77") : output[0] == '\0');
        assert(kill(guard, 0) == 0);
        if (!strcmp(scenario, "cancel_before_fork")) assert(!pipe_calls && !forks);
        else if (strcmp(scenario, "normal") && strcmp(scenario, "byte_reads") &&
                 strcmp(scenario, "child_group")) assert(injected);
        if (!strcmp(scenario, "reap_stuck")) {
            assert(acp_stop_requested && waits > 0);
            /* The fault hid child readiness. Reap it with the real syscall so
             * the test itself leaves no zombie; production had to return. */
            assert(waitpid(collector, &status, 0) == collector);
            /* SIGTERM when a loaded host had not yet run the fixture's
             * signal(SIGTERM, SIG_IGN); the KILL then reached a zombie. */
            assert(WIFSIGNALED(status) && (WTERMSIG(status) == SIGKILL || WTERMSIG(status) == SIGTERM));
        } else if (collector > 0) {
            errno = 0;
            assert(waitpid(collector, &status, WNOHANG) == -1 && errno == ECHILD);
        }
        assert(open_fds() == before);
        if (!success) break;
    }
    kill(guard, SIGKILL);
    assert(waitpid(guard, &status, 0) == guard);
    return 0;
}
