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
        assert(tc_copy_file("source", "prepared", 0755, -1, NULL) == 0);
        assert(!stat("prepared", &st) && st.st_size == 12 && (st.st_mode & 0777) == 0755);
        contents("source", "replacement");
        assert(tc_copy_file("source", "prepared", 0600, -1, NULL) == 0);
        assert(!stat("prepared", &st) && st.st_size == 11 && (st.st_mode & 0777) == 0600);
    } else if (!strcmp(argv[1], "faults")) {
        struct rlimit limit = {4, 4};
        assert(tc_copy_file("missing", "prepared", 0600, -1, NULL) < 0);
        assert(!mkdir("directory", 0700));
        assert(tc_copy_file("directory", "prepared", 0600, -1, NULL) < 0);
        contents("source", "payload too large for limit");
        signal(SIGXFSZ, SIG_IGN);
        assert(!setrlimit(RLIMIT_FSIZE, &limit));
        assert(tc_copy_file("source", "prepared", 0600, -1, NULL) < 0);
        assert(lstat("prepared", &st) < 0 && errno == ENOENT);
        assert(tc_copy_file("source", "directory", 0600, -1, NULL) < 0);
        assert(!lstat("directory", &st) && S_ISDIR(st.st_mode));
    } else if (!strcmp(argv[1], "report")) {
        /* A failed copy says how far it got: the bytes and time tell a slow
         * disk from a stuck one in the manager log. stderr goes to a pipe,
         * which the file-size limit that fails the copy does not cover. */
        struct rlimit limit = {4, 4};
        char log[256], *line;
        long long copied = -1, size = -1, seconds = -1;
        int captured[2];
        ssize_t n;
        assert(!pipe(captured) && dup2(captured[1], STDERR_FILENO) >= 0);
        assert(tc_copy_file("missing", "prepared", 0600, -1, NULL) < 0);
        contents("source", "payload too large for limit");
        signal(SIGXFSZ, SIG_IGN);
        assert(!setrlimit(RLIMIT_FSIZE, &limit));
        assert(tc_copy_file("source", "prepared", 0600, -1, NULL) < 0);
        close(captured[1]);
        close(STDERR_FILENO);
        assert((n = read(captured[0], log, sizeof(log) - 1)) > 0);
        log[n] = 0;
        assert(strstr(log, "copy open source failed: missing -> prepared: ") == log);
        /* An unopened source has no size to report. */
        assert(strstr(log, "(copied 0 bytes in 0 s)\n"));
        assert(!strstr(log, " of 0 bytes"));
        assert((line = strstr(log, "\ncopy write failed: source -> prepared: ")));
        assert(sscanf(line + 1, "copy write failed: source -> prepared: %*[^(](copied %lld of %lld bytes in %lld s)",
                      &copied, &size, &seconds) == 3);
        assert(copied >= 0 && copied <= 4 && size == 27 && seconds == 0);
    } else if (!strcmp(argv[1], "checksum")) {
        /* zlib.crc32(b"payload data"), as deploy records it. A copy whose
         * bytes read differ is removed, never left for exec, and the log
         * names the checksum of the bytes read and of deploy's file. */
        char log[512], *line;
        unsigned read_crc = 0, deployed = 0;
        int captured[2];
        ssize_t n;
        uint32_t crc = 1;
        long long read_crc_out = 1;
        contents("source", "payload data");
        assert(!tc_file_crc32("source", &crc) && crc == 0x84d769f0);
        assert(tc_file_crc32("missing", &crc) < 0 && errno == ENOENT);
        assert(tc_copy_file("source", "prepared", 0755, 0x84d769f0LL, &read_crc_out) == 0);
        assert(read_crc_out == -1); /* nothing damaged to report */
        assert(!stat("prepared", &st) && st.st_size == 12 && (st.st_mode & 0777) == 0755);
        assert(!pipe(captured) && dup2(captured[1], STDERR_FILENO) >= 0);
        assert(tc_copy_file("source", "prepared", 0755, 0x12345678LL, &read_crc_out) == TC_COPY_DISK_DAMAGED);
        assert(read_crc_out == 0x84d769f0); /* the bytes read, for the manager's log */
        assert(lstat("prepared", &st) < 0 && errno == ENOENT);
        /* A disk that cannot return the bytes (a bad sector) is damage too,
         * whether or not there is a checksum to check. */
        setenv("TC_TEST_EIO", "source", 1);
        assert(tc_copy_file("source", "prepared", 0755, -1, NULL) == TC_COPY_DISK_DAMAGED);
        assert(lstat("prepared", &st) < 0 && errno == ENOENT);
        /* What it read is no CRC-32, whatever part of the file came first. */
        assert(tc_copy_file("source", "prepared", 0755, 0x12345678LL, &read_crc_out) == TC_COPY_DISK_DAMAGED);
        assert(read_crc_out == TC_COPY_UNREADABLE);
        assert(tc_file_crc32("source", &crc) < 0 && errno == EIO);
        unsetenv("TC_TEST_EIO");
        /* So is one that cannot open it. */
        setenv("TC_TEST_EIO_OPEN", "source", 1);
        assert(tc_copy_file("source", "prepared", 0755, 0x12345678LL, &read_crc_out) == TC_COPY_DISK_DAMAGED);
        assert(read_crc_out == TC_COPY_UNREADABLE);
        assert(tc_file_crc32("source", &crc) < 0 && errno == EIO);
        unsetenv("TC_TEST_EIO_OPEN");
        /* No checksum to check against: the copy stands on its size alone. */
        contents("source", "other bytes");
        assert(tc_copy_file("source", "prepared", 0755, -1, NULL) == 0);
        close(captured[1]);
        close(STDERR_FILENO);
        assert((n = read(captured[0], log, sizeof(log) - 1)) > 0);
        log[n] = 0;
        assert((line = strstr(log, "copy verify failed: source -> prepared: ")) == log);
        assert(sscanf(line, "copy verify failed: source -> prepared: CRC-32 read %x, deployed %x (copied 12 of 12 bytes",
                      &read_crc, &deployed) == 2);
        assert(read_crc == 0x84d769f0 && deployed == 0x12345678);
        line = strchr(log, '\n') + 1;
        assert(strstr(line, "copy read failed: source -> prepared: Input/output error") == line);
        line = strchr(line, '\n') + 1;
        assert(strstr(line, "copy read failed: source -> prepared: Input/output error") == line);
        line = strchr(line, '\n') + 1;
        assert(strstr(line, "copy open source failed: source -> prepared: Input/output error") == line);
        assert(!strchr(strchr(line, '\n') + 1, '\n')); /* Only those four logged. */
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
        tc_worker_begin("next-test"); /* Overflow cancelled the preceding job. */
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
        assert(tc_copy_file("missing", "never", 0700, -1, NULL) < 0);
        assert(access("never", F_OK) < 0);
        {
            /* A checksum read stops between chunks too: a stalled disk
             * returns one at a time. */
            uint32_t crc;
            contents("source", "payload data");
            assert(tc_file_crc32("source", &crc) < 0 && errno == ECANCELED);
        }
    } else
        return 2;
    return 0;
}
