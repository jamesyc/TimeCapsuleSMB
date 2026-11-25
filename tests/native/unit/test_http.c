/* Drives telemetry_http (http.c) and its DNS client (dns.c) against servers
 * the Python side runs:
 *   test_http post URL [LIMIT]   POST "{}"; prints "ok <body>" or "fail <outcome>",
 *                                the elapsed ms and the socket() failures counted
 *   test_http get URL [LIMIT]    GET, the same
 *   test_http step URL           every wall clock (time, gettimeofday,
 *                                clock_gettime REALTIME) jumps an hour on every
 *                                read after the first: the deadline must not care
 *   test_http twice URL          POST twice; the second must reuse the DNS answer
 *   test_http nofile URL         POST with no descriptor left for a socket or file
 *   test_http resolve NAME       print the addresses tc_dns_resolve returns
 *   test_http merge              check how telemetry_http ranks the failures of
 *                                its addresses (merge_failure): "ok", or the
 *                                first wrong case and exit status 1
 *   test_http judge              check dns.c's verdict on every combination of
 *                                A and AAAA replies and addresses (judge()):
 *                                "ok", or the first wrong case and exit status 1
 *   test_http script -           one command per stdin line, one reply line each:
 *                                "resolve NAME" (addresses or "fail", then
 *                                "resource=N"), "post URL" ("ok <body>" or
 *                                "fail <outcome>", then "elapsed_ms=N"),
 *                                "advance SECONDS" (moves the monotonic clock
 *                                on), "limit N" (descriptor limit; 0 restores
 *                                it)
 * SIGTERM sets telemetry_stop, as the daemon's handler does.
 * TC_HTTP_DEADLINE_MS, TC_HTTP_CONNECT_MS, TC_HTTP_ADDRESS_MS,
 * TC_DNS_RETRANS_MS, TC_DNS_PORT and TC_RESOLV_CONF are set at build time by
 * the test. acp.c is not linked: acp_monotonic_ms is this file's, so the
 * script can move it on. */
#include "telemetry.h"
#include "acp.h"
#include "errclass.h"
#include "dns.h"
#include <assert.h>
#include <sys/resource.h>
#include <net/if.h>
#include "iflist.h"
#ifndef __APPLE__
#include <sys/syscall.h>
#endif

volatile sig_atomic_t telemetry_stop = 0;

/* The interface table dns.c reads to map a nameserver's scope name: the
 * host's loopback alone, under its real index, so a scoped link-local
 * nameserver on it is reachable. iflist.c parses only NetBSD's layout. */
int iflist_collect(struct if_table *out) {
    static const char *const names[] = {"lo0", "lo"};
    size_t i;
    memset(out, 0, sizeof(*out));
    for (i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
        unsigned index = if_nametoindex(names[i]);
        if (!index) continue;
        snprintf(out->links[0].name, sizeof(out->links[0].name), "%s", names[i]);
        out->links[0].index = index;
        out->link_count = 1;
    }
    return 0;
}

/* The kernel's clocks, read without the overrides below. */
static long long kernel_ns(clockid_t clock) {
#ifdef __APPLE__
    return (long long)clock_gettime_nsec_np(clock);
#else
    struct timespec now;
    if (syscall(SYS_clock_gettime, clock, &now)) return -1;
    return (long long)now.tv_sec * 1000000000LL + now.tv_nsec;
#endif
}

static long long advanced_ms;
long long acp_monotonic_ms(void) {
    long long ns = kernel_ns(CLOCK_MONOTONIC);
    return ns < 0 ? -1 : ns / 1000000 + advanced_ms;
}

/* NTP stepping the clock: every read after the first is an hour later. */
static int stepping, wall_reads;
static long long wall_ns(void) {
    return kernel_ns(CLOCK_REALTIME) + (stepping && wall_reads++ ? 3600LL * 1000000000 : 0);
}

time_t time(time_t *out) {
    time_t value = (time_t)(wall_ns() / 1000000000);
    if (out) *out = value;
    return value;
}

int gettimeofday(struct timeval *tv, void *tz) {
    long long ns = wall_ns();
    (void)tz;
    tv->tv_sec = (time_t)(ns / 1000000000);
    tv->tv_usec = (suseconds_t)(ns % 1000000000 / 1000);
    return 0;
}

int clock_gettime(clockid_t clock, struct timespec *ts) {
    long long ns = clock == CLOCK_REALTIME ? wall_ns() : kernel_ns(clock);
    if (ns < 0) { errno = EINVAL; return -1; }
    ts->tv_sec = (time_t)(ns / 1000000000);
    ts->tv_nsec = (long)(ns % 1000000000);
    return 0;
}

static void stop(int sig) { (void)sig; telemetry_stop = 1; }

static const char *const outcome_names[] = {"delivered", "cancelled", "busy", "local_resource",
                                            "acp_unavailable", "network", "server"};

/* http.c's merge_failure, exposed in test builds. */
enum tc_outcome tc_http_merge_failure(enum tc_outcome kept, enum tc_outcome got);

/* Every pair the request can meet, in both orders: a server that answered
 * wins, then the device's want of resources, then the network. kept starts
 * at NETWORK (no failure yet, or a failed lookup without a shortage), so
 * NETWORK must leave got as it is. */
static int merge_table(void) {
    static const struct { enum tc_outcome kept, got, want; } cases[] = {
        {TC_OUTCOME_NETWORK, TC_OUTCOME_NETWORK, TC_OUTCOME_NETWORK},
        {TC_OUTCOME_NETWORK, TC_OUTCOME_LOCAL_RESOURCE, TC_OUTCOME_LOCAL_RESOURCE},
        {TC_OUTCOME_LOCAL_RESOURCE, TC_OUTCOME_NETWORK, TC_OUTCOME_LOCAL_RESOURCE},
        {TC_OUTCOME_LOCAL_RESOURCE, TC_OUTCOME_LOCAL_RESOURCE, TC_OUTCOME_LOCAL_RESOURCE},
        {TC_OUTCOME_NETWORK, TC_OUTCOME_SERVER, TC_OUTCOME_SERVER},
        {TC_OUTCOME_SERVER, TC_OUTCOME_NETWORK, TC_OUTCOME_SERVER},
        {TC_OUTCOME_LOCAL_RESOURCE, TC_OUTCOME_SERVER, TC_OUTCOME_SERVER},
        {TC_OUTCOME_SERVER, TC_OUTCOME_LOCAL_RESOURCE, TC_OUTCOME_SERVER},
        {TC_OUTCOME_SERVER, TC_OUTCOME_SERVER, TC_OUTCOME_SERVER},
    };
    size_t i;
    for (i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        enum tc_outcome got = tc_http_merge_failure(cases[i].kept, cases[i].got);
        if (got != cases[i].want) {
            printf("case %lu gave %s\n", (unsigned long)i, outcome_names[got]);
            return 1;
        }
    }
    puts("ok");
    return 0;
}

/* dns.c's judge(), by state and verdict names, exposed in test builds. */
const char *tc_dns_judge(const char *a_reply, const char *aaaa_reply, size_t n4, size_t n6);

/* Every A reply against every AAAA reply, with no address, an IPv4 one or
 * an IPv6 one: the table beside judge(). */
static int judge_table(void) {
    static const char *const states[] = {"waiting", "clean", "failed", "no_name"};
    /* No address: by A reply (rows) and AAAA reply (columns), in states'
     * order. */
    static const char *const none[4][4] = {
        /*             waiting     clean       failed      no_name */
        /* waiting */ {"ask_next", "ask_next", "ask_next", "ask_next"},
        /* clean   */ {"ask_next", "gone",     "ask_next", "ask_next"},
        /* failed  */ {"ask_next", "ask_next", "ask_next", "ask_next"},
        /* no_name */ {"gone",     "gone",     "gone",     "gone"},
    };
    /* An address in either reply: by A reply alone, whatever AAAA said. */
    static const char *const some[4] = {"use_with_kept_ipv4", "keep", "use_with_kept_ipv4", "gone"};
    size_t a, aaaa, found;
    for (a = 0; a < 4; a++)
        for (aaaa = 0; aaaa < 4; aaaa++)
            for (found = 0; found < 3; found++) {
                /* found 0: none; 1: one IPv4 address; 2: one IPv6 address. */
                const char *want = found ? some[a] : none[a][aaaa];
                const char *got = tc_dns_judge(states[a], states[aaaa], found == 1, found == 2);
                if (strcmp(got, want)) {
                    printf("A %s, AAAA %s, %s: %s, not %s\n", states[a], states[aaaa],
                           found ? (found == 1 ? "IPv4" : "IPv6") : "no address", got, want);
                    return 1;
                }
            }
    puts("ok");
    return 0;
}

static void print_resolve(const char *name, long long deadline, int show_resource) {
    struct tc_dns_addr addrs[TC_DNS_MAX_ADDRS];
    size_t count, i;
    int resource = -1;
    char text[INET6_ADDRSTRLEN];
    if (tc_dns_resolve(name, addrs, TC_DNS_MAX_ADDRS, &count, deadline, &resource)) {
        fputs("fail", stdout);
    } else {
        for (i = 0; i < count; i++)
            printf("%s%s", i ? " " : "", inet_ntop(addrs[i].family, addrs[i].addr, text, sizeof(text)));
    }
    if (show_resource) printf(" resource=%d", resource);
    putchar('\n');
}

/* One POST in the script's process, so it shares the DNS answer cache with
 * the script's other commands. */
static void script_post(const char *url) {
    static unsigned char body[TC_HTTP_BUFFER(64)];
    size_t len;
    enum tc_outcome outcome;
    long long started = acp_monotonic_ms();
    if (!telemetry_http(url, "{}", body, sizeof(body), &len, &outcome))
        printf("ok %s", (char *)body);
    else
        printf("fail %s", outcome_names[outcome]);
    printf(" elapsed_ms=%lld\n", acp_monotonic_ms() - started);
}

static int script(void) {
    char line[512], arg[400];
    struct rlimit files, saved;
    assert(!getrlimit(RLIMIT_NOFILE, &saved));
    while (fgets(line, sizeof(line), stdin)) {
        long value;
        if (sscanf(line, "resolve %399s", arg) == 1) {
            print_resolve(arg, acp_monotonic_ms() + 5000, 1);
        } else if (sscanf(line, "post %399s", arg) == 1) {
            script_post(arg);
        } else if (sscanf(line, "advance %ld", &value) == 1) {
            advanced_ms += value * 1000;
            puts("ok");
        } else if (sscanf(line, "limit %ld", &value) == 1) {
            files = saved;
            if (value) files.rlim_cur = (rlim_t)value;
            assert(!setrlimit(RLIMIT_NOFILE, &files));
            puts("ok");
        } else {
            return 2;
        }
        fflush(stdout);
    }
    return 0;
}

/* nofile lowers only the soft descriptor limit and puts it back after the
 * request: a sanitizer build's leak check opens /proc at exit. */
static struct rlimit files_saved;
static void restore_files(void) { assert(!setrlimit(RLIMIT_NOFILE, &files_saved)); }

int main(int argc, char **argv) {
    static unsigned char body[TC_HTTP_BUFFER(4096)];
    size_t len = 99, limit = argc > 3 ? (size_t)strtoul(argv[3], NULL, 10) : 64;
    long long started = acp_monotonic_ms();
    struct tc_failure_counts counts;
    enum tc_outcome outcome;
    int rc;
    if (argc == 2 && !strcmp(argv[1], "merge")) return merge_table();
    if (argc == 2 && !strcmp(argv[1], "judge")) return judge_table();
    if (argc < 3 || limit > 4096) return 2;
    signal(SIGTERM, stop);
    signal(SIGPIPE, SIG_IGN); /* as the telemetry daemon does */
    if (!strcmp(argv[1], "script")) return script();
    if (!strcmp(argv[1], "resolve")) {
        /* The build's request deadline, which bounds the lookup in a request. */
        print_resolve(argv[2], acp_monotonic_ms() + TC_HTTP_DEADLINE_MS, 0);
        printf("elapsed_ms %lld\n", acp_monotonic_ms() - started);
        return 0;
    }
    stepping = !strcmp(argv[1], "step");
    if (!strcmp(argv[1], "twice")) {
        if (telemetry_http(argv[2], "{}", body, TC_HTTP_BUFFER(limit), &len, &outcome)) {
            puts("fail first");
            return 0;
        }
        assert(outcome == TC_OUTCOME_DELIVERED);
    }
    if (!strcmp(argv[1], "nofile")) {
        /* Only stdin, stdout and stderr: socket() and fopen() fail with EMFILE. */
        struct rlimit limit3;
        assert(!getrlimit(RLIMIT_NOFILE, &files_saved));
        limit3 = files_saved;
        limit3.rlim_cur = 3;
        assert(!setrlimit(RLIMIT_NOFILE, &limit3));
    }
    rc = telemetry_http(argv[2], strcmp(argv[1], "get") ? "{}" : NULL, body, TC_HTTP_BUFFER(limit), &len, &outcome);
    if (!strcmp(argv[1], "nofile")) restore_files();
    tc_failure_counts(&counts);
    if (rc == 0) {
        assert(outcome == TC_OUTCOME_DELIVERED && body[len] == 0);
        printf("ok %s\n", (char *)body);
    } else {
        assert(outcome != TC_OUTCOME_DELIVERED && len == 0);
        printf("fail %s\n", outcome_names[outcome]);
    }
    printf("elapsed_ms %lld\nsocket_failures %u\n", acp_monotonic_ms() - started,
           counts.socket_emfile + counts.socket_enfile + counts.socket_enobufs + counts.socket_other);
    return 0;
}
