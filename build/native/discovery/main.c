#include "../mdns/mdns.h"
#include "wcifsnd.h"
#include "../common/loop.h"

volatile sig_atomic_t g_stop = 0;

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
        cfg->diskless || nbns->enabled == 0 ? "disabled" : "waiting";
    setproctitle("nbns=%s mode=%s %s--netbios-name %s", state,
                 cfg->diskless ? "diskless" : "payload",
                 cfg->diskless ? "--diskless " : "", netbios);
#else
    (void)nbns; (void)cfg; (void)netbios;
#endif
}

static void on_acp_signal(int signo) { (void)signo; acp_stop_requested = 1; }

static int print_acp_mast(long long timeout_ms) {
    /* MaSt is text, potentially many lines. Allocate only for this one-shot
     * boot command, never in the daemon's small scalar facts buffers. */
    struct acp_request request;
    char *output = malloc(65537);
    size_t nonempty;
    int rc = 1;
    if (!output) return 1;
    memset(&request, 0, sizeof(request));
    request.key = "MaSt";
    request.form = ACP_ARRAY;
    request.multiline = 1;
    request.output = output;
    request.capacity = 65537;
    (void)acp_collect_run(&request, 1, timeout_ms, timeout_ms);
    nonempty = request.length;
    while (nonempty && output[nonempty - 1] == '\n') nonempty--;
    if (request.status == ACP_OK && nonempty) {
        rc = fwrite(output, 1, request.length, stdout) == request.length && fflush(stdout) == 0 ? 0 : 1;
    } else if (request.status == ACP_UNAVAILABLE && request.exit_status > 0) {
        rc = request.exit_status;
    }
    free(output);
    return rc;
}

static void usage(const char *prog) {
    fprintf(stderr,
            "Usage: %s [--diskless] [--netbios-name NAME] [--adisk-share NAME KEY UUID FLAGS]... [--debug-logging]\n"
            "       %s --print-link-plan | --print-mast [--timeout-seconds N] | --version\n"
            "Registers _smb/_adisk (and _afpovertcp when MDNS_ADVERTISE_AFP=1) with\n"
            "Apple's mDNSResponder on every link the device plan allows.\n"
            "When enabled, Apple's wcifsnd serves the canonical NetBIOS name.\n",
            prog, prog);
}

int tc_discovery_main(int argc, char **argv, int run_mdns, int run_netbios) {
    struct config cfg;
    struct plan_options options;
    struct plan_loop loop;
    struct registrant reg;
    struct wcifsnd nbns;
    char netbios[16] = "";
    int result = EXIT_OK;
    const char *facts_file = NULL;
    int print_plan = 0;
    long long mast_timeout_ms = (long long)TC_ACP_TIMEOUT_SECONDS * 1000;
    int i;

    /* Boot needs MaSt before disk-backed service can be staged. This fixed
     * read uses the same collector without starting a registrant or a plan. */
    if (argc >= 2 && !strcmp(argv[1], "--print-mast")) {
        if (argc == 4 && !strcmp(argv[2], "--timeout-seconds")) {
            char *end;
            unsigned long seconds;
            errno = 0;
            seconds = strtoul(argv[3], &end, 10);
            if (errno || !*argv[3] || *end || argv[3][0] == '-' || !seconds || seconds > 3600) return EXIT_USAGE;
            mast_timeout_ms = (long long)seconds * 1000;
        } else if (argc != 2) return EXIT_USAGE;
        signal(SIGTERM, on_acp_signal); signal(SIGINT, on_acp_signal);
        signal(SIGPIPE, SIG_IGN);
        return print_acp_mast(mast_timeout_ms);
    }

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
        } else if (!strcmp(argv[i], "--print-link-plan")) {
            print_plan = 1;
#ifdef TC_NATIVE_TEST
        } else if (!strcmp(argv[i], "--facts-file") && i + 1 < argc) {
            facts_file = argv[++i];
#endif
        } else if (!strcmp(argv[i], "--version")) {
            printf("%d\n", ADVERTISER_VERSION_CODE);
            return EXIT_OK;
        } else if (!strcmp(argv[i], "--help") || !strcmp(argv[i], "-h")) {
            usage(argv[0]);
            return EXIT_OK;
        } else {
            usage(argv[0]);
            return EXIT_USAGE;
        }
    }
    options.diskless = cfg.diskless;

    if (print_plan) {
        struct device_plan plan;
        int rc =
#ifdef TC_NATIVE_TEST
            facts_file != NULL ? device_plan_collect_from_file(&plan, facts_file, NULL, &options) :
#endif
            device_plan_collect(&plan, NULL, &options);
        if (rc != 0) {
            fprintf(stderr, "device plan collection failed\n");
            return EXIT_PLAN_FAILED;
        }
        device_plan_print(stdout, &plan);
        return EXIT_OK;
    }

    if (run_netbios && !cfg.diskless && !netbios[0]) { usage(argv[0]); return EXIT_USAGE; }
    wcifsnd_init(&nbns, run_netbios ? netbios : "");
    if (!run_netbios) nbns.enabled = 0;
    publish_readiness(&nbns, &cfg, netbios);

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGPIPE, SIG_IGN);
    registrant_install_ipc_fence();

    registrant_init(&reg, &cfg);
    plan_loop_init(&loop, &options, facts_file);
    plan_loop_request(&loop, plan_loop_now_ms());
    fprintf(stderr, "discoveryd %d starting%s%s\n", ADVERTISER_VERSION_CODE, cfg.diskless ? " (diskless)" : "",
            cfg.adisk_disks.count ? " with adisk rows" : "");

    while (!g_stop) {
        fd_set reads;
        int maxfd = -1;
        long long deadline = -1;
        long long now = plan_loop_now_ms();

        FD_ZERO(&reads);
        plan_loop_prepare(&loop, now, &reads, &maxfd, &deadline);
        if (run_mdns) registrant_prepare(&reg, &reads, &maxfd, &deadline);
        if (run_netbios) wcifsnd_prepare(&nbns, &reads, &maxfd, &deadline);
        if (plan_loop_wait(&reads, maxfd, now, deadline) < 0) {
            perror("select");
            result = EXIT_PLAN_FAILED;
            break;
        }
        if (g_stop) {
            break;
        }
        now = plan_loop_now_ms();
        if (plan_loop_dispatch(&loop, now, &reads)) {
            if (run_netbios) wcifsnd_apply_plan(&nbns, &loop.current, now);
            if (run_mdns) registrant_apply_plan(&reg, &loop.current, now);
        }
        if (run_netbios && wcifsnd_dispatch(&nbns, &reads, now) < 0) { result = EXIT_DAEMON_STALLED; break; }
        publish_readiness(&nbns, &cfg, netbios);
        if (run_mdns) registrant_dispatch(&reg, &reads, now);
    }

    fprintf(stderr, "discoveryd stopping; deregistering everything\n");
    if (run_netbios) wcifsnd_shutdown(&nbns);
    if (run_mdns) registrant_shutdown(&reg);
    plan_loop_close(&loop);
    return result;
}

#ifndef TC_UNIFIED_SERVICE
int main(int argc, char **argv) {
    return tc_discovery_main(argc, argv, 1, 1);
}
#endif
