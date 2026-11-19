/* Lines from several processes appending to one log at once stay whole
 * (common/log.c timestamped_fprintf), and a message of many lines that fills
 * the write buffer several times still stamps every line. */
#include "common/log.h"
#include <assert.h>
#include <sys/wait.h>

#define WRITERS 6
#define LINES 300

static int stamped(const char *line) {
    /* "YYYY-MM-DD HH:MM:SS " */
    return strlen(line) > 20 && line[4] == '-' && line[7] == '-' && line[10] == ' ' && line[13] == ':' &&
           line[16] == ':' && line[19] == ' ';
}
int main(void) {
    int fd = open("lines.log", O_RDWR | O_CREAT | O_TRUNC | O_APPEND, 0600), i, j;
    static char text[1 << 20];
    unsigned counts[WRITERS] = {0}, multi = 0;
    char *line, *save;
    ssize_t length;
    assert(fd >= 0 && dup2(fd, STDERR_FILENO) == STDERR_FILENO);
    for (i = 0; i < WRITERS; i++) {
        pid_t pid = fork();
        assert(pid >= 0);
        if (!pid) {
            /* A line long enough that byte-at-a-time writes would interleave. */
            for (j = 0; j < LINES; j++)
                timestamped_fprintf(stderr, "writer %d line %d %s end\n", i, j,
                                    "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyzabcdefghijklmnop");
            _exit(0);
        }
    }
    for (i = 0; i < WRITERS; i++) {
        int status;
        assert(wait(&status) > 0 && WIFEXITED(status) && !WEXITSTATUS(status));
    }
    /* 400 short lines: about 10 KB with stamps, several buffer-fulls. */
    {
        static char message[4096];
        size_t used = 0;
        for (j = 0; j < 400; j++)
            used += (size_t)snprintf(message + used, sizeof(message) - used, "m%03d\n", j);
        timestamped_fprintf(stderr, "%s", message);
    }
    length = pread(fd, text, sizeof(text) - 1, 0);
    assert(length > 0);
    text[length] = 0;
    for (line = strtok_r(text, "\n", &save); line; line = strtok_r(NULL, "\n", &save)) {
        int writer, number;
        char tail[128];
        assert(stamped(line));
        if (line[20] == 'm') {
            assert(strlen(line) == 24 && atoi(line + 21) == (int)multi);
            multi++;
            continue;
        }
        assert(sscanf(line + 20, "writer %d line %d %127s end", &writer, &number, tail) == 3);
        assert(writer >= 0 && writer < WRITERS && !strcmp(line + strlen(line) - 4, " end"));
        counts[writer]++;
    }
    for (i = 0; i < WRITERS; i++)
        assert(counts[i] == LINES);
    assert(multi == 400);
    return 0;
}
