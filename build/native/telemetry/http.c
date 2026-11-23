#include "telemetry.h"
#include "../common/acp.h"
#include "dns.h"
#include "errclass.h"

/* An HTTP/1.0 client for the heartbeat POST and the debug downloads, so
 * telemetry needs no fork to report: when the process table is full, curl
 * could not start, and a heartbeat sent then is the one that matters.
 *
 * HTTP/1.0 with Connection: close keeps responses unchunked; the body runs
 * to Content-Length or to the close. Only a 2xx status succeeds (curl -f
 * failed at 400 and the old caller also required 2xx). Each address gets
 * TC_HTTP_CONNECT_MS to connect, IPv6 and IPv4 alternating; the whole
 * request, name lookup included, has TC_HTTP_DEADLINE_MS on the monotonic
 * clock (NTP steps the wall clock right after boot, when the boot heartbeat
 * is in flight). TERM stops it within a second.
 *
 * The caller owns the buffer (TC_HTTP_BUFFER(limit) bytes), so a heartbeat
 * allocates nothing while the device is short of memory. The outcome says
 * why a request failed: the device short of sockets (LOCAL_RESOURCE), the
 * network or name lookup (NETWORK), an answer that is not a complete 2xx
 * (SERVER), or TERM (CANCELLED). */
#ifndef TC_HTTP_CONNECT_MS
#define TC_HTTP_CONNECT_MS 10000
#endif

static long long left_ms(long long deadline) {
    long long now = acp_monotonic_ms();
    return now < 0 ? 0 : deadline - now;
}

/* Waits for fd to be readable or writable; 1 ready, 0 timeout or stop, -1 error. */
static int wait_fd(int fd, int writing, long long deadline) {
    for (;;) {
        fd_set set;
        struct timeval timeout;
        long long left = left_ms(deadline);
        int ready;
        if (telemetry_stop || left <= 0) return 0;
        if (left > 1000) left = 1000; /* recheck the stop flag every second */
        FD_ZERO(&set);
        FD_SET(fd, &set);
        timeout.tv_sec = (time_t)(left / 1000);
        timeout.tv_usec = (suseconds_t)(left % 1000) * 1000;
        ready = select(fd + 1, writing ? NULL : &set, writing ? &set : NULL, NULL, &timeout);
        if (ready > 0) return 1;
        if (ready < 0 && errno != EINTR) return -1;
    }
}

/* Splits http://host[:port]/path; host may be a bracketed IPv6 literal.
 * host_header gets host[:port] as written, for the Host header. */
static int parse_url(const char *url, char *host, size_t host_size, unsigned *port, const char **path,
                     char *host_header, size_t header_size) {
    const char *p, *end, *slash;
    size_t n;
    if (strncmp(url, "http://", 7)) return -1;
    p = url + 7;
    slash = strchr(p, '/');
    if (!slash) return -1;
    *path = slash;
    *port = 80;
    if (*p == '[') {
        end = strchr(p, ']');
        if (!end || end > slash) return -1;
        n = (size_t)(end - p - 1);
        if (n == 0 || n >= host_size) return -1;
        memcpy(host, p + 1, n);
        end++;
    } else {
        end = p;
        while (end < slash && *end != ':') end++;
        n = (size_t)(end - p);
        if (n == 0 || n >= host_size) return -1;
        memcpy(host, p, n);
    }
    host[n] = 0;
    if (end < slash) {
        char *stop;
        unsigned long value;
        if (*end != ':') return -1;
        value = strtoul(end + 1, &stop, 10);
        if (stop != slash || value == 0 || value > 65535) return -1;
        *port = (unsigned)value;
    }
    n = (size_t)(slash - p);
    if (n >= header_size) return -1;
    memcpy(host_header, p, n);
    host_header[n] = 0;
    return 0;
}

/* A connected socket, or -1; *resource is set when a socket could not be
 * made for want of descriptors or memory. */
static int connect_any(const char *host, unsigned port, long long deadline, int *resource) {
    struct tc_dns_addr addrs[TC_DNS_MAX_ADDRS];
    size_t count, i;
    if (tc_dns_resolve(host, addrs, TC_DNS_MAX_ADDRS, &count, deadline, resource)) return -1;
    for (i = 0; i < count && !telemetry_stop && left_ms(deadline) > 0; i++) {
        struct sockaddr_storage ss;
        socklen_t sslen;
        long long until = acp_monotonic_ms() + TC_HTTP_CONNECT_MS;
        int fd, error = 0;
        socklen_t errlen = sizeof(error);
        memset(&ss, 0, sizeof(ss));
        if (addrs[i].family == AF_INET6) {
            struct sockaddr_in6 *v6 = (struct sockaddr_in6 *)&ss;
            v6->sin6_family = AF_INET6;
            v6->sin6_port = htons((unsigned short)port);
            memcpy(&v6->sin6_addr, addrs[i].addr, 16);
            sslen = sizeof(*v6);
        } else {
            struct sockaddr_in *v4 = (struct sockaddr_in *)&ss;
            v4->sin_family = AF_INET;
            v4->sin_port = htons((unsigned short)port);
            memcpy(&v4->sin_addr, addrs[i].addr, 4);
            sslen = sizeof(*v4);
        }
        if (until > deadline) until = deadline;
        fd = socket(addrs[i].family, SOCK_STREAM, 0);
        if (fd < 0) {
            if (tc_errno_record(TC_OP_SOCKET, errno) == TC_ERR_RESOURCE) *resource = 1;
            continue;
        }
        if (fd >= FD_SETSIZE) { close(fd); continue; } /* select() cannot watch it */
        if (fcntl(fd, F_SETFL, O_NONBLOCK) || fcntl(fd, F_SETFD, FD_CLOEXEC)) { close(fd); continue; }
        if (!connect(fd, (struct sockaddr *)&ss, sslen)) return fd;
        if (errno == EINPROGRESS && wait_fd(fd, 1, until) == 1 &&
            !getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &errlen) && !error)
            return fd;
        close(fd);
    }
    return -1;
}

static int send_all(int fd, const char *data, size_t len, long long deadline) {
    size_t sent = 0;
    while (sent < len) {
        ssize_t n = send(fd, data + sent, len - sent, 0);
        if (n > 0) { sent += (size_t)n; continue; }
        if (n < 0 && errno == EINTR) continue;
        if (n < 0 && errno == EAGAIN && wait_fd(fd, 1, deadline) == 1) continue;
        return -1;
    }
    return 0;
}

/* The value of header name (case-insensitive) in head, which ends at the
 * blank line, or NULL. */
static const char *header(const char *head, const char *name) {
    size_t n = strlen(name);
    const char *p = strstr(head, "\r\n");
    while (p && p[2] && p[2] != '\r') {
        p += 2;
        if (!strncasecmp(p, name, n) && p[n] == ':') {
            p += n + 1;
            while (*p == ' ' || *p == '\t') p++;
            return p;
        }
        p = strstr(p, "\r\n");
    }
    return NULL;
}

/* Content-Length as servers write it: optional spaces, digits, optional
 * spaces, end of line. Returns 0 when absent, 1 with *out set, -1 for any
 * other value or for two headers that disagree. head ends at the blank line. */
static int content_length(const char *head, long long *out) {
    const char *p = strstr(head, "\r\n");
    int found = 0;
    while (p && p[2] && p[2] != '\r') {
        p += 2;
        if (!strncasecmp(p, "Content-Length:", 15)) {
            const char *q = p + 15;
            long long value = 0;
            int digits = 0;
            while (*q == ' ' || *q == '\t') q++;
            for (; *q >= '0' && *q <= '9'; q++, digits++) {
                if (value > (LLONG_MAX - 9) / 10) return -1;
                value = value * 10 + (*q - '0');
            }
            while (*q == ' ' || *q == '\t') q++;
            if (!digits || (*q && *q != '\r') || (found && value != *out)) return -1;
            *out = value;
            found = 1;
        }
        p = strstr(p, "\r\n");
    }
    return found;
}

int telemetry_http(const char *url, const char *payload, unsigned char *buf, size_t cap, size_t *len,
                   enum tc_outcome *outcome) {
    char host[256], host_header[300], request[1024];
    const char *path, *value;
    unsigned port;
    size_t used = 0, body_at = 0, payload_len = payload ? strlen(payload) : 0, limit;
    long long started = acp_monotonic_ms(), deadline, expected = -1;
    int fd = -1, n, status, resource = 0;
    enum tc_outcome result = TC_OUTCOME_NETWORK;
    *len = 0;
    *outcome = TC_OUTCOME_SERVER;
    if (cap <= TC_HTTP_HEADER_MAX + 1 || started < 0 ||
        parse_url(url, host, sizeof(host), &port, &path, host_header, sizeof(host_header)))
        return -1;
    limit = cap - TC_HTTP_HEADER_MAX - 1;
    deadline = started + TC_HTTP_DEADLINE_MS;
    if (payload)
        n = snprintf(request, sizeof(request),
                     "POST %s HTTP/1.0\r\nHost: %s\r\nUser-Agent: timecapsulesmb-telemetry/" HEARTBEAT_AGENT_VERSION
                     "\r\nAuthorization: Bearer %s\r\nContent-Type: application/json\r\nContent-Length: %lu\r\n"
                     "Connection: close\r\n\r\n",
                     path, host_header, HEARTBEAT_TOKEN, (unsigned long)payload_len);
    else
        n = snprintf(request, sizeof(request),
                     "GET %s HTTP/1.0\r\nHost: %s\r\nUser-Agent: timecapsulesmb-telemetry/" HEARTBEAT_AGENT_VERSION
                     "\r\nConnection: close\r\n\r\n",
                     path, host_header);
    if (n < 0 || (size_t)n >= sizeof(request)) return -1;
    fd = connect_any(host, port, deadline, &resource);
    if (fd < 0) {
        result = resource ? TC_OUTCOME_LOCAL_RESOURCE : TC_OUTCOME_NETWORK;
        goto done;
    }
    if (send_all(fd, request, (size_t)n, deadline) || (payload_len && send_all(fd, payload, payload_len, deadline)))
        goto done;
    for (;;) {
        ssize_t got;
        if (used == cap - 1) { result = TC_OUTCOME_SERVER; goto done; } /* headers or body too large */
        got = recv(fd, buf + used, cap - 1 - used, 0);
        if (got > 0) {
            used += (size_t)got;
            buf[used] = 0;
            if (!body_at) {
                char *end = strstr((char *)buf, "\r\n\r\n");
                if (end) body_at = (size_t)(end - (char *)buf) + 4;
                else if (used >= TC_HTTP_HEADER_MAX) { result = TC_OUTCOME_SERVER; goto done; }
            }
            if (body_at && expected == -1) {
                char saved = (char)buf[body_at - 2];
                int found;
                buf[body_at - 2] = 0; /* the head ends at the blank line */
                found = content_length((char *)buf, &expected);
                value = header((char *)buf, "Transfer-Encoding");
                buf[body_at - 2] = (unsigned char)saved;
                /* chunked is never sent to HTTP/1.0 */
                if (found < 0 || value) { result = TC_OUTCOME_SERVER; goto done; }
                if (!found) expected = -2; /* read to the close */
                if (expected > (long long)limit) { result = TC_OUTCOME_SERVER; goto done; }
            }
            if (body_at && expected >= 0 && used - body_at >= (size_t)expected) break;
            continue;
        }
        if (got == 0) break;
        if (errno == EINTR) continue;
        if (errno == EAGAIN && wait_fd(fd, 0, deadline) == 1) continue;
        goto done;
    }
    result = TC_OUTCOME_SERVER;
    if (!body_at || sscanf((char *)buf, "HTTP/1.%*d %d", &status) != 1 || status < 200 || status > 299) goto done;
    if (expected >= 0 && used - body_at != (size_t)expected) goto done; /* short or extra body */
    if (used - body_at > limit) goto done;
    memmove(buf, buf + body_at, used - body_at);
    used -= body_at;
    buf[used] = 0;
    *len = used;
    result = TC_OUTCOME_DELIVERED;
done:
    if (fd >= 0) close(fd);
    if (telemetry_stop && result != TC_OUTCOME_DELIVERED) result = TC_OUTCOME_CANCELLED;
    *outcome = result;
    return result == TC_OUTCOME_DELIVERED ? 0 : -1;
}
