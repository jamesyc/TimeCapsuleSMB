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
                       const char *instance, uint16_t port, const unsigned char *txt, size_t txt_len) {
    struct reg_desired *d;
    if (*count >= max || txt_len > REG_TXT_MAX) {
        return -1;
    }
    d = &out[(*count)++];
    memset(d, 0, sizeof(*d));
    d->ifindex = ifindex;
    d->service = service;
    strncpy(d->instance, instance, sizeof(d->instance) - 1);
    d->port = port;
    if (txt_len > 0) {
        memcpy(d->txt, txt, txt_len);
    }
    d->txt_len = txt_len;
    return 0;
}

/* B.3: _smb on every link whose mask has SVC_SMB, _adisk (with the golden
 * TXT) where SVC_ADISK and adisk rows exist and waMA is known, _afpovertcp
 * only where SVC_AFP (config MDNS_ADVERTISE_AFP=1). Empty instance means
 * nothing is registered. */
size_t registrant_compute_desired(struct reg_desired *out, size_t max, const struct device_plan *plan,
                                  const struct config *cfg) {
    size_t count = 0;
    size_t i;
    unsigned char adisk_txt[REG_TXT_MAX];
    int adisk_txt_len = -1;
    static int logged_adisk_skip = 0;

    if (plan->id.instance[0] == '\0') {
        return 0;
    }
    if (adisk_enabled(cfg)) {
        if (plan->id.wama[0] != '\0') {
            adisk_txt_len = build_adisk_txt_record(adisk_txt, sizeof(adisk_txt), plan->id.wama, &cfg->adisk_disks);
        }
        if (adisk_txt_len < 0 && !logged_adisk_skip) {
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
            (void)add_desired(out, max, &count, link->link.index, REG_SMB, plan->id.instance, SMB_PORT, NULL, 0);
        }
        if ((link->mask & SVC_ADISK) && adisk_txt_len >= 0) {
            (void)add_desired(out, max, &count, link->link.index, REG_ADISK, plan->id.instance, ADISK_PORT,
                              adisk_txt, (size_t)adisk_txt_len);
        }
        if (link->mask & SVC_AFP) {
            (void)add_desired(out, max, &count, link->link.index, REG_AFP, plan->id.instance, AFP_PORT, NULL, 0);
        }
    }
    return count;
}

static int desired_equal(const struct reg_desired *a, const struct reg_desired *b) {
    return a->ifindex == b->ifindex && a->service == b->service && a->port == b->port &&
           strcmp(a->instance, b->instance) == 0 && a->txt_len == b->txt_len &&
           memcmp(a->txt, b->txt, a->txt_len) == 0;
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
}

static void schedule_retry(struct registrant *reg, long long now_ms) {
    long long at = now_ms + reg->backoff_ms;
    if (reg->retry_at_ms == 0 || at < reg->retry_at_ms) {
        reg->retry_at_ms = at;
    }
    reg->backoff_ms *= 2;
    if (reg->backoff_ms > REG_BACKOFF_MAX_MS) {
        reg->backoff_ms = REG_BACKOFF_MAX_MS;
    }
}

static void log_entry(const struct registrant *reg, const char *what, const struct reg_entry *entry, const char *detail) {
    (void)reg;
    fprintf(stderr, "registrant: %s if=%u %s \"%s\" port=%u txt=%lu%s%s\n", what, entry->desired.ifindex,
            reg_service_regtype(entry->desired.service), entry->desired.instance, (unsigned)entry->desired.port,
            (unsigned long)entry->desired.txt_len, detail[0] ? " " : "", detail);
}

static void reg_callback(DNSServiceRef sd, DNSServiceFlags flags, DNSServiceErrorType err,
                         const char *name, const char *regtype, const char *domain, void *ctx) {
    struct reg_entry *entry = (struct reg_entry *)ctx;
    (void)sd; (void)domain;
    if (err == kDNSServiceErr_NoError && (flags & kDNSServiceFlagsAdd)) {
        entry->status = REG_REGISTERED;
        fprintf(stderr, "registrant: registered if=%u %s \"%s\"\n", entry->desired.ifindex, regtype, name);
        if (strcmp(name, entry->desired.instance) != 0) {
            /* NoAutoRename means the daemon must never hand us "Name (2)". */
            fprintf(stderr, "registrant: daemon renamed \"%s\" to \"%s\"; treating as conflict\n", entry->desired.instance, name);
            entry->status = REG_CONFLICT;
        }
    } else if (err == kDNSServiceErr_NameConflict) {
        entry->status = REG_CONFLICT;
        fprintf(stderr, "registrant: name conflict if=%u %s \"%s\"; retrying with backoff\n", entry->desired.ifindex, regtype, name);
    } else if (err == kDNSServiceErr_NoError) {
        /* A remove (flags without Add) or a no-op: keep the entry as is. */
        return;
    } else {
        entry->status = REG_DEGRADED;
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

    if (reg->unreachable_this_round || !daemon_socket_present()) {
        entry->ref = NULL;
        entry->status = REG_DEGRADED;
        note_unreachable(reg);
        schedule_retry(reg, now_ms);
        return;
    }
    ipc_begin();
    err = DNSServiceRegister(&ref, kDNSServiceFlagsNoAutoRename, entry->desired.ifindex, entry->desired.instance,
                             reg_service_regtype(entry->desired.service), NULL, NULL, htons(entry->desired.port),
                             (uint16_t)entry->desired.txt_len, entry->desired.txt_len ? entry->desired.txt : NULL,
                             reg_callback, entry);
    ipc_end();
    if (err == kDNSServiceErr_NoError) {
        entry->ref = ref;
        entry->status = REG_PENDING;
        if (reg->daemon_unreachable) {
            fprintf(stderr, "registrant: mDNSResponder reachable again\n");
            reg->daemon_unreachable = 0;
        }
        log_entry(reg, "register", entry, "");
        return;
    }
    entry->ref = NULL;
    entry->status = REG_DEGRADED;
    if (err == kDNSServiceErr_ServiceNotRunning) {
        note_unreachable(reg);
    } else {
        char detail[64];
        (void)snprintf(detail, sizeof(detail), "error=%d", (int)err);
        log_entry(reg, "register failed", entry, detail);
    }
    schedule_retry(reg, now_ms);
}

void registrant_apply_plan(struct registrant *reg, const struct device_plan *plan, long long now_ms) {
    size_t i;
    struct reg_desired *desired = reg->desired;
    size_t count = registrant_compute_desired(desired, REG_MAX_ENTRIES, plan, reg->cfg);

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
            if (desired_equal(&entry->desired, &desired[j])) {
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
        if (fd < 0 || reads == NULL || !FD_ISSET(fd, reads)) {
            continue;
        }
        ipc_begin();
        err = DNSServiceProcessResult(entry->ref);
        ipc_end();
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
