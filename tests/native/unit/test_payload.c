#include "device.h"
#include <assert.h>
static int mode;
void trim_line(char *v) { (void)v; }
int read_first_line_command(const char *command, char *out, size_t cap) {
    (void)command; snprintf(out, cap, "NetBSD"); return 0;
}
int read_acp_value(const char *key, char *out, size_t cap) {
    const char *v = "";
    if (!strcmp(key, "syAP")) v = "0x6a";
    if (!strcmp(key, "syAM")) v = "TimeCapsule6,106";
    if (!strcmp(key, "syNm")) v = "Name\n\"quoted\"";
    if (!strcmp(key, "sySN") && !mode) v = "serial123";
    snprintf(out, cap, "%s", v);
    return *v ? 0 : -1;
}
int read_deploy_release_tag(char *out, size_t cap) { snprintf(out, cap, "v-test"); return 0; }
int read_uptime_seconds(long *out) { *out = 123; return 0; }
int main(void) {
    char json[4096];
    assert(!telemetry_payload(json, sizeof(json), "manual", "0123456789abcdef0123456789abcdef"));
    assert(strstr(json, "\"router_id\":\"serial123\""));
    assert(strstr(json, "\"device_syap\":\"106\""));
    assert(strstr(json, "Name\\n\\\"quoted\\\""));
    assert(strstr(json, "\"uptime_sec\":123"));
    assert(strstr(json, "\"deploy_release_tag\":\"v-test\""));
    assert(telemetry_payload(json, 12, "manual", ""));
    mode = 1;
    assert(!telemetry_payload(json, sizeof(json), "manual", ""));
    puts(json);
    return 0;
}
