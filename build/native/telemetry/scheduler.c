#include "telemetry.h"
int telemetry_schedule_due(struct telemetry_schedule *s, time_t now) {
    return now >= s->next_due;
}
void telemetry_schedule_finished(struct telemetry_schedule *s, time_t now, int delivered) {
    if (delivered) {
        s->boot_sent = 1;
        s->retry_seconds = 0;
        s->next_due = now + TC_INTERVAL_SECONDS;
        return;
    }
    /* Keep boot_sent unchanged so a retried boot heartbeat still says "boot".
     * Back off so an unreachable server never sees a tight retry loop. */
    s->retry_seconds = s->retry_seconds ? s->retry_seconds * 2 : TC_HEARTBEAT_RETRY_SECONDS;
    if (s->retry_seconds > TC_INTERVAL_SECONDS) s->retry_seconds = TC_INTERVAL_SECONDS;
    s->next_due = now + s->retry_seconds;
}
