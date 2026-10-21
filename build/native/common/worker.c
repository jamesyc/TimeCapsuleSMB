#include "worker.h"
#include "acp.h"
#include "parent.h"
#include "process.h"
#include <limits.h>
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
    acp_set_scope(1, tc_worker_cancelled);
    signal(SIGTERM, stop);
    signal(SIGINT, stop);
    signal(SIGPIPE, SIG_IGN);
#if defined(__NetBSD__)
    setproctitle("role=job %s", operation);
#else
    (void)operation;
#endif
}
int tc_worker_finish(int result) {
    /* A dead manager cannot reap/kill our remaining commands. This group was
     * created for this short-lived job, never Apple's boot or telemetry group. */
    if (parent_fd >= 0 && !tc_parent_alive(parent_fd) && getpgrp() == getpid())
        kill(-getpgrp(), SIGKILL);
    return result;
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
        tc_command_exec(&child, argv, output, output ? capacity - 1 : 0))
        return -1;
    child.deadline = now + (long long)timeout_seconds * 1000;
    for (;;) {
        now = acp_monotonic_ms();
        if (tc_worker_cancelled())
            tc_child_stop(&child, now, 1);
        if (tc_child_poll(&child, now))
            break;
        if (child.stopping && !forced_at)
            forced_at = now + TC_CHILD_GRACE_MS + 3000;
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
    if (child.stopping)
        stop(0); /* Do not issue another command after losing this operation. */
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

static uint32_t crc32_update(uint32_t crc, const unsigned char *bytes, size_t length) {
    static uint32_t table[256];
    uint32_t i, bit;
    if (!table[1])
        for (i = 0; i < 256; i++) {
            uint32_t c = i;
            for (bit = 0; bit < 8; bit++)
                c = c & 1 ? 0xEDB88320u ^ (c >> 1) : c >> 1;
            table[i] = c;
        }
    crc = ~crc;
    while (length--)
        crc = table[(crc ^ *bytes++) & 0xff] ^ (crc >> 8);
    return ~crc;
}

#ifdef TC_NATIVE_TEST
/* A host-test fault's setting: the variable's value or, for "@FILE", what
 * FILE holds, so a test clears the fault while a process runs by removing
 * FILE. NULL for none. */
static const char *test_fault(const char *variable, char *buffer, size_t size) {
    const char *value = getenv(variable);
    FILE *file;
    size_t n;
    if (!value || value[0] != '@')
        return value;
    if (!(file = fopen(value + 1, "r")))
        return NULL;
    n = fread(buffer, 1, size - 1, file);
    fclose(file);
    while (n && buffer[n - 1] == '\n')
        n--;
    buffer[n] = 0;
    return buffer;
}
int tc_test_eio(const char *variable, const char *path) {
    char buffer[1024];
    const char *failing = test_fault(variable, buffer, sizeof(buffer));
    if (failing && !strcmp(failing, path)) {
        errno = EIO;
        return 1;
    }
    return 0;
}
/* How long each read of path takes on host tests' simulated clock
 * (TC_TEST_SLOW_READ, "MS:PATH"), or -1 to read the real clock. */
static long long test_read_ms(const char *path) {
    char buffer[1024], *end;
    const char *value = test_fault("TC_TEST_SLOW_READ", buffer, sizeof(buffer));
    long long ms;
    if (!value)
        return -1;
    ms = strtoll(value, &end, 10);
    return *end == ':' && !strcmp(end + 1, path) ? ms : -1;
}
#endif
/* A payload file being read: what it is (fstat when opened), when reading it
 * started, and how far it got. A disk that has not returned the rest within
 * TC_PAYLOAD_READ_BUDGET_MS is failing; the last read, the one that finds the
 * end, is never refused. */
struct payload_reader {
    int fd;
    const char *path;
    struct stat st;
    long long started, reads;
    off_t offset;
};
/* Opens path; -1 with errno (EIO when the disk could not read what the file
 * is). */
static int reader_open(struct payload_reader *r, const char *path) {
    r->path = path;
    r->started = acp_monotonic_ms();
    r->reads = 0;
    r->offset = 0;
    r->fd = tc_test_eio("TC_TEST_EIO_OPEN", path) ? -1 : open(path, O_RDONLY);
    if (r->fd >= 0 && fstat(r->fd, &r->st)) {
        int saved = errno;
        close(r->fd);
        r->fd = -1;
        errno = saved;
    }
    return r->fd;
}
/* How long the file has been read so far, -1 when the clock cannot be read
 * (no budget then). Host tests' TC_TEST_SLOW_READ pins it on a simulated
 * clock, so a stall needs no real wait. */
static long long reader_elapsed(const struct payload_reader *r) {
    long long now;
#ifdef TC_NATIVE_TEST
    long long ms = test_read_ms(r->path);
    if (ms >= 0)
        return r->reads * ms;
#endif
    now = acp_monotonic_ms();
    return now < 0 || r->started < 0 ? -1 : now - r->started;
}
/* One read, refused with ETIMEDOUT once the budget is spent while bytes
 * remain. A read already waiting on the disk cannot be stopped; a failing
 * disk returns a chunk at a time, and the next read sees the budget. */
static ssize_t reader_read(struct payload_reader *r, void *buffer, size_t length) {
    ssize_t count;
    if (r->offset < r->st.st_size && reader_elapsed(r) >= TC_PAYLOAD_READ_BUDGET_MS) {
        errno = ETIMEDOUT;
        return -1;
    }
    r->reads++;
    count = tc_test_eio("TC_TEST_EIO", r->path) ? -1 : read(r->fd, buffer, length);
    if (count > 0)
        r->offset += count;
    return count;
}
long long tc_read_damage(int error) {
    return error == EIO ? TC_COPY_UNREADABLE : error == ETIMEDOUT ? TC_COPY_STALLED : 0;
}

int tc_file_crc32(const char *path, uint32_t *crc) {
    unsigned char buffer[32768];
    ssize_t count;
    struct payload_reader reader;
    int fd = reader_open(&reader, path), saved;
    *crc = 0;
    if (fd < 0)
        return -1;
    while ((count = reader_read(&reader, buffer, sizeof(buffer))) != 0) {
        if (count < 0 && errno == EINTR)
            continue;
        if (count >= 0 && tc_worker_cancelled()) {
            /* A stalled disk returns a chunk at a time: stop between them. */
            count = -1;
            errno = ECANCELED;
        }
        if (count < 0) {
            saved = errno;
            close(fd);
            errno = saved;
            return -1;
        }
        *crc = crc32_update(*crc, buffer, count);
    }
    close(fd);
    return 0;
}

int tc_copy_file(const char *source, const char *destination, mode_t mode, long long crc32, long long *read_out,
                 long long *elapsed_ms) {
    unsigned char buffer[32768];
    struct stat before, after, output_stat;
    int input = -1, output = -1, result = -1, mismatch = 0;
    off_t total = 0, size = -1;
    long long started = acp_monotonic_ms();
    struct payload_reader reader;
    /* What a read that failed said about the disk (tc_read_damage), 0 for nothing. */
    long long unread = 0;
    ssize_t count;
    /* Of the bytes read from the source, which must be those deploy wrote. */
    uint32_t read_crc = 0;
    const char *operation = "open source";
    int saved_errno;
    if (tc_worker_cancelled())
        return -1;
    input = reader_open(&reader, source);
    if (input < 0) {
        /* The disk could not read what the file is: damage, as a failed
         * read of its bytes is. */
        unread = tc_read_damage(errno);
        goto out;
    }
    before = reader.st;
    size = before.st_size;
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
            /* Not the read or write before it: the manager stopped this job. */
            operation = "cancel";
            errno = ECANCELED;
            goto out;
        }
        operation = "read";
        count = reader_read(&reader, buffer, sizeof(buffer));
        if (count < 0 && errno == EINTR)
            continue;
        if (count < 0) {
            /* The disk could not return the file's bytes, or only so slowly
             * that the budget ran out: damage, as much as other bytes
             * would be. */
            unread = tc_read_damage(errno);
            goto out;
        }
        if (!count)
            break;
        read_crc = crc32_update(read_crc, buffer, count);
        while (offset < (size_t)count) {
            operation = "write";
            ssize_t n = write(output, buffer + offset, count - offset);
            if (n < 0 && errno == EINTR)
                continue;
            if (n <= 0)
                goto out;
            offset += n;
            total += n;
        }
    }
    operation = "verify copied bytes";
    if (fstat(input, &after) || fstat(output, &output_stat))
        goto out;
    if (total != before.st_size || total != after.st_size || total != output_stat.st_size ||
        before.st_dev != after.st_dev || before.st_ino != after.st_ino || before.st_mtime != after.st_mtime) {
        errno = ESTALE;
        goto out;
    }
    if (crc32 >= 0 && read_crc != (uint32_t)crc32) {
        /* The disk returned other bytes than deploy wrote. The launch checks
         * the RAM copy, which is what exec reads. */
        operation = "verify";
        mismatch = 1;
        errno = EIO;
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
    if (elapsed_ms)
        *elapsed_ms = reader_elapsed(&reader);
out:
    saved_errno = errno;
    if (input >= 0)
        close(input);
    if (output >= 0)
        close(output);
    if (result) {
        /* The bytes and time say whether the disk was slow or stuck (a 10 MB
         * smbd normally copies in seconds). The size is unknown when the
         * source could not be opened. */
        long long seconds = (acp_monotonic_ms() - started) / 1000;
        char checksums[64];
        const char *detail = strerror(saved_errno);
        if (mismatch) {
            snprintf(checksums, sizeof(checksums), "CRC-32 read %08x, deployed %08x", (unsigned)read_crc,
                     (unsigned)crc32);
            detail = checksums;
        }
        if (size < 0)
            fprintf(stderr, "copy %s failed: %s -> %s: %s (copied %lld bytes in %lld s)\n", operation, source,
                    destination, detail, (long long)total, seconds);
        else
            fprintf(stderr, "copy %s failed: %s -> %s: %s (copied %lld of %lld bytes in %lld s)\n", operation,
                    source, destination, detail, (long long)total, (long long)size, seconds);
        unlink(destination);
    }
    errno = saved_errno;
    if (read_out)
        *read_out = unread ? unread : mismatch ? (long long)read_crc : -1;
    return result && (mismatch || unread) ? TC_COPY_DISK_DAMAGED : result;
}
