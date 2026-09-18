#include "device.h"
#if defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__)
#include <sys/sysctl.h>
#endif
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

int read_uptime_seconds(long *out) {
    if (out == NULL) {
        return -1;
    }
#if (defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__DragonFly__)) && defined(KERN_BOOTTIME)
    {
        int mib[2] = { CTL_KERN, KERN_BOOTTIME };
        struct timeval boot_time;
        size_t len = sizeof(boot_time);
        time_t now;

        memset(&boot_time, 0, sizeof(boot_time));
        if (sysctl(mib, 2, &boot_time, &len, NULL, 0) == 0 && boot_time.tv_sec > 0) {
            now = time(NULL);
            if (now >= boot_time.tv_sec && (unsigned long)(now - boot_time.tv_sec) <= (unsigned long)LONG_MAX) {
                *out = (long)(now - boot_time.tv_sec);
                return 0;
            }
        }
    }
#endif
#if defined(__linux__)
    {
        FILE *fp = fopen("/proc/uptime", "r");
        double uptime = 0.0;

        if (fp != NULL) {
            int parsed = fscanf(fp, "%lf", &uptime);
            (void)fclose(fp);
            if (parsed == 1 && uptime >= 0.0 && uptime <= (double)LONG_MAX) {
                *out = (long)uptime;
                return 0;
            }
        }
    }
#endif
    return -1;
}
