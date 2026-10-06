#include "proctable.h"
#if defined(__NetBSD__) && !defined(TC_NATIVE_TEST)
#include <sys/sysctl.h>
#endif

#if defined(LSSLEEP) && LSSLEEP != TC_LSSLEEP
#error "LSSLEEP changed"
#endif
#if defined(LSZOMB) && LSZOMB != TC_LSZOMB
#error "LSZOMB changed"
#endif
#if defined(L_SINTR) && L_SINTR != TC_L_SINTR
#error "L_SINTR changed"
#endif
#if defined(P_SYSTEM) && P_SYSTEM != TC_P_SYSTEM
#error "P_SYSTEM changed"
#endif

int tc_proc_exited(const struct tc_proc *p) {
    return p->stat == TC_LSZOMB || p->stat == TC_LSDEAD;
}

#if defined(__NetBSD__) || defined(TC_NATIVE_TEST)
/* Copies a kernel name field, which is not NUL-terminated when full. */
static void copy_field(char *out, size_t size, const char *field, size_t field_size) {
    size_t n = 0;
    while (n < field_size && n + 1 < size && field[n]) {
        out[n] = field[n];
        n++;
    }
    out[n] = 0;
}

/* Arguments arrive NUL-separated; ps prints them joined by spaces. */
static void join_arguments(char *text, size_t length, size_t size) {
    size_t i;
    if (length >= size)
        length = size - 1;
    while (length && !text[length - 1])
        length--;
    for (i = 0; i < length; i++)
        if (!text[i])
            text[i] = ' ';
    text[length] = 0;
}
#endif

#if defined(__NetBSD__) && !defined(TC_NATIVE_TEST)
static void fill(struct tc_proc *out, const struct kinfo_proc2 *p) {
    out->pid = (pid_t)p->p_pid;
    out->parent = (pid_t)p->p_ppid;
    out->group = (pid_t)p->p__pgid;
    out->stat = p->p_stat;
    out->flag = p->p_flag;
    out->threads = (int)p->p_nlwps;
    out->slept = p->p_slptime;
    out->cpu_us = (unsigned long long)p->p_rtime_sec * 1000000ULL + p->p_rtime_usec;
    copy_field(out->wmesg, sizeof(out->wmesg), p->p_wmesg, sizeof(p->p_wmesg));
    copy_field(out->comm, sizeof(out->comm), p->p_comm, sizeof(p->p_comm));
}

int tc_proctable_read(struct tc_proctable *table) {
    /* Static, so a read needs no memory when the device is short of it, and
     * not cleared: the kernel writes every element it returns. */
    static struct kinfo_proc2 procs[TC_PROCESS_MAX];
    int mib[6] = {CTL_KERN, KERN_PROC2, KERN_PROC_ALL, 0, sizeof(procs[0]), TC_PROCESS_MAX};
    size_t length = sizeof(procs), i;
    table->count = 0;
    if (sysctl(mib, 6, procs, &length, NULL, 0) < 0)
        return -1;
    for (i = 0; i < length / sizeof(procs[0]); i++)
        fill(&table->procs[table->count++], &procs[i]);
    return 0;
}

int tc_proctable_threads(pid_t pid, struct tc_proc_thread *out, size_t max) {
    static struct kinfo_lwp lwps[TC_THREAD_MAX];
    int mib[5] = {CTL_KERN, KERN_LWP, (int)pid, sizeof(lwps[0]), TC_THREAD_MAX};
    size_t length = sizeof(lwps), i, count;
    /* ENOMEM still fills the buffer, with the first TC_THREAD_MAX threads. */
    if (sysctl(mib, 5, lwps, &length, NULL, 0) < 0 && errno != ENOMEM)
        return -1;
    count = length / sizeof(lwps[0]);
    if (count > max)
        count = max;
    for (i = 0; i < count; i++) {
        out[i].lid = lwps[i].l_lid;
        out[i].stat = lwps[i].l_stat;
        out[i].flag = lwps[i].l_flag;
        out[i].slept = lwps[i].l_slptime;
        copy_field(out[i].wmesg, sizeof(out[i].wmesg), lwps[i].l_wmesg, sizeof(lwps[i].l_wmesg));
    }
    return (int)count;
}

/* 1 when PID is gone or a zombie: KERN_PROC_ARGS reports both, and a pid it
 * cannot find, as EINVAL, and a dying process as EFAULT. */
static int gone(pid_t pid) {
    struct kinfo_proc2 p;
    struct tc_proc entry;
    int mib[6] = {CTL_KERN, KERN_PROC2, KERN_PROC_PID, (int)pid, sizeof(p), 1};
    size_t length = sizeof(p);
    if (sysctl(mib, 6, &p, &length, NULL, 0) < 0)
        return 0;
    if (length < sizeof(p))
        return 1;
    fill(&entry, &p);
    return tc_proc_exited(&entry);
}

int tc_proctable_argv(pid_t pid, char *out, size_t size) {
    int mib[4] = {CTL_KERN, KERN_PROC_ARGS, (int)pid, KERN_PROC_ARGV};
    size_t length = size - 1;
    if (sysctl(mib, 4, out, &length, NULL, 0) < 0)
        return gone(pid) ? 1 : -1;
    join_arguments(out, length, size);
    return 0;
}
#elif defined(TC_NATIVE_TEST)
#include <dirent.h>
/* Every regular file in the TC_TEST_PROCS directory, so each test source
 * (external processes, waits, a fake daemon) writes its own. */
typedef int (*fixture_line_fn)(char *line, void *context);
static int each_fixture_line(fixture_line_fn fn, void *context) {
    const char *dir = getenv("TC_TEST_PROCS");
    DIR *d;
    struct dirent *entry;
    if (!dir) {
        errno = ENOSYS;
        return -1;
    }
    if (!(d = opendir(dir)))
        return errno == ENOENT ? 0 : -1;
    while ((entry = readdir(d))) {
        char path[1024], line[2048];
        FILE *stream;
        if (entry->d_name[0] == '.' || snprintf(path, sizeof(path), "%s/%s", dir, entry->d_name) >= (int)sizeof(path))
            continue;
        if (!(stream = fopen(path, "r")))
            continue; /* replaced between readdir and open */
        while (fgets(line, sizeof(line), stream))
            if (fn(line, context)) {
                fclose(stream);
                closedir(d);
                return 1;
            }
        fclose(stream);
    }
    closedir(d);
    return 0;
}

/* One fixture process row; *argv points into line. */
static int parse_row(char *line, struct tc_proc *p, const char **argv) {
    char wmesg[64], comm[64];
    long pid, parent, group;
    int offset = 0, live = !strncmp(line, "live ", 5);
    char *row = live ? line + 5 : line;
    if (!strncmp(line, "lwp ", 4))
        return 0;
    if (sscanf(row, "%ld %ld %ld %d %i %u %llu %63s %63s %n", &pid, &parent, &group, &p->stat, &p->flag,
               &p->slept, &p->cpu_us, wmesg, comm, &offset) != 9)
        return 0;
    if (live && kill((pid_t)pid, 0) && errno == ESRCH)
        return 0;
    p->pid = (pid_t)pid;
    p->parent = (pid_t)parent;
    p->group = (pid_t)group;
    p->threads = 0;
    copy_field(p->wmesg, sizeof(p->wmesg), strcmp(wmesg, "-") ? wmesg : "", sizeof(wmesg));
    copy_field(p->comm, sizeof(p->comm), comm, sizeof(comm));
    *argv = offset ? row + offset : row + strlen(row);
    return 1;
}

static int add_row(char *line, void *context) {
    struct tc_proctable *table = context;
    const char *argv;
    if (table->count < TC_PROCESS_MAX && parse_row(line, &table->procs[table->count], &argv))
        table->count++;
    return 0;
}
static int count_thread(char *line, void *context) {
    struct tc_proctable *table = context;
    long pid;
    int lid;
    size_t i;
    if (sscanf(line, "lwp %ld %d", &pid, &lid) == 2)
        for (i = 0; i < table->count; i++)
            if (table->procs[i].pid == (pid_t)pid)
                table->procs[i].threads++;
    return 0;
}

int tc_proctable_read(struct tc_proctable *table) {
    size_t i;
    table->count = 0;
    if (each_fixture_line(add_row, table) < 0 || each_fixture_line(count_thread, table) < 0)
        return -1;
    /* A process with thread lines has that many threads. */
    for (i = 0; i < table->count; i++)
        if (!table->procs[i].threads)
            table->procs[i].threads = 1;
    return 0;
}

struct thread_query {
    pid_t pid;
    struct tc_proc_thread *out;
    size_t max, count;
    int found;
};
static int add_thread(char *line, void *context) {
    struct thread_query *q = context;
    char wmesg[64];
    long pid;
    struct tc_proc_thread t;
    struct tc_proc p;
    const char *argv;
    if (sscanf(line, "lwp %ld %d %d %i %u %63s", &pid, &t.lid, &t.stat, &t.flag, &t.slept, wmesg) == 6) {
        if ((pid_t)pid == q->pid && q->count < q->max) {
            copy_field(t.wmesg, sizeof(t.wmesg), strcmp(wmesg, "-") ? wmesg : "", sizeof(wmesg));
            q->out[q->count++] = t;
        }
    } else if (parse_row(line, &p, &argv) && p.pid == q->pid) {
        q->found = 1;
    }
    return 0;
}

int tc_proctable_threads(pid_t pid, struct tc_proc_thread *out, size_t max) {
    struct thread_query q = {pid, out, max, 0, 0};
    if (each_fixture_line(add_thread, &q) < 0 || !q.found)
        return -1;
    return (int)q.count;
}

struct argv_query {
    pid_t pid;
    char *out;
    size_t size;
    int result;
};
static int find_argv(char *line, void *context) {
    struct argv_query *q = context;
    struct tc_proc p;
    const char *argv;
    if (!parse_row(line, &p, &argv) || p.pid != q->pid)
        return 0;
    if (!tc_proc_exited(&p)) {
        snprintf(q->out, q->size, "%s", argv);
        join_arguments(q->out, strcspn(q->out, "\n"), q->size);
        q->result = 0;
    }
    return 1;
}

int tc_proctable_argv(pid_t pid, char *out, size_t size) {
    struct argv_query q = {pid, out, size, 1};
    if (each_fixture_line(find_argv, &q) < 0)
        return -1;
    return q.result;
}
#else
int tc_proctable_read(struct tc_proctable *table) {
    table->count = 0;
    errno = ENOSYS;
    return -1;
}

int tc_proctable_threads(pid_t pid, struct tc_proc_thread *out, size_t max) {
    (void)pid;
    (void)out;
    (void)max;
    errno = ENOSYS;
    return -1;
}

int tc_proctable_argv(pid_t pid, char *out, size_t size) {
    (void)pid;
    (void)out;
    (void)size;
    errno = ENOSYS;
    return -1;
}
#endif
