#include "process.h"
#include <sys/resource.h>

static int pipe_setup(int fds[2], int nonblocking_read) {
    int flags;
    if (pipe(fds))
        return -1;
    flags = fcntl(fds[0], F_GETFL);
    if (flags < 0 || (nonblocking_read && fcntl(fds[0], F_SETFL, flags | O_NONBLOCK)) ||
        fcntl(fds[0], F_SETFD, FD_CLOEXEC) || fcntl(fds[1], F_SETFD, FD_CLOEXEC)) {
        close(fds[0]);
        close(fds[1]);
        return -1;
    }
    return 0;
}

void tc_close_other_fds(int keep) {
    long limit = sysconf(_SC_OPEN_MAX);
    int fd;
    if (limit < 0)
        limit = 1024;
    for (fd = 3; fd < limit; fd++)
        if (fd != keep)
            close(fd);
}

int tc_child_fork(struct tc_child *child, tc_child_fn function, void *data, const char *log, void *capture,
                  size_t capacity, long long deadline) {
    int life[2], output[2] = {-1, -1};
    pid_t pid;
    if (pipe_setup(life, 0))
        return -1;
    if (capacity && pipe_setup(output, 1)) {
        close(life[0]);
        close(life[1]);
        return -1;
    }
    pid = fork();
    if (pid == 0) {
        int fd;
        /* smbd's atexit handler sends kill(0,SIGTERM). Establish isolation
         * before calling any child code, including the foreground exec. */
        if (setpgid(0, 0))
            _exit(126);
        signal(SIGTERM, SIG_DFL);
        signal(SIGINT, SIG_DFL);
        signal(SIGHUP, SIG_DFL);
        signal(SIGCHLD, SIG_DFL);
        signal(SIGPIPE, SIG_DFL);
        if (dup2(life[0], STDIN_FILENO) < 0 || fcntl(STDIN_FILENO, F_SETFD, 0))
            _exit(126);
        fd = open(log ? log : "/dev/null", O_WRONLY | O_CREAT | O_APPEND, 0600);
        if (fd < 0 || dup2(fd, STDERR_FILENO) < 0 || dup2(capacity ? output[1] : fd, STDOUT_FILENO) < 0)
            _exit(126);
        tc_close_other_fds(-1);
        _exit(function(data));
    }
    close(life[0]);
    if (output[1] >= 0)
        close(output[1]);
    if (pid < 0) {
        close(life[1]);
        if (output[0] >= 0)
            close(output[0]);
        return -1;
    }
    memset(child, 0, sizeof(*child));
    child->pid = child->group = pid;
    child->lifetime = life[1];
    child->output = output[0];
    child->allow_kill = 1;
    child->capture = capture;
    child->capacity = capacity;
    child->deadline = deadline;
    return 0;
}

static int execute(void *data) {
    char *const *argv = data;
    execv(argv[0], argv);
    fprintf(stderr, "exec %s: %s\n", argv[0], strerror(errno));
    return 127;
}
int tc_child_exec(struct tc_child *child, char *const argv[], const char *log) {
    return tc_child_fork(child, execute, (void *)argv, log, NULL, 0, 0);
}
int tc_child_exec_capture(struct tc_child *child, char *const argv[], void *out, size_t capacity,
                          long long deadline) {
    return tc_child_fork(child, execute, (void *)argv, NULL, out, capacity, deadline);
}

void tc_child_prepare(const struct tc_child *child, fd_set *reads, int *maxfd, long long *deadline) {
    if (child->output >= 0 && child->group) {
        FD_SET(child->output, reads);
        if (child->output > *maxfd)
            *maxfd = child->output;
    }
    if (child->group && child->deadline > 0 && (*deadline < 0 || child->deadline < *deadline))
        *deadline = child->deadline;
}

void tc_child_stop(struct tc_child *child, long long now, int allow_kill) {
    if (!child->group || child->stopping)
        return;
    child->stopping = 1;
    child->allow_kill = allow_kill;
    child->deadline = now + 10000;
    /* Signal the owner first so discovery withdraws registrations and
     * telemetry drains its signed job. Only a bounded forced stop targets
     * descendants, and that escalation is forbidden for telemetry. */
    if (child->pid)
        kill(child->pid, SIGTERM);
    if (child->lifetime >= 0) {
        close(child->lifetime);
        child->lifetime = -1;
    }
}

static void capture_output(struct tc_child *child) {
    unsigned char buffer[4096];
    ssize_t n;
    if (child->output < 0)
        return;
    unsigned reads = 0;
    while (reads++ < 8) {
        n = read(child->output, buffer, sizeof(buffer));
        if (n < 0 && errno == EINTR)
            continue;
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
            return;
        if (n <= 0) {
            if (n < 0)
                child->overflow = 1;
            close(child->output);
            child->output = -1;
            return;
        }
        if ((size_t)n > child->capacity - child->used) {
            child->overflow = 1;
            close(child->output);
            child->output = -1;
            return;
        } else {
            memcpy(child->capture + child->used, buffer, n);
            child->used += n;
        }
    }
}

int tc_child_poll(struct tc_child *child, long long now) {
    pid_t waited;
    if (!child->group)
        return 0;
    capture_output(child);
    if (child->overflow && !child->stopping)
        tc_child_stop(child, now, 1);
    if (child->deadline > 0 && now >= child->deadline) {
        if (!child->stopping)
            tc_child_stop(child, now, child->allow_kill);
        else {
            if (child->allow_kill) {
                /* The group's descendants may still own a capture pipe. */
                kill(-child->group, SIGKILL);
                if (child->pid)
                    kill(child->pid, SIGKILL);
                child->overflow = 1;
            }
            child->deadline = now + 1000;
        }
    }
    if (!child->exited) {
        do {
            waited = waitpid(child->pid, &child->status, WNOHANG);
        } while (waited < 0 && errno == EINTR);
        if (waited == child->pid) {
            child->exited = 1;
            child->pid = 0;
        } else if (waited < 0) {
            child->exited = 1;
            child->pid = 0;
            child->overflow = 1;
        }
    }
    /* An exited smbd parent may still have workers draining. Never release
     * its generation (or clear Samba locks) while its group remains alive. */
    if (child->exited && child->output < 0) {
        if (kill(-child->group, 0) < 0 && errno == ESRCH)
            return 1;
        if (!child->stopping)
            tc_child_stop(child, now, child->allow_kill);
    }
    return 0;
}
int tc_child_ok(const struct tc_child *child) {
    return child->exited && !child->overflow && WIFEXITED(child->status) && WEXITSTATUS(child->status) == 0;
}
void tc_child_close(struct tc_child *child) {
    if (child->lifetime >= 0 && child->group)
        close(child->lifetime);
    if (child->output >= 0 && child->group)
        close(child->output);
    memset(child, 0, sizeof(*child));
    child->lifetime = child->output = -1;
}
