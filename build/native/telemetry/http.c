#include "telemetry.h"
#include "../common/acp.h"
#include "dns.h"
#include "errclass.h"

/* An HTTP/1.0 client for the heartbeat POST and the debug downloads, so
 * telemetry needs no fork to report: when the process table is full, curl
 * could not start, and a heartbeat sent then is the one that matters.
 *
 * HTTP/1.0 with Connection: close keeps responses unchunked; the body runs
 * to Content-Length or to the close. Only a complete 2xx answer is
 * delivered. The whole request, name lookup included, has
 * TC_HTTP_DEADLINE_MS on the monotonic clock (NTP steps the wall clock right
 * after boot, when the boot heartbeat is in flight). TERM stops it within a
 * second.
 *
 * The addresses are tried in turn, in tc_dns_resolve's order (IPv6 first).
 * An exchange that ends before the server's head arrives (no connection
 * within TC_HTTP_CONNECT_MS, a reset, a close or a stall) moves on to the next
 * address, sending the request again (a heartbeat carries its id, which
 * the server stores once, keeping the latest attempt). Every address but the last has at most
 * TC_HTTP_ADDRESS_MS for its exchange, so an IPv6 path that connects and
 * then stalls cannot use up the time IPv4 needs; the last has what is left.
 * A complete answer, 2xx or not, is the server's and ends the request, and
 * so does a close after the head, even one that cuts the body short
 * (SERVER).
 * Retrying is safe because the request is idempotent, and it is also used
 * where an answer may have been under way:
 *   - a reset or stall after the head moves on like any other failure, so
 *     the server can get the request twice: a heartbeat keeps its id
 *     (telemetry.h, struct tc_heartbeat_ident), under which the server
 *     keeps one row, and a debug download is a GET;
 *   - a server that answers early and closes while the request is still
 *     being sent (a 401 or 413 before it reads the body) looks like a
 *     reset: its answer is not read;
 *   - TC_HTTP_ADDRESS_MS bounds the whole exchange, not idle time, so a
 *     slow but steady debug download from an address that is not the last
 *     is cut and started again on the next one. The server publishes no
 *     AAAA record today, so its one address always has the full limit.
 *
 * The caller owns the buffer (TC_HTTP_BUFFER(limit) bytes), so a heartbeat
 * allocates nothing while the device is short of memory. The outcome says
 * why a request failed: the device short of descriptors, buffers or memory
 * for a socket or its traffic (LOCAL_RESOURCE, errclass.h), the network or
 * name lookup (NETWORK, a close before any byte of an answer included), an
 * answer that is not a complete 2xx (SERVER), or TERM (CANCELLED). Across addresses, and
 * with a failed name lookup counted as one more failure, a server that was
 * reached outranks the others, and otherwise the device's want of resources
 * outranks the network (merge_failure). */
#ifndef TC_HTTP_CONNECT_MS
#define TC_HTTP_CONNECT_MS 10000
#endif
#ifndef TC_HTTP_ADDRESS_MS
#define TC_HTTP_ADDRESS_MS 25000
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

/* LOCAL_RESOURCE for a socket call that failed for want of buffers or
 * memory (TC_OP_IO, counted), else NETWORK. */
static enum tc_outcome io_failure(int error) {
    return tc_errno_record(TC_OP_IO, error) == TC_ERR_RESOURCE ? TC_OUTCOME_LOCAL_RESOURCE : TC_OUTCOME_NETWORK;
}

/* A socket connected to addr by until; -1 when it did not connect, -2 when
 * it failed for want of descriptors, buffers or memory. */
static int connect_to(const struct tc_dns_addr *addr, unsigned port, long long until) {
    struct sockaddr_storage ss;
    socklen_t sslen, errlen;
    int fd, error = 0;
    memset(&ss, 0, sizeof(ss));
    if (addr->family == AF_INET6) {
        struct sockaddr_in6 *v6 = (struct sockaddr_in6 *)&ss;
        v6->sin6_family = AF_INET6;
        v6->sin6_port = htons((unsigned short)port);
        memcpy(&v6->sin6_addr, addr->addr, 16);
        sslen = sizeof(*v6);
    } else {
        struct sockaddr_in *v4 = (struct sockaddr_in *)&ss;
        v4->sin_family = AF_INET;
        v4->sin_port = htons((unsigned short)port);
        memcpy(&v4->sin_addr, addr->addr, 4);
        sslen = sizeof(*v4);
    }
    fd = socket(addr->family, SOCK_STREAM, 0);
    if (fd < 0) return tc_errno_record(TC_OP_SOCKET, errno) == TC_ERR_RESOURCE ? -2 : -1;
    if (fd >= FD_SETSIZE) { close(fd); return -1; } /* select() cannot watch it */
    if (fcntl(fd, F_SETFL, O_NONBLOCK) || fcntl(fd, F_SETFD, FD_CLOEXEC)) { close(fd); return -1; }
    if (acp_monotonic_ms() + TC_HTTP_CONNECT_MS < until) until = acp_monotonic_ms() + TC_HTTP_CONNECT_MS;
    errlen = sizeof(error);
    if (!tc_test_enobufs("connect") && !connect(fd, (struct sockaddr *)&ss, sslen)) return fd;
    error = errno;
    if (error == EINPROGRESS) {
        if (wait_fd(fd, 1, until) != 1) error = 0; /* no connection by until, or TERM */
        else if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &errlen)) error = errno;
        else if (!error) return fd;
    }
    close(fd);
    return error && io_failure(error) == TC_OUTCOME_LOCAL_RESOURCE ? -2 : -1;
}

/* 0 once all of data is sent, else why not: the errno of a failed send(),
 * or ETIMEDOUT when deadline passed (or TERM) first. */
static int send_all(int fd, const char *data, size_t len, long long deadline) {
    size_t sent = 0;
    while (sent < len) {
        ssize_t n = tc_test_enobufs("send") ? -1 : send(fd, data + sent, len - sent, 0);
        if (n > 0) { sent += (size_t)n; continue; }
        if (n < 0 && errno == EINTR) continue;
        if (n < 0 && errno == EAGAIN) {
            if (wait_fd(fd, 1, deadline) == 1) continue;
            return ETIMEDOUT;
        }
        return n < 0 ? errno : EPIPE;
    }
    return 0;
}

/* Content-Length as servers write it: optional spaces, digits, optional
 * spaces, end of line. Returns 0 when absent, 1 with *out set, -1 for any
 * other value, for two headers that disagree, or for any Transfer-Encoding
 * (chunked is never sent to HTTP/1.0). head ends at the blank line. */
static int content_length(const char *head, long long *out) {
    const char *p = strstr(head, "\r\n");
    int found = 0;
    while (p && p[2] && p[2] != '\r') {
        p += 2;
        if (!strncasecmp(p, "Transfer-Encoding:", 18)) return -1;
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

/* What an exchange is reading, and what each event there makes of it. This
 * table is the whole state machine; exchange() follows it and the tests in
 * tests/native/test_telemetry_http.py name its rows.
 *
 *   phase          event                               result
 *   HEAD           close, no byte                      NETWORK, next address
 *   HEAD           close, some bytes                   SERVER, next address
 *   HEAD           head past TC_HTTP_HEADER_MAX        SERVER, answered
 *   HEAD           blank line, bad length or chunked   SERVER, answered
 *   HEAD           blank line, length over limit       SERVER, answered
 *   HEAD           blank line, Content-Length n        -> BODY_LENGTH(n)
 *   HEAD           blank line, no length               -> BODY_TO_CLOSE
 *   BODY_LENGTH    n bytes, 2xx                        DELIVERED
 *   BODY_LENGTH    n bytes, other status               SERVER, answered
 *   BODY_LENGTH    close before n bytes                SERVER, answered
 *   BODY_TO_CLOSE  close, 2xx, body within limit       DELIVERED
 *   BODY_TO_CLOSE  close, other status                 SERVER, answered
 *   BODY_TO_CLOSE  body past limit, or buffer full     SERVER, answered
 *   any            reset or stall                      NETWORK (LOCAL_RESOURCE
 *                                                      for want of buffers),
 *                                                      next address
 *
 * "answered" is the server's complete answer, which another address would
 * only repeat; "next address" moves on (telemetry_http). BODY_LENGTH
 * stops reading at the n-th byte and drops whatever that read brought past
 * it, as curl does, so extra bytes count the same however the reads split
 * them. */
enum response_phase { RESPONSE_HEAD, RESPONSE_BODY_LENGTH, RESPONSE_BODY_TO_CLOSE };

/* One exchange with one address by until: DELIVERED with the body at the
 * front of buf and *len set, or why it failed, with *answered set when the
 * failure is the server's complete answer (the table above). */
static enum tc_outcome exchange(const struct tc_dns_addr *addr, unsigned port, const char *request, size_t request_len,
                                const char *payload, size_t payload_len, unsigned char *buf, size_t cap, size_t *len,
                                long long until, int *answered) {
    size_t used = 0, body_at = 0, limit = cap - TC_HTTP_HEADER_MAX - 1;
    long long expected = 0; /* BODY_LENGTH's n */
    enum response_phase phase = RESPONSE_HEAD;
    int fd = connect_to(addr, port, until), status, error;
    enum tc_outcome result = TC_OUTCOME_NETWORK;
    *answered = 0;
    if (fd < 0) return fd == -2 ? TC_OUTCOME_LOCAL_RESOURCE : TC_OUTCOME_NETWORK;
    if ((error = send_all(fd, request, request_len, until)) != 0 ||
        (payload_len && (error = send_all(fd, payload, payload_len, until)) != 0)) {
        result = io_failure(error);
        goto done;
    }
    for (;;) {
        ssize_t got;
        /* Only BODY_TO_CLOSE can fill the buffer: HEAD stops at
         * TC_HTTP_HEADER_MAX and BODY_LENGTH at n, at most limit. */
        if (used == cap - 1) goto refused;
        got = recv(fd, buf + used, cap - 1 - used, 0);
        if (got == 0) break; /* the close */
        if (got < 0) {
            if (errno == EINTR) continue;
            if (errno != EAGAIN) {
                result = io_failure(errno); /* a reset is the network's */
                goto done;
            }
            if (wait_fd(fd, 0, until) == 1) continue;
            goto done; /* stall or stop: NETWORK */
        }
        used += (size_t)got;
        buf[used] = 0;
        if (phase == RESPONSE_HEAD) {
            char *end = strstr((char *)buf, "\r\n\r\n"), saved;
            int found;
            /* A head longer than TC_HTTP_HEADER_MAX, blank line included,
             * is refused however the reads split it. */
            if (!end) {
                if (used >= TC_HTTP_HEADER_MAX) goto refused;
                continue;
            }
            body_at = (size_t)(end - (char *)buf) + 4;
            if (body_at > TC_HTTP_HEADER_MAX) goto refused;
            saved = (char)buf[body_at - 2];
            buf[body_at - 2] = 0; /* the head ends at the blank line */
            found = content_length((char *)buf, &expected);
            buf[body_at - 2] = (unsigned char)saved;
            if (found < 0 || (found && expected > (long long)limit)) goto refused;
            phase = found ? RESPONSE_BODY_LENGTH : RESPONSE_BODY_TO_CLOSE;
        }
        if (phase == RESPONSE_BODY_LENGTH && used - body_at >= (size_t)expected) break;
    }
    /* A close in HEAD is not an answer: before any byte it says nothing
     * about the server (a middlebox that accepts and hangs up looks the
     * same), after some it is a server's, but not one to stop at. */
    if (phase == RESPONSE_HEAD) {
        result = used ? TC_OUTCOME_SERVER : TC_OUTCOME_NETWORK;
        goto done;
    }
    if (sscanf((char *)buf, "HTTP/1.%*d %d", &status) != 1 || status < 200 || status > 299) goto refused;
    if (phase == RESPONSE_BODY_LENGTH) {
        if (used - body_at < (size_t)expected) goto refused; /* short */
        used = body_at + (size_t)expected; /* what the n-th byte's read brought past it is not the body */
    }
    if (used - body_at > limit) goto refused;
    memmove(buf, buf + body_at, used - body_at);
    used -= body_at;
    buf[used] = 0;
    *len = used;
    result = TC_OUTCOME_DELIVERED;
    goto done;
refused:
    result = TC_OUTCOME_SERVER;
    *answered = 1;
done:
    close(fd);
    return result;
}

/* The request's failure once one more address (or the name lookup) failed
 * with got after the earlier ones left kept: SERVER, then LOCAL_RESOURCE,
 * then NETWORK. A server that answered at all shows the network works, and
 * a device that ran short of sockets needs the retry rules for that,
 * whatever the other addresses met. */
static enum tc_outcome merge_failure(enum tc_outcome kept, enum tc_outcome got) {
    if (kept == TC_OUTCOME_SERVER || got == TC_OUTCOME_SERVER) return TC_OUTCOME_SERVER;
    if (kept == TC_OUTCOME_LOCAL_RESOURCE || got == TC_OUTCOME_LOCAL_RESOURCE) return TC_OUTCOME_LOCAL_RESOURCE;
    return TC_OUTCOME_NETWORK;
}
#ifdef TC_NATIVE_TEST
enum tc_outcome tc_http_merge_failure(enum tc_outcome kept, enum tc_outcome got) { return merge_failure(kept, got); }
#endif

int telemetry_http(const char *url, const char *payload, unsigned char *buf, size_t cap, size_t *len,
                   enum tc_outcome *outcome) {
    char host[256], host_header[300], request[1024];
    struct tc_dns_addr addrs[TC_DNS_MAX_ADDRS];
    const char *path;
    unsigned port;
    size_t payload_len = payload ? strlen(payload) : 0, count = 0, i;
    long long started = acp_monotonic_ms(), deadline;
    int n, resource;
    enum tc_outcome result = TC_OUTCOME_NETWORK;
    *len = 0;
    *outcome = TC_OUTCOME_SERVER;
    if (cap <= TC_HTTP_HEADER_MAX + 1 || started < 0 ||
        parse_url(url, host, sizeof(host), &port, &path, host_header, sizeof(host_header)))
        return -1;
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
    if (tc_dns_resolve(host, addrs, TC_DNS_MAX_ADDRS, &count, deadline, &resource))
        result = merge_failure(result, resource ? TC_OUTCOME_LOCAL_RESOURCE : TC_OUTCOME_NETWORK);
    for (i = 0; i < count && !telemetry_stop; i++) {
        long long now = acp_monotonic_ms(), until = deadline;
        enum tc_outcome got;
        int answered;
        if (now < 0 || now >= deadline) break;
        if (i + 1 < count && now + TC_HTTP_ADDRESS_MS < deadline) until = now + TC_HTTP_ADDRESS_MS;
        got = exchange(&addrs[i], port, request, (size_t)n, payload, payload_len, buf, cap, len, until, &answered);
        if (got == TC_OUTCOME_DELIVERED || answered) { result = got; break; }
        result = merge_failure(result, got);
    }
    if (telemetry_stop && result != TC_OUTCOME_DELIVERED) result = TC_OUTCOME_CANCELLED;
    *outcome = result;
    return result == TC_OUTCOME_DELIVERED ? 0 : -1;
}
