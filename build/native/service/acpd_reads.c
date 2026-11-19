#include "acpd_reads.h"
#include "../common/acp.h"
#include "../common/worker.h"
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#define TC_ACP_GET_REQUEST 144

/* How a read ended when it got no answer. */
enum { READ_TIMEOUT = -1, READ_REFUSED = -2, READ_RESET = -3, READ_STOPPED = -4, READ_ERROR = -5 };

static const unsigned char static_key[16] = {0x5b, 0x6f, 0xaf, 0x5d, 0x9d, 0x5b, 0x0e, 0x13,
                                             0x51, 0xf2, 0xda, 0x1d, 0xe7, 0xe8, 0xd6, 0x73};

static unsigned long adler32(const unsigned char *p, size_t n) {
    unsigned long a = 1, b = 0;
    while (n--) {
        a = (a + *p++) % 65521;
        b = (b + a) % 65521;
    }
    return b << 16 | a;
}
static void put32(unsigned char *p, unsigned long v) {
    p[0] = (unsigned char)(v >> 24);
    p[1] = (unsigned char)(v >> 16);
    p[2] = (unsigned char)(v >> 8);
    p[3] = (unsigned char)v;
}
/* A get request for one property, as integrations/acp.py composes it: the
 * 128-byte header (password keyed in) and one 16-byte element. */
static void get_request(const char *password, size_t length, const char name[4],
                        unsigned char out[TC_ACP_GET_REQUEST]) {
    unsigned char *body = out + 128;
    size_t i;
    memset(out, 0, TC_ACP_GET_REQUEST);
    memcpy(body, name, 4); /* element: name, flags 0, size 4, a zero value */
    put32(body + 8, 4);
    memcpy(out, "acpp", 4);
    put32(out + 4, 0x00030001); /* version */
    put32(out + 12, adler32(body, 16));
    put32(out + 16, 16);   /* body size */
    put32(out + 20, 4);    /* flags, as acp.get_properties sends */
    put32(out + 28, 0x14); /* get property */
    /* The password field: a static keystream XORed with the first 32 bytes. */
    for (i = 0; i < 32; i++)
        out[48 + i] = (unsigned char)((((i + 0x55) & 0xff) ^ static_key[i % 16]) ^
                                      (i < length ? (unsigned char)password[i] : 0));
    put32(out + 8, adler32(out, 128)); /* header checksum, computed with this field 0 */
}

static int port(void) {
#ifdef TC_NATIVE_TEST
    const char *text = getenv("TC_TEST_ACPD_PORT");
    if (text)
        return atoi(text);
#endif
    return TC_ACPD_PORT;
}
/* 1 when fd is ready, 0 at the deadline, READ_STOPPED when the job is
 * stopped, READ_ERROR on a poll error (errno). */
static int wait_fd(int fd, short events, long long deadline) {
    for (;;) {
        struct pollfd p;
        long long left = deadline - acp_monotonic_ms();
        int r;
        if (tc_worker_cancelled())
            return READ_STOPPED;
        if (left <= 0)
            return 0;
        p.fd = fd;
        p.events = events;
        p.revents = 0;
        r = poll(&p, 1, left > 1000 ? 1000 : (int)left);
        if (r > 0)
            return 1;
        if (r < 0 && errno != EINTR)
            return READ_ERROR;
    }
}
/* A connected socket to ACPd on family, or a READ_* code (errno for
 * READ_ERROR). A family with no listener, no route or no support is
 * READ_REFUSED. */
static int dial(int family, long long deadline) {
    struct sockaddr_in in4;
    struct sockaddr_in6 in6;
    struct sockaddr *addr;
    socklen_t length, size;
    int fd, r, error = 0;
    if (family == AF_INET6) {
        memset(&in6, 0, sizeof(in6));
        in6.sin6_family = AF_INET6;
        in6.sin6_port = htons((unsigned short)port());
        in6.sin6_addr = in6addr_loopback;
        addr = (struct sockaddr *)&in6;
        length = sizeof(in6);
    } else {
        memset(&in4, 0, sizeof(in4));
        in4.sin_family = AF_INET;
        in4.sin_port = htons((unsigned short)port());
        in4.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        addr = (struct sockaddr *)&in4;
        length = sizeof(in4);
    }
    fd = socket(family, SOCK_STREAM, 0);
    if (fd < 0)
        return READ_REFUSED;
    fcntl(fd, F_SETFL, O_NONBLOCK);
    if (!connect(fd, addr, length))
        return fd;
    if (errno == EINPROGRESS) {
        r = wait_fd(fd, POLLOUT, deadline);
        size = sizeof(error);
        if (r == 1 && getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &size))
            error = errno;
        if (r == 1 && !error)
            return fd;
        if (r != 1) {
            close(fd);
            return r ? r : READ_TIMEOUT; /* a full listen queue drops the handshake */
        }
    } else
        error = errno;
    close(fd);
    errno = error;
    switch (error) {
    case ECONNREFUSED: case EADDRNOTAVAIL: case ENETUNREACH: case EHOSTUNREACH: case EAFNOSUPPORT:
    case EPROTONOSUPPORT:
        return READ_REFUSED;
    case ETIMEDOUT:
        return READ_TIMEOUT;
    default:
        return READ_ERROR;
    }
}
/* One read: ms to the first reply byte, or to ACPd closing the accepted
 * connection; else a READ_* code (errno for READ_ERROR). The close leaves
 * the rest of the reply unread, so the kernel resets the connection: ACPd
 * ignores SIGPIPE (README F19), and only that session's thread sees it. */
static long long read_once(const unsigned char *request, int *family) {
    static const int families[] = {AF_INET6, AF_INET};
    long long start = acp_monotonic_ms(), deadline = start + TC_ACPD_READ_MS, answered = -1;
    unsigned char reply;
    size_t i, sent = 0;
    int fd = READ_REFUSED, result = 0, saved;
    for (i = 0; i < 2 && fd == READ_REFUSED; i++)
        if (!*family || families[i] == *family) {
            fd = dial(families[i], deadline);
            if (fd >= 0)
                *family = families[i]; /* the later reads go straight to it */
        }
    if (fd < 0)
        return fd;
    while (!result && sent < TC_ACP_GET_REQUEST) {
        ssize_t n = send(fd, request + sent, TC_ACP_GET_REQUEST - sent, 0);
        if (n > 0)
            sent += (size_t)n;
        else if (n < 0 && errno == ECONNRESET)
            result = READ_RESET;
        else if (n < 0 && errno != EAGAIN && errno != EINTR)
            result = READ_ERROR;
        else if ((result = wait_fd(fd, POLLOUT, deadline)) == 1)
            result = 0;
        else if (!result)
            result = READ_TIMEOUT;
    }
    /* The first reply byte, or a close after the accept, is the answer. */
    while (!result && answered < 0) {
        int r = wait_fd(fd, POLLIN, deadline);
        ssize_t n;
        if (r != 1) {
            result = r ? r : READ_TIMEOUT;
            break;
        }
        n = recv(fd, &reply, 1, 0);
        if (n >= 0)
            answered = acp_monotonic_ms() - start;
        else if (errno == ECONNRESET)
            result = READ_RESET;
        else if (errno != EAGAIN && errno != EINTR)
            result = READ_ERROR;
    }
    saved = errno;
    close(fd);
    errno = saved;
    if (answered >= 0)
        return answered;
    return result;
}
void tc_acpd_reads(const char *password, size_t length, char *out, size_t size) {
    unsigned char request[TC_ACP_GET_REQUEST];
    volatile unsigned char *wipe = request;
    size_t used, j;
    int i, family = 0;
    get_request(password, length, "syUT", request);
    used = (size_t)snprintf(out, size, "ms=");
    for (i = 1; i <= TC_ACPD_READS && used < size; i++) {
        long long ms = read_once(request, &family);
        int error = errno;
        static const char *const names[] = {"timeout", "refused", "reset", "stopped"};
        if (ms >= 0) {
            used += (size_t)snprintf(out + used, size - used, "%s%lld", i > 1 ? "," : "", ms);
            continue;
        }
        if (ms == READ_ERROR)
            snprintf(out + used, size - used, " error=%d:%d", i, error);
        else
            snprintf(out + used, size - used, " %s=%d", names[-ms - 1], i);
        break;
    }
    for (j = 0; j < sizeof(request); j++)
        wipe[j] = 0;
}
