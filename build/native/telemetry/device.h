#ifndef TC_TELEMETRY_DEVICE_H
#define TC_TELEMETRY_DEVICE_H
#include "telemetry.h"
#include "../common/acp.h"
#include "../common/config.h"
int read_deploy_release_tag(char *out, size_t cap);
int read_uptime_seconds(long *out);
#endif
