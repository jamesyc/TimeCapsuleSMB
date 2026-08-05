#include "device.h"
#if defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__)
#include <sys/sysctl.h>
#endif
void trim_line(char *value) {
    size_t len;

    if (value == NULL) {
        return;
    }
    while (*value != '\0' && isspace((unsigned char)*value)) {
        memmove(value, value + 1, strlen(value));
    }
    len = strlen(value);
    while (len > 0 && isspace((unsigned char)value[len - 1])) {
        value[--len] = '\0';
    }
}

int read_first_line_command(const char *command, char *out, size_t out_len) {
    FILE *fp;

    if (out_len == 0) {
        return -1;
    }
    out[0] = '\0';
    fp = popen(command, "r");
    if (fp == NULL) {
        return -1;
    }
    if (fgets(out, (int)out_len, fp) == NULL) {
        (void)pclose(fp);
        out[0] = '\0';
        return -1;
    }
    (void)pclose(fp);
    out[out_len - 1] = '\0';
    trim_line(out);
    return out[0] == '\0' ? -1 : 0;
}

int read_acp_value(const char *key, char *out, size_t out_len) {
    char command[128];

    if (snprintf(command, sizeof(command), "/usr/bin/acp -q %s 2>/dev/null", key) >= (int)sizeof(command)) {
        return -1;
    }
    return read_first_line_command(command, out, out_len);
}


static int copy_config_value(char *out, size_t out_len, char *value) {
    char quote;
    char *end;
    char *tail;
    size_t len;

    if (out_len == 0) {
        return -1;
    }
    out[0] = '\0';
    trim_line(value);
    if (value[0] == '\'' || value[0] == '"') {
        quote = value[0];
        value++;
        end = strchr(value, quote);
        if (end == NULL) {
            return -1;
        }
        tail = end + 1;
        while (*tail != '\0' && isspace((unsigned char)*tail)) {
            tail++;
        }
        if (*tail != '\0') {
            return -1;
        }
        *end = '\0';
    }
    len = strlen(value);
    if (len >= out_len) {
        return -1;
    }
    memcpy(out, value, len + 1);
    return 0;
}

static int read_config_value_file(const char *path, const char *key, char *out, size_t out_len) {
    FILE *fp;
    char line[512];
    size_t key_len = strlen(key);

    if (out_len == 0) {
        return -1;
    }
    out[0] = '\0';
    fp = fopen(path, "r");
    if (fp == NULL) {
        return -1;
    }
    while (fgets(line, sizeof(line), fp) != NULL) {
        char *cursor = line;
        while (*cursor != '\0' && isspace((unsigned char)*cursor)) {
            cursor++;
        }
        if (strncmp(cursor, key, key_len) != 0) {
            continue;
        }
        cursor += key_len;
        while (*cursor != '\0' && isspace((unsigned char)*cursor)) {
            cursor++;
        }
        if (*cursor != '=') {
            continue;
        }
        cursor++;
        (void)fclose(fp);
        return copy_config_value(out, out_len, cursor);
    }
    (void)fclose(fp);
    return -1;
}

int read_deploy_release_tag(char *out, size_t out_len) {
    return read_config_value_file(HEARTBEAT_FLASH_CONFIG_PATH, HEARTBEAT_DEPLOY_RELEASE_TAG_KEY, out, out_len);
}

int telemetry_enabled(void) {
    char value[16];
    /* Only an explicit opt-out disables reporting; older configs omit it. */
    return read_config_value_file(HEARTBEAT_FLASH_CONFIG_PATH, "TELEMETRY", value, sizeof(value)) != 0 ||
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
