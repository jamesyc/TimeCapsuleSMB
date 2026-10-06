#include "service.h"
#include <sys/stat.h>

/* smbd resolves its own hostname at every login (get_mydnsfullname; issue
 * #54), and a failed lookup is not cached, so every login waits on it. Map the
 * device hostname to 127.0.0.1 in Apple's /etc/hosts, which lives on the RAM
 * root and starts fresh at every boot.
 *
 * Our line is exactly "127.0.0.1\t<name> <name>.local", as the retired shell
 * manager also wrote it. Only lines of that exact form for another name are
 * removed after a rename; Apple's lines, comments and everything else are kept
 * byte for byte. The file is replaced through a temporary file and rename(),
 * and only when something changed. */

#define HOSTS_MAX 65536

void tc_hostname_read(char *out, size_t size) {
    out[0] = 0;
#ifdef TC_NATIVE_TEST
    /* Host tests cannot change the machine's hostname; they name a file. */
    const char *file = getenv("TC_TEST_HOSTNAME");
    if (file) {
        FILE *stream = fopen(file, "r");
        if (stream) {
            if (fgets(out, (int)size, stream))
                out[strcspn(out, "\r\n")] = 0;
            fclose(stream);
        }
        return;
    }
#endif
    if (gethostname(out, size))
        out[0] = 0;
    out[size - 1] = 0;
}

/* The name in one of our own lines, or 0 when the line is not ours. Doctor
 * repeats this and maps() in Python (device/probe.py); tests/native/test_hosts.py
 * checks that both read every line alike. */
static int our_line(const char *line, size_t length, char *name, size_t size) {
    const char *start, *space;
    size_t n;
    if (length < 11 || strncmp(line, "127.0.0.1\t", 10))
        return 0;
    start = line + 10;
    space = memchr(start, ' ', length - 10);
    if (!space)
        return 0;
    n = (size_t)(space - start);
    if (!n || n >= size || length - 10 != 2 * n + 7 || strncmp(space + 1, start, n) ||
        strncmp(space + 1 + n, ".local", 6))
        return 0;
    memcpy(name, start, n);
    name[n] = 0;
    return 1;
}

/* Whether a line (comments ignored) maps hostname or hostname.local. */
static int maps(const char *line, size_t length, const char *hostname, const char *local) {
    char copy[1024], *word, *save, *comment;
    if (length >= sizeof(copy))
        length = sizeof(copy) - 1;
    memcpy(copy, line, length);
    copy[length] = 0;
    if ((comment = strchr(copy, '#')))
        *comment = 0;
    word = strtok_r(copy, " \t\r", &save); /* address */
    while (word && (word = strtok_r(NULL, " \t\r", &save)))
        if (!strcmp(word, hostname) || !strcmp(word, local))
            return 1;
    return 0;
}

static int write_all(int fd, const char *data, size_t length) {
    while (length) {
        ssize_t n = write(fd, data, length);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0) {
            if (!n)
                errno = EIO;
            return -1;
        }
        data += n;
        length -= (size_t)n;
    }
    return 0;
}

int tc_hosts_update(const char *path, const char *hostname) {
    char local[272], temp[1024], *old = NULL, *out = NULL, stale[256];
    const char *slash = strrchr(path, '/');
    size_t used = 0, length = 0, pos = 0, capacity;
    mode_t mode = 0644;
    int fd, mapped = 0, changed = 0, saved;
    struct stat st;
    snprintf(local, sizeof(local), "%s.local", hostname);
    if (snprintf(temp, sizeof(temp), "%.*s.%s.tc", slash ? (int)(slash - path + 1) : 0, path,
                 slash ? slash + 1 : path) >= (int)sizeof(temp)) {
        errno = ENAMETOOLONG;
        return -1;
    }
    fd = open(path, O_RDONLY);
    if (fd >= 0) {
        /* Read to end of file rather than the size fstat reported, so contents
         * that grow meanwhile (or a pipe) are kept whole. */
        size_t size = 0;
        saved = fstat(fd, &st) ? errno : 0;
        if (!saved)
            mode = st.st_mode & 07777;
        while (!saved) {
            ssize_t n;
            if (length == size) {
                char *grown;
                if (size >= HOSTS_MAX) {
                    saved = EFBIG;
                    break;
                }
                size += 4096;
                if (!(grown = realloc(old, size + 1))) {
                    saved = ENOMEM;
                    break;
                }
                old = grown;
            }
            n = read(fd, old + length, size - length);
            if (n < 0 && errno == EINTR)
                continue;
            if (n < 0)
                saved = errno;
            else if (!n)
                break;
            else
                length += (size_t)n;
        }
        close(fd);
        if (saved) {
            free(old);
            errno = saved;
            return -1;
        }
    } else if (errno != ENOENT)
        return -1;
    /* Room for the old content plus one newline and our line. */
    capacity = length + strlen(hostname) * 2 + 32;
    if (!(out = malloc(capacity))) {
        free(old);
        errno = ENOMEM;
        return -1;
    }
    while (pos < length) {
        const char *line = old + pos, *end = memchr(line, '\n', length - pos);
        size_t line_length = end ? (size_t)(end - line) : length - pos;
        size_t span = end ? line_length + 1 : line_length;
        pos += span;
        if (our_line(line, line_length, stale, sizeof(stale)) && strcmp(stale, hostname)) {
            changed = 1;
            continue;
        }
        if (maps(line, line_length, hostname, local))
            mapped = 1;
        memcpy(out + used, line, span);
        used += span;
    }
    if (!mapped) {
        if (used && out[used - 1] != '\n')
            out[used++] = '\n';
        used += (size_t)snprintf(out + used, capacity - used, "127.0.0.1\t%s %s\n", hostname, local);
        changed = 1;
    }
    if (!changed) {
        free(old);
        free(out);
        return 0;
    }
    /* A leftover temporary file from an interrupted update is replaced. */
    fd = open(temp, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    saved = fd < 0 || write_all(fd, out, used) || fchmod(fd, mode) || fsync(fd) ? errno : 0;
    if (fd >= 0 && close(fd) && !saved)
        saved = errno;
    if (!saved && rename(temp, path))
        saved = errno;
    free(out);
    if (saved) {
        free(old);
        unlink(temp);
        errno = saved;
        return -1;
    }
    /* Log removals only once the new file is in place, one line per stale
     * mapping: scan the old contents again. */
    for (pos = 0; pos < length;) {
        const char *line = old + pos, *end = memchr(line, '\n', length - pos);
        size_t line_length = end ? (size_t)(end - line) : length - pos;
        pos += end ? line_length + 1 : line_length;
        if (our_line(line, line_length, stale, sizeof(stale)) && strcmp(stale, hostname))
            fprintf(stderr, "stage: removed the stale mapping for %s\n", stale);
    }
    free(old);
    if (!mapped)
        fprintf(stderr, "stage: mapped %s in %s\n", hostname, path);
    return 1;
}
