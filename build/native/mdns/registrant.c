#include "mdns.h"
#include "../dnssd/dnssd_ipc.h"   /* MDNS_UDS_SERVERPATH only */

const char *reg_service_regtype(enum reg_service service) {
    switch (service) {
    case REG_SMB: return SMB_REGTYPE;
    case REG_ADISK: return ADISK_REGTYPE;
    default: return AFP_REGTYPE;
    }
}

void registrant_init(struct registrant *reg, const struct config *cfg) {
    memset(reg, 0, sizeof(*reg));
    reg->cfg = cfg;
    reg->debug = cfg->debug_logging;
    reg->backoff_ms = REG_BACKOFF_MIN_MS;
}

static int add_desired(struct reg_desired *out, size_t max, size_t *count, unsigned ifindex, enum reg_service service,
                       uint16_t port) {
    struct reg_desired *d;
    if (*count >= max) {
        return -1;
    }
    d = &out[(*count)++];
    memset(d, 0, sizeof(*d));
    d->ifindex = ifindex;
    d->service = service;
    d->port = port;
    return 0;
}

/* B.3: _smb on every link whose mask has SVC_SMB, _adisk (with the golden
 * TXT) where SVC_ADISK and adisk rows exist and waMA is known, _afpovertcp
 * only where SVC_AFP (config MDNS_ADVERTISE_AFP=1). Apple owns the default
 * instance name, including conflict renames; ACP names are not registration inputs. */
size_t registrant_compute_desired(struct reg_desired *out, size_t max, const struct device_plan *plan,
                                  const struct config *cfg, unsigned char adisk_txt[REG_TXT_MAX],
                                  size_t *adisk_txt_len) {
    size_t count = 0;
    size_t i;
    int built_adisk_txt_len = -1;
    static int logged_adisk_skip = 0;

    *adisk_txt_len = 0;
    if (adisk_enabled(cfg)) {
        if (plan->id.wama[0] != '\0') {
            built_adisk_txt_len = build_adisk_txt_record(adisk_txt, REG_TXT_MAX, plan->id.wama, &cfg->adisk_disks);
            if (built_adisk_txt_len >= 0) *adisk_txt_len = (size_t)built_adisk_txt_len;
        }
        if (built_adisk_txt_len < 0 && !logged_adisk_skip) {
            fprintf(stderr, "registrant: _adisk skipped; waMA unavailable or TXT invalid\n");
            logged_adisk_skip = 1;
        }
    }
    for (i = 0; i < plan->link_count; i++) {
        const struct link_plan *link = &plan->links[i];
        if (link->link.index == 0 || link->mask == 0) {
            continue;
        }
        if (link->mask & SVC_SMB) {
            (void)add_desired(out, max, &count, link->link.index, REG_SMB, SMB_PORT);
        }
        if ((link->mask & SVC_ADISK) && built_adisk_txt_len >= 0) {
            (void)add_desired(out, max, &count, link->link.index, REG_ADISK, ADISK_PORT);
        }
        if (link->mask & SVC_AFP) {
            (void)add_desired(out, max, &count, link->link.index, REG_AFP, AFP_PORT);
        }
    }
    return count;
}

static int desired_equal(const struct reg_desired *a, const struct reg_desired *b) {
    return a->ifindex == b->ifindex && a->service == b->service && a->port == b->port;
}

static struct reg_entry *find_entry(struct registrant *reg, unsigned ifindex, enum reg_service service) {
    size_t i;
    for (i = 0; i < REG_MAX_ENTRIES; i++) {
        if (reg->entries[i].in_use && reg->entries[i].desired.ifindex == ifindex && reg->entries[i].desired.service == service) {
            return &reg->entries[i];
        }
    }
    return NULL;
}

/* B.7 IPC fence. The vendored stub is synchronous and unbounded on Unix:
 * DNSServiceRegister reads the daemon's 4-byte acknowledgement and
 * DNSServiceProcessResult reads a whole reply, both retrying EINTR, so a
 * daemon that accepts the socket but never answers would park this loop
 * forever -- unkillable by SIGTERM, deaf to route changes. Apple's own
 * clients behave the same, and a wedged mDNSResponder is a reboot on stock
 * firmware too; what we add is a way out: every stub call runs under an
 * alarm, and the handler exits with EXIT_DAEMON_STALLED. The daemon sees
 * the socket close (its goodbyes), the manager relaunches us on its next
 * pass, and the exit code names the cause. A stub-level timeout would need
 * the shared-connection model; this is the small option (AGENTS.md). */
#ifndef REG_IPC_ALARM_SECONDS
#define REG_IPC_ALARM_SECONDS 10
#endif

static void ipc_stalled(int signo) {
    static const char message[] = "registrant: mDNSResponder accepted the connection but did not answer; exiting for relaunch\n";
    (void)signo;
    (void)write(STDERR_FILENO, message, sizeof(message) - 1);
    _exit(EXIT_DAEMON_STALLED);
}

void registrant_install_ipc_fence(void) {
    signal(SIGALRM, ipc_stalled);
}

static void ipc_begin(void) {
    (void)alarm(REG_IPC_ALARM_SECONDS);
}

static void ipc_end(void) {
    (void)alarm(0);
}

static void release_ref(struct reg_entry *entry) {
    if (entry->ref != NULL) {
        ipc_begin();
        DNSServiceRefDeallocate(entry->ref);
        ipc_end();
        entry->ref = NULL;
    }
    entry->pending_until_ms = 0;
}

static void schedule_retry(struct registrant *reg, long long now_ms) {
    if (reg->retry_at_ms != 0) return;
    reg->retry_at_ms = now_ms + reg->backoff_ms;
    reg->backoff_ms *= 2;
    if (reg->backoff_ms > REG_BACKOFF_MAX_MS) {
        reg->backoff_ms = REG_BACKOFF_MAX_MS;
    }
}

static void log_entry(const struct registrant *reg, const char *what, const struct reg_entry *entry, const char *detail) {
    fprintf(stderr, "registrant: %s if=%u %s \"%s\" port=%u txt=%lu%s%s\n", what, entry->desired.ifindex,
            reg_service_regtype(entry->desired.service), "Apple default name", (unsigned)entry->desired.port,
            (unsigned long)(entry->desired.service == REG_ADISK ? reg->adisk_txt_len : 0),
            detail[0] ? " " : "", detail);
}

static void reg_callback(DNSServiceRef sd, DNSServiceFlags flags, DNSServiceErrorType err,
                         const char *name, const char *regtype, const char *domain, void *ctx) {
    struct reg_entry *entry = (struct reg_entry *)ctx;
    (void)sd; (void)domain;
    if (err == kDNSServiceErr_NoError && (flags & kDNSServiceFlagsAdd)) {
        entry->status = REG_REGISTERED;
        entry->pending_until_ms = 0;
        fprintf(stderr, "registrant: registered if=%u %s \"%s\"\n", entry->desired.ifindex, regtype, name);
    } else if (err == kDNSServiceErr_NameConflict) {
        entry->status = REG_CONFLICT;
        entry->pending_until_ms = 0;
        fprintf(stderr, "registrant: name conflict if=%u %s \"%s\"; retrying with backoff\n", entry->desired.ifindex, regtype, name);
    } else if (err == kDNSServiceErr_NoError) {
        /* A remove (flags without Add) or a no-op: keep the entry as is. */
        return;
    } else {
        entry->status = REG_DEGRADED;
        entry->pending_until_ms = 0;
        fprintf(stderr, "registrant: registration error %d if=%u %s \"%s\"; retrying with backoff\n", (int)err,
                entry->desired.ifindex, regtype, name);
    }
}

/* The daemon creates its socket only while it runs (F1). The stub's
 * ConnectToServer blocks up to 3 s retrying connect(), so when the socket
 * is absent we mark the round unreachable without calling it, and once one
 * attempt in a round fails that way the remaining entries wait for the
 * backoff timer instead of each paying the same stall. */
static int daemon_socket_present(void) {
    return access(MDNS_UDS_SERVERPATH, F_OK) == 0;
}

static void note_unreachable(struct registrant *reg) {
    if (!reg->daemon_unreachable) {
        fprintf(stderr, "registrant: mDNSResponder unreachable; registrations degraded until it answers (never started by us; reboot recovers)\n");
        reg->daemon_unreachable = 1;
    }
    reg->unreachable_this_round = 1;
}

/* Registers one entry now; on failure leaves it ref-less for the retry. */
static void try_register(struct registrant *reg, struct reg_entry *entry, long long now_ms) {
    DNSServiceErrorType err;
    DNSServiceRef ref = NULL;
    long long completed_ms;
    size_t txt_len = entry->desired.service == REG_ADISK ? reg->adisk_txt_len : 0;

    if (reg->unreachable_this_round || !daemon_socket_present()) {
        entry->ref = NULL;
        entry->status = REG_DEGRADED;
        note_unreachable(reg);
        schedule_retry(reg, now_ms);
        return;
    }
    ipc_begin();
    /* Stock diskd (NetBSD 4 LE, 2026-09-19) renamed SMB and ADisk together
     * after an SMB-only conflict. NULL selects the daemon's shared default
     * name; explicit names or NoAutoRename opt out of that native behavior. */
    err = DNSServiceRegister(&ref, 0, entry->desired.ifindex, NULL,
                             reg_service_regtype(entry->desired.service), NULL, NULL, htons(entry->desired.port),
                             (uint16_t)txt_len, txt_len ? reg->adisk_txt : NULL,
                             reg_callback, entry);
    ipc_end();
    completed_ms = acp_monotonic_ms();
    if (err == kDNSServiceErr_NoError) {
        entry->ref = ref;
        entry->status = REG_PENDING;
        entry->pending_until_ms = completed_ms + REG_PENDING_TIMEOUT_MS;
        if (reg->daemon_unreachable) {
            fprintf(stderr, "registrant: mDNSResponder reachable again\n");
            reg->daemon_unreachable = 0;
        }
        log_entry(reg, "register", entry, "");
        return;
    }
    entry->ref = NULL;
    entry->status = REG_DEGRADED;
    entry->pending_until_ms = 0;
    if (err == kDNSServiceErr_ServiceNotRunning) {
        note_unreachable(reg);
    } else {
        char detail[64];
        (void)snprintf(detail, sizeof(detail), "error=%d", (int)err);
        log_entry(reg, "register failed", entry, detail);
    }
    schedule_retry(reg, completed_ms);
}

void registrant_apply_plan(struct registrant *reg, const struct device_plan *plan, long long now_ms) {
    size_t i;
    struct reg_desired *desired = reg->desired;
    unsigned char candidate_adisk_txt[REG_TXT_MAX];
    size_t candidate_adisk_txt_len;
    size_t count = registrant_compute_desired(desired, REG_MAX_ENTRIES, plan, reg->cfg,
                                              candidate_adisk_txt, &candidate_adisk_txt_len);
    int adisk_txt_changed = candidate_adisk_txt_len != reg->adisk_txt_len ||
        memcmp(candidate_adisk_txt, reg->adisk_txt, candidate_adisk_txt_len) != 0;

    /* One line per plan change with the desired set (guide C.3); the 30 s
     * poll re-derives the same plan most of the time and stays silent. */
    {
        char line[1024];
        size_t used = (size_t)snprintf(line, sizeof(line), "registrant: plan %s mode=%s desired=%lu",
                                       plan->status.validated ? "validated" : "incomplete",
                                       router_mode_name(plan->mode), (unsigned long)count);
        for (i = 0; i < count && used < sizeof(line) - 40; i++) {
            used += (size_t)snprintf(line + used, sizeof(line) - used, " [if=%u %s]", desired[i].ifindex,
                                     reg_service_regtype(desired[i].service));
        }
        if (strcmp(line, reg->last_plan_line) != 0 || reg->debug) {
            fprintf(stderr, "%s\n", line);
            strncpy(reg->last_plan_line, line, sizeof(reg->last_plan_line) - 1);
        }
    }
    reg->unreachable_this_round = 0;
    /* Deregister what is no longer desired or whose identity/TXT changed
     * (changed TXT = dealloc + register; the daemon sends the goodbye). */
    for (i = 0; i < REG_MAX_ENTRIES; i++) {
        struct reg_entry *entry = &reg->entries[i];
        size_t j;
        int keep = 0;
        if (!entry->in_use) {
            continue;
        }
        for (j = 0; j < count; j++) {
            if (desired_equal(&entry->desired, &desired[j]) &&
                !(adisk_txt_changed && entry->desired.service == REG_ADISK)) {
                keep = 1;
                break;
            }
        }
        if (!keep) {
            log_entry(reg, "deregister", entry, "");
            release_ref(entry);
            entry->in_use = 0;
        }
    }
    if (candidate_adisk_txt_len > 0) {
        memcpy(reg->adisk_txt, candidate_adisk_txt, candidate_adisk_txt_len);
    }
    reg->adisk_txt_len = candidate_adisk_txt_len;
    /* Register new entries. */
    for (i = 0; i < count; i++) {
        struct reg_entry *entry = find_entry(reg, desired[i].ifindex, desired[i].service);
        size_t slot;
        if (entry != NULL) {
            continue;
        }
        for (slot = 0; slot < REG_MAX_ENTRIES && reg->entries[slot].in_use; slot++) {
        }
        if (slot >= REG_MAX_ENTRIES) {
            break;
        }
        entry = &reg->entries[slot];
        memset(entry, 0, sizeof(*entry));
        entry->in_use = 1;
        entry->desired = desired[i];
        try_register(reg, entry, now_ms);
    }
}

void registrant_prepare(struct registrant *reg, fd_set *reads, int *maxfd, long long *deadline_ms) {
    size_t i;
    for (i = 0; i < REG_MAX_ENTRIES; i++) {
        const struct reg_entry *entry = &reg->entries[i];
        int fd;
        if (!entry->in_use || entry->ref == NULL) {
            continue;
        }
        fd = DNSServiceRefSockFD(entry->ref);
        if (fd >= 0) {
            FD_SET(fd, reads);
            if (fd > *maxfd) *maxfd = fd;
        }
        if (entry->status == REG_PENDING && entry->pending_until_ms > 0 &&
            (*deadline_ms < 0 || entry->pending_until_ms < *deadline_ms)) {
            *deadline_ms = entry->pending_until_ms;
        }
    }
    if (reg->retry_at_ms > 0 && (*deadline_ms < 0 || reg->retry_at_ms < *deadline_ms)) {
        *deadline_ms = reg->retry_at_ms;
    }
}

void registrant_dispatch(struct registrant *reg, const fd_set *reads, long long now_ms) {
    size_t i;
    int failures = 0;
    int live = 0;

    for (i = 0; i < REG_MAX_ENTRIES; i++) {
        struct reg_entry *entry = &reg->entries[i];
        int fd;
        DNSServiceErrorType err;
        if (!entry->in_use || entry->ref == NULL) {
            continue;
        }
        live++;
        fd = DNSServiceRefSockFD(entry->ref);
        if (fd >= 0 && reads != NULL && FD_ISSET(fd, reads)) {
            ipc_begin();
            err = DNSServiceProcessResult(entry->ref);
            ipc_end();
        } else if (entry->status == REG_PENDING && entry->pending_until_ms > 0 &&
                   now_ms >= entry->pending_until_ms) {
            log_entry(reg, "initial callback timed out", entry, "retrying with backoff");
            release_ref(entry);
            entry->status = REG_DEGRADED;
            schedule_retry(reg, now_ms);
            continue;
        } else {
            continue;
        }
        if (err != kDNSServiceErr_NoError) {
            /* The connection is dead (daemon gone or protocol error). */
            char detail[64];
            (void)snprintf(detail, sizeof(detail), "process-result error=%d", (int)err);
            log_entry(reg, "connection lost", entry, detail);
            release_ref(entry);
            entry->status = REG_DEGRADED;
            failures++;
            schedule_retry(reg, now_ms);
        } else if (entry->status == REG_CONFLICT || entry->status == REG_DEGRADED) {
            /* The callback reported an error: drop the ref and retry later. */
            release_ref(entry);
            schedule_retry(reg, now_ms);
        } else if (entry->status == REG_REGISTERED) {
            reg->backoff_ms = REG_BACKOFF_MIN_MS;
        }
    }
    if (failures > 0 && failures == live && !reg->daemon_unreachable) {
        fprintf(stderr, "registrant: mDNSResponder connection lost on every registration; retrying with backoff\n");
        reg->daemon_unreachable = 1;
    }
    if (reg->retry_at_ms > 0 && now_ms >= reg->retry_at_ms) {
        reg->retry_at_ms = 0;
        reg->unreachable_this_round = 0;
        for (i = 0; i < REG_MAX_ENTRIES; i++) {
            struct reg_entry *entry = &reg->entries[i];
            if (entry->in_use && entry->ref == NULL) {
                try_register(reg, entry, now_ms);
            }
        }
    }
}

void registrant_shutdown(struct registrant *reg) {
    size_t i;
    for (i = 0; i < REG_MAX_ENTRIES; i++) {
        struct reg_entry *entry = &reg->entries[i];
        if (entry->in_use) {
            log_entry(reg, "deregister", entry, "shutdown");
            release_ref(entry);
            entry->in_use = 0;
        }
    }
}
