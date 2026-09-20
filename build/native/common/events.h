#ifndef TC_EVENTS_H
#define TC_EVENTS_H
#include "platform.h"

enum { TC_EVENT_STOP = 1, TC_EVENT_CHILD = 2, TC_EVENT_RELOAD = 4 };
struct tc_events {
    int wake[2], disk;
    volatile sig_atomic_t pending;
};
int tc_events_init(struct tc_events *);
void tc_events_close(struct tc_events *);
void tc_events_prepare(const struct tc_events *, fd_set *, int *maxfd);
unsigned tc_events_take(struct tc_events *);
int tc_events_disks(struct tc_events *);
#endif
