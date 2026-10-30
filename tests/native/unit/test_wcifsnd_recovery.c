#include "../../../build/native/discovery/wcifsnd.h"
#include <assert.h>

/* Drive production dispatch with synthetic time and syscall outcomes. Real
 * process/socket ownership is covered separately by test_wcifsnd.py. */
static int forks, signals, last_signal, wait_error, fork_error, socket_error, sockets;
static pid_t waited;
static pid_t fake_fork(void) { forks++; errno = fork_error; return fork_error ? -1 : 42; }
static pid_t fake_waitpid(pid_t pid, int *status, int options) {
    assert(pid == 42 && options == WNOHANG);
    *status = 0; errno = wait_error;
    return wait_error ? -1 : waited;
}
static int fake_kill(pid_t pid, int sig) {
    assert(pid == 42); signals++; last_signal = sig; return 0;
}
static int fake_socket(int domain, int type, int protocol) {
    sockets++;
    if (socket_error) { errno = socket_error; return -1; }
    return socket(domain, type, protocol);
}
static int inspection_done, inspections, exec_error, owned;
static int fake_inspection_poll(struct tc_child *child, long long now) {
    (void)now;
    if (inspection_done) child->exited = 1; /* exit status 0 */
    return inspection_done;
}
static int fake_command_exec(struct tc_child *child, char *const argv[], void *out, size_t capacity) {
    (void)argv; (void)out; (void)capacity;
    inspections++;
    if (exec_error) return -1;
    child->group = 77; child->nested = 1;
    return 0;
}
#define fork fake_fork
#define waitpid fake_waitpid
#define kill fake_kill
#define socket fake_socket
#define tc_child_poll fake_inspection_poll
#define tc_command_exec fake_command_exec
#include "../../../build/native/discovery/wcifsnd.c"
#undef tc_child_poll
#undef tc_command_exec

int tc_native_nbns_sockets_present(const char *text, pid_t pid, unsigned control_port) {
    (void)text; (void)pid; (void)control_port;
    return owned;
}

static void init(struct wcifsnd *w) {
    wcifsnd_init(w, "machine");
    w->desired = w->validated = 1;
    forks = signals = last_signal = wait_error = fork_error = socket_error = sockets = 0;
    inspection_done = inspections = exec_error = 0; owned = 1;
    waited = 0;
}

static void retry_deadlines(void) {
    struct wcifsnd w;
    long long now = 100, delay;
    unsigned i;
    init(&w);
    for (i = 1; i <= 8; i++) {
        w.child = 0; w.phase = WC_OFF;
        fail(&w, "injected failure", now);
        fail(&w, "same failure", now); /* One attempt, one increment. */
        assert(!wcifsnd_dispatch(&w, NULL, now));
        delay = 1000LL << (i < 6 ? i : 6);
        assert(w.wake == now + delay && w.failures == (i < 6 ? i : 6));
        long long deadline = -1;
        int maxfd = -1;
        fd_set reads;
        FD_ZERO(&reads); wcifsnd_prepare(&w, &reads, &maxfd, &deadline);
        assert(deadline == w.wake && maxfd == -1);
        assert(!wcifsnd_dispatch(&w, NULL, w.wake - 1) && forks == (int)i - 1);
        assert(!wcifsnd_dispatch(&w, NULL, w.wake) && forks == (int)i);
        assert(w.child == 42 && w.phase == WC_STARTING);
        now = w.wake + 1;
    }
    w.child = 0; w.phase = WC_ACTIVE; w.active_since = now - 59999;
    fail(&w, "short-lived success", now);
    assert(!wcifsnd_dispatch(&w, NULL, now) && w.wake == now + 64000);
    w.phase = WC_ACTIVE; w.active_since = now - 60000;
    fail(&w, "healthy interval", now);
    assert(!wcifsnd_dispatch(&w, NULL, now) && w.wake == now + 2000 && w.failures == 1);
}

static void cleanup_before_retry(void) {
    struct wcifsnd w;
    init(&w); w.child = 42; w.phase = WC_ACTIVE;
    fail(&w, "lost acknowledgement", 100);
    assert(w.phase == WC_STOPPING && last_signal == SIGTERM);
    assert(!wcifsnd_dispatch(&w, NULL, 2099) && signals == 1 && !forks);
    assert(!wcifsnd_dispatch(&w, NULL, 2100) && last_signal == SIGKILL && !forks);
    assert(!wcifsnd_dispatch(&w, NULL, 4099) && signals == 2 && !forks);
    assert(wcifsnd_dispatch(&w, NULL, 4100) == -1 && !forks);

    init(&w); w.child = 42; w.phase = WC_REGISTERING;
    fail(&w, "partial registration", 100);
    waited = 42;
    assert(!wcifsnd_dispatch(&w, NULL, 250));
    assert(w.phase == WC_OFF && w.child == 0 && w.wake == 2250 && !forks);
    waited = 0;
    assert(!wcifsnd_dispatch(&w, NULL, 2250) && forks == 1);
    assert(w.record == 0 && !w.sent);
}

static void stopped_inspection_defers_retry(void) {
    struct wcifsnd w;
    fd_set reads;
    int maxfd = -1;
    long long deadline = -1;
    init(&w);
    w.child = 42; w.phase = WC_INSPECTING;
    w.inspection.group = 77; w.inspection.nested = 1;
    w.inspection.lifetime = w.inspection.output = -1;
    w.inspection_limit = 20000;
    waited = 42; /* Native child exits while the inspector ignores TERM. */
    assert(!wcifsnd_dispatch(&w, NULL, 100));
    assert(w.phase == WC_OFF && !w.child && w.inspection.stopping);
    assert(w.wake == 2100 && w.inspection.deadline == 10100 && !forks);

    FD_ZERO(&reads);
    wcifsnd_prepare(&w, &reads, &maxfd, &deadline);
    assert(deadline == 10100 && deadline > w.wake);
    assert(!wcifsnd_dispatch(&w, NULL, 3100) && !forks);
    deadline = -1;
    wcifsnd_prepare(&w, &reads, &maxfd, &deadline);
    assert(deadline == 10100); /* No expired retry deadline or busy select. */

    inspection_done = 1;
    assert(!wcifsnd_dispatch(&w, NULL, 5100));
    assert(!w.inspection.group && forks == 1 && w.child == 42);
}

/* A starting child is ready once fstat shows it owns its ports. Discovery
 * never binds the control port to test it: a child binding at that moment
 * would fail. Until the start deadline it inspects again after a wait that
 * starts at 200 ms and doubles. */
static void starting_child_is_inspected_until_it_owns_its_ports(void) {
    struct wcifsnd w;
    init(&w);
    assert(!wcifsnd_dispatch(&w, NULL, 1000) && forks == 1);
    assert(w.phase == WC_STARTING && w.deadline == 6000 && w.wake == 1100);
    assert(!wcifsnd_dispatch(&w, NULL, 1099) && !inspections);
    assert(!wcifsnd_dispatch(&w, NULL, 1100) && inspections == 1 && w.phase == WC_INSPECTING);
    assert(!wcifsnd_dispatch(&w, NULL, 1200) && inspections == 1); /* one at a time */

    owned = 0; inspection_done = 1;            /* not bound yet */
    assert(!wcifsnd_dispatch(&w, NULL, 1300));
    assert(w.phase == WC_STARTING && w.wake == 1500 && !w.failed && !w.inspection.group);
    inspection_done = 0;
    assert(!wcifsnd_dispatch(&w, NULL, 1499) && inspections == 1);
    assert(!wcifsnd_dispatch(&w, NULL, 1500) && inspections == 2);
    inspection_done = 1;                       /* still not bound: twice the wait */
    assert(!wcifsnd_dispatch(&w, NULL, 1600) && w.phase == WC_STARTING && w.wake == 2000);
    inspection_done = 0;
    assert(!wcifsnd_dispatch(&w, NULL, 2000) && inspections == 3);

    owned = 1; inspection_done = 1;            /* bound: register */
    assert(!sockets);
    assert(!wcifsnd_dispatch(&w, NULL, 2100));
    assert(w.phase == WC_REGISTERING && w.fd >= 0 && sockets == 1 && !w.failed);
    assert(w.deadline == 2100 + reply_ms(&w) && forks == 1);
    close(w.fd); w.fd = -1;

    w.child = 0; w.phase = WC_OFF; w.wake = 0; /* the next child starts at 100 ms again */
    assert(!wcifsnd_dispatch(&w, NULL, 3000) && forks == 2 && w.inspect_ms == 100 && w.wake == 3100);

    init(&w);                                  /* never owned by the deadline */
    assert(!wcifsnd_dispatch(&w, NULL, 1000) && forks == 1);
    owned = 0;
    long long at = 1100, wait = 100;
    while (1) {                                /* 1100, 1300, 1700, 2500, 4100, 6000 */
        inspection_done = 0;
        assert(!wcifsnd_dispatch(&w, NULL, at) && w.phase == WC_INSPECTING);
        inspection_done = 1;
        assert(!wcifsnd_dispatch(&w, NULL, at));
        if (at == 6000) break;
        wait *= 2;
        assert(w.phase == WC_STARTING && !w.failed && w.wake == (at + wait < 6000 ? at + wait : 6000));
        at = w.wake;
    }
    assert(w.failed && w.phase == WC_STOPPING); /* on the deadline, not after it */
    assert(last_signal == SIGTERM && !sockets && inspections == 6);

    init(&w); exec_error = 1;                  /* fstat cannot start */
    assert(!wcifsnd_dispatch(&w, NULL, 1000) && forks == 1);
    assert(!wcifsnd_dispatch(&w, NULL, 1100) && w.failed && w.phase == WC_STOPPING);
    assert(inspections == 1 && !sockets);
}

static void errors_and_stale_replies(void) {
    struct wcifsnd w;
    init(&w); fork_error = EAGAIN;
    assert(!wcifsnd_dispatch(&w, NULL, 100) && w.failed && !w.child);
    assert(!wcifsnd_dispatch(&w, NULL, 100) && w.wake == 2100 && forks == 1);
    fork_error = 0;
    assert(!wcifsnd_dispatch(&w, NULL, 2100));
    wait_error = EINTR;
    assert(!wcifsnd_dispatch(&w, NULL, 2101));
    wait_error = EINVAL;
    assert(wcifsnd_dispatch(&w, NULL, 2101) == -1 && forks == 2);
    wait_error = ECHILD;
    assert(!wcifsnd_dispatch(&w, NULL, 2102) && !w.child && w.wake == 6102);

    init(&w); w.phase = WC_INSPECTING; w.child = 42; w.inspection.group = 77; w.inspection.nested = 1;
    w.inspection.lifetime = w.inspection.output = -1; w.inspection_limit = 20000;
    inspection_done = 1; socket_error = EMFILE;
    assert(!wcifsnd_dispatch(&w, NULL, 100) && w.phase == WC_STOPPING && w.failed);
    assert(last_signal == SIGTERM && !forks && sockets == 1);

    init(&w); w.phase = WC_REGISTERING; w.child = 42; w.deadline = 10000;
    assert(!wcifsnd_dispatch(&w, NULL, 100)); /* send(-1) fails. */
    assert(w.phase == WC_STOPPING && w.failed && !forks);
    int pipefd[2];
    assert(!pipe(pipefd));
    init(&w); w.phase = WC_REGISTERING; w.child = 42; w.deadline = 10000;
    w.fd = pipefd[0]; w.sent = 1;
    fd_set reads; FD_ZERO(&reads); FD_SET(w.fd, &reads);
    assert(!wcifsnd_dispatch(&w, &reads, 100)); /* recv(pipe) fails. */
    assert(w.phase == WC_STOPPING && w.failed && w.fd == -1 && !forks);
    close(pipefd[1]);

    init(&w); request_name(&w);
    uint16_t old = w.transaction;
    fail(&w, "lost reply", 100);
    assert(!wcifsnd_dispatch(&w, NULL, 100));
    assert(!wcifsnd_dispatch(&w, NULL, 2100));
    request_name(&w);
    assert(w.transaction == old + 1); /* Do not reinitialize the controller. */
    unsigned char stale[2]; put16(stale, old);
    assert(response(&w, stale, sizeof(stale)) == 0);
}

static void eligibility_and_shutdown(void) {
    struct wcifsnd w;
    struct device_plan p = {0};
    p.status.validated = 1; p.link_count = 1;
    p.links[0].mask = SVC_SMB; p.links[0].addr_count = 1;
    p.links[0].link.flags = TC_IFF_UP | TC_IFF_BROADCAST;
    p.links[0].addrs[0].family = AF_INET;
    assert(inet_pton(AF_INET, "192.0.2.1", &p.links[0].addrs[0].v4) == 1);
    init(&w);
    wcifsnd_apply_plan(&w, &p, 100);
    assert(w.desired && w.validated);
    fail(&w, "injected failure", 100);
    assert(!wcifsnd_dispatch(&w, NULL, 100));
    wcifsnd_apply_plan(&w, &p, 200);
    assert(w.wake == 2100 && w.failures == 1);
    p.status.validated = 0;
    wcifsnd_apply_plan(&w, &p, 300);
    assert(w.desired && !w.validated);
    assert(!wcifsnd_dispatch(&w, NULL, 3000) && !forks);
    long long deadline = -1;
    int maxfd = -1;
    fd_set reads;
    FD_ZERO(&reads); wcifsnd_prepare(&w, &reads, &maxfd, &deadline);
    assert(deadline == -1); /* An expired retry must not spin on invalid facts. */
    p.status.validated = 1;
    wcifsnd_apply_plan(&w, &p, 3100);
    assert(!wcifsnd_dispatch(&w, NULL, 3100) && forks == 1);
    w.phase = WC_ACTIVE;
    p.status.validated = 0;
    wcifsnd_apply_plan(&w, &p, 3200);
    assert(w.phase == WC_ACTIVE && !signals); /* Retain an existing child. */
    p.links[0].addr_count = 0; /* Losing IPv4 cancels recovery. */
    wcifsnd_apply_plan(&w, &p, 3300);
    assert(!w.desired && !w.failures && w.phase == WC_STOPPING);
    waited = 42;
    assert(!wcifsnd_dispatch(&w, NULL, 3400) && !w.child && forks == 1);
    assert(!wcifsnd_dispatch(&w, NULL, 100000) && forks == 1);

    init(&w);
    fail(&w, "injected failure", 100);
    assert(!wcifsnd_dispatch(&w, NULL, 100));
    wcifsnd_shutdown(&w);
    assert(!w.desired && !w.failed);
    assert(!wcifsnd_dispatch(&w, NULL, 100000) && !forks);

    init(&w); w.child = 42; w.phase = WC_REGISTERING;
    fail(&w, "failure during registration", 100);
    wcifsnd_apply_plan(&w, &p, 101); /* Disabled while failed child is stopping. */
    waited = 42;
    assert(!wcifsnd_dispatch(&w, NULL, 102) && !w.child && !w.failed);
    assert(!wcifsnd_dispatch(&w, NULL, 100000) && !forks);

    init(&w); w.child = 42; w.phase = WC_REGISTERING;
    fail(&w, "failure during registration", 100);
    waited = 42;
    wcifsnd_shutdown(&w);
    assert(!w.child && !w.desired && !w.failed && !forks);
}

/* An add waits WCIFSND_FIRST_REPLY_MS until a reply wait times out, then
 * WCIFSND_REPLY_MS until a child has stayed registered for a minute or
 * native NBNS is no longer wanted. */
static void reply_wait_lengthens_after_a_timeout(void) {
    struct wcifsnd w;
    struct device_plan p = {0};
    init(&w);
    assert(!w.patient && reply_ms(&w) == 30000);

    w.phase = WC_REGISTERING; w.child = 42; w.deadline = 30100; w.sent = 1;
    assert(!wcifsnd_dispatch(&w, NULL, 30099) && !w.failed);
    assert(!wcifsnd_dispatch(&w, NULL, 30100));
    assert(w.failed && w.phase == WC_STOPPING && w.patient && reply_ms(&w) == 100000);

    /* Any other failure keeps the long wait. */
    waited = 42;
    assert(!wcifsnd_dispatch(&w, NULL, 30200) && !w.child);
    w.child = 42; w.phase = WC_INSPECTING; w.inspection.group = 77; w.inspection.nested = 1;
    w.inspection.lifetime = w.inspection.output = -1; w.inspection_limit = 60000;
    inspection_done = 1; socket_error = EMFILE; waited = 0;
    assert(!wcifsnd_dispatch(&w, NULL, 40000) && w.failed && w.patient);
    socket_error = 0; inspection_done = 0;

    /* A child that stays registered for a minute resets it at its next failure. */
    init(&w); w.patient = 1; w.child = 42; w.phase = WC_ACTIVE; w.active_since = 100;
    fail(&w, "short-lived success", 60099);
    assert(w.patient);
    init(&w); w.patient = 1; w.child = 42; w.phase = WC_ACTIVE; w.active_since = 100;
    fail(&w, "healthy interval", 60100);
    assert(!w.patient && reply_ms(&w) == 30000);

    /* So does losing eligibility: a later start is a new boot of native NBNS. */
    init(&w); w.patient = 1;
    wcifsnd_apply_plan(&w, &p, 100);
    assert(!w.desired && !w.patient);
}

static void set4(struct if_addr *a, const char *text) {
    memset(a, 0, sizeof(*a));
    a->family = AF_INET;
    assert(inet_pton(AF_INET, text, &a->v4) == 1);
}

/* Apple's wcifsnd enumeration: which interfaces it answers NBNS on. */
static void apple_nbns_address_rule(void) {
    const unsigned lan = TC_IFF_UP | TC_IFF_BROADCAST;
    struct if_addr a[3];
    set4(&a[0], "169.254.155.207");
    assert(addr_link_has_nbns_ipv4(lan, a, 1));                  /* 169.254 alone */
    set4(&a[1], "0.0.0.0");
    assert(!addr_link_has_nbns_ipv4(lan, a, 2));                 /* beside dhclient's 0.0.0.0 */
    assert(!addr_link_has_nbns_ipv4(lan, a + 1, 1));             /* 0.0.0.0 alone */
    set4(&a[2], "10.0.1.1");
    assert(addr_link_has_nbns_ipv4(lan, a, 3));                  /* a lease beside them */
    set4(&a[1], "192.168.1.218");
    assert(addr_link_has_nbns_ipv4(lan, a, 2));                  /* routable beside 169.254 */
    set4(&a[1], "169.254.1.2");
    assert(addr_link_has_nbns_ipv4(lan, a, 2));                  /* two 169.254 aliases */
    memset(&a[1], 0, sizeof(a[1]));
    a[1].family = AF_INET6;
    assert(inet_pton(AF_INET6, "fe80::1", &a[1].v6) == 1);
    assert(addr_link_has_nbns_ipv4(lan, a, 2));                  /* IPv6 is not another address */
    set4(&a[1], "192.168.1.218");
    assert(addr_link_has_nbns_ipv4(0xffffe043u, a, 2));          /* NetBSD 6's sign-extended flags */
    assert(!addr_link_has_nbns_ipv4(TC_IFF_UP, a, 2));           /* not BROADCAST */
    assert(!addr_link_has_nbns_ipv4(TC_IFF_BROADCAST, a, 2));    /* down */
    assert(!addr_link_has_nbns_ipv4(lan | TC_IFF_LOOPBACK, a, 2));
    assert(!addr_link_has_nbns_ipv4(0, a, 2));                   /* synthetic link: no RTM_IFINFO */
    assert(!addr_link_has_nbns_ipv4(lan, a, 0));
}

/* A device with no DHCP server holds only 169.254 beside 0.0.0.0 on its LAN:
 * no child, no retries, and nbns=none until a lease arrives. */
static void no_eligible_address_starts_nothing_until_a_lease(void) {
    struct wcifsnd w;
    struct device_plan p = {0};
    init(&w);                                                     /* resets the fakes */
    wcifsnd_init(&w, "machine");                                  /* and starts with no plan */
    assert(!strcmp(wcifsnd_readiness(&w, 0), "waiting"));       /* no plan yet */
    assert(!strcmp(wcifsnd_readiness(&w, 1), "disabled"));
    p.status.validated = 1; p.link_count = 1;
    p.links[0].mask = SVC_SMB; p.links[0].link.flags = TC_IFF_UP | TC_IFF_BROADCAST;
    p.links[0].addr_count = 2;
    set4(&p.links[0].addrs[0], "0.0.0.0");
    set4(&p.links[0].addrs[1], "169.254.155.207");
    wcifsnd_apply_plan(&w, &p, 100);
    assert(!w.eligible && !w.desired && !strcmp(wcifsnd_readiness(&w, 0), "none"));
    assert(!wcifsnd_dispatch(&w, NULL, 100000) && !forks);
    p.status.validated = 0;                                       /* incomplete facts are not "none" */
    wcifsnd_apply_plan(&w, &p, 100100);
    assert(!strcmp(wcifsnd_readiness(&w, 0), "waiting"));
    p.status.validated = 1;
    set4(&p.links[0].addrs[0], "192.168.1.218");                  /* BOUND replaces 0.0.0.0 */
    wcifsnd_apply_plan(&w, &p, 100200);
    assert(w.eligible && w.desired && !strcmp(wcifsnd_readiness(&w, 0), "waiting"));
    assert(!wcifsnd_dispatch(&w, NULL, 100200) && forks == 1);
    assert(!strcmp(wcifsnd_readiness(&w, 0), "starting"));
    w.phase = WC_ACTIVE;
    assert(!strcmp(wcifsnd_readiness(&w, 0), "ready"));
    set4(&p.links[0].addrs[0], "0.0.0.0");                        /* the lease is gone again */
    wcifsnd_apply_plan(&w, &p, 100300);
    assert(!w.desired && w.phase == WC_STOPPING);
    waited = 42;
    assert(!wcifsnd_dispatch(&w, NULL, 100400) && !w.child);
    assert(!strcmp(wcifsnd_readiness(&w, 0), "none"));
    assert(!wcifsnd_dispatch(&w, NULL, 200000) && forks == 1);
}

int main(void) {
    retry_deadlines(); cleanup_before_retry(); stopped_inspection_defers_retry();
    starting_child_is_inspected_until_it_owns_its_ports();
    errors_and_stale_replies(); eligibility_and_shutdown(); reply_wait_lengthens_after_a_timeout();
    apple_nbns_address_rule(); no_eligible_address_starts_nothing_until_a_lease();
    return 0;
}
