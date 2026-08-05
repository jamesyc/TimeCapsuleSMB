#include "telemetry.h"
int telemetry_schedule_due(struct telemetry_schedule *s, time_t now) {
    return !s->boot_sent || now >= s->next_due;
}
void telemetry_schedule_started(struct telemetry_schedule *s, time_t now) {
    s->boot_sent = 1;
    s->next_due = now + TC_INTERVAL_SECONDS;
}
