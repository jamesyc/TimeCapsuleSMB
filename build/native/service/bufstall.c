#include "bufstall.h"
#include <dirent.h>
#if defined(__NetBSD__) && !defined(TC_NATIVE_TEST)
#include <sys/sysctl.h>
#endif

int tc_bufstall_wmesg(const char *wmesg, size_t length) {
    /* Wait messages are truncated to 8 bytes and then not NUL-terminated. */
    static const char *const waits[] = {"getnewbu", "needbuf", "buf_mall",
#ifdef TC_BUFSTALL_EXTRA_WMESG
                                        /* Device validation builds only: a wait we can create. */
                                        TC_BUFSTALL_EXTRA_WMESG,
#endif
    };
    size_t i;
    for (i = 0; i < sizeof(waits) / sizeof(waits[0]); i++) {
        size_t n = strlen(waits[i]);
        if (length >= n && !memcmp(wmesg, waits[i], n) && (n == 8 || length == n || !wmesg[n]))
            return 1;
    }
    return 0;
}

uint64_t tc_bufstall_default_lowater(uint64_t hiwater) {
    return hiwater >> 3; /* buf_setwm()'s BUFMEM_WMSHIFT on NetBSD 4 and 6 */
}

static void worsen(struct tc_bufstall *s, enum tc_bufstall_outcome outcome) {
    if (s->outcome < outcome)
        s->outcome = outcome;
}

static enum tc_bufstall_action end_episode(struct tc_bufstall *s, enum tc_bufstall_action action) {
    s->ended_outcome = s->outcome;
    s->ended_longest = s->longest;
    s->outcome = TC_BUFSTALL_NO_EPISODE;
    s->longest = 0;
    s->capped_logged = 0;
    return action;
}

enum tc_bufstall_action tc_bufstall_step(struct tc_bufstall *s, const struct tc_bufstall_sample *sample,
                                         long long now, uint64_t *lowater) {
    struct tc_bufstall_seen seen[TC_PROCESS_MAX];
    size_t i, j, count = 0;
    int stalled = 0, stuck = 0;
    long long longest = 0;
    for (i = 0; i < sample->count && count < TC_PROCESS_MAX; i++) {
        long long since = now;
        for (j = 0; j < s->seen_count; j++)
            if (s->seen[j].pid == sample->pids[i]) {
                since = s->seen[j].since;
                break;
            }
        seen[count].pid = sample->pids[i];
        seen[count++].since = since;
        if (now - since > longest)
            longest = now - since;
        if (now - since >= TC_BUFSTALL_TRIGGER_MS)
            stalled = 1;
        /* Stalled for the whole hold while raised, whenever it began waiting. */
        if (s->raised && now - (since > s->raised_at ? since : s->raised_at) >= TC_BUFSTALL_HOLD_MS)
            stuck = 1;
    }
    memcpy(s->seen, seen, count * sizeof(seen[0]));
    s->seen_count = count;
    if (stalled)
        s->last_stalled_at = now;
    if ((stalled || s->outcome) && longest > s->longest)
        s->longest = longest;
    *lowater = tc_bufstall_default_lowater(sample->hiwater);
    if (s->raised) {
        if (stuck) {
            s->raised = 0;
            s->retry_at = now + TC_BUFSTALL_HOLD_MS;
            worsen(s, TC_BUFSTALL_OUTCOME_STUCK);
            return TC_BUFSTALL_STUCK;
        }
        if (now - s->last_stalled_at >= TC_BUFSTALL_QUIET_MS) {
            s->raised = 0;
            return end_episode(s, TC_BUFSTALL_RESOLVED);
        }
        return stalled ? TC_BUFSTALL_WAKE : TC_BUFSTALL_NONE;
    }
    if (stalled && now >= s->retry_at) {
        if (sample->hiwater < TC_BUFSTALL_GAP || sample->bufmem >= sample->hiwater - TC_BUFSTALL_GAP) {
            /* Check again later: the pagedaemon may shrink the cache. */
            s->retry_at = now + TC_BUFSTALL_HOLD_MS;
            worsen(s, TC_BUFSTALL_OUTCOME_CAPPED);
            if (s->capped_logged)
                return TC_BUFSTALL_NONE;
            s->capped_logged = 1;
            return TC_BUFSTALL_CAPPED;
        }
        s->raised = 1;
        s->raised_at = now;
        worsen(s, TC_BUFSTALL_OUTCOME_RESOLVED);
        *lowater = sample->hiwater - TC_BUFSTALL_GAP;
        return TC_BUFSTALL_RAISE;
    }
    /* Left raised by an earlier manager, or a restore the kernel refused. */
    if (sample->lowater > *lowater)
        return TC_BUFSTALL_RESTORE;
    if (s->outcome && now - s->last_stalled_at >= TC_BUFSTALL_QUIET_MS)
        return end_episode(s, TC_BUFSTALL_ENDED);
    return TC_BUFSTALL_NONE;
}

void tc_bufstall_failed(struct tc_bufstall *s, long long now) {
    s->raised = 0;
    s->retry_at = now + TC_BUFSTALL_HOLD_MS;
    worsen(s, TC_BUFSTALL_OUTCOME_FAILED);
}

const char *tc_bufstall_outcome_name(enum tc_bufstall_outcome outcome) {
    switch (outcome) {
    case TC_BUFSTALL_OUTCOME_RESOLVED: return "resolved";
    case TC_BUFSTALL_OUTCOME_STUCK: return "stuck";
    case TC_BUFSTALL_OUTCOME_CAPPED: return "capped";
    case TC_BUFSTALL_OUTCOME_FAILED: return "failed";
    default: return "none";
    }
}

int tc_bufstall_wake(void) {
    int pass;
    for (pass = 0; pass < TC_BUFWAKE_PASSES; pass++) {
        DIR *dir = opendir(TC_BUFWAKE_DIR);
        if (!dir)
            return -1;
        while (readdir(dir))
            ;
        closedir(dir);
    }
#ifdef TC_NATIVE_TEST
    {
        /* Host tests count wakes; there is no kernel to watch. */
        const char *path = getenv("TC_TEST_BUFCACHE");
        char wakes[1024];
        FILE *stream;
        if (path && snprintf(wakes, sizeof(wakes), "%s.wakes", path) < (int)sizeof(wakes) &&
            (stream = fopen(wakes, "a"))) {
            fprintf(stream, "%d\n", pass);
            fclose(stream);
        }
    }
#endif
    return 0;
}

#if defined(__NetBSD__) && !defined(TC_NATIVE_TEST)
/* The vm.bufmem* nodes are created at boot with dynamic numbers. One
 * CTL_QUERY of the vm node finds them: sysctlbyname() would link libc's MIB
 * tree learner and qsort, about 6 KB more in NetBSD 4's static image. */
static const char *const vm_names[3] = {"bufmem", "bufmem_lowater", "bufmem_hiwater"};
static int vm_mib[3];
static int vm_numbers(void) {
    static struct sysctlnode nodes[64];
    int mib[2] = {CTL_VM, CTL_QUERY};
    struct sysctlnode query;
    size_t length = sizeof(nodes), i, j;
    int found = 0;
    if (vm_mib[0])
        return 0;
    memset(&query, 0, sizeof(query));
    query.sysctl_flags = SYSCTL_VERSION;
    if (sysctl(mib, 2, nodes, &length, &query, sizeof(query)) < 0)
        return -1;
    for (j = 0; j < 3; j++)
        for (i = 0; i < length / sizeof(nodes[0]); i++)
            if (!strcmp(nodes[i].sysctl_name, vm_names[j])) {
                vm_mib[j] = nodes[i].sysctl_num;
                found++;
                break;
            }
    if (found != 3) {
        vm_mib[0] = 0;
        errno = ENOENT;
        return -1;
    }
    return 0;
}
/* NetBSD 4 exports these as 64-bit quads, NetBSD 6 as 32-bit longs. */
static int vm_read(int number, uint64_t *value, size_t *size) {
    int mib[2] = {CTL_VM, number};
    union {
        uint32_t u32;
        uint64_t u64;
    } v;
    size_t length = sizeof(v);
    memset(&v, 0, sizeof(v));
    if (sysctl(mib, 2, &v, &length, NULL, 0))
        return -1;
    if (length == sizeof(v.u32))
        *value = v.u32;
    else if (length == sizeof(v.u64))
        *value = v.u64;
    else {
        errno = EINVAL;
        return -1;
    }
    if (size)
        *size = length;
    return 0;
}

int tc_bufstall_read(struct tc_bufstall_sample *out) {
    /* Room for every process: kern.maxproc is 84 on both kernels. Static,
     * so a sample needs no memory when the device is short of it. */
    static struct kinfo_proc2 procs[TC_PROCESS_MAX];
    int mib[6] = {CTL_KERN, KERN_PROC2, KERN_PROC_ALL, 0, sizeof(procs[0]), TC_PROCESS_MAX};
    size_t length = sizeof(procs), i;
    memset(out, 0, sizeof(*out));
    if (vm_numbers() || vm_read(vm_mib[0], &out->bufmem, NULL) || vm_read(vm_mib[1], &out->lowater, NULL) ||
        vm_read(vm_mib[2], &out->hiwater, NULL)) {
        vm_mib[0] = 0; /* look the numbers up again next time */
        return -1;
    }
    if (sysctl(mib, 6, procs, &length, NULL, 0) < 0)
        return -1;
    for (i = 0; i < length / sizeof(procs[0]) && out->count < TC_PROCESS_MAX; i++)
        if (tc_bufstall_wmesg(procs[i].p_wmesg, sizeof(procs[i].p_wmesg)))
            out->pids[out->count++] = (pid_t)procs[i].p_pid;
    return 0;
}

int tc_bufstall_set_lowater(uint64_t value) {
    int mib[2] = {CTL_VM, 0};
    uint64_t current;
    size_t size;
    if (vm_numbers() || vm_read(vm_mib[1], &current, &size))
        return -1;
    mib[1] = vm_mib[1];
    if (size == sizeof(uint32_t)) {
        uint32_t narrow = (uint32_t)value;
        return sysctl(mib, 2, NULL, NULL, &narrow, sizeof(narrow));
    }
    return sysctl(mib, 2, NULL, NULL, &value, sizeof(value));
}
#elif defined(TC_NATIVE_TEST)
/* The fixture holds "bufmem N", "lowater N", "hiwater N" and "wait PID WMESG"
 * lines; "readonly" makes writes fail. Each write is appended to PATH.writes. */
static const char *fixture(void) {
    const char *path = getenv("TC_TEST_BUFCACHE");
    if (!path)
        errno = ENOSYS;
    return path;
}

int tc_bufstall_read(struct tc_bufstall_sample *out) {
    char line[128], word[32];
    unsigned long long value;
    long pid;
    const char *path = fixture();
    FILE *stream;
    memset(out, 0, sizeof(*out));
    if (!path || !(stream = fopen(path, "r")))
        return -1;
    while (fgets(line, sizeof(line), stream)) {
        if (sscanf(line, "wait %ld %31s", &pid, word) == 2) {
            if (tc_bufstall_wmesg(word, strlen(word)) && out->count < TC_PROCESS_MAX)
                out->pids[out->count++] = (pid_t)pid;
        } else if (sscanf(line, "%31s %llu", word, &value) == 2) {
            if (!strcmp(word, "bufmem"))
                out->bufmem = value;
            else if (!strcmp(word, "lowater"))
                out->lowater = value;
            else if (!strcmp(word, "hiwater"))
                out->hiwater = value;
        }
    }
    fclose(stream);
    return 0;
}

int tc_bufstall_set_lowater(uint64_t value) {
    char text[8192], temp[1024], writes[1024], line[128];
    size_t used = 0;
    int readonly = 0, n;
    struct tc_bufstall_sample sample;
    const char *path = fixture();
    FILE *stream;
    if (!path || tc_bufstall_read(&sample) || !(stream = fopen(path, "r")))
        return -1;
    while (fgets(line, sizeof(line), stream)) {
        if (!strncmp(line, "readonly", 8))
            readonly = 1;
        n = snprintf(text + used, sizeof(text) - used, "%s",
                     strncmp(line, "lowater ", 8) ? line : "");
        if (n < 0 || (size_t)n >= sizeof(text) - used) {
            fclose(stream);
            errno = EOVERFLOW;
            return -1;
        }
        used += (size_t)n;
    }
    fclose(stream);
    if (readonly) {
        errno = EPERM;
        return -1;
    }
    /* The kernel's own check (sysctl_bufvm_update). */
    if (sample.hiwater < value + TC_BUFSTALL_GAP) {
        errno = EINVAL;
        return -1;
    }
    n = snprintf(text + used, sizeof(text) - used, "lowater %llu\n", (unsigned long long)value);
    if (n < 0 || (size_t)n >= sizeof(text) - used ||
        snprintf(temp, sizeof(temp), "%s.tmp", path) >= (int)sizeof(temp) ||
        snprintf(writes, sizeof(writes), "%s.writes", path) >= (int)sizeof(writes)) {
        errno = EOVERFLOW;
        return -1;
    }
    used += (size_t)n;
    if (!(stream = fopen(temp, "w")))
        return -1;
    if (fwrite(text, 1, used, stream) != used) {
        fclose(stream);
        return -1;
    }
    if (fclose(stream) || rename(temp, path))
        return -1;
    if ((stream = fopen(writes, "a"))) {
        fprintf(stream, "%llu\n", (unsigned long long)value);
        fclose(stream);
    }
    return 0;
}
#else
int tc_bufstall_read(struct tc_bufstall_sample *out) {
    memset(out, 0, sizeof(*out));
    errno = ENOSYS;
    return -1;
}

int tc_bufstall_set_lowater(uint64_t value) {
    (void)value;
    errno = ENOSYS;
    return -1;
}
#endif
