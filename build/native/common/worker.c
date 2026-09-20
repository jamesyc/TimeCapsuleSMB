#include "worker.h"
#include "acp.h"
#include "parent.h"
#include "process.h"
#include <sys/stat.h>

static volatile sig_atomic_t stopped;
static int parent_fd = -1;
static void stop(int sig) {
    (void)sig;
    stopped = 1;
    acp_stop_requested = 1;
}

void tc_worker_begin(const char *operation) {
    stopped = 0;
    acp_stop_requested = 0;
    parent_fd = tc_parent_pipe();
    signal(SIGTERM, stop);
    signal(SIGINT, stop);
    signal(SIGPIPE, SIG_IGN);
#if defined(__NetBSD__)
    setproctitle("role=job %s", operation);
#else
    (void)operation;
#endif
}
int tc_worker_cancelled(void) {
    if (stopped || !tc_parent_alive(parent_fd)) {
        stop(0);
        return 1;
    }
    return 0;
}

static int command(char *const argv[], char *output, size_t capacity, unsigned timeout_seconds) {
    struct tc_child child = {0};
    long long now = acp_monotonic_ms(), forced_at = 0;
    int result;
    if (output && capacity)
        output[0] = 0;
    if (now < 0 || tc_worker_cancelled() ||
        (output ? tc_child_exec_capture(&child, argv, output, capacity - 1, 0)
                : tc_child_exec(&child, argv, NULL)))
        return -1;
    child.deadline = now + (long long)timeout_seconds * 1000;
    for (;;) {
        now = acp_monotonic_ms();
        if (tc_worker_cancelled())
            tc_child_stop(&child, now, 1);
        if (tc_child_poll(&child, now))
            break;
        if (child.stopping && !forced_at)
            forced_at = now + 13000;
        if (forced_at && now >= forced_at) {
            /* An unkillable kernel waiter requires device recovery. Bound
             * this worker too; never start another command from this job. */
            fprintf(stderr, "command %s did not stop\n", argv[0]);
            tc_child_close(&child);
            stop(0);
            return -1;
        }
        usleep(10000);
    }
    result = tc_child_ok(&child) && !child.stopping ? 0 : -1;
    if (output)
        output[child.used] = 0;
    if (result)
        fprintf(stderr, "command %s failed or timed out (exit=%d signal=%d)\n", argv[0],
                WIFEXITED(child.status) ? WEXITSTATUS(child.status) : -1,
                WIFSIGNALED(child.status) ? WTERMSIG(child.status) : 0);
    tc_child_close(&child);
    return result;
}
int tc_command_run(char *const argv[], unsigned timeout_seconds) {
    return command(argv, NULL, 0, timeout_seconds);
}
int tc_command_capture(char *const argv[], char *out, size_t capacity, unsigned timeout_seconds) {
    if (capacity < 2)
        return -1;
    return command(argv, out, capacity, timeout_seconds);
}

int tc_worker_result(const void *data, size_t length) {
    const unsigned char *bytes = data;
    while (length) {
        ssize_t n;
        if (tc_worker_cancelled())
            return -1;
        n = write(STDOUT_FILENO, bytes, length);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            return -1;
        bytes += n;
        length -= n;
    }
    return 0;
}

int tc_make_dir(const char *path, mode_t mode) {
    struct stat st;
    if (tc_worker_cancelled())
        return -1;
    if (!mkdir(path, mode))
        return 0;
    if (errno == EEXIST && !lstat(path, &st)) {
        if (S_ISDIR(st.st_mode))
            return 0;
        errno = ENOTDIR;
    }
    fprintf(stderr, "directory unavailable: %s: %s\n", path, strerror(errno));
    return -1;
}

int tc_copy_file(const char *source, const char *destination, mode_t mode) {
    unsigned char buffer[32768];
    struct stat before, after, output_stat;
    int input = -1, output = -1, result = -1;
    off_t total = 0;
    ssize_t count;
    const char *operation = "open source";
    int saved_errno;
    if (tc_worker_cancelled())
        return -1;
    input = open(source, O_RDONLY);
    if (input < 0 || fstat(input, &before))
        goto out;
    if (!S_ISREG(before.st_mode)) {
        errno = EINVAL;
        goto out;
    }
    /* Callers pass a prepared RAM filename, never a live executable or user
     * path. Publication happens only after its owner has validated this job. */
    operation = "prepare destination";
    if (unlink(destination) && errno != ENOENT)
        goto out;
    output = open(destination, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (output < 0)
        goto out;
    for (;;) {
        size_t offset = 0;
        if (tc_worker_cancelled()) {
            errno = ECANCELED;
            goto out;
        }
        operation = "read";
        count = read(input, buffer, sizeof(buffer));
        if (count < 0 && errno == EINTR)
            continue;
        if (count < 0)
            goto out;
        if (!count)
            break;
        while (offset < (size_t)count) {
            operation = "write";
            ssize_t n = write(output, buffer + offset, count - offset);
            if (n < 0 && errno == EINTR)
                continue;
            if (n <= 0)
                goto out;
            offset += n;
        }
        total += count;
    }
    operation = "verify copied bytes";
    if (fstat(input, &after) || fstat(output, &output_stat))
        goto out;
    if (total != before.st_size || total != after.st_size || total != output_stat.st_size ||
        before.st_dev != after.st_dev || before.st_ino != after.st_ino || before.st_mtime != after.st_mtime) {
        errno = ESTALE;
        goto out;
    }
    operation = "chmod";
    if (fchmod(output, mode))
        goto out;
    operation = "flush";
    if (fsync(output))
        goto out;
    operation = "close";
    if (close(output)) {
        output = -1;
        goto out;
    }
    output = -1;
    result = 0;
out:
    saved_errno = errno;
    if (input >= 0)
        close(input);
    if (output >= 0)
        close(output);
    if (result) {
        fprintf(stderr, "copy %s failed: %s -> %s: %s\n", operation, source, destination,
                strerror(saved_errno));
        unlink(destination);
    }
    errno = saved_errno;
    return result;
}
