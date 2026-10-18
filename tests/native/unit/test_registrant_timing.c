/* Drive the real registrant with virtual acknowledgement/callback time.
 * Socket transport and alarm exits stay covered by the fake-daemon tests. */
#include "discovery/registrant.h"
#include <assert.h>

/* The re-announcement schedule this harness holds the build to. */
#define FIVE_MINUTES_MS (5LL * 60 * 1000)
#define TWENTY_MINUTES_MS (20LL * 60 * 1000)

static long long now, ack_delay;
static unsigned registrations;
static DNSServiceErrorType callback_error, process_error, update_error;
struct update_call {
    DNSServiceRef ref;
    uint32_t ttl;
    size_t len;
    unsigned char txt[REG_TXT_MAX];
    long long at;
    int announces;
};
static struct update_call updates[8];
static unsigned update_count;
/* A registration and the TXT record mDNSResponder holds for it. */
struct fake_ref {
    int live;
    DNSServiceRegisterReply callback;
    void *context;
    uint32_t ttl;
    size_t len;
    unsigned char txt[REG_TXT_MAX];
};
static struct fake_ref refs[REG_MAX_ENTRIES];
static long long fake_clock(void) { return now; }
static int fake_access(const char *path, int mode) { (void)path; (void)mode; return 0; }
static DNSServiceErrorType fake_register(DNSServiceRef *ref, DNSServiceFlags flags, uint32_t index,
    const char *name, const char *type, const char *domain, const char *host, uint16_t port,
    uint16_t length, const void *txt, DNSServiceRegisterReply callback, void *context) {
    size_t i;
    (void)flags; (void)index; (void)name; (void)type; (void)domain; (void)host;
    (void)port;
    for (i = 0; i < REG_MAX_ENTRIES && refs[i].live; i++) {}
    assert(i < REG_MAX_ENTRIES && length <= sizeof(refs[i].txt));
    refs[i] = (struct fake_ref){1, callback, context, REG_TXT_TTL, length, {0}};
    if (length) memcpy(refs[i].txt, txt, length);
    *ref = (DNSServiceRef)&refs[i];
    registrations++;
    now += ack_delay;
    return kDNSServiceErr_NoError;
}
static void fake_deallocate(DNSServiceRef ref) {
    struct fake_ref *r = (struct fake_ref *)ref;
    assert(r->live);
    r->live = 0;
}
static int fake_fd(DNSServiceRef ref) { return 3 + (int)((struct fake_ref *)ref - refs); }
/* process_error stands for a lost daemon connection: no callback runs. */
static DNSServiceErrorType fake_result(DNSServiceRef ref) {
    struct fake_ref *r = (struct fake_ref *)ref;
    assert(r->live);
    if (process_error != kDNSServiceErr_NoError) return process_error;
    r->callback(ref, kDNSServiceFlagsAdd, callback_error, "Capsule", "_smb._tcp", "local.", r->context);
    return kDNSServiceErr_NoError;
}
/* mDNSResponder's mDNS_Update: an accepted update announces only when it
 * changes the record's rdata or TTL; a rejected one changes nothing. */
static DNSServiceErrorType fake_update(DNSServiceRef ref, DNSRecordRef record, DNSServiceFlags flags,
    uint16_t length, const void *rdata, uint32_t ttl) {
    struct fake_ref *r = (struct fake_ref *)ref;
    struct update_call *call = &updates[update_count];
    assert(r->live && record == NULL && flags == 0);
    assert(update_count < sizeof(updates) / sizeof(updates[0]) && length <= sizeof(call->txt));
    call->ref = ref;
    call->ttl = ttl;
    call->len = length;
    memcpy(call->txt, rdata, length);
    call->at = now;
    call->announces = 0;
    update_count++;
    if (update_error != kDNSServiceErr_NoError) return update_error;
    call->announces = ttl != r->ttl || length != r->len || memcmp(rdata, r->txt, length) != 0;
    r->ttl = ttl;
    r->len = length;
    memcpy(r->txt, rdata, length);
    return kDNSServiceErr_NoError;
}
#define acp_monotonic_ms fake_clock
#define access fake_access
#define DNSServiceRegister fake_register
#define DNSServiceRefDeallocate fake_deallocate
#define DNSServiceRefSockFD fake_fd
#define DNSServiceProcessResult fake_result
#define DNSServiceUpdateRecord fake_update
#include "../../../build/native/discovery/registrant.c"

static void start(struct registrant *reg, const struct config *cfg, unsigned count) {
    struct device_plan plan = {0};
    unsigned i;
    memset(refs, 0, sizeof(refs));
    registrations = 0;
    now = 100;
    callback_error = 0;
    plan.status.validated = 1;
    plan.link_count = count;
    for (i = 0; i < count; i++) {
        plan.links[i].link.index = i + 1;
        plan.links[i].mask = SVC_SMB;
    }
    registrant_init(reg, cfg);
    registrant_apply_plan(reg, &plan, now);
    assert(registrations == count);
}
static void callbacks(struct registrant *reg) {
    fd_set reads;
    int maxfd = -1;
    long long deadline = -1;
    FD_ZERO(&reads);
    registrant_prepare(reg, &reads, &maxfd, &deadline);
    registrant_dispatch(reg, &reads, now);
}
static void pending_origin(const struct config *cfg) {
    struct registrant reg;
    long long completed;
    /* An acknowledgement may take longer than an entire callback budget. */
    ack_delay = REG_PENDING_TIMEOUT_MS + 1000;
    start(&reg, cfg, 1);
    completed = now;
    now = completed + REG_PENDING_TIMEOUT_MS - 1;
    registrant_dispatch(&reg, NULL, now);
    assert(reg.entries[0].status == REG_PENDING && registrations == 1);
    callbacks(&reg);
    assert(reg.entries[0].status == REG_REGISTERED);
    now += 2;
    registrant_dispatch(&reg, NULL, now);
    assert(reg.entries[0].status == REG_REGISTERED && !reg.retry_at_ms);
    registrant_shutdown(&reg);

    start(&reg, cfg, 1);
    completed = now;
    now = completed + REG_PENDING_TIMEOUT_MS;
    registrant_dispatch(&reg, NULL, now);
    assert(reg.entries[0].status == REG_DEGRADED && !reg.entries[0].ref);
    assert(reg.retry_at_ms == now + REG_BACKOFF_MIN_MS);
    registrant_shutdown(&reg);
}
static void backoff_rounds(const struct config *cfg, unsigned count) {
    struct registrant reg;
    const long long delays[] = {200, 400, 800, 1000, 1000};
    size_t i;
    ack_delay = 0;
    start(&reg, cfg, count);
    callback_error = kDNSServiceErr_NameConflict;
    for (i = 0; i < sizeof(delays) / sizeof(delays[0]); i++) {
        long long failed_at = now;
        callbacks(&reg);
        assert(reg.retry_at_ms == failed_at + delays[i]);
        now = reg.retry_at_ms - 1;
        registrant_dispatch(&reg, NULL, now);
        assert(registrations == count * (i + 1));
        now++;
        registrant_dispatch(&reg, NULL, now);
        assert(registrations == count * (i + 2));
    }
    callback_error = 0;
    callbacks(&reg);
    assert(reg.backoff_ms == REG_BACKOFF_MIN_MS && !reg.retry_at_ms);
    callback_error = kDNSServiceErr_NameConflict;
    callbacks(&reg);
    assert(reg.retry_at_ms == now + 200);
    registrant_shutdown(&reg);
}

static struct reg_entry *entry_for(struct registrant *reg, enum reg_service service) {
    size_t i;
    for (i = 0; i < REG_MAX_ENTRIES; i++) {
        if (reg->entries[i].in_use && reg->entries[i].desired.service == service) return &reg->entries[i];
    }
    assert(!"no entry for service");
    return NULL;
}
/* When the discovery loop's wait would end, or -1 for no deadline. */
static long long next_deadline(struct registrant *reg) {
    fd_set reads;
    int maxfd = -1;
    long long deadline = -1;
    FD_ZERO(&reads);
    registrant_prepare(reg, &reads, &maxfd, &deadline);
    return deadline;
}
static void apply(struct registrant *reg, unsigned mask) {
    struct device_plan plan = {0};
    plan.status.validated = 1;
    plan.link_count = 1;
    plan.links[0].link.index = 1;
    plan.links[0].mask = mask;
    strcpy(plan.id.wama, "e8:8d:28:58:f1:5c");
    registrant_apply_plan(reg, &plan, now);
}
static void start_adisk(struct registrant *reg, const struct config *cfg) {
    memset(refs, 0, sizeof(refs));
    registrations = update_count = 0;
    now = 100;
    ack_delay = 0;
    callback_error = process_error = update_error = 0;
    registrant_init(reg, cfg);
    apply(reg, SVC_SMB | SVC_ADISK);
    assert(registrations == 2);
}
/* Registers _smb and _adisk; returns when their callbacks arrived. */
static long long register_adisk(struct registrant *reg, const struct config *cfg) {
    start_adisk(reg, cfg);
    now += 40;
    callbacks(reg);
    assert(entry_for(reg, REG_SMB)->status == REG_REGISTERED);
    assert(entry_for(reg, REG_ADISK)->status == REG_REGISTERED);
    return now;
}
/* Update number `index` went to the current _adisk registration, now, with
 * the TXT it registered and this TTL, and mDNSResponder announced it. */
static void expect_update(struct registrant *reg, unsigned index, uint32_t ttl) {
    const struct update_call *call = &updates[index];
    assert(update_count == index + 1);
    assert(call->ref == entry_for(reg, REG_ADISK)->ref);
    assert(call->ttl == ttl && call->at == now && call->announces);
    assert(call->len == reg->adisk_txt_len && memcmp(call->txt, reg->adisk_txt, call->len) == 0);
}
/* Waits like the discovery loop: to the deadline, dispatching there. */
static void run_to_deadline(struct registrant *reg) {
    long long deadline = next_deadline(reg);
    assert(deadline >= now);
    now = deadline;
    registrant_dispatch(reg, NULL, now);
}
static void dispatch_at(struct registrant *reg, long long at) {
    now = at;
    registrant_dispatch(reg, NULL, now);
}

static void reannounce_schedule(const struct config *cfg) {
    struct registrant reg;
    /* Counted from the callback, not the request 40 ms before it. */
    long long registered_at = register_adisk(&reg, cfg);

    assert(next_deadline(&reg) == registered_at + FIVE_MINUTES_MS);
    dispatch_at(&reg, registered_at + FIVE_MINUTES_MS - 1);
    assert(update_count == 0);
    run_to_deadline(&reg);
    /* The TTL steps one second down, which mDNSResponder announces; the same
     * TXT with the same TTL would announce nothing. */
    expect_update(&reg, 0, REG_TXT_TTL - 1);
    assert(next_deadline(&reg) == registered_at + TWENTY_MINUTES_MS);
    dispatch_at(&reg, registered_at + TWENTY_MINUTES_MS - 1);
    assert(update_count == 1);
    run_to_deadline(&reg);
    expect_update(&reg, 1, REG_TXT_TTL);
    /* Done: the wait has no deadline of its own, and a day on nothing more is sent. */
    assert(next_deadline(&reg) == -1);
    dispatch_at(&reg, now + 24LL * 60 * 60 * 1000);
    assert(update_count == 2);
    registrant_shutdown(&reg);
}

static void late_wake(const struct config *cfg) {
    struct registrant reg;
    long long registered_at = register_adisk(&reg, cfg);

    /* A wait that ends late sends the first then and keeps the 15 minutes
     * between the two. */
    dispatch_at(&reg, registered_at + FIVE_MINUTES_MS + 7000);
    expect_update(&reg, 0, REG_TXT_TTL - 1);
    assert(next_deadline(&reg) == registered_at + TWENTY_MINUTES_MS + 7000);
    /* Another callback for the same registration (a rename after a later
     * conflict) does not start the schedule over. */
    callbacks(&reg);
    assert(next_deadline(&reg) == registered_at + TWENTY_MINUTES_MS + 7000);
    run_to_deadline(&reg);
    expect_update(&reg, 1, REG_TXT_TTL);
    callbacks(&reg);
    assert(next_deadline(&reg) == -1);
    registrant_shutdown(&reg);
}

static void conflicts(const struct config *cfg) {
    struct registrant reg;
    long long started, registered_at;
    unsigned rounds;

    start_adisk(&reg, cfg);
    started = now;
    callback_error = kDNSServiceErr_NameConflict;
    /* Every attempt conflicts for longer than both re-announcements take. */
    for (rounds = 0; now < started + TWENTY_MINUTES_MS + FIVE_MINUTES_MS; rounds++) {
        callbacks(&reg);
        assert(entry_for(&reg, REG_ADISK)->ref == NULL);
        /* The wait ends only for the retry. */
        assert(next_deadline(&reg) == reg.retry_at_ms);
        dispatch_at(&reg, rounds < 3 ? reg.retry_at_ms : started + TWENTY_MINUTES_MS + FIVE_MINUTES_MS);
    }
    assert(update_count == 0);
    callback_error = 0;
    now += 40;
    callbacks(&reg);
    registered_at = now;
    assert(next_deadline(&reg) == registered_at + FIVE_MINUTES_MS);
    run_to_deadline(&reg);
    expect_update(&reg, 0, REG_TXT_TTL - 1);
    registrant_shutdown(&reg);
}

/* Each new _adisk registration (the old one's daemon connection lost, the
 * plan dropping and re-adding it, a changed TXT) is announced by
 * mDNSResponder as new and gets both re-announcements again; the old
 * registration's due ones never go out. */
static void after_connection_loss(const struct config *cfg) {
    struct registrant reg;
    long long registered_at = register_adisk(&reg, cfg);

    run_to_deadline(&reg);
    expect_update(&reg, 0, REG_TXT_TTL - 1);
    process_error = kDNSServiceErr_ServiceNotRunning;
    callbacks(&reg);
    assert(entry_for(&reg, REG_ADISK)->ref == NULL && reg.retry_at_ms > 0);
    process_error = 0;
    /* The retry falls due with the old second re-announcement. */
    dispatch_at(&reg, registered_at + TWENTY_MINUTES_MS);
    assert(entry_for(&reg, REG_ADISK)->status == REG_PENDING && update_count == 1);
    now += 40;
    callbacks(&reg);
    registered_at = now;
    assert(next_deadline(&reg) == registered_at + FIVE_MINUTES_MS);
    run_to_deadline(&reg);
    expect_update(&reg, 1, REG_TXT_TTL - 1);
    registrant_shutdown(&reg);
}

static void after_plan_change(struct config *cfg) {
    struct registrant reg;
    long long registered_at = register_adisk(&reg, cfg);

    run_to_deadline(&reg);
    expect_update(&reg, 0, REG_TXT_TTL - 1);
    /* The plan stops wanting _adisk past its second re-announcement. */
    apply(&reg, SVC_SMB);
    dispatch_at(&reg, registered_at + TWENTY_MINUTES_MS);
    assert(update_count == 1 && next_deadline(&reg) == -1);
    apply(&reg, SVC_SMB | SVC_ADISK);
    now += 40;
    callbacks(&reg);
    assert(next_deadline(&reg) == now + FIVE_MINUTES_MS);
    run_to_deadline(&reg);
    expect_update(&reg, 1, REG_TXT_TTL - 1);
    /* A changed TXT (the AFP setting flipped) registers again. */
    strcpy(cfg->adisk_disks.disks[0].disk_advf, "0x83");
    apply(&reg, SVC_SMB | SVC_ADISK);
    assert(entry_for(&reg, REG_ADISK)->status == REG_PENDING);
    now += 40;
    callbacks(&reg);
    registered_at = now;
    assert(next_deadline(&reg) == registered_at + FIVE_MINUTES_MS);
    run_to_deadline(&reg);
    expect_update(&reg, 2, REG_TXT_TTL - 1);
    assert(updates[2].len != updates[1].len || memcmp(updates[2].txt, updates[1].txt, updates[1].len) != 0);
    strcpy(cfg->adisk_disks.disks[0].disk_advf, "0x82");
    registrant_shutdown(&reg);
}

/* mDNSResponder rejects the first update; the second must still change the
 * TTL it holds, or mDNSResponder would announce nothing. */
static void after_a_rejected_update(const struct config *cfg) {
    struct registrant reg;
    long long registered_at = register_adisk(&reg, cfg);

    update_error = kDNSServiceErr_NoMemory;
    run_to_deadline(&reg);
    assert(update_count == 1 && !updates[0].announces);
    update_error = 0;
    assert(next_deadline(&reg) == registered_at + TWENTY_MINUTES_MS);
    run_to_deadline(&reg);
    expect_update(&reg, 1, REG_TXT_TTL - 1);
    assert(next_deadline(&reg) == -1);
    registrant_shutdown(&reg);

    /* Both accepted: the second puts Apple's default back, and announces. */
    (void)register_adisk(&reg, cfg);
    run_to_deadline(&reg);
    expect_update(&reg, 0, REG_TXT_TTL - 1);
    run_to_deadline(&reg);
    expect_update(&reg, 1, REG_TXT_TTL);
    registrant_shutdown(&reg);
}

/* registrant_prepare never ends the wait for a re-announcement that
 * registrant_dispatch would not send, or the loop would spin on a past
 * deadline. */
static void deadline_follows_dispatch(const struct config *cfg) {
    struct registrant reg;
    struct reg_entry *adisk;

    register_adisk(&reg, cfg);
    adisk = entry_for(&reg, REG_ADISK);
    adisk->status = REG_PENDING;
    adisk->pending_until_ms = 0;
    adisk->reannounce_at_ms = now - 1;
    assert(next_deadline(&reg) == -1);
    registrant_dispatch(&reg, NULL, now);
    assert(update_count == 0);
    adisk->status = REG_REGISTERED;
    assert(next_deadline(&reg) == now - 1);
    registrant_dispatch(&reg, NULL, now);
    expect_update(&reg, 0, REG_TXT_TTL - 1);
    registrant_shutdown(&reg);
}

int main(int argc, char **argv) {
    struct config cfg = {0};
    const char *scenario = argc > 1 ? argv[1] : "";
    struct adisk_disk *disk = &cfg.adisk_disks.disks[0];

    if (strcmp(scenario, "callbacks") == 0) {
        pending_origin(&cfg);
        backoff_rounds(&cfg, 1);
        backoff_rounds(&cfg, 4);
        return 0;
    }
    strcpy(disk->share_name, "Data");
    strcpy(disk->disk_key, "dk2");
    strcpy(disk->disk_advf, "0x82");
    strcpy(disk->uuid, "12345678-1234-1234-1234-123456789012");
    cfg.adisk_disks.count = 1;
    if (strcmp(scenario, "reannounce-schedule") == 0) {
        reannounce_schedule(&cfg);
        late_wake(&cfg);
    } else if (strcmp(scenario, "reannounce-conflicts") == 0) {
        conflicts(&cfg);
    } else if (strcmp(scenario, "reannounce-restart") == 0) {
        after_connection_loss(&cfg);
        after_plan_change(&cfg);
    } else if (strcmp(scenario, "reannounce-rejected") == 0) {
        after_a_rejected_update(&cfg);
    } else if (strcmp(scenario, "reannounce-deadline") == 0) {
        deadline_follows_dispatch(&cfg);
    } else {
        fprintf(stderr, "unknown scenario \"%s\"\n", scenario);
        return 2;
    }
    return 0;
}
