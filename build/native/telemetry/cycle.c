#include "telemetry.h"

int telemetry_nonce(char out[33]) {
    unsigned char bytes[16];
    size_t off = 0, i;
    int fd = open("/dev/urandom", O_RDONLY);
    if (fd < 0) return -1;
    while (off < sizeof(bytes)) {
        ssize_t n = read(fd, bytes + off, sizeof(bytes) - off);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) { close(fd); return -1; }
        off += (size_t)n;
    }
    close(fd);
    for (i = 0; i < sizeof(bytes); i++) snprintf(out + 2 * i, 3, "%02x", bytes[i]);
    return 0;
}

int telemetry_cycle(const char *reason, int lock_fd, int *delivered) {
    char payload[HEARTBEAT_MAX_JSON], nonce[33];
    unsigned char *body = NULL;
    size_t len;
    struct telemetry_response response;
    int rc;
    *delivered = 0;
    if (telemetry_nonce(nonce) || telemetry_payload(payload, sizeof(payload), reason, nonce) || telemetry_stop) return 1;
    if (telemetry_http(HEARTBEAT_ENDPOINT, payload, &body, &len, TC_RESPONSE_MAX)) {
        fputs("telemetry: POST failed\n", stderr); return 1;
    }
    /* The server has the heartbeat; later response or debug-job failures
     * must not resend it. */
    *delivered = 1;
    rc = telemetry_response_parse((const char *)body, len, &response);
    free(body);
    if (rc) { fputs("telemetry: invalid response\n", stderr); return 1; }
    if (!response.debug) return 0;
    if (!telemetry_authorized(&response, payload)) {
        fputs("telemetry: debug authorization signature invalid\n", stderr); return 1;
    }
    if (telemetry_stop) return 1;
    return telemetry_debug_job(reason, lock_fd);
}
