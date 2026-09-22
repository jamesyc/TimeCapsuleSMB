#include "wcifsnd.h"
#include "../common/log.h"

/* Only test builds may substitute a fake child/port or shorten deadlines. */
#ifndef TC_NATIVE_TEST
#undef WCIFSND_PATH
#undef WCIFSND_PORT
#undef WCIFSND_START_MS
#undef WCIFSND_REPLY_MS
#undef WCIFSND_STOP_MS
#endif
#ifndef WCIFSND_PATH
#define WCIFSND_PATH "/sbin/wcifsnd"
#endif
#ifndef WCIFSND_PORT
#define WCIFSND_PORT 922
#endif
#ifndef WCIFSND_START_MS
#define WCIFSND_START_MS 5000
#endif
#ifndef WCIFSND_REPLY_MS
#define WCIFSND_REPLY_MS 10000
#endif
#ifndef WCIFSND_STOP_MS
#define WCIFSND_STOP_MS 2000
#endif

static unsigned get16(const unsigned char *p) { return ((unsigned)p[0] << 8) | p[1]; }
static void put16(unsigned char *p, unsigned n) { p[0] = n >> 8; p[1] = n; }

static void request_name(struct wcifsnd *w) {
    unsigned char name[16];
    const char *text = w->record == 1 ? "WORKGROUP" : w->name;
    size_t i;
    memset(name, ' ', 15);
    memcpy(name, text, strlen(text));
    name[15] = w->record == 2 ? 0x20 : 0;
    memset(w->request, 0, sizeof(w->request));
    put16(w->request, ++w->transaction);
    put16(w->request + 2, 0x2900);
    w->request[5] = w->request[11] = 1;
    w->request[12] = 32;
    for (i = 0; i < 16; i++) {
        w->request[13 + 2*i] = 'A' + (name[i] >> 4);
        w->request[14 + 2*i] = 'A' + (name[i] & 15);
    }
    put16(w->request + 46, 32); put16(w->request + 48, 1);
    put16(w->request + 50, 0xc00c);
    put16(w->request + 52, 32); put16(w->request + 54, 1);
    w->request[59] = 5;
    /* Apple private IPC: rdlength is little endian, PID is big endian.
     * This intentionally differs from public NBNS address registration. */
    w->request[60] = 6;
    if (w->record == 1) w->request[62] = 0x80;
    for (i = 0; i < 4; i++) w->request[64+i] = (unsigned long)getpid() >> (24-8*i);
}

/* 0 = unrelated or bounded wait ACK, 1 = success, -1 = invalid/negative. */
static int response(const struct wcifsnd *w, const unsigned char *p, size_t n) {
    unsigned flags;
    if (n < 2 || get16(p) != w->transaction) return 0;
    if (n < 58 || memcmp(p + 12, w->request + 12, 34) ||
        get16(p + 4) != 0 || get16(p + 6) != 1 || get16(p + 8) || get16(p + 10) ||
        get16(p + 46) != 32 || get16(p + 48) != 1) return -1;
    flags = get16(p + 2);
    if ((flags & 0xf800) == 0xb800 && !(flags & 15) && n == 58 && get16(p + 54) == 2) return 0;
    return n == 62 && get16(p + 54) == 6 && (flags & 0xf800) == 0xa800 && !(flags & 15) ? 1 : -1;
}

static struct sockaddr_in endpoint(unsigned port) {
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a)); a.sin_family = AF_INET;
    a.sin_port = htons(port); a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    return a;
}

static void stop_child(struct wcifsnd *w, long long now) {
    if (w->fd >= 0) { close(w->fd); w->fd = -1; }
    if (w->phase == WC_STOPPING) return;
    if (w->child > 0) {
        (void)kill(w->child, SIGTERM);
        w->phase = WC_STOPPING; w->killed = 0;
        w->deadline = now + WCIFSND_STOP_MS; w->wake = now;
    } else w->phase = WC_OFF;
}

static void fail(struct wcifsnd *w, const char *why, long long now) {
    if (w->failed) return;
    timestamped_fprintf(stderr, "wcifsnd: %s; replacing native child\n", why);
    if (w->active_since >= 0 && now - w->active_since >= 60000) w->failures = 0;
    w->active_since = -1;
    w->failed = 1; stop_child(w, now);
}

void wcifsnd_init(struct wcifsnd *w, const char *name) {
    size_t i;
    memset(w, 0, sizeof(*w)); w->fd = -1; w->active_since = -1;
    snprintf(w->name, sizeof(w->name), "%s", name);
    for (i = 0; w->name[i]; i++) w->name[i] = toupper((unsigned char)w->name[i]);
    if (!strcmp(w->name, "WORKGROUP"))
        timestamped_fprintf(stderr, "wcifsnd: machine name matches WORKGROUP; omitting duplicate group name\n");
}

void wcifsnd_apply_plan(struct wcifsnd *w, const struct device_plan *p, long long now) {
    size_t i, j;
    int ipv4 = 0;
    for (i = 0; i < p->link_count; i++) {
        if (!(p->links[i].mask & SVC_SMB)) continue;
        for (j = 0; j < p->links[i].addr_count; j++) {
            const struct if_addr *a = &p->links[i].addrs[j];
            if (a->family == AF_INET && addr_is_service_address(a)) ipv4 = 1;
        }
    }
    /* Apple native NBNS is automatic; only live service eligibility gates it. */
    w->desired = w->name[0] && !p->options.diskless && ipv4;
    /* Incomplete facts may retain a live native child, but every replacement
     * must pass the same validated-plan gate as initial startup. */
    w->validated = p->status.validated;
    if (!w->desired) {
        w->failed = 0; w->failures = 0; w->active_since = -1;
        stop_child(w, now);
        if (w->phase == WC_OFF) w->wake = 0;
    }
    /* OEM scope after the initial eligibility gate. HUP refreshes addresses
     * inside Apple; it must not add another reference to any name. */
    else if (w->phase == WC_ACTIVE && p->status.validated && kill(w->child, SIGHUP))
        fail(w, "interface refresh failed", now);
}

void wcifsnd_prepare(struct wcifsnd *w, fd_set *reads, int *maxfd, long long *deadline) {
    if (w->fd >= 0 && w->sent && w->phase == WC_REGISTERING) {
        FD_SET(w->fd, reads); if (w->fd > *maxfd) *maxfd = w->fd;
    }
    if (w->phase != WC_OFF || (w->desired && w->validated) || w->failed) {
        long long at = w->phase == WC_OFF && w->failed ? 0 : w->wake;
        if (*deadline < 0 || at < *deadline) *deadline = at;
    }
}

static void spawn_child(struct wcifsnd *w, long long now) {
    int limit = getdtablesize();
    pid_t child = fork();
    if (child == 0) {
        int fd;
        sigset_t mask;
        sigemptyset(&mask); sigprocmask(SIG_SETMASK, &mask, NULL);
        signal(SIGTERM, SIG_DFL); signal(SIGINT, SIG_DFL); signal(SIGPIPE, SIG_DFL);
        signal(SIGHUP, SIG_DFL); signal(SIGCHLD, SIG_DFL); signal(SIGALRM, SIG_DFL);
        alarm(0);
        fd = open("/dev/null", O_RDWR);
        if (fd < 0 || dup2(fd, 0) < 0 || dup2(fd, 1) < 0 || dup2(fd, 2) < 0) _exit(126);
        /* The dns_sd stub does not mark all IPC sockets CLOEXEC. Closing
         * all inherited descriptors prevents a child retaining Bonjour refs. */
        for (fd = 3; fd < limit; fd++) close(fd);
        execl(WCIFSND_PATH, "wcifsnd", (char *)NULL);
        _exit(127);
    }
    if (child < 0) { fail(w, "fork failed", now); return; }
    w->child = child; w->phase = WC_STARTING;
    w->deadline = now + WCIFSND_START_MS; w->wake = now + 100;
    w->record = 0; w->sent = 0; w->active_since = -1;
}

static int ready_socket(struct wcifsnd *w) {
    struct sockaddr_in a = endpoint(WCIFSND_PORT);
    int fd = socket(AF_INET, SOCK_DGRAM, 0), rc, error;
    if (fd < 0) return -1;
    rc = bind(fd, (struct sockaddr *)&a, sizeof(a)); error = errno;
    close(fd);
    if (rc == 0) return 0;
    if (error != EADDRINUSE) return -1;
    w->fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (w->fd < 0 || w->fd >= FD_SETSIZE) return -1;
    a = endpoint(0);
    if (fcntl(w->fd, F_SETFD, FD_CLOEXEC) || fcntl(w->fd, F_SETFL, O_NONBLOCK) ||
        bind(w->fd, (struct sockaddr *)&a, sizeof(a))) return -1;
    a = endpoint(WCIFSND_PORT);
    return connect(w->fd, (struct sockaddr *)&a, sizeof(a)) ? -1 : 1;
}

int wcifsnd_dispatch(struct wcifsnd *w, const fd_set *reads, long long now) {
    if (w->child > 0) {
        int status;
        pid_t got = waitpid(w->child, &status, WNOHANG);
        if (got == w->child || (got < 0 && errno == ECHILD)) {
            int stopping = w->phase == WC_STOPPING;
            w->child = 0; w->phase = WC_OFF; w->wake = 0;
            if (!stopping) fail(w, "child exited", now);
        } else if (got < 0 && errno != EINTR) {
            timestamped_fprintf(stderr, "wcifsnd: waitpid failed; cannot safely replace child\n");
            return -1;
        }
    }
    if (w->phase == WC_OFF) {
        if (w->failed) {
            /* Apple adds are reference-counted and survive client exit. A
             * lost ACK must never cause another add to that native child.
             * Reap it first, then retry with empty native registration state;
             * Bonjour keeps its independent mDNSResponder connections. */
            w->failed = 0;
            if (w->failures < 6) w->failures++;
            w->wake = now + (1000LL << w->failures);
            timestamped_fprintf(stderr, "wcifsnd: retry in %lld ms\n", w->wake - now);
        }
        if (w->desired && w->validated && now >= w->wake) spawn_child(w, now);
        return 0;
    }
    if (w->phase == WC_STOPPING) {
        if (now >= w->deadline) {
            if (w->killed) {
                timestamped_fprintf(stderr, "wcifsnd: child did not exit after SIGKILL; replacing discovery generation\n");
                return -1;
            }
            (void)kill(w->child, SIGKILL); w->killed = 1;
            w->deadline = now + WCIFSND_STOP_MS;
        }
        w->wake = now + 100;
        return 0;
    }
    if (w->phase == WC_STARTING && now >= w->wake) {
        int ready = ready_socket(w);
        if (ready < 0 || (ready == 0 && now >= w->deadline)) {
            fail(w, "control listener unavailable", now); return 0;
        }
        w->wake = now + 100;
        if (!ready) return 0;
        w->phase = WC_REGISTERING; w->deadline = now + WCIFSND_REPLY_MS;
        request_name(w);
    }
    if (w->phase == WC_REGISTERING) {
        if (now >= w->deadline) { fail(w, "registration timed out", now); return 0; }
        if (!w->sent && now >= w->wake) {
            ssize_t n = send(w->fd, w->request, sizeof(w->request), 0);
            if (n == (ssize_t)sizeof(w->request)) w->sent = 1;
            else if (n >= 0 || (errno != EINTR && errno != EAGAIN && errno != EWOULDBLOCK)) {
                fail(w, "registration send failed", now); return 0;
            }
        }
        if (w->sent && reads && FD_ISSET(w->fd, reads)) {
            unsigned char reply[512];
            ssize_t n = recv(w->fd, reply, sizeof(reply), 0);
            int result;
            if (n < 0) {
                if (errno != EINTR && errno != EAGAIN && errno != EWOULDBLOCK) fail(w, "registration receive failed", now);
                return 0;
            }
            result = response(w, reply, (size_t)n);
            if (result < 0) { fail(w, "registration rejected or invalid reply", now); return 0; }
            if (result > 0) {
                w->record++;
                if (w->record == 1 && !strcmp(w->name, "WORKGROUP")) w->record++;
                if (w->record == 3) {
                    w->phase = WC_ACTIVE; w->active_since = now;
                    timestamped_fprintf(stderr, "wcifsnd: registered %s with Apple's native NetBIOS daemon\n", w->name);
                } else { request_name(w); w->sent = 0; w->deadline = now + WCIFSND_REPLY_MS; }
            }
        }
        w->wake = now + (w->sent ? 1000 : 100);
        if (w->wake > w->deadline) w->wake = w->deadline;
    } else if (w->phase == WC_ACTIVE) w->wake = now + 1000;
    return 0;
}

void wcifsnd_shutdown(struct wcifsnd *w) {
    int i;
    w->desired = 0; w->failed = 0;
    stop_child(w, acp_monotonic_ms());
    for (i = 0; w->child > 0 && i < 50; i++) {
        struct timeval pause = {0, 100000};
        if (wcifsnd_dispatch(w, NULL, acp_monotonic_ms()) < 0) break;
        if (w->child > 0) select(0, NULL, NULL, NULL, &pause);
    }
}
