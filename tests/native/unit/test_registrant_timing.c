/* Drive the real registrant with virtual acknowledgement/callback time.
 * Socket transport and alarm exits stay covered by the fake-daemon tests. */
#include "discovery/registrant.h"
#include <assert.h>

static long long now, ack_delay;
static unsigned registrations;
static DNSServiceErrorType callback_error;
struct fake_ref {
    int live;
    DNSServiceRegisterReply callback;
    void *context;
};
static struct fake_ref refs[REG_MAX_ENTRIES];
static long long fake_clock(void) { return now; }
static int fake_access(const char *path, int mode) { (void)path; (void)mode; return 0; }
static DNSServiceErrorType fake_register(DNSServiceRef *ref, DNSServiceFlags flags, uint32_t index,
    const char *name, const char *type, const char *domain, const char *host, uint16_t port,
    uint16_t length, const void *txt, DNSServiceRegisterReply callback, void *context) {
    size_t i;
    (void)flags; (void)index; (void)name; (void)type; (void)domain; (void)host;
    (void)port; (void)length; (void)txt;
    for (i = 0; i < REG_MAX_ENTRIES && refs[i].live; i++) {}
    assert(i < REG_MAX_ENTRIES);
    refs[i] = (struct fake_ref){1, callback, context};
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
static DNSServiceErrorType fake_result(DNSServiceRef ref) {
    struct fake_ref *r = (struct fake_ref *)ref;
    assert(r->live);
    r->callback(ref, kDNSServiceFlagsAdd, callback_error, "Capsule", "_smb._tcp", "local.", r->context);
    return kDNSServiceErr_NoError;
}
#define acp_monotonic_ms fake_clock
#define access fake_access
#define DNSServiceRegister fake_register
#define DNSServiceRefDeallocate fake_deallocate
#define DNSServiceRefSockFD fake_fd
#define DNSServiceProcessResult fake_result
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
int main(void) {
    struct config cfg = {0};
    pending_origin(&cfg);
    backoff_rounds(&cfg, 1);
    backoff_rounds(&cfg, 4);
    return 0;
}
