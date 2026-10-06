/* The process table's host fixture (service/proctable.c): what the manager's
 * tests give it in place of KERN_PROC2, KERN_LWP and KERN_PROC_ARGS. */
#include "service/proctable.h"
#include <assert.h>
#include <sys/stat.h>
#include <sys/wait.h>

static char dir[512];

static void write_file(const char *name, const char *text) {
    char path[600];
    FILE *f;
    snprintf(path, sizeof(path), "%s/%s", dir, name);
    f = fopen(path, "w");
    assert(f && fputs(text, f) >= 0 && !fclose(f));
}
static int present(const struct tc_proctable *t, pid_t pid) {
    size_t i;
    for (i = 0; i < t->count; i++)
        if (t->procs[i].pid == pid)
            return 1;
    return 0;
}
static const struct tc_proc *find(const struct tc_proctable *t, pid_t pid) {
    size_t i;
    for (i = 0; i < t->count; i++)
        if (t->procs[i].pid == pid)
            return &t->procs[i];
    abort();
}
/* A PID that existed and is gone: a reaped child's. */
static pid_t gone_pid(void) {
    pid_t pid = fork();
    assert(pid >= 0);
    if (!pid)
        _exit(0);
    assert(waitpid(pid, NULL, 0) == pid);
    return pid;
}

int main(int argc, char **argv) {
    static struct tc_proctable t;
    struct tc_proc_thread threads[4];
    char line[64], text[1024], command[24];
    pid_t me = getpid(), gone = gone_pid();
    assert(argc == 2);

    unsetenv("TC_TEST_PROCS");
    errno = 0;
    assert(tc_proctable_read(&t) == -1 && errno == ENOSYS);
    snprintf(dir, sizeof(dir), "%s/procs", argv[1]);
    setenv("TC_TEST_PROCS", dir, 1);
    /* No fixture yet: an empty table, like a test that sets none. */
    assert(tc_proctable_read(&t) == 0 && t.count == 0);
    assert(!mkdir(dir, 0700));

    /* Each test source writes its own file. */
    write_file("base", "2 1 2 3 0x80 4 0 select diskd /sbin/diskd -i lo0 -d local.\n"
                       "31 2 31 5 0x0 0 0 - smbd (smbd)\n"
                       "40 1 40 3 0x0 75 1200 biowait a_command_name_longer_than_16 x\n"
                       "garbage line\n");
    snprintf(text, sizeof(text),
             "live %ld 1 %ld 3 0x80 0 0 wait sh sh -c sleep\n"
             "live %ld 1 %ld 3 0x80 0 0 wait sh sh -c gone\n"
             "119 1 2 3 0x80 0 900 kqueue ACPd /sbin/ACPd\n"
             "lwp 119 1 3 0x80 0 kqueue\nlwp 119 5 3 0x0 40 tstile\nlwp 119 6 3 0x0 2 biowait\n",
             (long)me, (long)me, (long)gone, (long)gone);
    write_file("external", text);
    assert(tc_proctable_read(&t) == 0);
    assert(t.count == 5);
    assert(find(&t, 2)->stat == 3 && find(&t, 2)->flag == 0x80 && find(&t, 2)->slept == 4);
    assert(!strcmp(find(&t, 2)->wmesg, "select") && !strcmp(find(&t, 2)->comm, "diskd"));
    assert(find(&t, 2)->threads == 1 && find(&t, 2)->parent == 1 && find(&t, 2)->group == 2);
    /* "-" is no wait message; zombies stay in the table, marked exited. */
    assert(!strcmp(find(&t, 31)->wmesg, "") && tc_proc_exited(find(&t, 31)));
    assert(!tc_proc_exited(find(&t, 2)));
    /* Names are cut like the kernel's 16-byte p_comm. */
    assert(!strcmp(find(&t, 40)->comm, "a_command_name_l") && find(&t, 40)->cpu_us == 1200);
    /* A live row stays while its PID exists and is dropped once it is gone. */
    assert(present(&t, me) && !present(&t, gone));
    assert(find(&t, 119)->threads == 3);

    assert(tc_proctable_threads(119, threads, 4) == 3);
    assert(threads[1].lid == 5 && threads[1].stat == 3 && threads[1].flag == 0 && threads[1].slept == 40);
    assert(!strcmp(threads[1].wmesg, "tstile"));
    assert(tc_proctable_threads(119, threads, 2) == 2);
    assert(tc_proctable_threads(2, threads, 4) == 0);
    /* A process that exited has no threads to read. */
    assert(tc_proctable_threads(77, threads, 4) == -1);

    /* Arguments as ps prints them, cut to fit. */
    assert(tc_proctable_argv(2, line, sizeof(line)) == 0 && !strcmp(line, "/sbin/diskd -i lo0 -d local."));
    assert(tc_proctable_argv(2, command, sizeof(command)) == 0 && !strcmp(command, "/sbin/diskd -i lo0 -d l"));
    /* Gone, or a zombie: 1, so the audit leaves it out. */
    assert(tc_proctable_argv(77, line, sizeof(line)) == 1);
    assert(tc_proctable_argv(31, line, sizeof(line)) == 1);
    assert(tc_proctable_argv(gone, line, sizeof(line)) == 1);
    return 0;
}
