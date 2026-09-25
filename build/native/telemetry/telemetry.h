#ifndef TC_TELEMETRY_H
#define TC_TELEMETRY_H
#include "../common/platform.h"
#include <sys/stat.h>
#include <limits.h>
#include "../vendor/tweetnacl.h"
#define HEARTBEAT_AGENT_VERSION "3"
#ifndef HEARTBEAT_ENDPOINT
#define HEARTBEAT_ENDPOINT "http://timecapsulesmb.jamesyc.com/v1/router-heartbeats"
#endif
#define HEARTBEAT_TOKEN "8a3598c2b142dffda9513a4c41ff4dacb47abce9cd4e85cc0fd3260257c40c5d"
#define HEARTBEAT_MAX_FIELD 256
#define HEARTBEAT_MAX_JSON 4096
#ifndef HEARTBEAT_FLASH_CONFIG_PATH
#define HEARTBEAT_FLASH_CONFIG_PATH "/mnt/Flash/tcapsulesmb.conf"
#endif
#define HEARTBEAT_DEPLOY_RELEASE_TAG_KEY "TC_DEPLOY_RELEASE_TAG"


#ifndef TC_TELEMETRY_LANE
#define TC_TELEMETRY_LANE "6"
#endif
#ifndef TC_DEBUG_BASE_URL
#define TC_DEBUG_BASE_URL "http://timecapsulesmb.jamesyc.com/downloads/bin/heartbeat"
#endif
#ifndef TC_DEBUG_QUERY
/* The server maps this additive query to debug artifacts while legacy
 * heartbeat downloads retain their original meaning and HTTP proxy route. */
#define TC_DEBUG_QUERY "?debug=true"
#endif
#ifndef TC_CURL_PATH
#define TC_CURL_PATH "/usr/bin/curl"
#endif
#ifndef TC_TELEMETRY_WORK_ROOT
#define TC_TELEMETRY_WORK_ROOT "/mnt/Memory"
#endif
#define TC_DEBUG_PATH TC_TELEMETRY_WORK_ROOT "/debug"
#define TC_DEBUG_SIGNATURE_PATH TC_TELEMETRY_WORK_ROOT "/debug.sig"
#define TC_RESPONSE_MAX 4096
#define TC_DEBUG_MAX 1048576
#define TC_SIGNATURE_MAX 512
#define TC_INTERVAL_SECONDS 43200
/* An undelivered heartbeat (usually DNS not ready yet right after boot)
 * retries after this delay, doubling up to TC_INTERVAL_SECONDS. */
#ifndef TC_HEARTBEAT_RETRY_SECONDS
#define TC_HEARTBEAT_RETRY_SECONDS 60
#endif
#ifndef TC_CLEANUP_INTERVAL_SECONDS
#define TC_CLEANUP_INTERVAL_SECONDS 30
#endif
#define TC_EXIT_BUSY 75
struct telemetry_response { int debug; char signature[129]; };
struct telemetry_schedule { time_t next_due; int boot_sent; time_t retry_seconds; };
extern volatile sig_atomic_t telemetry_stop;
int telemetry_enabled(void);
int telemetry_payload(char *json, size_t cap, const char *reason, const char *nonce);
int telemetry_response_parse(const char *json, size_t len, struct telemetry_response *out);
int telemetry_verify(const unsigned char *data, size_t len, const unsigned char *signature, size_t sig_len);
int telemetry_authorized(const struct telemetry_response *response, const char *payload);
int telemetry_http(const char *url, const char *payload, unsigned char **out, size_t *len, size_t limit);
int telemetry_cycle(const char *reason, int lock_fd, int *delivered);
int telemetry_debug_job(const char *reason, int lock_fd);
int telemetry_nonce(char out[33]);
int telemetry_schedule_due(struct telemetry_schedule *schedule, time_t now);
void telemetry_schedule_finished(struct telemetry_schedule *schedule, time_t now, int delivered);
int telemetry_lock(void);
/* The caller must hold the workspace lock for these file mutations. */
void telemetry_workspace_error(const char *operation, const char *path);
int telemetry_remove_file(const char *path);
int telemetry_cleanup_locked(void);
int telemetry_recover(void);

#endif
