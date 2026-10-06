#include "device.h"
/* ACP and flash-config readers live in common/ since v3.1.0 (acp.c,
 * config.c); this file keeps only the telemetry-specific pieces. */
int read_deploy_release_tag(char *out, size_t out_len) {
    return config_read_value(HEARTBEAT_FLASH_CONFIG_PATH, HEARTBEAT_DEPLOY_RELEASE_TAG_KEY, out, out_len);
}

int telemetry_enabled(void) {
    char value[16];
    /* Only an explicit opt-out disables reporting; older configs omit it. */
    return config_read_value(HEARTBEAT_FLASH_CONFIG_PATH, "TELEMETRY", value, sizeof(value)) != 0 ||
           strcmp(value, "false") != 0;
}

/* Uptime is the kernel's monotonic clock, which counts from boot. It is one
 * clock, so sntpd setting the wall clock after boot cannot change it; the
 * wall clock minus kern.boottime is two readings that agree only if the
 * kernel moves boottime by the same step. */
int read_uptime_seconds(long *out) {
    long long ms = acp_monotonic_ms();
    if (out == NULL || ms < 0) {
        return -1;
    }
    *out = (long)(ms / 1000);
    return 0;
}
