#include "log.h"
#include <sys/stat.h>

int tc_log_trim(const char *path) {
    unsigned char tail[16384];
    struct stat st;
    int fd = open(path, O_RDWR | O_NOFOLLOW), result = 0;
    ssize_t length;
    if (fd < 0)
        return errno == ENOENT ? 0 : -1;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode)) {
        close(fd);
        return -1;
    }
    if (st.st_size > 32768) {
        /* Keep the same inode: live writers retain their open descriptors.
         * Logs are diagnostics, so concurrent lines may be lost during this
         * bounded trim; no temporary file or daemon restart is required. */
        length = pread(fd, tail, sizeof(tail), st.st_size - sizeof(tail));
        if (length < 0 || pwrite(fd, tail, length, 0) != length || ftruncate(fd, length))
            result = -1;
    }
    close(fd);
    return result;
}
static int timestamped_vfprintf(FILE *stream, const char *format, va_list ap);
void timestamped_perror(const char *message);

/* Writes size bytes to fd, across short writes and EINTR. */
static int write_all(int fd, const char *data, size_t size) {
    while (size) {
        ssize_t n = write(fd, data, size);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            return -1;
        data += n;
        size -= (size_t)n;
    }
    return 0;
}

/* Every line gets the timestamp. Lines are gathered into one buffer and go
 * out with one write(2) per buffer-full, after anything stdio still holds:
 * the manager and its jobs append to one runtime.log (O_APPEND), and writes
 * of a byte at a time from two processes interleave into lines nothing can
 * parse. stdio alone would not do: unbuffered streams write at most BUFSIZ
 * (1 KiB on NetBSD) at a time. */
static int timestamped_write_message(FILE *stream, const char *message) {
    char stamp[32], out[4096];
    size_t used = 0, stamp_length = 0;
    time_t now = time(NULL);
    struct tm *tm_info = localtime(&now);
    int fd = fileno(stream), line_start = 1;

    if (tm_info != NULL && (stamp_length = strftime(stamp, sizeof(stamp) - 1, "%Y-%m-%d %H:%M:%S", tm_info)) > 0)
        stamp[stamp_length++] = ' ';
    if (fflush(stream) != 0 || fd < 0)
        return -1;
    for (; *message != '\0'; message++) {
        /* Room for a stamp and one byte: a line longer than the buffer goes
         * out in pieces, each still whole. */
        if (used + stamp_length + 1 > sizeof(out)) {
            if (write_all(fd, out, used) != 0)
                return -1;
            used = 0;
        }
        if (line_start) {
            memcpy(out + used, stamp, stamp_length);
            used += stamp_length;
        }
        out[used++] = *message;
        line_start = *message == '\n';
    }
    return write_all(fd, out, used);
}

static int timestamped_vfprintf(FILE *stream, const char *format, va_list ap) {
    char stack_message[4096];
    int result;

    if (stream != stderr && stream != stdout) {
        return vfprintf(stream, format, ap);
    }

    result = vsnprintf(stack_message, sizeof(stack_message), format, ap);
    if (result < 0) {
        return result;
    }
    if ((size_t)result >= sizeof(stack_message)) {
        stack_message[sizeof(stack_message) - 2] = '\n';
        stack_message[sizeof(stack_message) - 1] = '\0';
    }

    if (timestamped_write_message(stream, stack_message) != 0) {
        return -1;
    }
    fflush(stream);
    return result;
}

int timestamped_fprintf(FILE *stream, const char *format, ...) {
    va_list ap;
    int result;

    va_start(ap, format);
    result = timestamped_vfprintf(stream, format, ap);
    va_end(ap);
    return result;
}

void timestamped_perror(const char *message) {
    int saved_errno = errno;

    if (message != NULL && message[0] != '\0') {
        timestamped_fprintf(stderr, "%s: %s\n", message, strerror(saved_errno));
    } else {
        timestamped_fprintf(stderr, "%s\n", strerror(saved_errno));
    }
}
