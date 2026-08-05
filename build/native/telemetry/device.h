#ifndef TC_TELEMETRY_DEVICE_H
#define TC_TELEMETRY_DEVICE_H
#include "telemetry.h"
void trim_line(char *value);
int read_first_line_command(const char *command, char *out, size_t cap);
int read_acp_value(const char *key, char *out, size_t cap);
int read_deploy_release_tag(char *out, size_t cap);
int read_uptime_seconds(long *out);
#endif
