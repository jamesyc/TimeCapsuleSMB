/* Stuck-process decisions (service/stuck.c), read from the manager's process
 * table and the proctable host fixture for threads. */
#include "service/stuck.h"
#include <assert.h>
#include <sys/stat.h>

#define SLEEP 3      /* LSSLEEP */
#define RUN 7        /* LSONPROC */
#define SINTR 0x80   /* L_SINTR */
#define SYSTEM 0x200 /* P_SYSTEM */
#define MS TC_STUCK_MS
#define REPORT TC_STUCK_REPORT_MS

static struct tc_stuck_event events[64];
static size_t recorded;
static void record(const struct tc_stuck_event *e, void *context) {
    assert(context == &recorded);
    assert(recorded < sizeof(events) / sizeof(events[0]));
    events[recorded++] = *e;
}
static size_t step(struct tc_stuck *s, const struct tc_stuck_sample *in, long long now) {
    size_t count;
    recorded = 0;
    count = tc_stuck_step(s, in, now, record, &recorded);
    assert(count == recorded);
    return count;
}
#define SLEPT(ms) ((unsigned)((ms) / 1000))

static struct tc_stuck_thread thread(pid_t pid, int lid, unsigned slept, unsigned long long cpu_us, const char *wmesg) {
    struct tc_stuck_thread t;
    memset(&t, 0, sizeof(t));
    t.pid = pid;
    t.group = pid;
    t.lid = lid;
    t.slept = slept;
    t.cpu_us = cpu_us;
    snprintf(t.wmesg, sizeof(t.wmesg), "%s", wmesg);
    snprintf(t.comm, sizeof(t.comm), "smbd");
    return t;
}
static struct tc_stuck_sample sample(size_t count, const struct tc_stuck_thread *threads) {
    struct tc_stuck_sample out;
    memset(&out, 0, sizeof(out));
    out.count = count;
    if (count) /* glibc declares memcpy's source nonnull even for zero bytes */
        memcpy(out.threads, threads, count * sizeof(threads[0]));
    return out;
}
static size_t step1(struct tc_stuck *s, struct tc_stuck_thread t, long long now) {
    struct tc_stuck_sample in = sample(1, &t);
    return step(s, &in, now);
}
static size_t step0(struct tc_stuck *s, long long now) {
    struct tc_stuck_sample in = sample(0, NULL);
    return step(s, &in, now);
}
static void fresh(struct tc_stuck *s) { memset(s, 0, sizeof(*s)); }

static void uninterruptible(void) {
    assert(tc_stuck_uninterruptible(SLEEP, 0, "biowait", 7));
    assert(tc_stuck_uninterruptible(SLEEP, 0x4, "tstile", 6)); /* L_INMEM and other flags */
    assert(!tc_stuck_uninterruptible(SLEEP, SINTR, "select", 6));
    /* Kernel threads sleep this way all the time. */
    assert(!tc_stuck_uninterruptible(SLEEP, SYSTEM, "syncer", 6));
    assert(!tc_stuck_uninterruptible(RUN, 0, "", 0));
    assert(!tc_stuck_uninterruptible(5, 0, "", 0)); /* LSZOMB */
}

static void single_sleep_crosses_the_threshold_once(void) {
    struct tc_stuck s;
    long long t;
    fresh(&s);
    for (t = 0; t < MS; t += 1000)
        assert(step1(&s, thread(40, 0, (unsigned)(t / 1000), 500, "biowait"), 1000000 + t) == 0);
    assert(step1(&s, thread(40, 0, MS / 1000, 500, "biowait"), 1000000 + MS) == 1);
    assert(events[0].change == TC_STUCK_STARTED && events[0].entry.thread.pid == 40);
    assert(events[0].duration_ms == MS);
    assert(!strcmp(events[0].entry.thread.wmesg, "biowait"));
    /* Not logged again while it lasts. */
    assert(step1(&s, thread(40, 0, MS / 1000 + 1, 500, "biowait"), 1000000 + MS + 1000) == 0);
}

static void kernel_sleep_time_counts_before_the_first_sample(void) {
    struct tc_stuck s;
    fresh(&s);
    /* A manager started after the process went to sleep. */
    assert(step1(&s, thread(41, 0, SLEPT(MS) + 30, 0, "vnlock"), 5000000) == 1);
    assert(events[0].change == TC_STUCK_STARTED && events[0].duration_ms == MS + 30000);
}

static void wait_that_keeps_waking_counts_from_first_sighting(void) {
    struct tc_stuck s;
    long long t;
    fresh(&s);
    /* NetBSD 6 needbuf: wakes every 250 ms to retry, so the kernel's sleep
     * time is always 0, and it uses almost no CPU (here 0.01%). */
    for (t = 0; t < MS; t += 1000)
        assert(step1(&s, thread(42, 0, 0, 1000 + (unsigned long long)t / 10, "needbuf"), t) == 0);
    assert(step1(&s, thread(42, 0, 0, 1000 + MS / 10, "needbuf"), MS) == 1);
    assert(events[0].change == TC_STUCK_STARTED && events[0].duration_ms == MS);
}

static void changing_wait_channel_is_one_episode(void) {
    struct tc_stuck s;
    long long t;
    fresh(&s);
    for (t = 0; t < MS; t += 1000)
        assert(step1(&s, thread(43, 0, 0, 0, (t / 1000) % 2 ? "getnewbu" : "biowait"), t) == 0);
    assert(step1(&s, thread(43, 0, 0, 0, "biowait"), MS) == 1);
    assert(events[0].change == TC_STUCK_STARTED);
}

static void cpu_progress_is_not_stuck(void) {
    struct tc_stuck s;
    long long t;
    fresh(&s);
    /* Seen waiting on the disk in every sample, but running in between. */
    for (t = 0; t <= 3 * MS; t += 1000)
        assert(step1(&s, thread(44, 0, 0, (unsigned long long)t * 50, "biowait"), t) == 0);
}

static void long_retrying_wait_stays_one_episode(void) {
    struct tc_stuck s;
    long long t;
    size_t started = 0, reports = 0, cleared = 0;
    fresh(&s);
    /* A wait that wakes to retry, using 50 us of CPU a second: 180 ms an
     * hour, past the budget over the whole episode but never in one window. */
    for (t = 0; t <= 3600000; t += 1000) {
        size_t i, n = step1(&s, thread(51, 0, 0, (unsigned long long)t / 20, "needbuf"), t);
        for (i = 0; i < n; i++) {
            started += events[i].change == TC_STUCK_STARTED;
            reports += events[i].change == TC_STUCK_REPORT;
            cleared += events[i].change == TC_STUCK_CLEARED;
        }
    }
    assert(started == 1 && reports == 1 && cleared == 0);
    /* Running past the budget within one window still ends it. */
    assert(step1(&s, thread(51, 0, 0, 3600000 / 20 + TC_STUCK_CPU_US + 1, "needbuf"), 3601000) == 1);
    assert(events[0].change == TC_STUCK_CLEARED && events[0].duration_ms == 3601000);
}

static void cpu_progress_clears_a_stuck_episode(void) {
    struct tc_stuck s;
    fresh(&s);
    assert(step1(&s, thread(45, 0, SLEPT(MS) + 10, 1000, "biowait"), 1000000) == 1);
    assert(events[0].change == TC_STUCK_STARTED);
    assert(step1(&s, thread(45, 0, 0, 1000 + TC_STUCK_CPU_US + 1, "biowait"), 1001000) == 1);
    assert(events[0].change == TC_STUCK_CLEARED && events[0].duration_ms == MS + 11000);
    /* A new episode starts from that sighting. */
    assert(step1(&s, thread(45, 0, 0, 1000 + TC_STUCK_CPU_US + 1, "biowait"), 1002000) == 0);
}

static void waking_up_clears_and_short_sleeps_are_silent(void) {
    struct tc_stuck s;
    fresh(&s);
    assert(step1(&s, thread(46, 0, SLEPT(MS) / 2, 0, "biowait"), 1000000) == 0);
    assert(step0(&s, 1001000) == 0);
    assert(s.count == 0);
    assert(step1(&s, thread(46, 0, SLEPT(MS) + 1, 0, "biowait"), 2000000) == 1);
    assert(step0(&s, 2005000) == 1);
    assert(events[0].change == TC_STUCK_CLEARED && events[0].duration_ms == MS + 6000);
    assert(events[0].entry.thread.pid == 46 && !events[0].entry.reported);
}

static void long_episode_reports_once_and_its_clear_says_so(void) {
    struct tc_stuck s;
    fresh(&s);
    assert(step1(&s, thread(47, 0, SLEPT(MS), 0, "biowait"), 1000000) == 1);
    assert(events[0].change == TC_STUCK_STARTED);
    assert(step1(&s, thread(47, 0, SLEPT(MS), 0, "biowait"), 1000000 + REPORT - MS - 1) == 0);
    assert(step1(&s, thread(47, 0, SLEPT(MS), 0, "biowait"), 1000000 + REPORT - MS) == 1);
    assert(events[0].change == TC_STUCK_REPORT && events[0].duration_ms == REPORT);
    assert(step1(&s, thread(47, 0, 100, 0, "biowait"), 1000000 + 2 * REPORT) == 0);
    assert(step0(&s, 1000000 + 2 * REPORT + 1000) == 1);
    assert(events[0].change == TC_STUCK_CLEARED && events[0].entry.reported);
}

static void first_sighting_past_report_time_starts_and_reports(void) {
    struct tc_stuck s;
    fresh(&s);
    /* ps-visible sleep time is capped at 127 s; the kernel's is not. */
    assert(step1(&s, thread(48, 0, REPORT / 1000 + 5, 0, "tstile"), 9000000) == 2);
    assert(events[0].change == TC_STUCK_STARTED && events[1].change == TC_STUCK_REPORT);
}

static void reused_pid_is_a_new_episode(void) {
    struct tc_stuck s;
    fresh(&s);
    assert(step1(&s, thread(49, 0, SLEPT(MS) + 10, 5000000, "biowait"), 1000000) == 1);
    /* Less CPU time than before: another process got the PID. */
    assert(step1(&s, thread(49, 0, 0, 10, "biowait"), 1001000) == 1);
    assert(events[0].change == TC_STUCK_CLEARED);
    assert(s.count == 1 && !s.entries[0].stuck && s.entries[0].since == 1001000);
}

static void threads_are_followed_separately(void) {
    struct tc_stuck s;
    struct tc_stuck_thread both[2];
    struct tc_stuck_sample in;
    fresh(&s);
    both[0] = thread(50, 3, SLEPT(MS) + 10, 0, "biowait");
    both[1] = thread(50, 7, 5, 0, "tstile");
    in = sample(2, both);
    assert(step(&s, &in, 1000000) == 1);
    assert(events[0].entry.thread.lid == 3);
    in = sample(1, &both[1]);
    in.threads[0].slept = 6;
    assert(step(&s, &in, 1001000) == 1);
    assert(events[0].change == TC_STUCK_CLEARED && events[0].entry.thread.lid == 3);
    assert(s.count == 1 && s.entries[0].thread.lid == 7);
}

static void every_change_reaches_the_callback(void) {
    struct tc_stuck s;
    struct tc_stuck_thread many[40];
    struct tc_stuck_sample in;
    size_t i;
    fresh(&s);
    for (i = 0; i < 40; i++)
        many[i] = thread((pid_t)(60 + i), 0, SLEPT(MS) + 10, 0, "biowait");
    in = sample(40, many);
    assert(step(&s, &in, 1000000) == 40);
    assert(s.count == 40);
    for (i = 0; i < 40; i++)
        assert(s.entries[i].stuck && events[i].entry.thread.pid == (pid_t)(60 + i));
}

static void words(void) {
    char out[17];
    tc_stuck_word(out, sizeof(out), "smbd");
    assert(!strcmp(out, "smbd"));
    tc_stuck_word(out, sizeof(out), "a b:c,d/e");
    assert(!strcmp(out, "a_b_c_d_e"));
    tc_stuck_word(out, 5, "mDNSResponder");
    assert(!strcmp(out, "mDNS"));
}

static void proc(struct tc_proctable *t, pid_t pid, int stat, int flag, int threads, unsigned slept, const char *wmesg,
                 const char *comm) {
    struct tc_proc *p = &t->procs[t->count++];
    memset(p, 0, sizeof(*p));
    p->pid = pid;
    p->group = pid;
    p->stat = stat;
    p->flag = flag;
    p->threads = threads;
    p->slept = slept;
    p->cpu_us = 1200;
    snprintf(p->wmesg, sizeof(p->wmesg), "%s", wmesg);
    snprintf(p->comm, sizeof(p->comm), "%s", comm);
}

static void read_from_table(const char *dir) {
    static struct tc_proctable t;
    struct tc_stuck_sample out;
    char path[512];
    FILE *f;
    memset(&t, 0, sizeof(t));
    proc(&t, 4242, SLEEP, 0x4, 1, 75, "biowait", "smbd");
    proc(&t, 4243, SLEEP, 0x84, 1, 75, "select", "smbd");  /* interruptible */
    proc(&t, 0, SLEEP, 0x204, 1, 127, "syncer", "system");  /* kernel thread */
    proc(&t, 4244, RUN, 0, 1, 0, "", "dd");
    proc(&t, 119, SLEEP, 0x80, 3, 0, "kqueue", "ACPd");     /* threads below */
    proc(&t, 120, SLEEP, 0x80, 2, 0, "kqueue", "gone");     /* exited: no threads */
    /* Thread state comes from KERN_LWP, here the proctable fixture. */
    snprintf(path, sizeof(path), "%s/procs", dir);
    mkdir(path, 0700);
    setenv("TC_TEST_PROCS", path, 1);
    snprintf(path, sizeof(path), "%s/procs/acpd", dir);
    f = fopen(path, "w");
    assert(f);
    fprintf(f, "119 1 2 3 0x80 0 900 kqueue ACPd /sbin/ACPd\n");
    fprintf(f, "lwp 119 1 3 0x80 0 kqueue\n");
    fprintf(f, "lwp 119 5 3 0x0 40 tstile\n");
    fprintf(f, "lwp 119 6 3 0x0 2 biowait\n");
    fclose(f);
    assert(tc_stuck_read(&out, &t) == 0 && !out.truncated);
    assert(out.count == 3);
    assert(out.threads[0].pid == 4242 && out.threads[0].lid == 0 && out.threads[0].slept == 75);
    assert(out.threads[0].cpu_us == 1200 && !strcmp(out.threads[0].wmesg, "biowait") &&
           !strcmp(out.threads[0].comm, "smbd"));
    assert(out.threads[1].pid == 119 && out.threads[1].lid == 5 && out.threads[1].slept == 40);
    assert(!strcmp(out.threads[1].wmesg, "tstile") && !strcmp(out.threads[1].comm, "ACPd"));
    assert(out.threads[2].pid == 119 && out.threads[2].lid == 6);
    unlink(path);
}

int main(int argc, char **argv) {
    assert(argc == 2);
    uninterruptible();
    single_sleep_crosses_the_threshold_once();
    kernel_sleep_time_counts_before_the_first_sample();
    wait_that_keeps_waking_counts_from_first_sighting();
    changing_wait_channel_is_one_episode();
    cpu_progress_is_not_stuck();
    long_retrying_wait_stays_one_episode();
    cpu_progress_clears_a_stuck_episode();
    waking_up_clears_and_short_sleeps_are_silent();
    long_episode_reports_once_and_its_clear_says_so();
    first_sighting_past_report_time_starts_and_reports();
    reused_pid_is_a_new_episode();
    threads_are_followed_separately();
    every_change_reaches_the_callback();
    words();
    read_from_table(argv[1]);
    return 0;
}
