#include <signal.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <time.h>

static double monotonic_seconds(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) _exit(97);
    return now.tv_sec + now.tv_nsec / 1e9;
}

static void record(const char *key, const char *kind) {
    const char *path = getenv("TC_TEST_ACP_CALLS");
    FILE *file;
    if (!path) return;
    file = fopen(path, "a");
    if (!file) _exit(90);
    fprintf(file, "%s %ld %s %.9f\n", key, (long)getpid(), kind, monotonic_seconds());
    fclose(file);
}

/* Pace the sequence, not each spawn: reply N is due TC_TEST_ACP_PACE_MS * N
 * after the first call, so a late interpreter/sanitizer start spends less
 * time sleeping. Keep the existing key/PID/role record fields. */
static void paced_reply(void) {
    const char *path = getenv("TC_TEST_ACP_CALLS"), *pace_ms = getenv("TC_TEST_ACP_PACE_MS");
    char line[256], key[5], role[16];
    long pid;
    unsigned count = 0;
    double at, first = 0, remaining;
    FILE *file = path ? fopen(path, "r") : NULL;
    if (!file || !pace_ms) _exit(98);
    while (fgets(line, sizeof(line), file)) {
        if (sscanf(line, "%4s %ld %15s %lf", key, &pid, role, &at) == 4 && !strcmp(role, "parent")) {
            if (!count) first = at;
            count++;
        }
    }
    fclose(file);
    if (!count) _exit(99);
    remaining = first + count * (atoi(pace_ms) / 1000.0) - monotonic_seconds();
    if (remaining > 0) usleep((useconds_t)(remaining * 1e6));
}

/* TC_TEST_ACP_KEY is one key, a comma-separated list, or "*" for all. */
static int key_selected(const char *keys, const char *key) {
    size_t len = strlen(key);
    const char *p = keys;
    if (!strcmp(keys, "*")) return 1;
    while ((p = strstr(p, key)) != NULL) {
        if ((p == keys || p[-1] == ',') && (p[len] == '\0' || p[len] == ',')) return 1;
        p += len;
    }
    return 0;
}

int main(int argc, char **argv) {
    const char *mode = getenv("TC_TEST_ACP_MODE");
    const char *key = getenv("TC_TEST_ACP_KEY");
    const char *value = "";
    int i;
    if (argc != 3 || (strcmp(argv[1], "-q") && strcmp(argv[1], "-A"))) return 91;
    if (!key) key = "syAP";
    if (!mode || !key_selected(key, argv[2])) mode = "normal";
    if (!strcmp(mode, "ignore_term")) signal(SIGTERM, SIG_IGN);
    record(argv[2], "parent");
    if (!strcmp(mode, "crash")) { kill(getpid(), SIGKILL); return 93; }
    if (!strcmp(mode, "inspect_fds")) {
        for (i = 3; i < 64; i++) if (fcntl(i, F_GETFD) >= 0) return 94;
    }
    if (!strcmp(mode, "file")) {
        FILE *fp = fopen(getenv("TC_TEST_ACP_FILE"), "rb");
        int ch;
        if (!fp) return 96;
        while ((ch = fgetc(fp)) != EOF) putchar(ch);
        fclose(fp);
        return getenv("TC_TEST_ACP_EXIT") ? atoi(getenv("TC_TEST_ACP_EXIT")) : 0;
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
    if (!strcmp(mode, "slow")) usleep(1500000);
    if (!strcmp(mode, "slow_each")) paced_reply();
    if (!strcmp(argv[2], "syAP")) value = "0x77";
    if (!strcmp(argv[2], "syAM")) value = "TimeCapsule8,119";
    if (!strcmp(argv[2], "syNm")) value = "Test Capsule";
    if (!strcmp(argv[2], "waMA")) value = "02:00:00:00:00:01";
    if (!strcmp(argv[2], "raMA")) value = "02:00:00:00:00:02";
    if (getenv("TC_TEST_PLAN_MODE")) {
        int nat = !strcmp(getenv("TC_TEST_PLAN_MODE"), "nat");
        if (!strcmp(argv[2], "raNA") || !strcmp(argv[2], "raDS")) value = nat ? "1" : "0";
        if (!strcmp(argv[2], "usbF")) value = "0x450";
    }
    if (!*value) return 1; /* sySN is unavailable: exercise the normal fallback. */
    puts(value);
    if (!strcmp(mode, "drain"))
        for (i = 0; i < 65536; i++) putchar('x');
    return 0;
}
