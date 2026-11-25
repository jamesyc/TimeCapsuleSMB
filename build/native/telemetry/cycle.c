#include "telemetry.h"
#include "planner.h"
#include "entropy.h"

int telemetry_nonce(char out[33]) {
    unsigned char bytes[16];
    size_t i;
    if (tc_random_bytes(bytes, sizeof(bytes))) return -1;
    for (i = 0; i < sizeof(bytes); i++) snprintf(out + 2 * i, 3, "%02x", bytes[i]);
    return 0;
}

int telemetry_cycle(const char *reason, int lock_fd, struct tc_payload_context *ctx, enum tc_outcome *outcome) {
    /* Static: a heartbeat must not need memory the device may not have. */
    static unsigned char body[TC_HTTP_BUFFER(TC_RESPONSE_MAX)];
    char payload[HEARTBEAT_MAX_JSON], nonce[33];
    size_t len;
    enum tc_outcome posted;
    struct tc_payload_result built;
    struct telemetry_response response;
    int rc;
    *outcome = TC_OUTCOME_SERVER;
    if (telemetry_nonce(nonce)) {
        *outcome = tc_setup_outcome(errno, 0);
        return 1;
    }
    if (telemetry_payload(payload, sizeof(payload), reason, nonce, ctx, &built)) {
        *outcome = built.failure;
    } else if (telemetry_stop) {
        *outcome = TC_OUTCOME_CANCELLED;
    } else if (!telemetry_enabled()) {
        /* Turned off while the payload was built (an acp collection can
         * take a minute): the last moment before anything leaves the
         * device. The daemon then exits at its next check, and a report
         * fails, as one started with telemetry off does. */
        fputs("telemetry: turned off; heartbeat not sent\n", stderr);
        *outcome = TC_OUTCOME_CANCELLED;
    } else if (telemetry_http(HEARTBEAT_ENDPOINT, payload, body, sizeof(body), &len, &posted)) {
        *outcome = posted;
        fputs("telemetry: POST failed\n", stderr);
    } else {
        /* The server has the heartbeat; later response or debug-job
         * failures must not resend it. */
        *outcome = TC_OUTCOME_DELIVERED;
    }
    /* A child that could not start ranks above the failure it led to. */
    if (built.local_resource && *outcome > TC_OUTCOME_LOCAL_RESOURCE) *outcome = TC_OUTCOME_LOCAL_RESOURCE;
    if (*outcome != TC_OUTCOME_DELIVERED) return 1;
    /* A report owns no debug workspace and must remain safe to kill. Its
     * success is the POST itself, regardless of the server's debug reply. */
    if (lock_fd < 0) return 0;
    rc = telemetry_response_parse((const char *)body, len, &response);
    if (rc) { fputs("telemetry: invalid response\n", stderr); return 1; }
    if (!response.debug) return 0;
    if (!telemetry_authorized(&response, payload)) {
        fputs("telemetry: debug authorization signature invalid\n", stderr); return 1;
    }
    if (telemetry_stop) return 1;
    return telemetry_debug_job(reason, lock_fd);
}
