#include "events.h"
#if defined(__NetBSD__)
#include <sys/event.h>
#endif

static struct tc_events *signal_owner;
static void signal_received(int sig) {
    int saved = errno;
    if (signal_owner) {
        unsigned char byte = 1;
        signal_owner->pending |= sig == SIGCHLD  ? TC_EVENT_CHILD
                                 : sig == SIGHUP ? TC_EVENT_RELOAD
                                                 : TC_EVENT_STOP;
        /* A full pipe is already readable. Flags carry the event; bytes only
         * close the signal-before-select race without allocating a file. */
        (void)write(signal_owner->wake[1], &byte, 1);
    }
    errno = saved;
}
static void signals(sigset_t *set) {
    sigemptyset(set);
    sigaddset(set, SIGTERM);
    sigaddset(set, SIGINT);
    sigaddset(set, SIGHUP);
    sigaddset(set, SIGCHLD);
}
static int disk_events_open(void) {
#if defined(__NetBSD__)
    struct kfilter_mapping mapping;
    struct kevent change;
    struct timespec zero = {0, 0};
    int fd = kqueue();
    if (fd < 0)
        return -1;
    memset(&mapping, 0, sizeof(mapping));
    mapping.name = (char *)"EVFILT_DEVICE";
    /* Apple diskd uses this private filter on both NetBSD 4 and 6. Resolve it
     * by name; do not consume printd's single-reader /dev/usb event stream.
     * Two independent subscriptions and select readiness were verified on
     * NetBSD 4 in the September 20 cable-bump investigation. */
    if (fd >= FD_SETSIZE || fcntl(fd, F_SETFD, FD_CLOEXEC) || ioctl(fd, KFILTER_BYNAME, &mapping))
        goto fail;
    EV_SET(&change, 0, mapping.filter, EV_ADD, 0, 0, 0);
    if (kevent(fd, &change, 1, NULL, 0, &zero))
        goto fail;
    return fd;
fail:
    close(fd);
    return -1;
#else
    return -1;
#endif
}
int tc_events_init(struct tc_events *events) {
    struct sigaction action;
    int i;
    memset(events, 0, sizeof(*events));
    events->wake[0] = events->wake[1] = events->disk = -1;
    if (pipe(events->wake))
        return -1;
    for (i = 0; i < 2; i++) {
        int flags = fcntl(events->wake[i], F_GETFL);
        if (events->wake[i] >= FD_SETSIZE || flags < 0 ||
            fcntl(events->wake[i], F_SETFL, flags | O_NONBLOCK) ||
            fcntl(events->wake[i], F_SETFD, FD_CLOEXEC)) {
            tc_events_close(events);
            return -1;
        }
    }
    memset(&action, 0, sizeof(action));
    action.sa_handler = signal_received;
    signals(&action.sa_mask);
    signal_owner = events;
    if (sigaction(SIGTERM, &action, NULL) || sigaction(SIGINT, &action, NULL) ||
        sigaction(SIGHUP, &action, NULL) || sigaction(SIGCHLD, &action, NULL)) {
        tc_events_close(events);
        return -1;
    }
    signal(SIGPIPE, SIG_IGN);
    events->disk = disk_events_open();
    if (events->disk < 0)
        fputs("events: Apple device notifications unavailable; using inventory fallback\n", stderr);
    return 0;
}
void tc_events_close(struct tc_events *events) {
    if (signal_owner == events)
        signal_owner = NULL;
    if (events->wake[0] >= 0)
        close(events->wake[0]);
    if (events->wake[1] >= 0)
        close(events->wake[1]);
    if (events->disk >= 0)
        close(events->disk);
    events->wake[0] = events->wake[1] = events->disk = -1;
}
void tc_events_prepare(const struct tc_events *events, fd_set *reads, int *maxfd) {
    FD_SET(events->wake[0], reads);
    if (events->wake[0] > *maxfd)
        *maxfd = events->wake[0];
    if (events->disk >= 0) {
        FD_SET(events->disk, reads);
        if (events->disk > *maxfd)
            *maxfd = events->disk;
    }
}
unsigned tc_events_take(struct tc_events *events) {
    sigset_t mask, old;
    unsigned result;
    unsigned char bytes[128];
    signals(&mask);
    sigprocmask(SIG_BLOCK, &mask, &old);
    result = (unsigned)events->pending;
    events->pending = 0;
    while (read(events->wake[0], bytes, sizeof(bytes)) > 0) {
    }
    sigprocmask(SIG_SETMASK, &old, NULL);
    return result;
}
int tc_events_disks(struct tc_events *events) {
#if defined(__NetBSD__)
    struct kevent changes[16];
    struct timespec zero = {0, 0};
    int round, changed = 0;
    if (events->disk < 0)
        return 0;
    for (round = 0; round < 4; round++) {
        int n = kevent(events->disk, NULL, 0, changes, 16, &zero), i;
        if (n < 0 && errno == EINTR)
            continue;
        if (n < 0)
            goto failed;
        for (i = 0; i < n; i++) {
            if (changes[i].flags & EV_ERROR)
                goto failed;
            if (changes[i].fflags & 0xc0000000u)
                changed = 1;
        }
        if (n < 16)
            break;
    }
    return changed;
failed:
    fputs("events: device notification failed; using inventory fallback\n", stderr);
    close(events->disk);
    events->disk = -1;
    return changed;
#else
    (void)events;
    return 0;
#endif
}
