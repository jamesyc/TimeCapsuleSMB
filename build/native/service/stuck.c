#include "stuck.h"

int tc_stuck_uninterruptible(int stat, int flag, const char *wmesg, size_t wmesg_length) {
    if ((flag & TC_P_SYSTEM) || stat != TC_LSSLEEP)
        return 0;
#ifdef TC_STUCK_TEST_WMESG
    /* Device validation builds only: an interruptible wait we can create
     * (sleep(1)'s nanosleep) counts as uninterruptible. */
    {
        size_t n = strlen(TC_STUCK_TEST_WMESG);
        if (wmesg_length >= n && !memcmp(wmesg, TC_STUCK_TEST_WMESG, n))
            return 1;
    }
#else
    (void)wmesg;
    (void)wmesg_length;
#endif
    return !(flag & TC_L_SINTR);
}

void tc_stuck_word(char *out, size_t size, const char *text) {
    size_t i;
    if (!size)
        return;
    for (i = 0; i + 1 < size && text[i]; i++) {
        unsigned char c = (unsigned char)text[i];
        out[i] = (isalnum(c) || c == '.' || c == '-' || c == '_') ? (char)c : '_';
    }
    out[i] = 0;
}

static void emit(tc_stuck_emit_fn fn, void *context, size_t *written, enum tc_stuck_change change,
                 const struct tc_stuck_entry *entry, long long now) {
    struct tc_stuck_event event;
    event.change = change;
    event.entry = *entry;
    event.duration_ms = now - entry->since;
    fn(&event, context);
    (*written)++;
}

size_t tc_stuck_step(struct tc_stuck *s, const struct tc_stuck_sample *sample, long long now, tc_stuck_emit_fn fn,
                     void *context) {
    /* Static: the manager is one process, and this keeps 14 KB off its stack. */
    static struct tc_stuck_entry next[TC_STUCK_MAX];
    unsigned char matched[TC_STUCK_MAX];
    size_t i, j, count = 0, written = 0;
    memset(matched, 0, sizeof(matched));
    for (i = 0; i < sample->count && count < TC_STUCK_MAX; i++) {
        const struct tc_stuck_thread *t = &sample->threads[i];
        struct tc_stuck_entry *e = &next[count++];
        const struct tc_stuck_entry *old = NULL;
        /* The kernel's own count covers a sleep that began before this
         * manager saw it; a timed wait resets it, and the first sighting
         * then holds. */
        long long kernel_since = now - (long long)t->slept * 1000;
        for (j = 0; j < s->count; j++)
            if (!matched[j] && s->entries[j].thread.pid == t->pid && s->entries[j].thread.lid == t->lid) {
                matched[j] = 1;
                old = &s->entries[j];
                break;
            }
        /* CPU time going backwards is a new process under a reused PID. */
        if (old && t->cpu_us >= old->window_cpu && t->cpu_us - old->window_cpu <= TC_STUCK_CPU_US) {
            *e = *old;
            if (kernel_since < e->since)
                e->since = kernel_since;
            if (now - e->window_at >= TC_STUCK_MS) {
                e->window_at = now;
                e->window_cpu = t->cpu_us;
            }
        } else {
            /* It ran in between: the earlier episode is over. */
            if (old && old->stuck)
                emit(fn, context, &written, TC_STUCK_CLEARED, old, now);
            memset(e, 0, sizeof(*e));
            e->since = kernel_since;
            e->window_at = now;
            e->window_cpu = t->cpu_us;
        }
        e->thread = *t;
        if (!e->stuck && now - e->since >= TC_STUCK_MS) {
            e->stuck = 1;
            emit(fn, context, &written, TC_STUCK_STARTED, e, now);
        }
        if (e->stuck && !e->reported && now - e->since >= TC_STUCK_REPORT_MS) {
            e->reported = 1;
            emit(fn, context, &written, TC_STUCK_REPORT, e, now);
        }
    }
    /* Awake, or gone, since the last sample. */
    for (j = 0; j < s->count; j++)
        if (!matched[j] && s->entries[j].stuck)
            emit(fn, context, &written, TC_STUCK_CLEARED, &s->entries[j], now);
    memcpy(s->entries, next, count * sizeof(next[0]));
    s->count = count;
    return written;
}

static void add(struct tc_stuck_sample *out, const struct tc_proc *p, int lid, unsigned slept,
                const char *wmesg) {
    struct tc_stuck_thread *t;
    if (out->count >= TC_STUCK_MAX) {
        out->truncated = 1;
        return;
    }
    t = &out->threads[out->count++];
    memset(t, 0, sizeof(*t));
    t->pid = p->pid;
    t->group = p->group;
    t->lid = lid;
    t->slept = slept;
    t->cpu_us = p->cpu_us;
    snprintf(t->wmesg, sizeof(t->wmesg), "%s", wmesg);
    snprintf(t->comm, sizeof(t->comm), "%s", p->comm);
}

int tc_stuck_read(struct tc_stuck_sample *out, const struct tc_proctable *table) {
    static struct tc_proc_thread threads[TC_THREAD_MAX];
    size_t i;
    int j, count;
    out->count = 0;
    out->truncated = 0;
    for (i = 0; i < table->count; i++) {
        const struct tc_proc *p = &table->procs[i];
        if (p->flag & TC_P_SYSTEM)
            continue;
        if (p->threads <= 1) {
            if (tc_stuck_uninterruptible(p->stat, p->flag, p->wmesg, strlen(p->wmesg)))
                add(out, p, 0, p->slept, p->wmesg);
            continue;
        }
        /* The process entry shows one representative thread. NetBSD 4's
         * thread entry has no CPU time, so threads use the process's. A
         * process that exited since the table was read has no threads. */
        count = tc_proctable_threads(p->pid, threads, TC_THREAD_MAX);
        for (j = 0; j < count; j++)
            if (tc_stuck_uninterruptible(threads[j].stat, threads[j].flag, threads[j].wmesg,
                                         strlen(threads[j].wmesg)))
                add(out, p, threads[j].lid, threads[j].slept, threads[j].wmesg);
    }
    return 0;
}
