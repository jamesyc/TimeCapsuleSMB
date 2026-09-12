#include <signal.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void record(const char *key, const char *kind) {
    const char *path = getenv("TC_TEST_ACP_CALLS");
    FILE *file;
    if (!path) return;
    file = fopen(path, "a");
    if (!file) _exit(90);
    fprintf(file, "%s %ld %s\n", key, (long)getpid(), kind);
    fclose(file);
}

int main(int argc, char **argv) {
    const char *mode = getenv("TC_TEST_ACP_MODE");
    const char *key = getenv("TC_TEST_ACP_KEY");
    const char *value = "";
    int i;
    if (argc != 3 || strcmp(argv[1], "-q")) return 91;
    if (!key) key = "syAP";
    if (!mode || (strcmp(key, "*") && strcmp(key, argv[2]))) mode = "normal";
    if (!strcmp(mode, "ignore_term")) signal(SIGTERM, SIG_IGN);
    record(argv[2], "parent");
    if (!strcmp(mode, "crash")) { kill(getpid(), SIGKILL); return 93; }
    if (!strcmp(mode, "inspect_fds")) {
        for (i = 3; i < 64; i++) if (fcntl(i, F_GETFD) >= 0) return 94;
    }
    if (!strcmp(mode, "value")) {
        const char *text = getenv("TC_TEST_ACP_VALUE");
        if (!text) return 95;
        fputs(text, stdout);
        if (!getenv("TC_TEST_ACP_NO_NEWLINE")) putchar('\n');
        return 0;
    }
    if (!strcmp(mode, "nul")) {
        fwrite("first\0hidden\n", 1, 13, stdout);
        return 0;
    }
    if (!strcmp(mode, "drip") || !strcmp(mode, "drip_after_line")) {
        if (!strcmp(mode, "drip_after_line")) puts("0x77");
        for (;;) { putchar('x'); fflush(stdout); usleep(50000); }
    }
    if (!strcmp(mode, "descendant")) {
        pid_t child = fork();
        if (child < 0) return 92;
        if (child > 0) return 0;
        signal(SIGTERM, SIG_IGN);
        record(argv[2], "descendant");
        for (;;) pause();
    }
    if (!strcmp(mode, "line_hang")) { puts("value"); fflush(stdout); }
    if (!strcmp(mode, "closed_hang")) close(STDOUT_FILENO);
    if (!strcmp(mode, "hang") || !strcmp(mode, "line_hang") ||
        !strcmp(mode, "closed_hang") || !strcmp(mode, "ignore_term"))
        for (;;) pause();
    if (!strcmp(mode, "oversized")) {
        for (i = 0; i < 4096; i++) putchar('x');
        putchar('\n'); return 0;
    }
    if (!strcmp(mode, "nonzero")) { puts("untrusted output"); return 1; }
    if (!strcmp(mode, "empty")) { puts(""); return 0; }
    if (!strcmp(mode, "slow")) sleep(6);
    if (!strcmp(mode, "slow_each")) usleep(300000);
    if (!strcmp(argv[2], "syAP")) value = "0x77";
    if (!strcmp(argv[2], "syAM")) value = "TimeCapsule8,119";
    if (!strcmp(argv[2], "syNm")) value = "Test Capsule";
    if (!strcmp(argv[2], "waMA")) value = "02:00:00:00:00:01";
    if (!strcmp(argv[2], "raMA")) value = "02:00:00:00:00:02";
    if (!*value) return 1; /* sySN is unavailable: exercise the normal fallback. */
    puts(value);
    if (!strcmp(mode, "drain"))
        for (i = 0; i < 65536; i++) putchar('x');
    return 0;
}
