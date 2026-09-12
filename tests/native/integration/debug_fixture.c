#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <string.h>
#include <signal.h>

int main(int argc, char **argv) {
    const char *marker = getenv("TC_TEST_MARKER");
    const char *finish = getenv("TC_TEST_FINISH");
    const char *lock_text = getenv("TC_DEBUG_LOCK_FD");
    char signature_path[1024];
    FILE *out;
    int flags;
    (void)argc;
    if (!lock_text) return 50;
    flags = fcntl(atoi(lock_text), F_GETFD);
    if (flags < 0 || (flags & FD_CLOEXEC)) return 51;
    snprintf(signature_path, sizeof(signature_path), "%s.sig", argv[0]);
    if (!access(signature_path, F_OK)) return 52;
    if (getenv("TC_TEST_DETACH")) {
        pid_t child = fork();
        if (child < 0) return 53;
        if (child > 0) return 0;
    }
    if (!marker || !(out = fopen(marker, "w"))) return 1;
    fprintf(out, "executed\n%ld\n", (long)getpid());
    fclose(out);
    while (finish && access(finish, F_OK)) usleep(10000);
    if (getenv("TC_TEST_LEAVE_SIG_DIR") && mkdir(signature_path, 0700)) return 54;
    if (getenv("TC_TEST_CRASH")) raise(SIGKILL);
    return getenv("TC_TEST_FAIL") ? 17 : 0;
}
