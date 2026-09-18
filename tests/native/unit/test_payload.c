#include "device.h"
#include "plan.h"
#include <assert.h>
#include <sys/utsname.h>
volatile sig_atomic_t telemetry_stop;
volatile sig_atomic_t acp_stop_requested;
static int mode;
void trim_line(char *v) { (void)v; }
int uname(struct utsname *name) {
    memset(name, 0, sizeof(*name));
    strcpy(name->sysname, "NetBSD"); strcpy(name->release, "6.0"); strcpy(name->machine, "evbarm");
    return 0;
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
/* The identity test supplies an incomplete plan without probing the host. */
int device_plan_collect(struct device_plan *out, const struct device_plan *previous, const struct plan_options *options) {
    assert(previous == NULL && options->diskless == 0);
    memset(out, 0, sizeof(*out));
    strcpy(out->status.reason, "mode");
    return 0;
}
const char *router_mode_name(enum router_mode value) {
    assert(value == ROUTER_MODE_UNKNOWN);
    return "unknown";
}
const char *link_role_name(enum link_role value) { (void)value; assert(0); return ""; }
int addr_is_service_address(const struct if_addr *addr) { (void)addr; assert(0); return 0; }
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
