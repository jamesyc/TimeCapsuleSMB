/* A registration made after select() must not act on that select()'s result
 * (discovery/main.c applies a plan between the wait and registrant_dispatch):
 *   registrant_stale_ready_fd <facts-file>
 * The descriptor the wait reported readable is closed before the plan is
 * applied, so the new DNSServiceRef socket reuses its number and inherits
 * its readable bit. The fake daemon holds its reply: reading that socket
 * would block until the IPC fence exits with EXIT_DAEMON_STALLED. After
 * "dispatched", a normal prepare/select/dispatch loop must still deliver
 * the reply once the test releases it. */
#include <sys/select.h>
#include <sys/time.h>
#include "common/plan.h"
#include "discovery/registrant.h"

int main(int argc, char **argv) {
    struct device_facts facts;
    struct device_plan plan;
    struct plan_options options;
    struct config cfg;
    struct registrant reg;
    fd_set reads;
    FILE *fp;
    int pipefd[2], stale, deadline;

    if (argc != 2 || (fp = fopen(argv[1], "r")) == NULL || device_facts_parse_file(&facts, fp) != 0) return 2;
    fclose(fp);
    memset(&options, 0, sizeof(options));
    memset(&cfg, 0, sizeof(cfg));
    if (device_plan_build(&plan, &facts, NULL, &options, 100000) != 0 || !plan.status.validated) return 3;
    registrant_install_ipc_fence();
    registrant_init(&reg, &cfg);

    /* The wait reported this descriptor readable; then it was drained and closed. */
    if (pipe(pipefd) || write(pipefd[1], "x", 1) != 1) return 4;
    FD_ZERO(&reads);
    FD_SET(pipefd[0], &reads);
    stale = pipefd[0];
    close(pipefd[0]);
    close(pipefd[1]);

    registrant_apply_plan(&reg, &plan, 100000);
    if (!reg.entries[0].in_use || reg.entries[0].ref == NULL) return 5;
    printf("reused=%d\n", DNSServiceRefSockFD(reg.entries[0].ref) == stale);
    fflush(stdout);
    registrant_dispatch(&reg, &reads, 100000);
    printf("dispatched status=%d\n", (int)reg.entries[0].status);
    fflush(stdout);

    for (deadline = 0; deadline < 50 && reg.entries[0].status != REG_REGISTERED; deadline++) {
        struct timeval timeout = {0, 100000};
        int maxfd = -1;
        long long wake = -1;
        FD_ZERO(&reads);
        registrant_prepare(&reg, &reads, &maxfd, &wake);
        if (select(maxfd + 1, &reads, NULL, NULL, &timeout) < 0) return 6;
        registrant_dispatch(&reg, &reads, 100000);
    }
    printf("final status=%d\n", (int)reg.entries[0].status);
    registrant_shutdown(&reg);
    return 0;
}
