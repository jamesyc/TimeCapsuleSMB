#ifndef TC_TELEMETRY_H
#define TC_TELEMETRY_H
#include "../common/platform.h"
#include <sys/stat.h>
#include <limits.h>
#include "../vendor/tweetnacl.h"
#define HEARTBEAT_AGENT_VERSION "4"
#ifndef HEARTBEAT_ENDPOINT
#define HEARTBEAT_ENDPOINT "http://timecapsulesmb.jamesyc.com/v1/router-heartbeats"
#endif
#define HEARTBEAT_TOKEN "8a3598c2b142dffda9513a4c41ff4dacb47abce9cd4e85cc0fd3260257c40c5d"
#define HEARTBEAT_MAX_FIELD 256
/* json_escape refuses a field longer than its buffer in payload.c, so those
 * buffers bound the payload: about 4 KB of fields at their limits, ~0.4 KB of
 * v2 fields and TC_MAX_LINKS links of up to ~160 bytes with fully escaped
 * names, about 9.5 KB in all. An oversized payload sends no heartbeat. */
#define HEARTBEAT_MAX_JSON 16384
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
extern volatile sig_atomic_t telemetry_stop;
int telemetry_enabled(void);
/* How a heartbeat attempt ended, for the retry rules. One attempt that meets
 * several failures reports the first in this order: delivered, cancelled,
 * busy, local_resource, acp_unavailable, network, server. */
enum tc_outcome {
    TC_OUTCOME_DELIVERED, TC_OUTCOME_CANCELLED, TC_OUTCOME_BUSY, TC_OUTCOME_LOCAL_RESOURCE,
    TC_OUTCOME_ACP_UNAVAILABLE, TC_OUTCOME_NETWORK, TC_OUTCOME_SERVER
};

struct tc_heartbeat_facts;
/* What building a payload found: failure is why it failed (cancelled,
 * acp_unavailable when there is no identity, server when it does not fit);
 * local_resource that an acp child could not start for want of resources
 * (the payload still went, with cached values). */
struct tc_payload_result { enum tc_outcome failure; int local_resource; };
/* Failed attempts since the last delivered heartbeat (planner.h), sent as
 * "delivery": how long and why a device could not report. since is a
 * monotonic time from this process, 0 for none. */
struct tc_delivery { unsigned failed; long since; unsigned local_resource, acp_unavailable, network, server; };
/* What a payload is built from: the facts this process keeps, whether to
 * read acp afresh (heartbeat_facts.h; otherwise acp runs only if nothing was
 * ever read), and the delivery record (NULL: none). */
struct tc_payload_context {
    struct tc_heartbeat_facts *facts;
    int refresh;
    const struct tc_delivery *delivery;
};
int telemetry_payload(char *json, size_t cap, const char *reason, const char *nonce,
                      const struct tc_payload_context *ctx, struct tc_payload_result *result);
int telemetry_response_parse(const char *json, size_t len, struct telemetry_response *out);
int telemetry_verify(const unsigned char *data, size_t len, const unsigned char *signature, size_t sig_len);
int telemetry_authorized(const struct telemetry_response *response, const char *payload);
/* The status line and headers http.c reads before the body. */
#define TC_HTTP_HEADER_MAX 4096
/* The buffer telemetry_http needs for a body of at most limit bytes. */
#define TC_HTTP_BUFFER(limit) (TC_HTTP_HEADER_MAX + (limit) + 1)
/* GET (payload NULL) or POST url into buf (cap bytes: TC_HTTP_BUFFER of the
 * largest body accepted). 0 with the body at buf (NUL-terminated, *len
 * bytes), or -1; *outcome says which (http.c). */
int telemetry_http(const char *url, const char *payload, unsigned char *buf, size_t cap, size_t *len,
                   enum tc_outcome *outcome);
/* A negative lock_fd sends a report only, without debug files or execution. */
/* One heartbeat: payload, POST and, with a lock_fd, the debug job the reply
 * authorizes. *outcome says how the heartbeat itself ended (the debug job
 * comes after delivery and does not change it); the return value is the
 * exit status, which a failed debug job also sets. */
int telemetry_cycle(const char *reason, int lock_fd, const struct tc_payload_context *ctx, enum tc_outcome *outcome);
int telemetry_debug_job(const char *reason, int lock_fd);
int telemetry_nonce(char out[33]);
int telemetry_lock(void);
/* The caller must hold the workspace lock for these file mutations. */
void telemetry_workspace_error(const char *operation, const char *path);
int telemetry_remove_file(const char *path);
int telemetry_cleanup_locked(void);
int telemetry_recover(void);

#endif
