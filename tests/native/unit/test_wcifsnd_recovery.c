#include "../../../build/native/discovery/wcifsnd.h"
#include <assert.h>

/* Drive production dispatch with synthetic time and syscall outcomes. Real
 * process/socket ownership is covered separately by test_wcifsnd.py. */
static int forks, signals, last_signal, wait_error, fork_error, socket_error;
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
    (void)domain; (void)type; (void)protocol;
    errno = socket_error; return -1;
}
#define fork fake_fork
#define waitpid fake_waitpid
#define kill fake_kill
#define socket fake_socket
#include "../../../build/native/discovery/wcifsnd.c"

static void init(struct wcifsnd *w) {
    wcifsnd_init(w, "machine");
    w->desired = w->validated = 1;
    forks = signals = last_signal = wait_error = fork_error = socket_error = 0;
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

    init(&w); w.phase = WC_STARTING; w.child = 42; socket_error = EMFILE;
    assert(!wcifsnd_dispatch(&w, NULL, 100) && w.phase == WC_STOPPING && w.failed);
    assert(last_signal == SIGTERM && !forks);

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

int main(void) {
    retry_deadlines(); cleanup_before_retry(); errors_and_stale_replies(); eligibility_and_shutdown();
    return 0;
}
