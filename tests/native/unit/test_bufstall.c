/* Buffer-stall decisions (service/bufstall.c), the in-process wake, and the
 * host fixture that stands in for vm.bufmem* and KERN_PROC2 wait messages. */
#include "service/bufstall.h"
#include <assert.h>

#define HI 40243200ULL /* the NetBSD 4 LE device's vm.bufmem_hiwater */
#define T TC_BUFSTALL_TRIGGER_MS
#define QUIET TC_BUFSTALL_QUIET_MS
#define HOLD TC_BUFSTALL_HOLD_MS

static struct tc_bufstall_sample sample(uint64_t bufmem, size_t count, ...) {
    struct tc_bufstall_sample out;
    va_list pids;
    size_t i;
    memset(&out, 0, sizeof(out));
    out.bufmem = bufmem;
    out.lowater = HI >> 3;
    out.hiwater = HI;
    out.count = count;
    va_start(pids, count);
    for (i = 0; i < count; i++)
        out.pids[i] = (pid_t)va_arg(pids, int);
    va_end(pids);
    return out;
}
static uint64_t lowater;
static enum tc_bufstall_action step(struct tc_bufstall *s, struct tc_bufstall_sample in, long long now) {
    return tc_bufstall_step(s, &in, now, &lowater);
}
static void fresh(struct tc_bufstall *s) { memset(s, 0, sizeof(*s)); }

static void wait_messages(void) {
    /* Kernel fields are 8 bytes and not NUL-terminated when full. */
    char getnewbuf[8] = {'g', 'e', 't', 'n', 'e', 'w', 'b', 'u'};
    char buf_malloc[8] = {'b', 'u', 'f', '_', 'm', 'a', 'l', 'l'};
    char needbuf[8] = "needbuf";
    char nanosleep[8] = {'n', 'a', 'n', 'o', 's', 'l', 'e', 'e'};
    char needbufx[8] = {'n', 'e', 'e', 'd', 'b', 'u', 'f', 'x'};
    assert(tc_bufstall_wmesg(getnewbuf, 8));
    assert(tc_bufstall_wmesg(buf_malloc, 8));
    assert(tc_bufstall_wmesg(needbuf, 8));
    assert(!tc_bufstall_wmesg(nanosleep, 8));
    assert(!tc_bufstall_wmesg(needbufx, 8));
    assert(!tc_bufstall_wmesg("select", 6));
    assert(!tc_bufstall_wmesg("", 0));
    assert(!tc_bufstall_wmesg("needbu", 6));
    /* Untruncated names, as the host fixture writes them. */
    assert(tc_bufstall_wmesg("getnewbuf", 9));
    assert(tc_bufstall_wmesg("buf_malloc", 10));
    assert(tc_bufstall_wmesg("needbuf", 7));
    /* Apple's value is buf_setwm()'s hiwater >> 3, as read on both devices. */
    assert(tc_bufstall_default_lowater(40243200) == 5030400);
    assert(tc_bufstall_default_lowater(40263680) == 5032960);
    assert(!strcmp(tc_bufstall_outcome_name(TC_BUFSTALL_OUTCOME_RESOLVED), "resolved"));
    assert(!strcmp(tc_bufstall_outcome_name(TC_BUFSTALL_OUTCOME_STUCK), "stuck"));
    assert(!strcmp(tc_bufstall_outcome_name(TC_BUFSTALL_OUTCOME_CAPPED), "capped"));
    assert(!strcmp(tc_bufstall_outcome_name(TC_BUFSTALL_OUTCOME_FAILED), "failed"));
}

static void raise_wake_resolve(void) {
    struct tc_bufstall s;
    fresh(&s);
    /* A wait shorter than the trigger does nothing. */
    assert(step(&s, sample(3000000, 1, 10), 0) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(3000000, 1, 10), T - 1) == TC_BUFSTALL_NONE);
    /* Still waiting: raise to the highest value the kernel accepts. */
    assert(step(&s, sample(3000000, 1, 10), T) == TC_BUFSTALL_RAISE);
    assert(lowater == HI - TC_BUFSTALL_GAP && s.raised);
    /* Every sample while a process stays stalled wakes again. */
    assert(step(&s, sample(3000000, 1, 10), T + 1) == TC_BUFSTALL_WAKE);
    assert(step(&s, sample(3000000, 1, 10), T + 2) == TC_BUFSTALL_WAKE);
    /* The stall clears. Brief waits under load do not keep the raise. */
    assert(step(&s, sample(9000000, 0), T + 3) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(9000000, 1, 11), T + 4) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(9000000, 1, 12), T + 2 + QUIET - 1) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(9000000, 1, 13), T + 2 + QUIET) == TC_BUFSTALL_RESOLVED);
    assert(lowater == HI >> 3 && !s.raised);
    assert(s.ended_outcome == TC_BUFSTALL_OUTCOME_RESOLVED && s.ended_longest == T + 2);
    assert(s.outcome == TC_BUFSTALL_NO_EPISODE && s.longest == 0);
    /* Nothing more to do; a later stall raises again. */
    assert(step(&s, sample(9000000, 0), T + 3 + QUIET) == TC_BUFSTALL_NONE);
    long long next = T + 4 + QUIET;
    assert(step(&s, sample(3000000, 1, 30), next) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(3000000, 1, 30), next + T) == TC_BUFSTALL_RAISE);
}

static void brief_waits_do_not_add_up(void) {
    struct tc_bufstall s;
    long long now;
    pid_t pid = 100;
    fresh(&s);
    /* Different processes, each waiting briefly, are normal I/O. */
    for (now = 0; now < 4 * T; now += T / 2)
        assert(step(&s, sample(3000000, 1, pid++), now) == TC_BUFSTALL_NONE);
    /* A process that stops waiting and waits again starts over. */
    fresh(&s);
    assert(step(&s, sample(3000000, 1, 10), 0) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(3000000, 0), T - 1) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(3000000, 1, 10), T) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(3000000, 1, 10), 2 * T) == TC_BUFSTALL_RAISE);
}

static void stuck_counts_waiters_from_before_and_after_the_raise(void) {
    struct tc_bufstall s;
    /* A process that was waiting before the raise: the hold counts from it. */
    fresh(&s);
    assert(step(&s, sample(3000000, 1, 10), 0) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(3000000, 1, 10), T) == TC_BUFSTALL_RAISE);
    assert(step(&s, sample(3000000, 1, 10), T + HOLD - 1) == TC_BUFSTALL_WAKE);
    assert(step(&s, sample(3000000, 1, 10), T + HOLD) == TC_BUFSTALL_STUCK);
    assert(lowater == HI >> 3 && !s.raised && s.outcome == TC_BUFSTALL_OUTCOME_STUCK);
    /* A process that starts waiting after the raise and never stops is just
     * as stuck: the raise freed pid 10 and pid 20 is a new stall. */
    fresh(&s);
    assert(step(&s, sample(3000000, 1, 10), 0) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(3000000, 1, 10), T) == TC_BUFSTALL_RAISE);
    assert(step(&s, sample(3000000, 1, 20), T + 1) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(3000000, 1, 20), T + 1 + T) == TC_BUFSTALL_WAKE);
    assert(step(&s, sample(3000000, 1, 20), T + 1 + HOLD - 1) == TC_BUFSTALL_WAKE);
    assert(step(&s, sample(3000000, 1, 20), T + 1 + HOLD) == TC_BUFSTALL_STUCK);
}

static void stuck_retries_then_resolves(void) {
    struct tc_bufstall s;
    fresh(&s);
    assert(step(&s, sample(3000000, 1, 10), 0) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(3000000, 1, 10), T) == TC_BUFSTALL_RAISE);
    long long stuck = T + HOLD;
    assert(step(&s, sample(3000000, 1, 10), stuck) == TC_BUFSTALL_STUCK);
    /* Not raised and waiting: nothing until the retry; Apple's value stays. */
    assert(step(&s, sample(3000000, 1, 10), stuck + 1) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(3000000, 1, 10), stuck + HOLD - 1) == TC_BUFSTALL_NONE);
    /* Raising again is cheap. */
    assert(step(&s, sample(3000000, 1, 10), stuck + HOLD) == TC_BUFSTALL_RAISE);
    assert(s.raised && lowater == HI - TC_BUFSTALL_GAP);
    /* This time it helps. The episode keeps its worst outcome. */
    long long cleared = stuck + HOLD + 1;
    assert(step(&s, sample(3000000, 0), cleared) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(3000000, 0), stuck + HOLD + QUIET) == TC_BUFSTALL_RESOLVED);
    assert(s.ended_outcome == TC_BUFSTALL_OUTCOME_STUCK && s.ended_longest == stuck + HOLD);
}

static void capped_rechecks_and_raises_when_the_cache_shrinks(void) {
    struct tc_bufstall s;
    fresh(&s);
    /* At the high-water mark no low-water mark the kernel accepts helps. */
    assert(step(&s, sample(HI - TC_BUFSTALL_GAP, 1, 10), 0) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(HI - TC_BUFSTALL_GAP, 1, 10), T) == TC_BUFSTALL_CAPPED);
    assert(!s.raised && s.outcome == TC_BUFSTALL_OUTCOME_CAPPED);
    /* Logged once per episode, checked again every hold. */
    assert(step(&s, sample(HI, 1, 10), T + 1) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(HI, 1, 10), T + HOLD) == TC_BUFSTALL_NONE);
    /* The pagedaemon shrank the cache: the next check raises. */
    assert(step(&s, sample(3000000, 1, 10), T + HOLD + 1) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(3000000, 1, 10), T + 2 * HOLD) == TC_BUFSTALL_RAISE);
    assert(step(&s, sample(3000000, 0), T + 2 * HOLD + QUIET) == TC_BUFSTALL_RESOLVED);
    assert(s.ended_outcome == TC_BUFSTALL_OUTCOME_CAPPED);
    /* One byte below the edge still raises above bufmem. */
    fresh(&s);
    assert(step(&s, sample(HI - TC_BUFSTALL_GAP - 1, 1, 10), 0) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(HI - TC_BUFSTALL_GAP - 1, 1, 10), T) == TC_BUFSTALL_RAISE);
    assert(lowater == HI - TC_BUFSTALL_GAP);
    /* A tiny or unreadable high-water mark never underflows. */
    struct tc_bufstall_sample tiny = sample(0, 1, 10);
    tiny.hiwater = TC_BUFSTALL_GAP - 1;
    tiny.lowater = tiny.hiwater >> 3;
    fresh(&s);
    assert(tc_bufstall_step(&s, &tiny, 0, &lowater) == TC_BUFSTALL_NONE);
    assert(tc_bufstall_step(&s, &tiny, T, &lowater) == TC_BUFSTALL_CAPPED);
}

static void capped_episode_ends_without_a_write(void) {
    struct tc_bufstall s;
    fresh(&s);
    assert(step(&s, sample(HI, 1, 10), 0) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(HI, 1, 10), T) == TC_BUFSTALL_CAPPED);
    assert(step(&s, sample(HI, 0), T + 1) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(HI, 0), T + QUIET - 1) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(HI, 0), T + QUIET) == TC_BUFSTALL_ENDED);
    assert(s.ended_outcome == TC_BUFSTALL_OUTCOME_CAPPED && s.ended_longest == T);
    /* The next episode is logged again. */
    long long next = T + QUIET + 1;
    assert(step(&s, sample(HI, 1, 11), next) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(HI, 1, 11), next + T + HOLD) == TC_BUFSTALL_CAPPED);
}

static void refused_raise_retries(void) {
    struct tc_bufstall s;
    fresh(&s);
    assert(step(&s, sample(3000000, 1, 10), 0) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(3000000, 1, 10), T) == TC_BUFSTALL_RAISE);
    tc_bufstall_failed(&s, T);
    assert(!s.raised && s.outcome == TC_BUFSTALL_OUTCOME_FAILED);
    assert(step(&s, sample(3000000, 1, 10), T + 1) == TC_BUFSTALL_NONE);
    assert(step(&s, sample(3000000, 1, 10), T + HOLD) == TC_BUFSTALL_RAISE);
    assert(step(&s, sample(3000000, 0), T + HOLD + QUIET) == TC_BUFSTALL_RESOLVED);
    assert(s.ended_outcome == TC_BUFSTALL_OUTCOME_FAILED);
}

static void restores_a_raised_mark_it_did_not_set(void) {
    struct tc_bufstall s;
    struct tc_bufstall_sample in = sample(3000000, 0);
    fresh(&s);
    /* An earlier manager's raise, or a restore the kernel refused. */
    in.lowater = HI - TC_BUFSTALL_GAP;
    assert(tc_bufstall_step(&s, &in, 0, &lowater) == TC_BUFSTALL_RESTORE && lowater == HI >> 3);
    assert(tc_bufstall_step(&s, &in, 1, &lowater) == TC_BUFSTALL_RESTORE);
    in.lowater = HI >> 3;
    assert(tc_bufstall_step(&s, &in, 2, &lowater) == TC_BUFSTALL_NONE);
    /* A lower mark than Apple's is not ours to change. */
    in.lowater = (HI >> 3) - 1;
    assert(tc_bufstall_step(&s, &in, 3, &lowater) == TC_BUFSTALL_NONE);
    /* While stalled, the raise wins. */
    in = sample(3000000, 1, 10);
    in.lowater = HI - TC_BUFSTALL_GAP;
    fresh(&s);
    assert(tc_bufstall_step(&s, &in, 0, &lowater) == TC_BUFSTALL_RESTORE);
    assert(tc_bufstall_step(&s, &in, T, &lowater) == TC_BUFSTALL_RAISE);
}

static void full_table(void) {
    struct tc_bufstall s;
    struct tc_bufstall_sample in = sample(3000000, 0);
    size_t i;
    fresh(&s);
    for (i = 0; i < TC_PROCESS_MAX; i++)
        in.pids[i] = (pid_t)(1000 + i);
    in.count = TC_PROCESS_MAX;
    assert(tc_bufstall_step(&s, &in, 0, &lowater) == TC_BUFSTALL_NONE && s.seen_count == TC_PROCESS_MAX);
    assert(tc_bufstall_step(&s, &in, T, &lowater) == TC_BUFSTALL_RAISE);
}

static void write_text(const char *path, const char *text) {
    FILE *f = fopen(path, "w");
    assert(f && fputs(text, f) >= 0 && !fclose(f));
}
/* The manager's process table for the pass; wait messages as the kernel
 * stores them, 8 bytes at most. */
static struct tc_proctable table;
static void wait_table(void) {
    static const struct {
        pid_t pid;
        const char *wmesg;
    } rows[] = {{10, "getnewbu"}, {11, "select"}, {12, "needbuf"}, {13, ""}};
    size_t i;
    memset(&table, 0, sizeof(table));
    for (i = 0; i < sizeof(rows) / sizeof(rows[0]); i++) {
        table.procs[i].pid = rows[i].pid;
        snprintf(table.procs[i].wmesg, sizeof(table.procs[i].wmesg), "%s", rows[i].wmesg);
    }
    table.count = i;
}
static void fixture(const char *dir) {
    char path[512], writes[600], wakes[600], line[64];
    struct tc_bufstall_sample in;
    FILE *f;
    wait_table();
    unsetenv("TC_TEST_BUFCACHE");
    errno = 0;
    assert(tc_bufstall_read(&in, &table) == -1 && errno == ENOSYS);
    snprintf(path, sizeof(path), "%s/bufcache", dir);
    snprintf(writes, sizeof(writes), "%s.writes", path);
    snprintf(wakes, sizeof(wakes), "%s.wakes", path);
    setenv("TC_TEST_BUFCACHE", path, 1);
    write_text(path, "bufmem 3000000\nlowater 5030400\nhiwater 40243200\n");
    assert(!tc_bufstall_read(&in, &table));
    assert(in.bufmem == 3000000 && in.lowater == 5030400 && in.hiwater == HI);
    /* Only the buffer waits, in table order. */
    assert(in.count == 2 && in.pids[0] == 10 && in.pids[1] == 12);
    /* The vm values alone. */
    memset(&in, 0, sizeof(in));
    assert(!tc_bufstall_read_vm(&in) && in.hiwater == HI && in.count == 0);
    /* The kernel refuses a mark within 16 bytes of the high-water mark. */
    errno = 0;
    assert(tc_bufstall_set_lowater(HI - TC_BUFSTALL_GAP + 1) == -1 && errno == EINVAL);
    assert(!tc_bufstall_set_lowater(HI - TC_BUFSTALL_GAP));
    assert(!tc_bufstall_read(&in, &table) && in.lowater == HI - TC_BUFSTALL_GAP && in.count == 2);
    assert(!tc_bufstall_set_lowater(HI >> 3));
    f = fopen(writes, "r");
    assert(f);
    assert(fgets(line, sizeof(line), f) && !strcmp(line, "40243184\n"));
    assert(fgets(line, sizeof(line), f) && !strcmp(line, "5030400\n"));
    assert(!fgets(line, sizeof(line), f));
    fclose(f);
    write_text(path, "bufmem 3000000\nlowater 5030400\nhiwater 40243200\nreadonly\n");
    errno = 0;
    assert(tc_bufstall_set_lowater(HI >> 2) == -1 && errno == EPERM);
    /* The wake reads the directory in this process, every pass. */
    assert(!tc_bufstall_wake());
    f = fopen(wakes, "r");
    assert(f && fgets(line, sizeof(line), f) && atoi(line) == TC_BUFWAKE_PASSES);
    fclose(f);
}

int main(int argc, char **argv) {
    assert(argc == 2);
    wait_messages();
    raise_wake_resolve();
    brief_waits_do_not_add_up();
    stuck_counts_waiters_from_before_and_after_the_raise();
    stuck_retries_then_resolves();
    capped_rechecks_and_raises_when_the_cache_shrinks();
    capped_episode_ends_without_a_write();
    refused_raise_retries();
    restores_a_raised_mark_it_did_not_set();
    full_table();
    fixture(argv[1]);
    return 0;
}
