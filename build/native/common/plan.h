#ifndef TC_PLAN_H
#define TC_PLAN_H
#include "platform.h"
#include "acp.h"
#include "iflist.h"
#include <sys/param.h>
#ifndef MAXHOSTNAMELEN
#define MAXHOSTNAMELEN 256
#endif

/* The device plan: facts -> topology -> policy -> identity, shared by
 * discovery (Bonjour registrations and native NBNS eligibility) and
 * telemetry. Samba enumerates and binds interfaces itself. */

enum link_role { LINK_ROLE_LAN, LINK_ROLE_WAN, LINK_ROLE_GUEST, LINK_ROLE_ISOLATED };
enum router_mode { ROUTER_MODE_UNKNOWN, ROUTER_MODE_BRIDGE, ROUTER_MODE_DHCP, ROUTER_MODE_NAT };
enum service_bit { SVC_SMB = 1, SVC_AFP = 2, SVC_ADISK = 4 };
/* One link may own every address the interface table can hold (64). A lower
 * per-link cap would silently drop discovery/telemetry address ownership. */
#define TC_MAX_ADDRS_PER_LINK TC_MAX_ADDRS

/* ---- raw facts ---- */

enum acp_key_index {
    ACP_KEY_raNA, ACP_KEY_raDS, ACP_KEY_waNM, ACP_KEY_usbF,
    ACP_KEY_laIP, ACP_KEY_waIP, ACP_KEY_waLL, ACP_KEY_gnRo,
    ACP_KEY_syNm, ACP_KEY_waMA,
    ACP_KEY_COUNT
};
extern const char *const device_acp_keys[ACP_KEY_COUNT];

struct device_config {
    int advertise_afp;
    int debug_logging;
};

struct device_facts {
    struct acp_value acp[ACP_KEY_COUNT];
    struct if_table ifs;
    int ifs_ok;
    struct device_config config;
    char hostname[MAXHOSTNAMELEN];
};

/* ---- derived plan ---- */

struct identity {
    char instance[64];          /* "" when no name is known */
    char netbios[16];
    char wama[18];              /* XX:XX:XX:XX:XX:XX or "" */
    int retained;               /* syNm/waMA read aborted; values carried from the previous plan */
};

struct link_plan {
    struct if_link link;
    enum link_role role;
    unsigned mask;
    int retained;               /* role/mask come from the last validated plan */
    struct if_addr addrs[TC_MAX_ADDRS_PER_LINK];
    size_t addr_count;
    int synthetic;              /* addresses only: no RTM_IFINFO for this index (parser gap) */
};

struct plan_status {
    int validated;              /* domains (a)+(b) coherent this collection */
    int cold_start;             /* no validated plan to retain from */
    char reason[32];            /* why not validated */
    unsigned long stale_seconds;
};

struct plan_options {
    int diskless;
};

struct device_plan {
    enum router_mode mode;
    struct link_plan links[TC_MAX_LINKS];
    size_t link_count;
    struct identity id;
    struct device_config config;  /* same collected settings used by telemetry */
    struct plan_status status;
    struct plan_options options;
    /* Snapshot of the raw inputs for --print-link-plan / telemetry. */
    struct acp_bool raNA, raDS, waNM;
    struct acp_u32 usbF;
    struct acp_ipv4 laIP, waIP, waLL, gnRo;
    int wan_disks_allowed;      /* usbF & 8 as read (0 when unavailable) */
    long long validated_at_ms;  /* monotonic; meaningful when validated */
    int addrs_truncated;        /* a link could not hold all its addresses */
};

/* addr.c */
enum addr_kind { ADDR_UNUSABLE, ADDR_LOOPBACK, ADDR_LINK_LOCAL, ADDR_PRIVATE, ADDR_ULA, ADDR_GLOBAL };
enum addr_kind addr4_kind(uint32_t network_order);
enum addr_kind addr6_kind(const struct in6_addr *addr);
int addr_is_service_address(const struct if_addr *addr);
const char *addr_text(const struct if_addr *addr, char *out, size_t out_len);

/* facts.c */
int collect_device_facts(struct device_facts *out);
/* Non-blocking variant for the daemons: ACP children run one at a time
 * under the collection budget; the cheap parts (iflist, config, hostname)
 * complete synchronously on the final pump. */
struct facts_collector {
    struct acp_collector acp;
    struct acp_request requests[ACP_KEY_COUNT];
    struct device_facts *facts;
    int done;
};
int facts_collect_begin(struct facts_collector *c, struct device_facts *facts);
int facts_collect_fd(const struct facts_collector *c);
long long facts_collect_deadline_ms(const struct facts_collector *c);
int facts_collect_pump(struct facts_collector *c);      /* 1 when finished */
void facts_collect_cancel(struct facts_collector *c);
int device_facts_read_config(struct device_config *out, const char *path);
#ifdef TC_NATIVE_TEST
int device_facts_parse_file(struct device_facts *out, FILE *fp);     /* --facts-file */
#endif

/* topology.c */
enum router_mode router_mode_from_facts(const struct device_facts *facts);
int topology_ownership_coherent(const struct device_facts *facts, const char **reason);
void topology_assign_roles(struct device_plan *plan, const struct device_facts *facts);
int link_owns_ipv4(const struct link_plan *link, uint32_t network_order);
const char *link_role_name(enum link_role role);
const char *router_mode_name(enum router_mode mode);

/* policy.c */
unsigned policy_lan_mask(const struct device_facts *facts, const struct plan_options *options);
void policy_assign_masks(struct device_plan *plan, const struct device_facts *facts);

/* identity.c */
void identity_derive(struct identity *out, const struct device_facts *facts);
int normalize_server_string(char *out, size_t out_len, const char *value);
int normalize_instance_name(char *out, size_t out_len, const char *value);
int normalize_netbios_name(char *out, size_t out_len, const char *value);
int normalize_host_label(char *out, size_t out_len, const char *value);
int normalize_mac_text(char *out, size_t out_len, const char *value);

/* plan.c — `previous` is the caller's LAST VALIDATED plan (or NULL); a plan
 * whose status is not validated must never be passed back as previous. */
void device_plan_prune_history(struct device_plan *previous, const struct device_plan *current);
int device_plan_build(struct device_plan *out, const struct device_facts *facts,
                      const struct device_plan *previous, const struct plan_options *options, long long now_ms);
int device_plan_collect(struct device_plan *out, const struct device_plan *previous, const struct plan_options *options);
#ifdef TC_NATIVE_TEST
int device_plan_collect_from_file(struct device_plan *out, const char *facts_path,
                                  const struct device_plan *previous, const struct plan_options *options);
#endif
void device_plan_print(FILE *stream, const struct device_plan *plan);
const struct link_plan *device_plan_find_link(const struct device_plan *plan, unsigned index);
int link_plan_has_service_address(const struct link_plan *link);
#endif
