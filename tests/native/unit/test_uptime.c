/* Heartbeat uptime (telemetry/device.c) is the kernel's monotonic clock.
 * sntpd steps the wall clock after a boot that started from a stale clock,
 * by a day in the field; that step must not move the reported uptime. This
 * time() replaces libc's for the code linked into this test. */
#include "device.h"
#include <assert.h>
#include <sys/time.h>

static time_t wall_step;
time_t time(time_t *out) {
    struct timeval now;
    time_t value;
    gettimeofday(&now, NULL);
    value = now.tv_sec + wall_step;
    if (out)
        *out = value;
    return value;
}

static long monotonic_seconds(void) {
    struct timespec now;
    assert(clock_gettime(CLOCK_MONOTONIC, &now) == 0);
    return (long)now.tv_sec;
}

int main(void) {
    long before, after, uptime, stepped;
    before = monotonic_seconds();
    assert(read_uptime_seconds(&uptime) == 0);
    after = monotonic_seconds();
    assert(before <= uptime && uptime <= after);
    wall_step = 86400;
    assert(time(NULL) > before + 86000); /* the step reaches this binary's time() */
    assert(read_uptime_seconds(&stepped) == 0);
    assert(stepped >= uptime && stepped <= monotonic_seconds());
    assert(read_uptime_seconds(NULL) == -1);
    return 0;
}
