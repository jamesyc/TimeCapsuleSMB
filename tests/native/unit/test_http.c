/* telemetry_http's parent deadline runs on the monotonic clock.
 *   test_http step    wall clock jumps an hour forward mid-request: still succeeds
 *   test_http expire  curl outlives TC_HTTP_DEADLINE_MS: killed, fails promptly
 * TC_CURL_PATH names a fake curl that sleeps $FAKE_CURL_SLEEP seconds, then
 * prints a body and a 200 status the way `-w "\n%{http_code}"` does. */
#include "telemetry.h"
#include "acp.h"
#include <assert.h>

volatile sig_atomic_t telemetry_stop = 0;

/* NTP stepping the clock: every read after the first is an hour later. */
static int wall_reads = 0;
time_t time(time_t *out) {
    struct timeval now;
    time_t value;
    gettimeofday(&now, NULL);
    value = now.tv_sec + (wall_reads++ ? 3600 : 0);
    if (out) *out = value;
    return value;
}

int main(int argc, char **argv) {
    unsigned char *body = NULL;
    size_t len = 0;
    long long started = acp_monotonic_ms();
    int rc;

    if (argc != 2) return 2;
    rc = telemetry_http("http://127.0.0.1/unused", "{}", &body, &len, 64);
    if (!strcmp(argv[1], "step")) {
        assert(rc == 0);
        assert(len == 2 && !memcmp(body, "ok", 2));
        free(body);
    } else {
        assert(rc == -1 && body == NULL);
        /* Killed at the deadline, not after curl's full sleep. */
        assert(acp_monotonic_ms() - started < TC_HTTP_DEADLINE_MS + 2000);
    }
    return 0;
}
