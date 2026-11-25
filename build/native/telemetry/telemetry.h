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
 * buffers bound the payload before the delivery record and health: about
 * 4 KB of fields at their limits, ~0.4 KB of v2 fields and TC_MAX_LINKS
 * links of up to ~160 bytes with fully escaped names, at most
 * TC_PAYLOAD_BASE_MAX (test_largest_possible_heartbeat_still_fits). The
 * delivery record adds at most TC_DELIVERY_JSON_MAX and the health object
 * TC_HEALTH_JSON_MAX (test_payload.c, test_health.c); payload.c checks
 * that the three fit. An oversized payload sends no heartbeat; a health
 * object that does not fit goes as null. */
#define HEARTBEAT_MAX_JSON 16384
#define TC_PAYLOAD_BASE_MAX 9728
#define TC_DELIVERY_JSON_MAX 192
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
/* The first retry of an undelivered heartbeat (usually DNS not ready yet
 * right after boot); planner.h has how later ones back off and their caps. */
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
 * several failures (building the payload, then the request) reports the
 * first in this order: delivered, cancelled, busy, local_resource,
 * acp_unavailable, network, server. Within the request (telemetry_http), the
 * failures of the name lookup and of each address rank differently: server,
 * then local_resource, then network, since a server that answered at all
 * shows the network works. */
enum tc_outcome {
    TC_OUTCOME_DELIVERED, TC_OUTCOME_CANCELLED, TC_OUTCOME_BUSY, TC_OUTCOME_LOCAL_RESOURCE,
    TC_OUTCOME_ACP_UNAVAILABLE, TC_OUTCOME_NETWORK, TC_OUTCOME_SERVER
};

struct tc_heartbeat_facts;
/* Which acp facts a heartbeat may keep instead of reading acp
 * (tc_heartbeat_facts_reusable): ones under max_age seconds old and, with
 * complete_only, from a complete collection. A zeroed one ({0, 0}) always
 * reads, as one-shot modes do; TC_FACTS_FOREVER keeps any it has. With
 * nothing collected yet a heartbeat reads whatever this says. The planner
 * picks one per attempt (planner.h's table). */
struct tc_facts_reuse { long max_age; int complete_only; };
#define TC_FACTS_FOREVER LONG_MAX
/* What building a payload found: failure is why it failed (cancelled,
 * acp_unavailable when there is no identity, server when it does not fit);
 * local_resource that an acp child could not start for want of resources
 * (the payload still went, with cached values). */
struct tc_payload_result { enum tc_outcome failure; int local_resource; };
/* Failed attempts since the last delivered heartbeat (planner.h), sent as
 * "delivery": how long and why a device could not report. since is a
 * monotonic time from this process, 0 for none. */
struct tc_delivery { unsigned failed; long since; unsigned local_resource, acp_unavailable, network, server; };
/* A heartbeat's id, kept from its first attempt until it is delivered, so
 * every retry of one heartbeat carries one id and the server keeps the
 * latest attempt (it may have stored an earlier one whose answer never
 * came back). Two states: none (id empty), or pending for one reason and
 * router ID. telemetry_payload builds an id when there is none or the
 * reason or router ID changed (a scheduled retry that became resource_high
 * is another heartbeat; the server ignores an id under another router);
 * the daemon clears it once a heartbeat is delivered. */
struct tc_heartbeat_ident {
    char id[72];
    char reason[64];
    char router_id[HEARTBEAT_MAX_FIELD + 8];
};
static inline void tc_heartbeat_ident_clear(struct tc_heartbeat_ident *ident) { ident->id[0] = '\0'; }
/* What a payload is built from: the facts this process keeps, which of them
 * it may keep (struct tc_facts_reuse), the delivery record (NULL: none) and the
 * pending heartbeat id (NULL: a new id each time, as one-shot modes send). */
struct tc_health;
struct tc_payload_context {
    struct tc_heartbeat_facts *facts;
    struct tc_facts_reuse reuse;
    const struct tc_delivery *delivery;
    struct tc_heartbeat_ident *ident;
    struct tc_health *health;  /* NULL: no health object */
    /* The attempt record: what this attempt's payload carried, the health
     * events among them (health.h), which the daemon marks reported once
     * the heartbeat is delivered. telemetry_payload clears it and fills it
     * in; the daemon clears it when it plans an attempt, so an attempt that
     * never builds a payload (workspace busy, no nonce) carries nothing. */
    struct tc_payload_sent { int health; unsigned health_token; } sent;
};
int telemetry_payload(char *json, size_t cap, const char *reason, const char *nonce,
                      struct tc_payload_context *ctx, struct tc_payload_result *result);
int telemetry_response_parse(const char *json, size_t len, struct telemetry_response *out);
int telemetry_verify(const unsigned char *data, size_t len, const unsigned char *signature, size_t sig_len);
int telemetry_authorized(const struct telemetry_response *response, const char *payload);
/* The whole request, name lookup included (http.c). */
#ifndef TC_HTTP_DEADLINE_MS
#define TC_HTTP_DEADLINE_MS 70000
#endif
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
int telemetry_cycle(const char *reason, int lock_fd, struct tc_payload_context *ctx, enum tc_outcome *outcome);
int telemetry_debug_job(const char *reason, int lock_fd);
int telemetry_nonce(char out[33]);
int telemetry_lock(void);
/* The caller must hold the workspace lock for these file mutations. */
void telemetry_workspace_error(const char *operation, const char *path);
int telemetry_remove_file(const char *path);
int telemetry_cleanup_locked(void);
int telemetry_recover(void);

#endif
