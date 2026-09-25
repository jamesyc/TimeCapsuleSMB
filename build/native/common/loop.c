#include "loop.h"
#include "log.h"
#if defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__DragonFly__)
#include <net/route.h>
#define TC_HAVE_PF_ROUTE 1
#endif

long long plan_loop_now_ms(void) {
    return acp_monotonic_ms();
}

static int open_route_socket(void) {
#ifdef TC_HAVE_PF_ROUTE
    int fd = socket(PF_ROUTE, SOCK_RAW, 0);
    int flags;
    if (fd < 0) {
        return -1;
    }
    flags = fcntl(fd, F_GETFL);
    if (flags >= 0) {
        (void)fcntl(fd, F_SETFL, flags | O_NONBLOCK);
    }
    (void)fcntl(fd, F_SETFD, FD_CLOEXEC);
    return fd;
#else
    return -1;
#endif
}

void plan_loop_init(struct plan_loop *loop, const struct plan_options *options, const char *facts_file) {
    memset(loop, 0, sizeof(*loop));
    loop->options = *options;
#ifdef TC_NATIVE_TEST
    loop->facts_file = facts_file;
    if (facts_file != NULL) {
        loop->route_fd = -1;
        return;
    }
#else
    (void)facts_file;
#endif
    loop->route_fd = open_route_socket();
    if (loop->route_fd < 0) {
        timestamped_fprintf(stderr, "plan: PF_ROUTE socket unavailable; relying on the %d s poll\n", TC_PLAN_POLL_MS / 1000);
    }
}

void plan_loop_close(struct plan_loop *loop) {
    if (loop->collecting) {
        facts_collect_cancel(&loop->collector);
        loop->collecting = 0;
    }
    if (loop->route_fd >= 0) {
        close(loop->route_fd);
        loop->route_fd = -1;
    }
}

void plan_loop_request(struct plan_loop *loop, long long now_ms) {
    if (loop->recollect_at_ms == 0 || loop->recollect_at_ms > now_ms) {
        loop->recollect_at_ms = now_ms;
    }
}

static void lower(long long *deadline_ms, long long candidate) {
    if (candidate >= 0 && (*deadline_ms < 0 || candidate < *deadline_ms)) {
        *deadline_ms = candidate;
    }
}

void plan_loop_prepare(struct plan_loop *loop, long long now_ms, fd_set *reads, int *maxfd, long long *deadline_ms) {
    (void)now_ms;
    if (loop->route_fd >= 0) {
        FD_SET(loop->route_fd, reads);
        if (loop->route_fd > *maxfd) *maxfd = loop->route_fd;
    }
    if (loop->collecting) {
        int fd = facts_collect_fd(&loop->collector);
        if (fd >= 0) {
            FD_SET(fd, reads);
            if (fd > *maxfd) *maxfd = fd;
        }
        lower(deadline_ms, facts_collect_deadline_ms(&loop->collector));
    } else {
        lower(deadline_ms, loop->next_poll_ms);
        if (loop->recollect_at_ms > 0) {
            lower(deadline_ms, loop->recollect_at_ms);
        }
    }
}

/* Interface and address messages on either kernel layout (F4/I1):
 * NEWADDR 0xc, DELADDR 0xd, OIFINFO/IFINFO 0xf, IFANNOUNCE 0x10, IFINFO 0x14. */
static int route_message_is_link_change(unsigned type) {
    return type == 0xc || type == 0xd || type == 0xf || type == 0x10 || type == 0x14;
}

static void drain_route_socket(struct plan_loop *loop, long long now_ms) {
    unsigned char buf[2048];
    for (;;) {
        ssize_t n = read(loop->route_fd, buf, sizeof(buf));
        if (n < 0) {
            if (errno == EINTR) continue;
            if (errno != EAGAIN && errno != EWOULDBLOCK) {
                timestamped_fprintf(stderr, "plan: PF_ROUTE read failed: %s; closing\n", strerror(errno));
                close(loop->route_fd);
                loop->route_fd = -1;
            }
            return;
        }
        if (n >= 4 && route_message_is_link_change(buf[3]) && loop->recollect_at_ms == 0) {
            loop->recollect_at_ms = now_ms + TC_PLAN_DEBOUNCE_MS;
        }
        if (n == 0) {
            return;
        }
    }
}

static void begin_collection(struct plan_loop *loop, long long now_ms) {
    loop->recollect_at_ms = 0;
    loop->next_poll_ms = now_ms + TC_PLAN_POLL_MS;
#ifdef TC_NATIVE_TEST
    if (loop->facts_file != NULL) {
        FILE *fp;
        int rc;
        loop->collecting = 0;
        fp = fopen(loop->facts_file, "r");
        rc = fp == NULL ? -1 : device_facts_parse_file(&loop->facts, fp);
        if (fp != NULL) (void)fclose(fp);
        if (rc != 0) {
            timestamped_fprintf(stderr, "plan: facts file %s unreadable\n", loop->facts_file);
            return;
        }
        loop->collector.done = 1;
        loop->collecting = 1;   /* finish_collection() consumes it below */
        return;
    }
#endif
    loop->collecting = 1;
    (void)facts_collect_begin(&loop->collector, &loop->facts);
}

static int finish_collection(struct plan_loop *loop, long long now_ms) {
    size_t i;
    loop->collecting = 0;
    (void)device_plan_build(&loop->current, &loop->facts,
                            loop->have_validated ? &loop->last_validated : NULL,
                            &loop->options, now_ms);
    /* C.11: an address without an IFINFO record still takes its role, but
     * it means our interface parsing missed a record; say so once. */
    for (i = 0; i < loop->current.link_count; i++) {
        const struct link_plan *link = &loop->current.links[i];
        if (link->synthetic && link->role != LINK_ROLE_ISOLATED && !loop->logged_synthetic) {
            timestamped_fprintf(stderr, "plan: interface index %u has addresses but no RTM_IFINFO record (parser gap); role=%s from its addresses\n",
                                link->link.index, link_role_name(link->role));
            loop->logged_synthetic = 1;
        }
    }
    if (loop->current.status.validated) {
        loop->last_validated = loop->current;
        loop->have_validated = 1;
    } else if (loop->have_validated) {
        device_plan_prune_history(&loop->last_validated, &loop->current);
    }
    return 1;
}

int plan_loop_dispatch(struct plan_loop *loop, long long now_ms, const fd_set *reads) {
    if (loop->route_fd >= 0 && reads != NULL && FD_ISSET(loop->route_fd, reads)) {
        drain_route_socket(loop, now_ms);
    }
    if (loop->collecting) {
        if (loop->collector.done || facts_collect_pump(&loop->collector) == 1) {
            return finish_collection(loop, now_ms);
        }
        return 0;
    }
    if ((loop->recollect_at_ms > 0 && now_ms >= loop->recollect_at_ms) ||
        (loop->next_poll_ms > 0 && now_ms >= loop->next_poll_ms) || loop->next_poll_ms == 0) {
        begin_collection(loop, now_ms);
        if (loop->collecting &&
            (loop->collector.done || facts_collect_pump(&loop->collector) == 1)) {
            return finish_collection(loop, now_ms);
        }
    }
    return 0;
}

int plan_loop_wait(fd_set *reads, int maxfd, long long now_ms, long long deadline_ms) {
    struct timeval timeout;
    long long wait_ms = deadline_ms < 0 ? TC_PLAN_POLL_MS : deadline_ms - now_ms;
    int rc;
    if (wait_ms < 0) wait_ms = 0;
    timeout.tv_sec = wait_ms / 1000;
    timeout.tv_usec = (wait_ms % 1000) * 1000;
    rc = select(maxfd + 1, reads, NULL, NULL, &timeout);
    if (rc < 0) {
        if (errno == EINTR) {
            FD_ZERO(reads);
            return 0;
        }
        return -1;
    }
    return rc;
}
