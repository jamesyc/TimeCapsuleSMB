/* The actual job/command helpers must own descendants after worker death.
 * Apple's utilities do not interpret stdin EOF as a parent-death request. */
#include "common/worker.h"
#include "common/process.h"
#include "common/acp.h"
#include <assert.h>

static const char *program, *mode;
static volatile sig_atomic_t signalled;
static void term(int sig) { (void)sig; signalled = 1; }
static void record(const char *name) {
    FILE *f = fopen(name, "w");
    assert(f);
    fprintf(f, "%ld %ld %ld\n", (long)getpid(), (long)getppid(), (long)getpgrp());
    assert(!fclose(f));
}
static pid_t recorded(const char *name, pid_t *group) {
    FILE *f = fopen(name, "r");
    long pid = 0, parent, pgid;
    if (f) {
        if (fscanf(f, "%ld %ld %ld", &pid, &parent, &pgid) == 3 && group) *group = pgid;
        fclose(f);
    }
    return pid;
}
static int job(void *unused) {
    char *args[] = {(char *)program, "external", (char *)mode, NULL};
    int rc;
    (void)unused;
    tc_worker_begin("nested-test");
    if (!strcmp(mode, "acp") || !strcmp(mode, "acp-descendant") || !strcmp(mode, "acp-timeout")) {
        char result[128];
        rc = read_acp_value("syNm", result, sizeof(result));
        if (!strcmp(mode, "acp-descendant") || !strcmp(mode, "acp-timeout")) {
            assert(rc == ACP_ABORT);
            assert(!!acp_stop_requested == !strcmp(mode, "acp-descendant"));
            return tc_worker_finish(7);
        }
    } else rc = tc_command_run(args, !strcmp(mode, "timeout") ? 1 : 30);
    return tc_worker_finish(rc ? 1 : 0);
}
int main(int argc, char **argv) {
    struct tc_child outer = {0};
    pid_t nested = 0, pgid = 0;
    long long end;
    int done = 0;
    assert(argc >= 2);
    program = argv[0]; mode = argv[1];
    if (!strcmp(mode, "external") || !strcmp(mode, "-q")) {
        signal(SIGTERM, SIG_IGN);
        record("command");
        if ((argc == 3 && !strcmp(argv[2], "grandchild")) || getenv("TC_NESTED_DESCENDANT")) {
            pid_t child = fork(); assert(child >= 0);
            if (child > 0) return 0;
            close(0); close(2);
            if (!getenv("TC_NESTED_DESCENDANT")) close(1);
            record("grandchild");
        }
        for (;;) pause();
    }
    alarm(12);
    if (getpgrp() != getpid()) assert(!setpgid(0, 0));
    signal(SIGTERM, term);
    if (!strcmp(mode, "acp-descendant")) assert(!setenv("TC_NESTED_DESCENDANT", "1", 1));
    assert(!tc_child_fork(&outer, job, NULL, NULL, NULL, 0, 0));
    end = acp_monotonic_ms() + 3000;
    while (!(nested = recorded("command", &pgid)) && acp_monotonic_ms() < end) usleep(1000);
    assert(nested > 0 && pgid == outer.group && pgid != getpgrp());
    if (!strcmp(mode, "grandchild")) {
        end = acp_monotonic_ms() + 3000;
        while (!recorded("grandchild", NULL) && acp_monotonic_ms() < end) usleep(1000);
        assert(recorded("grandchild", NULL) > 0);
    }
    if (!strcmp(mode, "parent-eof")) {
        close(outer.lifetime); outer.lifetime = -1;
    } else if (!strcmp(mode, "term")) {
        tc_child_stop(&outer, acp_monotonic_ms(), 1);
    } else if (strcmp(mode, "timeout") && strcmp(mode, "grandchild") &&
               strcmp(mode, "acp-descendant") && strcmp(mode, "acp-timeout")) {
        assert(!kill(outer.pid, SIGKILL));
    }
    end = acp_monotonic_ms() + 5000;
    while (!(done = tc_child_poll(&outer, acp_monotonic_ms())) && acp_monotonic_ms() < end) usleep(1000);
    assert(done && !signalled);
    if (!strcmp(mode, "acp-descendant") || !strcmp(mode, "acp-timeout"))
        assert(WIFEXITED(outer.status) && WEXITSTATUS(outer.status) == 7);
    assert(kill(-pgid, 0) < 0 && errno == ESRCH);
    tc_child_close(&outer);
    return 0;
}
