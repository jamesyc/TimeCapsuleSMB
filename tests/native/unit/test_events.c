#include "common/events.h"
#include <assert.h>

int main(void) {
    struct tc_events events;
    fd_set reads;
    struct timeval timeout = {1, 0};
    int maxfd = -1, i;
    alarm(10);
    assert(!tc_events_init(&events));
    assert(tc_events_take(&events) == 0);
    /* Apple device subscriptions are optional. Signals still wake select on
     * unsupported kernels; a signal before select must not be lost. */
    for (i = 0; i < 10000; i++)
        raise(SIGHUP);
    raise(SIGCHLD);
    FD_ZERO(&reads);
    tc_events_prepare(&events, &reads, &maxfd);
    assert(select(maxfd + 1, &reads, NULL, NULL, &timeout) > 0);
    assert(tc_events_take(&events) == (TC_EVENT_RELOAD | TC_EVENT_CHILD));
    assert(tc_events_take(&events) == 0);
    raise(SIGTERM);
    assert(tc_events_take(&events) == TC_EVENT_STOP);
    tc_events_close(&events);
    assert(events.wake[0] == -1 && events.wake[1] == -1 && events.disk == -1);
    return 0;
}
