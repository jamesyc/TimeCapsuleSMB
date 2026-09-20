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
TC_LOCAL void log_timestamp_prefix(FILE *stream);
TC_LOCAL int timestamped_write_message(FILE *stream, const char *message);
TC_LOCAL int timestamped_vfprintf(FILE *stream, const char *format, va_list ap);
void timestamped_perror(const char *message);
TC_LOCAL void log_timestamp_prefix(FILE *stream) {
    time_t now;
    struct tm *tm_info;
    char stamp[32];

    now = time(NULL);
    tm_info = localtime(&now);
    if (tm_info != NULL && strftime(stamp, sizeof(stamp), "%Y-%m-%d %H:%M:%S", tm_info) > 0) {
        fputs(stamp, stream);
        fputc(' ', stream);
    }
}

TC_LOCAL int timestamped_write_message(FILE *stream, const char *message) {
    const char *cursor;

    cursor = message;
    while (*cursor != '\0') {
        log_timestamp_prefix(stream);
        while (*cursor != '\0') {
            int ch = (unsigned char)*cursor++;
            if (fputc(ch, stream) == EOF) {
                return -1;
            }
            if (ch == '\n') {
                break;
            }
        }
    }
    return 0;
}

TC_LOCAL int timestamped_vfprintf(FILE *stream, const char *format, va_list ap) {
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
