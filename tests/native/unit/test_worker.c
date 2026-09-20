#include "common/worker.h"
#include "common/process.h"
#include <assert.h>
#include <sys/resource.h>
#include <sys/stat.h>

static void contents(const char *path, const char *text) {
    FILE *file = fopen(path, "w");
    assert(file && fputs(text, file) >= 0 && !fclose(file));
}
int main(int argc, char **argv) {
    char output[64];
    struct stat st;
    assert(argc == 2);
    alarm(15);
    tc_worker_begin("test");
    if (!strcmp(argv[1], "copy")) {
        contents("source", "payload data");
        assert(tc_copy_file("source", "prepared", 0755) == 0);
        assert(!stat("prepared", &st) && st.st_size == 12 && (st.st_mode & 0777) == 0755);
        contents("source", "replacement");
        assert(tc_copy_file("source", "prepared", 0600) == 0);
        assert(!stat("prepared", &st) && st.st_size == 11 && (st.st_mode & 0777) == 0600);
    } else if (!strcmp(argv[1], "faults")) {
        struct rlimit limit = {4, 4};
        assert(tc_copy_file("missing", "prepared", 0600) < 0);
        assert(!mkdir("directory", 0700));
        assert(tc_copy_file("directory", "prepared", 0600) < 0);
        contents("source", "payload too large for limit");
        signal(SIGXFSZ, SIG_IGN);
        assert(!setrlimit(RLIMIT_FSIZE, &limit));
        assert(tc_copy_file("source", "prepared", 0600) < 0);
        assert(lstat("prepared", &st) < 0 && errno == ENOENT);
        assert(tc_copy_file("source", "directory", 0600) < 0);
        assert(!lstat("directory", &st) && S_ISDIR(st.st_mode));
    } else if (!strcmp(argv[1], "directories")) {
        assert(!tc_make_dir("directory", 0700));
        assert(!tc_make_dir("directory", 0700));
        assert(!symlink("directory", "link"));
        assert(tc_make_dir("link", 0700) < 0);
        contents("file", "preserve");
        assert(tc_make_dir("file", 0700) < 0);
    } else if (!strcmp(argv[1], "commands")) {
        char *ok[] = {"/bin/sh", "-c", "printf result", NULL};
        char *fail[] = {"/bin/sh", "-c", "printf error; exit 4", NULL};
        char *timeout[] = {"/bin/sleep", "10", NULL};
        assert(!tc_command_capture(ok, output, sizeof(output), 2));
        assert(!strcmp(output, "result"));
        assert(tc_command_capture(ok, output, 3, 2) < 0);
        assert(tc_command_capture(fail, output, sizeof(output), 2) < 0);
        assert(!strcmp(output, "error"));
        assert(tc_command_run(timeout, 0) < 0);
    } else if (!strcmp(argv[1], "cancel")) {
        int life[2];
        assert(!pipe(life) && dup2(life[0], STDIN_FILENO) >= 0);
        close(life[0]);
        tc_worker_begin("cancel");
        assert(!tc_worker_cancelled());
        close(life[1]);
        assert(tc_worker_cancelled());
        assert(tc_make_dir("never", 0700) < 0);
        assert(tc_copy_file("missing", "never", 0700) < 0);
        assert(access("never", F_OK) < 0);
    } else
        return 2;
    return 0;
}
