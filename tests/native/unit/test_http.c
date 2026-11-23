/* Drives telemetry_http (http.c) and its DNS client (dns.c) against servers
 * the Python side runs:
 *   test_http post URL [LIMIT]   POST "{}"; prints "ok <body>" or "fail <outcome>",
 *                                the elapsed ms and the socket() failures counted
 *   test_http get URL [LIMIT]    GET, the same
 *   test_http step URL           the wall clock jumps an hour on every read
 *                                after the first: the deadline must not care
 *   test_http twice URL          POST twice; the second must reuse the DNS answer
 *   test_http nofile URL         POST with no descriptor left for a socket
 *   test_http resolve NAME       print the addresses tc_dns_resolve returns
 * SIGTERM sets telemetry_stop, as the daemon's handler does.
 * TC_HTTP_DEADLINE_MS, TC_HTTP_CONNECT_MS, TC_DNS_SERVER_MS, TC_DNS_PORT and
 * TC_RESOLV_CONF are set at build time by the test. */
#include "telemetry.h"
#include "acp.h"
#include "errclass.h"
#include "dns.h"
#include <assert.h>
#include <sys/resource.h>

volatile sig_atomic_t telemetry_stop = 0;

/* NTP stepping the clock: every read after the first is an hour later. */
static int stepping, wall_reads;
time_t time(time_t *out) {
    struct timeval now;
    time_t value;
    gettimeofday(&now, NULL);
    value = now.tv_sec + (stepping && wall_reads++ ? 3600 : 0);
    if (out) *out = value;
    return value;
}

static void stop(int sig) { (void)sig; telemetry_stop = 1; }

static const char *const outcome_names[] = {"delivered", "cancelled", "busy", "local_resource",
                                            "acp_unavailable", "network", "server"};

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
    if (argc < 3 || limit > 4096) return 2;
    signal(SIGTERM, stop);
    if (!strcmp(argv[1], "resolve")) {
        struct tc_dns_addr addrs[TC_DNS_MAX_ADDRS];
        size_t count, i;
        int resource = 0;
        char text[INET6_ADDRSTRLEN];
        if (tc_dns_resolve(argv[2], addrs, TC_DNS_MAX_ADDRS, &count, acp_monotonic_ms() + 5000, &resource)) {
            puts("fail");
        } else {
            for (i = 0; i < count; i++)
                printf("%s%s", i ? " " : "", inet_ntop(addrs[i].family, addrs[i].addr, text, sizeof(text)));
            putchar('\n');
        }
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
        /* Only stdin, stdout and stderr: socket() fails with EMFILE. */
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
