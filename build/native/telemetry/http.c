#include "telemetry.h"
#include "../common/acp.h"

/* curl's own --max-time is 60 s; this parent deadline also covers startup
 * and reaping. It runs on the monotonic clock: NTP steps the wall clock right
 * after boot, when the boot heartbeat is in flight. */
#ifndef TC_HTTP_DEADLINE_MS
#define TC_HTTP_DEADLINE_MS 70000
#endif

static int deadline_passed(long long started) {
    long long now = acp_monotonic_ms();
    return now < 0 || now - started >= TC_HTTP_DEADLINE_MS;
}

/* Read curl stdout ourselves. A server that omits Content-Length must not be
 * able to fill the device ramdisk before a post-download size check. */
int telemetry_http(const char *url, const char *payload, unsigned char **out, size_t *len, size_t limit) {
    int input[2], output[2], status = 0, failed = 0;
    pid_t pid;
    size_t used = 0, sent = 0, payload_len = payload ? strlen(payload) : 0;
    unsigned char *buf;
    long long started = acp_monotonic_ms();
    char auth[128];
    *out = NULL; *len = 0;
    if (started < 0 || limit > TC_DEBUG_MAX || pipe(input)) return -1;
    if (pipe(output)) { close(input[0]); close(input[1]); return -1; }
    buf = malloc(limit + 5);
    if (!buf) { close(input[0]); close(input[1]); close(output[0]); close(output[1]); return -1; }
    snprintf(auth, sizeof(auth), "Authorization: Bearer %s", HEARTBEAT_TOKEN);
    pid = fork();
    if (pid == 0) {
        dup2(input[0], STDIN_FILENO); dup2(output[1], STDOUT_FILENO);
        close(input[0]); close(input[1]); close(output[0]); close(output[1]);
        if (payload) execl(TC_CURL_PATH, "curl", "-q", "-fsS", "--connect-timeout", "10", "--max-time", "60",
            "-H", auth, "-H", "Content-Type: application/json", "--data-binary", "@-",
            "-w", "\n%{http_code}", url, (char *)NULL);
        else execl(TC_CURL_PATH, "curl", "-q", "-fsS", "--connect-timeout", "10", "--max-time", "60",
            "-w", "\n%{http_code}", url, (char *)NULL);
        _exit(127);
    }
    close(input[0]); close(output[1]);
    if (pid < 0) { close(input[1]); close(output[0]); free(buf); return -1; }
    if (!payload_len) { close(input[1]); input[1] = -1; }
    fcntl(output[0], F_SETFL, O_NONBLOCK);
    if (input[1] >= 0) fcntl(input[1], F_SETFL, O_NONBLOCK);
    while (!failed) {
        fd_set reads, writes;
        struct timeval timeout;
        int maxfd = output[0], ready;
        ssize_t n;
        if (telemetry_stop || deadline_passed(started)) { failed = 1; break; }
        FD_ZERO(&reads); FD_ZERO(&writes); FD_SET(output[0], &reads);
        if (input[1] >= 0) { FD_SET(input[1], &writes); if (input[1] > maxfd) maxfd = input[1]; }
        timeout.tv_sec = 1; timeout.tv_usec = 0;
        ready = select(maxfd + 1, &reads, &writes, NULL, &timeout);
        if (ready < 0) { if (errno == EINTR) continue; failed = 1; break; }
        if (input[1] >= 0 && FD_ISSET(input[1], &writes)) {
            n = write(input[1], payload + sent, payload_len - sent);
            if (n > 0) sent += (size_t)n;
            else if (n < 0 && errno != EINTR && errno != EAGAIN) { failed = 1; break; }
            if (sent == payload_len) { close(input[1]); input[1] = -1; }
        }
        if (FD_ISSET(output[0], &reads)) {
            n = read(output[0], buf + used, limit + 5 - used);
            if (n == 0) break;
            if (n < 0) { if (errno == EINTR || errno == EAGAIN) continue; failed = 1; break; }
            used += (size_t)n;
            if (used > limit + 4) { failed = 1; break; }
        }
    }
    if (input[1] >= 0) close(input[1]);
    close(output[0]);
    /* EOF does not imply curl has exited. Keep the parent deadline and TERM
     * handling active while reaping as well as while reading its output. */
    while (1) {
        pid_t done;
        if (telemetry_stop || deadline_passed(started)) failed = 1;
        if (failed) kill(pid, SIGKILL);
        done = waitpid(pid, &status, failed ? 0 : WNOHANG);
        if (done == pid) break;
        if (done < 0 && errno != EINTR) { failed = 1; break; }
        if (done == 0) usleep(10000);
    }
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0 || used < 4 ||
        buf[used - 4] != '\n' || buf[used - 3] != '2' ||
        !isdigit(buf[used - 2]) || !isdigit(buf[used - 1])) failed = 1;
    if (failed) { free(buf); return -1; }
    used -= 4; buf[used] = 0;
    *out = buf; *len = used;
    return 0;
}
