#ifndef TC_REPORT_DEADLINE_H
#define TC_REPORT_DEADLINE_H
/* The manager stops a `telemetry --report` (a buffer-stall report) after
 * this long. Telemetry checks its own worst case for a one-shot heartbeat
 * against it (telemetry/main.c). */
#ifndef TC_BUFSTALL_REPORT_TIMEOUT_MS
#define TC_BUFSTALL_REPORT_TIMEOUT_MS 180000
#endif
#endif
