#include "../../../build/native/discovery/wcifsnd.c"

int tc_native_nbns_sockets_present(const char *text, pid_t pid, unsigned control_port) {
    (void)text; (void)pid; (void)control_port;
    return 1;
}

static void hex(const unsigned char *p, size_t n) {
    size_t i;
    for (i = 0; i < n; i++) printf("%02x", p[i]);
    putchar('\n');
}

static void make_reply(const struct wcifsnd *w, unsigned char *p, size_t n,
                       unsigned flags, unsigned rdlength) {
    memset(p, 0, n);
    memcpy(p, w->request, 2);
    put16(p + 2, flags); put16(p + 6, 1);
    memcpy(p + 12, w->request + 12, 34);
    put16(p + 46, 32); put16(p + 48, 1); put16(p + 54, rdlength);
}

int main(void) {
    struct wcifsnd w;
    unsigned char p[62];
    int record;
    pid_t unrelated, owned, got;
    int status;
    wcifsnd_init(&w, "machine");
    for (record = 0; record < 3; record++) {
        w.record = record; request_name(&w); hex(w.request, sizeof(w.request));
    }
    make_reply(&w, p, 62, 0xa800, 6);
    printf("success=%d\n", response(&w, p, 62));
    put16(p, w.transaction - 1); printf("stale=%d\n", response(&w, p, 62));
    put16(p, w.transaction); p[13] ^= 1; printf("malformed=%d\n", response(&w, p, 62));
    /* Apple's defended-name reply and any other rcode are provisional. */
    make_reply(&w, p, 62, 0xa803, 6); printf("defended=%d\n", response(&w, p, 62));
    make_reply(&w, p, 62, 0xa805, 6); printf("negative=%d\n", response(&w, p, 62));
    make_reply(&w, p, 62, 0xa807, 6); printf("conflict=%d\n", response(&w, p, 62));
    /* Apple never sends WACK on 922; it is not final like any other reply. */
    make_reply(&w, p, 58, 0xb800, 2); printf("wack=%d\n", response(&w, p, 58));
    make_reply(&w, p, 62, 0xa800, 12); printf("rdlength=%d\n", response(&w, p, 62));
    make_reply(&w, p, 62, 0xa800, 6); printf("short=%d\n", response(&w, p, 61));
    printf("runt=%d\n", response(&w, p, 1));

    unrelated = fork();
    if (unrelated == 0) _exit(37);
    owned = fork();
    if (owned == 0) { pause(); _exit(0); }
    usleep(50000);
    w.child = owned; w.phase = WC_ACTIVE; w.desired = 1;
    (void)wcifsnd_dispatch(&w, NULL, 1000);
    got = waitpid(unrelated, &status, 0);
    printf("unrelated=%d\n", got == unrelated && WIFEXITED(status) && WEXITSTATUS(status) == 37);
    kill(owned, SIGKILL); waitpid(owned, NULL, 0);
    return 0;
}
