#include "telemetry.h"
#include <assert.h>
int main(void) {
    struct telemetry_schedule s = {0, 0, 0};
    /* The first heartbeat is due immediately. */
    assert(telemetry_schedule_due(&s, 100));

    /* An undelivered boot heartbeat retries with doubling delays and stays
     * a boot heartbeat. */
    telemetry_schedule_finished(&s, 100, 0);
    assert(!s.boot_sent);
    assert(!telemetry_schedule_due(&s, 100 + TC_HEARTBEAT_RETRY_SECONDS - 1));
    assert(telemetry_schedule_due(&s, 100 + TC_HEARTBEAT_RETRY_SECONDS));
    telemetry_schedule_finished(&s, 200, 0);
    assert(s.next_due == 200 + 2 * TC_HEARTBEAT_RETRY_SECONDS);
    telemetry_schedule_finished(&s, 300, 0);
    assert(s.next_due == 300 + 4 * TC_HEARTBEAT_RETRY_SECONDS);

    /* The backoff never exceeds the normal interval. */
    for (int i = 0; i < 40; i++) telemetry_schedule_finished(&s, 1000, 0);
    assert(s.retry_seconds == TC_INTERVAL_SECONDS);
    assert(s.next_due == 1000 + TC_INTERVAL_SECONDS);
    assert(!s.boot_sent);

    /* Delivery marks the boot heartbeat sent, resets the backoff, and
     * returns to the normal interval. */
    telemetry_schedule_finished(&s, 5000, 1);
    assert(s.boot_sent);
    assert(s.retry_seconds == 0);
    assert(!telemetry_schedule_due(&s, 5000 + TC_INTERVAL_SECONDS - 1));
    assert(telemetry_schedule_due(&s, 5000 + TC_INTERVAL_SECONDS));

    /* A later failed scheduled heartbeat starts the backoff from the base. */
    telemetry_schedule_finished(&s, 50000, 0);
    assert(s.boot_sent);
    assert(s.next_due == 50000 + TC_HEARTBEAT_RETRY_SECONDS);
    return 0;
}
