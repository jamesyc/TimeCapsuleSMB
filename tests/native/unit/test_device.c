#include "device_faults.h"
#include <assert.h>
#include <syslog.h>

volatile sig_atomic_t telemetry_stop;
static const char *scenario;
static pid_t owner, collector;
static int injected, forks, pipe_calls, fd_sets, clocks, waits;

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
    if (collector > 0 && (once("cancel_after_fork") || once("cancel_before_group"))) telemetry_stop = 1;
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
    return clock_gettime(clock, value);
}

int test_select(int count, fd_set *readable, fd_set *writable, fd_set *errors, struct timeval *timeout) {
    if (once("select_error")) { errno = EBADF; return -1; }
    if (once("select_eintr")) { errno = EINTR; return -1; }
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
    int before, result, status, success, repeat;
    char output[256];
    pid_t guard;
    assert(argc == 2);
    scenario = argv[1]; owner = getpid();
    openlog("telemetry-collector-test", LOG_NDELAY, LOG_USER);
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
              !strcmp(scenario, "read_eagain") || !strcmp(scenario, "wait_eintr");
    if (!strcmp(scenario, "cancel_before_fork")) telemetry_stop = 1;
    /* Repeated calls catch descriptors retained within a live scheduler. */
    for (repeat = 0; repeat < 3; repeat++) {
        injected = forks = pipe_calls = fd_sets = clocks = waits = 0;
        collector = 0;
        strcpy(output, "stale value must not escape");
        result = read_acp_value("syAP", output, sizeof(output));
        assert(result == (success ? ACP_OK : ACP_ABORT));
        assert(success ? !strcmp(output, "0x77") : output[0] == '\0');
        assert(kill(guard, 0) == 0);
        if (!strcmp(scenario, "cancel_before_fork")) assert(!pipe_calls && !forks);
        else if (strcmp(scenario, "normal") && strcmp(scenario, "byte_reads") &&
                 strcmp(scenario, "child_group")) assert(injected);
        if (!strcmp(scenario, "reap_stuck")) {
            assert(telemetry_stop && waits > 0);
            /* The fault hid child readiness. Reap it with the real syscall so
             * the test itself leaves no zombie; production had to return. */
            assert(waitpid(collector, &status, 0) == collector);
            assert(WIFSIGNALED(status) && WTERMSIG(status) == SIGKILL);
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
