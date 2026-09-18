#ifndef TC_WCIFSND_H
#define TC_WCIFSND_H
#include "../common/plan.h"

/* Names belong to this child generation, not to the UDP client connection.
 * Never retransmit an accepted add: Apple's registrations are refcounted. */
enum wcifsnd_phase { WC_OFF, WC_STARTING, WC_REGISTERING, WC_ACTIVE, WC_STOPPING };
struct wcifsnd {
    enum wcifsnd_phase phase;
    pid_t child;
    int fd, enabled, desired, failed, killed, sent, record;
    uint16_t transaction;
    long long deadline, wake;
    char name[16];
    unsigned char request[68];
};
void wcifsnd_init(struct wcifsnd *w, const char *name);
void wcifsnd_apply_plan(struct wcifsnd *w, const struct device_plan *plan, long long now);
void wcifsnd_prepare(struct wcifsnd *w, fd_set *reads, int *maxfd, long long *deadline);
/* -1 asks the manager to replace the entire discovery generation. */
int wcifsnd_dispatch(struct wcifsnd *w, const fd_set *reads, long long now);
void wcifsnd_shutdown(struct wcifsnd *w);
#endif
