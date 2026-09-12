#include "device.h"
#include <syslog.h>
#if defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__)
#include <sys/sysctl.h>
#endif
void trim_line(char *value) {
    size_t len;

    if (value == NULL) {
        return;
    }
    while (*value != '\0' && isspace((unsigned char)*value)) {
        memmove(value, value + 1, strlen(value));
    }
    len = strlen(value);
    while (len > 0 && isspace((unsigned char)value[len - 1])) {
        value[--len] = '\0';
    }
}

static long long monotonic_milliseconds(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) return -1;
    return (long long)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static void stop_acp_child(pid_t child, int grouped) {
    int status, i;
    pid_t result;
    pid_t target = grouped ? -child : child;
    /* Keep the direct child unreaped until group signalling is complete, so
     * its PID cannot be reused while descendants still hold the pipe open. */
    kill(target, SIGTERM);
    for (i = 0; i < 10; i++) usleep(100000);
    kill(target, SIGKILL);
    for (i = 0; i < 20; i++) {
        result = waitpid(child, &status, WNOHANG);
        if (result == child || (result < 0 && errno == ECHILD)) return;
        if (result < 0 && errno != EINTR) break;
        usleep(50000);
    }
    /* Even reaping must not hang the scheduler. A kernel-stuck child cannot
     * be recovered here; exit the daemon instead of starting more collectors. */
    telemetry_stop = 1;
    fputs("telemetry: ACP child could not be reaped after SIGKILL\n", stderr);
    syslog(LOG_DAEMON | LOG_ERR, "telemetry: ACP child could not be reaped after SIGKILL");
}

int read_acp_value(const char *key, char *out, size_t out_len) {
    int output[2], flags, status, eof = 0, line_done = 0, grouped = 0;
    size_t used = 0;
    long long started, now;
    pid_t child, waited;
    const char *failure;

    if (!out_len) return ACP_ABORT;
    out[0] = '\0';
    if (telemetry_stop) return ACP_ABORT;
    started = monotonic_milliseconds();
    if (started < 0) { failure = "cannot read monotonic clock"; goto failed; }
    if (pipe(output)) { failure = "cannot create output pipe"; goto failed; }
    flags = fcntl(output[0], F_GETFL);
    if (flags < 0 || fcntl(output[0], F_SETFL, flags | O_NONBLOCK) ||
        fcntl(output[0], F_SETFD, FD_CLOEXEC) || fcntl(output[1], F_SETFD, FD_CLOEXEC)) {
        close(output[0]); close(output[1]);
        failure = "cannot configure output pipe"; goto failed;
    }
    child = fork();
    if (child == 0) {
        int null_fd;
        if (setpgid(0, 0)) _exit(126);
        signal(SIGTERM, SIG_DFL); signal(SIGINT, SIG_DFL); signal(SIGPIPE, SIG_DFL);
        if (dup2(output[1], STDOUT_FILENO) < 0 || fcntl(STDOUT_FILENO, F_SETFD, 0)) _exit(126);
        if (output[0] != STDOUT_FILENO) close(output[0]);
        if (output[1] != STDOUT_FILENO) close(output[1]);
        null_fd = open("/dev/null", O_RDWR);
        if (null_fd < 0 || dup2(null_fd, STDIN_FILENO) < 0 || dup2(null_fd, STDERR_FILENO) < 0) _exit(126);
        if (null_fd > STDERR_FILENO) close(null_fd);
        execl(TC_ACP_PATH, "acp", "-q", key, (char *)NULL);
        _exit(127);
    }
    close(output[1]);
    if (child < 0) {
        close(output[0]); failure = "cannot fork"; goto failed;
    }
    /* Both sides set the group to close the fork/exec race. EACCES means the
     * child already exec'd; ESRCH can mean it already exited. The child sets
     * its group before exec, and stays unreaped until group signalling ends. */
    if (setpgid(child, child) == 0 || errno == EACCES || errno == ESRCH) grouped = 1;
    else { failure = "cannot establish collector process group"; goto abort_child; }
    for (;;) {
        fd_set reads;
        struct timeval timeout;
        int ready;
        char chunk[256];
        ssize_t n;
        size_t i;
        now = monotonic_milliseconds();
        if (telemetry_stop) { failure = "cancelled"; goto abort_child; }
        if (now < 0) { failure = "cannot read monotonic clock"; goto abort_child; }
        if (now - started >= (long long)TC_ACP_TIMEOUT_SECONDS * 1000) {
            failure = "timed out"; goto abort_child;
        }
        if (eof) {
            waited = waitpid(child, &status, WNOHANG);
            if (waited == child) {
                close(output[0]);
                if (telemetry_stop) { out[0] = '\0'; return ACP_ABORT; }
                if (!WIFEXITED(status) || WEXITSTATUS(status) == 126 || WEXITSTATUS(status) == 127) {
                    failure = "collector setup, exec, or signal failure"; goto failed;
                }
                if (WEXITSTATUS(status) != 0) {
                    out[0] = '\0'; return ACP_UNAVAILABLE;
                }
                out[used] = '\0'; trim_line(out);
                return out[0] ? ACP_OK : ACP_UNAVAILABLE;
            }
            if (waited < 0 && errno != EINTR) {
                close(output[0]); failure = "cannot reap collector"; goto failed;
            }
        }
        FD_ZERO(&reads);
        if (!eof) FD_SET(output[0], &reads);
        timeout.tv_sec = 0; timeout.tv_usec = 100000;
        ready = select(eof ? 0 : output[0] + 1, &reads, NULL, NULL, &timeout);
        if (ready < 0) {
            if (errno == EINTR) continue;
            failure = "cannot wait for output"; goto abort_child;
        }
        if (!ready || eof) continue;
        n = read(output[0], chunk, sizeof(chunk));
        if (n == 0) { eof = 1; continue; }
        if (n < 0) {
            if (errno == EINTR || errno == EAGAIN) continue;
            failure = "cannot read output"; goto abort_child;
        }
        /* Capture the first line, but drain later output so it cannot block
         * the child. The same deadline covers reading and process exit. */
        for (i = 0; i < (size_t)n && !line_done; i++) {
            if (chunk[i] == '\n') line_done = 1;
            else if (!chunk[i] || used + 1 >= out_len) {
                failure = "invalid or oversized output"; goto abort_child;
            } else out[used++] = chunk[i];
        }
    }
abort_child:
    close(output[0]);
    stop_acp_child(child, grouped);
failed:
    out[0] = '\0';
    fprintf(stderr, "telemetry: ACP %s %s\n", key, failure);
    syslog(LOG_DAEMON | LOG_ERR, "telemetry: ACP %s %s", key, failure);
    return ACP_ABORT;
}


static int copy_config_value(char *out, size_t out_len, char *value) {
    char quote;
    char *end;
    char *tail;
    size_t len;

    if (out_len == 0) {
        return -1;
    }
    out[0] = '\0';
    trim_line(value);
    if (value[0] == '\'' || value[0] == '"') {
        quote = value[0];
        value++;
        end = strchr(value, quote);
        if (end == NULL) {
            return -1;
        }
        tail = end + 1;
        while (*tail != '\0' && isspace((unsigned char)*tail)) {
            tail++;
        }
        if (*tail != '\0') {
            return -1;
        }
        *end = '\0';
    }
    len = strlen(value);
    if (len >= out_len) {
        return -1;
    }
    memcpy(out, value, len + 1);
    return 0;
}

static int read_config_value_file(const char *path, const char *key, char *out, size_t out_len) {
    FILE *fp;
    char line[512];
    size_t key_len = strlen(key);

    if (out_len == 0) {
        return -1;
    }
    out[0] = '\0';
    fp = fopen(path, "r");
    if (fp == NULL) {
        return -1;
    }
    while (fgets(line, sizeof(line), fp) != NULL) {
        char *cursor = line;
        while (*cursor != '\0' && isspace((unsigned char)*cursor)) {
            cursor++;
        }
        if (strncmp(cursor, key, key_len) != 0) {
            continue;
        }
        cursor += key_len;
        while (*cursor != '\0' && isspace((unsigned char)*cursor)) {
            cursor++;
        }
        if (*cursor != '=') {
            continue;
        }
        cursor++;
        (void)fclose(fp);
        return copy_config_value(out, out_len, cursor);
    }
    (void)fclose(fp);
    return -1;
}

int read_deploy_release_tag(char *out, size_t out_len) {
    return read_config_value_file(HEARTBEAT_FLASH_CONFIG_PATH, HEARTBEAT_DEPLOY_RELEASE_TAG_KEY, out, out_len);
}

int telemetry_enabled(void) {
    char value[16];
    /* Only an explicit opt-out disables reporting; older configs omit it. */
    return read_config_value_file(HEARTBEAT_FLASH_CONFIG_PATH, "TELEMETRY", value, sizeof(value)) != 0 ||
           strcmp(value, "false") != 0;
}

int read_uptime_seconds(long *out) {
    if (out == NULL) {
        return -1;
    }
#if (defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__DragonFly__)) && defined(KERN_BOOTTIME)
    {
        int mib[2] = { CTL_KERN, KERN_BOOTTIME };
        struct timeval boot_time;
        size_t len = sizeof(boot_time);
        time_t now;

        memset(&boot_time, 0, sizeof(boot_time));
        if (sysctl(mib, 2, &boot_time, &len, NULL, 0) == 0 && boot_time.tv_sec > 0) {
            now = time(NULL);
            if (now >= boot_time.tv_sec && (unsigned long)(now - boot_time.tv_sec) <= (unsigned long)LONG_MAX) {
                *out = (long)(now - boot_time.tv_sec);
                return 0;
            }
        }
    }
#endif
#if defined(__linux__)
    {
        FILE *fp = fopen("/proc/uptime", "r");
        double uptime = 0.0;

        if (fp != NULL) {
            int parsed = fscanf(fp, "%lf", &uptime);
            (void)fclose(fp);
            if (parsed == 1 && uptime >= 0.0 && uptime <= (double)LONG_MAX) {
                *out = (long)uptime;
                return 0;
            }
        }
    }
#endif
    return -1;
}
