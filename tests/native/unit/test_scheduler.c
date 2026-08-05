#include "telemetry.h"
#include <assert.h>
int main(void) {
    struct telemetry_schedule s = {0, 0};
    assert(telemetry_schedule_due(&s, 100));
    telemetry_schedule_started(&s, 100);
    assert(s.boot_sent);
    assert(!telemetry_schedule_due(&s, 100));
    assert(!telemetry_schedule_due(&s, 43299));
    assert(telemetry_schedule_due(&s, 43300));
    telemetry_schedule_started(&s, 43300);
    assert(!telemetry_schedule_due(&s, 43300));
    return 0;
}
