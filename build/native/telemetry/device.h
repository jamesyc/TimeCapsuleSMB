#ifndef TC_TELEMETRY_DEVICE_H
#define TC_TELEMETRY_DEVICE_H
#include "telemetry.h"
void trim_line(char *value);
enum { ACP_OK = 0, ACP_UNAVAILABLE = -1, ACP_ABORT = -2 };
#ifndef TC_ACP_PATH
#define TC_ACP_PATH "/usr/bin/acp"
#endif
#ifndef TC_ACP_TIMEOUT_SECONDS
#define TC_ACP_TIMEOUT_SECONDS 20
#endif
int read_acp_value(const char *key, char *out, size_t cap);
int read_deploy_release_tag(char *out, size_t cap);
int read_uptime_seconds(long *out);
#endif
