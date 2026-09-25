#ifndef TC_MDNS_REGISTRANT_H
#define TC_MDNS_REGISTRANT_H
#include "types.h"
#include "../dnssd/dns_sd.h"

/* The registrant (guide B.7): `desired` = set of (link index, service,
 * port, TXT) derived from the device plan; `active` = one
 * DNSServiceRef per (link index, service) with its status. Everything is
 * registered through Apple's mDNSResponder IPC using its default instance
 * name and automatic conflict renaming. A registration error backs
 * off (1,2,4,...,30 s) and retries with unchanged desired state. */

enum reg_service { REG_SMB = 0, REG_ADISK = 1, REG_AFP = 2, REG_SERVICE_COUNT = 3 };
enum reg_status { REG_PENDING, REG_REGISTERED, REG_CONFLICT, REG_DEGRADED };
#define REG_MAX_ENTRIES (TC_MAX_LINKS * REG_SERVICE_COUNT)
/* One length-prefixed system item plus every individually valid disk item. */
#define REG_TXT_MAX                                                                                                  \
    (1 + (sizeof(ADISK_SYS_TXT_PREFIX) - 1) + 17 + (sizeof(ADISK_SYS_TXT_SUFFIX) - 1) +                              \
     ADISK_MAX_DISKS * (1 + MAX_TXT_STRING))
#ifndef REG_BACKOFF_MIN_MS
#define REG_BACKOFF_MIN_MS 1000
#endif
#ifndef REG_BACKOFF_MAX_MS
#define REG_BACKOFF_MAX_MS 30000
#endif
#ifndef REG_PENDING_TIMEOUT_MS
#define REG_PENDING_TIMEOUT_MS 10000
#endif

struct reg_desired {
    unsigned ifindex;
    enum reg_service service;
    uint16_t port;                 /* host order */
};

struct reg_entry {
    int in_use;
    struct reg_desired desired;
    DNSServiceRef ref;             /* NULL while waiting for a retry */
    enum reg_status status;
    long long pending_until_ms;    /* initial callback deadline, 0 otherwise */
    int polled;                    /* ref's socket is in the current select() set */
};

struct registrant {
    struct reg_entry entries[REG_MAX_ENTRIES];
    struct reg_desired desired[REG_MAX_ENTRIES];
    unsigned char adisk_txt[REG_TXT_MAX];
    size_t adisk_txt_len;
    long long backoff_ms;
    long long retry_at_ms;         /* 0 = nothing pending */
    int daemon_unreachable;        /* logged once per transition */
    int unreachable_this_round;    /* skip further attempts until the backoff timer */
    int debug;
    char last_plan_line[1024];
    const struct config *cfg;
};

void registrant_install_ipc_fence(void);   /* SIGALRM -> exit EXIT_DAEMON_STALLED; call once at startup */
void registrant_init(struct registrant *reg, const struct config *cfg);
/* Derives the desired set from a plan (pure; testable). Returns the count. */
size_t registrant_compute_desired(struct reg_desired *out, size_t max, const struct device_plan *plan,
                                  const struct config *cfg, unsigned char adisk_txt[REG_TXT_MAX],
                                  size_t *adisk_txt_len);
/* Applies a new plan: deregisters entries no longer desired, registers new ones. */
void registrant_apply_plan(struct registrant *reg, const struct device_plan *plan, long long now_ms);
/* Adds the active refs' sockets and the backoff timer to a select() set. */
void registrant_prepare(struct registrant *reg, fd_set *reads, int *maxfd, long long *deadline_ms);
/* Processes readable refs and due retries. */
void registrant_dispatch(struct registrant *reg, const fd_set *reads, long long now_ms);
/* Deallocates every ref (the daemon sends goodbyes). */
void registrant_shutdown(struct registrant *reg);
const char *reg_service_regtype(enum reg_service service);
#endif
