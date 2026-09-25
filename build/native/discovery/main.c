#include "discovery.h"
#include "wcifsnd.h"
#include "../common/loop.h"
#include "../common/parent.h"
#ifdef TC_SERVICE_MULTICALL
#define main tc_discovery_main
#endif

volatile sig_atomic_t g_stop = 0;
static int parent_fd = -1;
static int collect_cancelled(void) { return g_stop || !tc_parent_alive(parent_fd); }

static void on_signal(int signo) {
    (void)signo;
    g_stop = 1;
}

/* Native process titles expose only current in-memory readiness. Doctor also
 * checks the child's PPID and sockets; no stale status/PID file is needed. */
static void publish_readiness(const struct wcifsnd *nbns, const struct config *cfg,
                              const char *netbios) {
#if defined(__NetBSD__)
    const char *state = nbns->phase == WC_ACTIVE ? "ready" :
        nbns->phase != WC_OFF ? "starting" :
        cfg->diskless ? "disabled" : "waiting";
    setproctitle("role=discovery nbns=%s mode=%s %s--netbios-name %s", state,
                 cfg->diskless ? "diskless" : "payload",
                 cfg->diskless ? "--diskless " : "", netbios);
#else
    (void)nbns; (void)cfg; (void)netbios;
#endif
}

static void usage(const char *prog) {
    fprintf(stderr,
            "Usage: %s [--diskless] [--netbios-name NAME] [--adisk-share NAME KEY UUID FLAGS]... [--debug-logging]\n"
            "       %s --help\n"
            "Registers _smb/_adisk (and _afpovertcp when MDNS_ADVERTISE_AFP=1) with\n"
            "Apple's mDNSResponder on every link the device plan allows.\n"
            "When enabled, Apple's wcifsnd serves the canonical NetBIOS name.\n",
            prog, prog);
}

int main(int argc, char **argv) {
    struct config cfg;
    struct plan_options options;
    struct plan_loop loop;
    struct registrant reg;
    struct wcifsnd nbns;
    char netbios[16] = "";
    int result = EXIT_OK;
    const char *facts_file = NULL;
    int i;

    memset(&cfg, 0, sizeof(cfg));
    memset(&options, 0, sizeof(options));
    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--diskless")) {
            cfg.diskless = 1;
        } else if (!strcmp(argv[i], "--netbios-name") && i + 1 < argc) {
            if (strlen(argv[i + 1]) > 15 || normalize_netbios_name(netbios, sizeof(netbios), argv[++i]) != 0) {
                fprintf(stderr, "netbios name must be 15 bytes or fewer and contain letters or digits\n");
                usage(argv[0]); return EXIT_USAGE;
            }
        } else if (!strcmp(argv[i], "--adisk-share") && i + 4 < argc) {
            /* The manager passes its final Samba share names directly, so
             * discovery never needs a second disk inventory or state file. */
            if (add_adisk_disk_config(&cfg, argv[i + 1], argv[i + 2], argv[i + 3], argv[i + 4]) != 0) {
                return EXIT_INVALID_ADISK_DISK;
            }
            i += 4;
        } else if (!strcmp(argv[i], "--debug-logging")) {
            cfg.debug_logging = 1;
#ifdef TC_NATIVE_TEST
        } else if (!strcmp(argv[i], "--facts-file") && i + 1 < argc) {
            facts_file = argv[++i];
#endif
        } else if (!strcmp(argv[i], "--help") || !strcmp(argv[i], "-h")) {
            usage(argv[0]);
            return EXIT_OK;
        } else {
            usage(argv[0]);
            return EXIT_USAGE;
        }
    }
    options.diskless = cfg.diskless;

    if (!cfg.diskless && !netbios[0]) { usage(argv[0]); return EXIT_USAGE; }
    wcifsnd_init(&nbns, netbios);
    publish_readiness(&nbns, &cfg, netbios);

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGPIPE, SIG_IGN);
    registrant_install_ipc_fence();

    registrant_init(&reg, &cfg);
    parent_fd = tc_parent_pipe();
    if (parent_fd >= 0 && getpgrp() == getpid())
        acp_set_scope(1, collect_cancelled);
    plan_loop_init(&loop, &options, facts_file);
    plan_loop_request(&loop, plan_loop_now_ms());
    fprintf(stderr, "discovery starting%s%s\n", cfg.diskless ? " (diskless)" : "",
            cfg.adisk_disks.count ? " with adisk rows" : "");

    while (!g_stop && !acp_stop_requested) {
        fd_set reads;
        int maxfd = -1;
        long long deadline = -1;
        long long now = plan_loop_now_ms();

        FD_ZERO(&reads);
        plan_loop_prepare(&loop, now, &reads, &maxfd, &deadline);
        registrant_prepare(&reg, &reads, &maxfd, &deadline);
        wcifsnd_prepare(&nbns, &reads, &maxfd, &deadline);
        tc_parent_prepare(parent_fd, &reads, &maxfd);
        if (plan_loop_wait(&reads, maxfd, now, deadline) < 0) {
            perror("select");
            result = EXIT_PLAN_FAILED;
            break;
        }
        if (g_stop || (parent_fd >= 0 && FD_ISSET(parent_fd, &reads) && !tc_parent_alive(parent_fd))) {
            break;
        }
        now = plan_loop_now_ms();
        if (plan_loop_dispatch(&loop, now, &reads)) {
            wcifsnd_apply_plan(&nbns, &loop.current, now);
            registrant_apply_plan(&reg, &loop.current, now);
        }
        /* Native NBNS retries preserve Bonjour; only unsafe child cleanup
         * escalates to the manager's whole-process recovery. */
        if (wcifsnd_dispatch(&nbns, &reads, now) < 0) { result = EXIT_DAEMON_STALLED; break; }
        publish_readiness(&nbns, &cfg, netbios);
        registrant_dispatch(&reg, &reads, now);
    }

    fprintf(stderr, "discovery stopping; deregistering everything\n");
    wcifsnd_shutdown(&nbns);
    registrant_shutdown(&reg);
    plan_loop_close(&loop);
    return result;
}
