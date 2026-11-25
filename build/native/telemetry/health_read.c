#include "health.h"
#include "../common/acp.h"
#if defined(__NetBSD__) && !defined(TC_NATIVE_TEST)
#include <sys/param.h>
#include <sys/sysctl.h>
#include <sys/mbuf.h>
#include <uvm/uvm_extern.h>
#endif
#include <stddef.h>

#define MAX_DEPTH 16

#if defined(__NetBSD__) && !defined(TC_NATIVE_TEST)
static int int_sysctl(int a, int b, int c, int levels) {
    int mib[3] = {a, b, c}, value;
    size_t length = sizeof(value);
    return sysctl(mib, (unsigned)levels, &value, &length, NULL, 0) || length != sizeof(value) ? -1 : value;
}

static void read_counters(struct tc_counters *c) {
    struct uvmexp_sysctl u;
    struct mbstat m;
    int mib[6];
    size_t length;
    memset(c, 0, sizeof(*c));
    c->maxproc = int_sysctl(CTL_KERN, KERN_MAXPROC, 0, 2);
    c->maxfiles = int_sysctl(CTL_KERN, KERN_MAXFILES, 0, 2);
    /* With no buffer the kernel answers with the length it would copy:
     * every open file plus KERN_FILESLOP spare entries. Nothing is copied.
     * NetBSD 4 counts a file only while the requested element count is
     * above zero (NetBSD 6 counts them all), so ask for all of them. */
    mib[0] = CTL_KERN; mib[1] = KERN_FILE2; mib[2] = KERN_FILE_BYFILE;
    mib[3] = 0; mib[4] = (int)sizeof(struct kinfo_file); mib[5] = INT_MAX;
    length = 0;
    c->files = -1;
    if (!sysctl(mib, 6, NULL, &length, NULL, 0) && length / sizeof(struct kinfo_file) >= KERN_FILESLOP)
        c->files = (int)(length / sizeof(struct kinfo_file)) - KERN_FILESLOP;
    /* A kernel whose struct ends before a field read here would leave it
     * unset: clear both structs and require the fields read. */
    memset(&u, 0, sizeof(u));
    mib[0] = CTL_VM; mib[1] = VM_UVMEXP2;
    length = sizeof(u);
    c->have_mem = !sysctl(mib, 2, &u, &length, NULL, 0) &&
                  length >= offsetof(struct uvmexp_sysctl, execpages) + sizeof(u.execpages);
    if (c->have_mem) {
        c->pagesize = u.pagesize; c->free = u.free; c->active = u.active; c->inactive = u.inactive;
        c->freetarg = u.freetarg; c->anon = u.anonpages; c->file = u.filepages;
        c->fltnoram = u.fltnoram;
    }
    memset(&m, 0, sizeof(m));
    mib[0] = CTL_KERN; mib[1] = KERN_MBUF; mib[2] = MBUF_STATS;
    length = sizeof(m);
    c->have_mbuf = !sysctl(mib, 3, &m, &length, NULL, 0) &&
                   length >= offsetof(struct mbstat, m_drain) + sizeof(m.m_drain);
    if (c->have_mbuf) c->mbuf_drops = m.m_drops;
    c->nmbclusters = int_sysctl(CTL_KERN, KERN_MBUF, MBUF_NMBCLUSTERS, 3);
}

/* Open descriptors of one process: the KERN_FILE2 length query again, by
 * process. A pid that has exited still gets the KERN_FILESLOP padding, so
 * it reads as 0, like a process with none; -1 when the query fails. */
static int process_fds(pid_t pid) {
    int mib[6] = {CTL_KERN, KERN_FILE2, KERN_FILE_BYPID, (int)pid, (int)sizeof(struct kinfo_file), INT_MAX};
    size_t length = 0;
    if (sysctl(mib, 6, NULL, &length, NULL, 0) || length / sizeof(struct kinfo_file) < KERN_FILESLOP)
        return -1;
    return (int)(length / sizeof(struct kinfo_file)) - KERN_FILESLOP;
}

/* The kernel message buffer, oldest first, from the write pointer round.
 * These kernels do not report how much KERN_MSGBUF copied, so ask for the
 * buffer's size first (no output buffer) and read exactly that. */
static long read_kmsg(char *out, size_t size) {
    int mib[2] = {CTL_KERN, KERN_MSGBUF};
    size_t want = 0, length;
    if (sysctl(mib, 2, NULL, &want, NULL, 0)) return -1;
    if (want > size - 1) want = size - 1;
    length = want;
    if (want && sysctl(mib, 2, out, &length, NULL, 0) && errno != ENOMEM) return -1;
    return (long)(length < want ? length : want);
}
#elif defined(TC_NATIVE_TEST)
/* Host tests: TC_TEST_HEALTH names a file of "name value" lines (a missing
 * name is a failed read), TC_TEST_KMSG a file holding the message buffer
 * (no file: unreadable). */
static long long fixture(const char *name, long long missing) {
    const char *path = getenv("TC_TEST_HEALTH");
    char line[256], key[64];
    long long value, result = missing;
    FILE *f = path ? fopen(path, "r") : NULL;
    if (!f) return missing;
    while (fgets(line, sizeof(line), f))
        if (sscanf(line, "%63s %lld", key, &value) == 2 && !strcmp(key, name)) result = value;
    fclose(f);
    return result;
}

static void read_counters(struct tc_counters *c) {
    memset(c, 0, sizeof(*c));
    c->maxproc = (int)fixture("maxproc", -1);
    c->maxfiles = (int)fixture("maxfiles", -1);
    c->files = (int)fixture("files", -1);
    c->nmbclusters = (int)fixture("nmbclusters", -1);
    c->have_mem = fixture("free", -1) >= 0;
    c->pagesize = fixture("pagesize", 4096);
    c->free = fixture("free", 0);
    c->active = fixture("active", 0);
    c->inactive = fixture("inactive", 0);
    c->freetarg = fixture("freetarg", 0);
    c->anon = fixture("anon", 0);
    c->file = fixture("file", 0);
    c->fltnoram = fixture("fltnoram", 0);
    c->have_mbuf = fixture("mbuf_drops", -1) >= 0;
    c->mbuf_drops = (unsigned long)fixture("mbuf_drops", 0);
}

/* "fds.PID N" lines in the counters fixture; a pid without one reads as 0,
 * as the kernel answers for an exited process. */
static int process_fds(pid_t pid) {
    char key[32];
    snprintf(key, sizeof(key), "fds.%ld", (long)pid);
    return (int)fixture(key, 0);
}

static long read_kmsg(char *out, size_t size) {
    const char *path = getenv("TC_TEST_KMSG");
    FILE *f = path ? fopen(path, "r") : NULL;
    size_t length;
    if (!f) return -1;
    length = fread(out, 1, size - 1, f);
    fclose(f);
    return (long)length;
}
#else
static void read_counters(struct tc_counters *c) {
    memset(c, 0, sizeof(*c));
    c->maxproc = c->maxfiles = c->files = c->nmbclusters = -1;
}
static long read_kmsg(char *out, size_t size) { (void)out; (void)size; return -1; }
static int process_fds(pid_t pid) { (void)pid; return -1; }
#endif

static const struct tc_proc *find(const struct tc_proctable *t, pid_t pid) {
    size_t i;
    for (i = 0; i < t->count; i++)
        if (t->procs[i].pid == pid) return &t->procs[i];
    return NULL;
}

/* Steps from init (depth 0) through parents; kernel threads and orphans whose
 * parent is not in the table stop the walk where it is. */
static unsigned depth_of(const struct tc_proctable *t, size_t index) {
    unsigned depth = 0;
    pid_t parent = t->procs[index].parent;
    while (parent > 1 && depth < MAX_DEPTH) {
        const struct tc_proc *p = find(t, parent);
        depth++;
        if (!p) break;
        parent = p->parent;
    }
    return parent == 1 ? depth + 1 : depth;
}

/* A daemon's listener: a live process named comm whose parent is not one
 * too (smbd's connection children are). It does not matter who
 * started it, so a test run of telemetry from a shell sees the same. A
 * connection child whose listener exited (its parent is now init) passes
 * too, so while it lives a refusal from the gone listener still counts as
 * a failure: rare, and a dead listener is worth reporting anyway. */
static int is_listener(const struct tc_proctable *t, const struct tc_proc *p, const char *comm) {
    const struct tc_proc *parent;
    if (tc_proc_exited(p) || strcmp(p->comm, comm)) return 0;
    parent = find(t, p->parent);
    return !parent || strcmp(parent->comm, comm);
}

static void add_group(struct tc_proc_group *all, size_t *count, size_t max, const char *comm, unsigned depth) {
    size_t i;
    for (i = 0; i < *count; i++)
        if (all[i].depth == depth && !strcmp(all[i].comm, comm)) {
            all[i].count++;
            return;
        }
    if (*count < max) {
        snprintf(all[*count].comm, sizeof(all[*count].comm), "%s", comm);
        all[*count].depth = depth;
        all[*count].count = 1;
        (*count)++;
    }
}

static void add_wait(struct tc_wait_count *all, size_t *count, size_t max, const char *wmesg) {
    size_t i;
    for (i = 0; i < *count; i++)
        if (!strcmp(all[i].wmesg, wmesg)) {
            all[i].count++;
            return;
        }
    if (*count < max) {
        snprintf(all[*count].wmesg, sizeof(all[*count].wmesg), "%s", wmesg);
        all[*count].count = 1;
        (*count)++;
    }
}

static int by_group_count(const void *a, const void *b) {
    const struct tc_proc_group *x = a, *y = b;
    if (x->count != y->count) return x->count < y->count ? 1 : -1;
    if (x->depth != y->depth) return x->depth < y->depth ? -1 : 1;
    return strcmp(x->comm, y->comm);
}
static int by_wait_count(const void *a, const void *b) {
    const struct tc_wait_count *x = a, *y = b;
    if (x->count != y->count) return x->count < y->count ? 1 : -1;
    return strcmp(x->wmesg, y->wmesg);
}

/* Insertion into the few largest by value, biggest first. */
#define INSERT_TOP(array, count, max, field, value, comm_text)                                                \
    do {                                                                                                      \
        size_t j_, k_;                                                                                        \
        for (j_ = 0; j_ < (count) && (array)[j_].field >= (value); j_++)                                      \
            ;                                                                                                 \
        if (j_ < (max)) {                                                                                     \
            k_ = (count) < (max) ? (count)++ : (max) - 1;                                                     \
            for (; k_ > j_; k_--) (array)[k_] = (array)[k_ - 1];                                              \
            snprintf((array)[j_].comm, sizeof((array)[j_].comm), "%s", (comm_text));                          \
            (array)[j_].field = (value);                                                                      \
        }                                                                                                     \
    } while (0)

static void read_snapshot(struct tc_snapshot *s, long now) {
    static struct tc_proctable table;
    static struct tc_proc_group groups[TC_PROCESS_MAX];
    static struct tc_wait_count waits[TC_PROCESS_MAX];
    size_t i, group_count = 0, wait_count = 0;
    memset(s, 0, sizeof(*s));
    s->at = now;
    s->smbd_children = -1;
    if (tc_proctable_read(&table)) return;
    s->read = 1;
    s->procs = (unsigned)table.count;
    for (i = 0; i < table.count; i++) {
        const struct tc_proc *p = &table.procs[i];
        if (p->flag & TC_P_SYSTEM) {
            s->system++;
            continue;
        }
        if (tc_proc_exited(p)) {
            s->zombies++;
            add_group(groups, &group_count, TC_PROCESS_MAX, "(zombie)", depth_of(&table, i));
            continue;
        }
        if (p->stat == TC_LSSLEEP && !(p->flag & TC_L_SINTR)) s->blocked++;
        add_group(groups, &group_count, TC_PROCESS_MAX, p->comm, depth_of(&table, i));
        if (p->wmesg[0]) add_wait(waits, &wait_count, TC_PROCESS_MAX, p->wmesg);
        INSERT_TOP(s->large, s->largest, TC_TOP_LARGEST, rss_pages, p->rss_pages, p->comm);
        if (is_listener(&table, p, "smbd")) s->smbd_up = 1;
    }
    /* Every live child of smbd's listener: max smbd processes refuses new
     * connections once Samba's own count of them reaches it, whatever the
     * table holds. Samba counts its connection children; this build runs
     * notifyd and cleanupd inside the listener (patch 0008), so they are no
     * children. The one child Samba leaves out is smbd-scavenger, which the
     * listener forks at the first orphaned durable handle and keeps: its
     * comm is "smbd" too, and nothing in the kernel's process table tells
     * it apart, so once it runs this is one above Samba's count. */
    if (s->smbd_up) {
        s->smbd_children = 0;
        for (i = 0; i < table.count; i++) {
            const struct tc_proc *parent = find(&table, table.procs[i].parent);
            if (!tc_proc_exited(&table.procs[i]) && parent && is_listener(&table, parent, "smbd"))
                s->smbd_children++;
        }
    }
    for (i = 0; i < table.count; i++) {
        const struct tc_proc *p = &table.procs[i];
        int fds;
        if ((p->flag & TC_P_SYSTEM) || tc_proc_exited(p) || (fds = process_fds(p->pid)) <= 0) continue;
        INSERT_TOP(s->fd, s->most_fds, TC_TOP_FDS, fds, (unsigned)fds, p->comm);
    }
    qsort(groups, group_count, sizeof(groups[0]), by_group_count);
    qsort(waits, wait_count, sizeof(waits[0]), by_wait_count);
    s->groups = group_count < TC_TOP_GROUPS ? group_count : TC_TOP_GROUPS;
    memcpy(s->group, groups, s->groups * sizeof(groups[0]));
    s->waits = wait_count < TC_TOP_WAITS ? wait_count : TC_TOP_WAITS;
    memcpy(s->wait, waits, s->waits * sizeof(waits[0]));
}

void tc_health_read_sample(struct tc_reading *out, long now) {
    static char kmsg[TC_KMSG_MAX];
    long length, i, text = 0;
    read_counters(&out->counters);
    read_snapshot(&out->snapshot, now);
    length = read_kmsg(kmsg, sizeof(kmsg));
    /* Until the ring first wraps, the part after the write pointer is still
     * zero bytes, and it comes first. Drop NULs so the text matches. */
    for (i = 0; i < length; i++)
        if (kmsg[i]) kmsg[text++] = kmsg[i];
    kmsg[length < 0 ? 0 : text] = 0;
    out->kmsg = kmsg;
    out->kmsg_len = length < 0 ? -1 : text;
}

/* ---- probes ----
 * Each target, over loopback, non-blocking:
 *   connecting  -- connected --> sending (smbd: the NEGOTIATE) --> reading
 *               -- refused --> absent (the tracker decides if it fails)
 *               -- reset (accepted, then hung up at once) --> closed
 *               -- no such address or route (no ::1) --> skipped
 *               -- out of buffers, memory or descriptors here --> skipped,
 *                  counted with socket() failures: not the service's fault
 *               -- nothing in TC_HEALTH_PROBE_CONNECT_MS --> timeout (a full
 *                  listen queue: nothing accepts)
 *               -- any other error --> down
 *   reading     -- sshd's first byte, or an SMB2 reply (any status) --> ok,
 *                  then draining
 *               -- EOF or a reset first --> closed (no fork, or smbd's
 *                  max smbd processes)
 *               -- other bytes, or nothing in TC_HEALTH_PROBE_ANSWER_MS --> silent
 *   draining    -- the answer is in: send sshd a line naming the probe, then
 *                  EOF to either server, and read until the server's EOF or
 *                  the answer limit, so neither side resets the connection.
 *                  The line is not an SSH identification on purpose: sshd
 *                  logs it ("Bad protocol version identification") and
 *                  closes before privilege separation forks its pre-auth
 *                  child, where a valid one would add that child, a third
 *                  process in a round the table margin counts as two
 * smbd is asked something because a parent that never accepts looks just
 * like a waiting child otherwise: the kernel completes the handshake. The
 * probe makes sshd or smbd fork one child for a moment; smbd's exits cleanly
 * and is not logged (patch 0083 logs only unclean exits). socket() failing
 * is skipped (counted unless the kernel lacks the family). TERM skips what
 * is left, keeping a result already in. */
/* sshd over IPv4 only: health.h says why. */
struct target { int family; unsigned port; int smb; };
static const struct target targets[TC_PROBES] = {
    {AF_INET, TC_HEALTH_SSH_PORT, 0}, {AF_INET, TC_HEALTH_SMB_PORT, 1},
    {AF_INET6, TC_HEALTH_SMB_PORT, 1},
};
enum step { STEP_CONNECTING, STEP_READING, STEP_DRAINING, STEP_DONE };
struct probe {
    int fd;
    enum step step;
    size_t got;
    unsigned char reply[8];
    long long until;
    enum tc_probe_result result;
};

/* The 4-byte session header (length 108), the 64-byte SMB2 header (command
 * 0, one credit), and the request offering 2.0.2, 2.1, 3.0 and 3.0.2, so
 * smbd answers whatever its "min protocol" is (3.1.1 would need negotiate
 * contexts). */
static const unsigned char smb2_negotiate[112] = {
    0x00, 0x00, 0x00, 0x6c,
    0xfe, 'S', 'M', 'B', 0x40, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x24, 0x00, 0x04, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    't', 'c', 's', 'm', 'b', '-', 'h', 'e', 'a', 'l', 't', 'h', 'p', 'r', 'o', 'b',
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x02, 0x02, 0x10, 0x02, 0x00, 0x03, 0x02, 0x03,
};

static void finish(struct probe *p, enum tc_probe_result result) {
    if (p->fd >= 0) close(p->fd);
    p->fd = -1;
    p->step = STEP_DONE;
    p->result = result;
}

/* Refused: nothing listens there (absent). No such address or route on
 * this kernel (no ::1), or no buffer, memory or descriptor for it here:
 * the probe could not run (skipped), which says nothing about the
 * service. */
static enum tc_probe_result connect_failure(int error) {
    if (error == ECONNREFUSED) return TC_PROBE_ABSENT;
    /* A reset or abort this early still followed a finished handshake (a
     * refusal is ECONNREFUSED): the server accepted and hung up before the
     * probe looked, which is closed, as when the hang-up comes later. */
    if (error == ECONNRESET || error == ECONNABORTED) return TC_PROBE_CLOSED;
    if (error == EADDRNOTAVAIL || error == ENETUNREACH) return TC_PROBE_SKIPPED;
    return tc_errno_record(TC_OP_IO, error) == TC_ERR_RESOURCE ? TC_PROBE_SKIPPED : TC_PROBE_DOWN;
}
#ifdef TC_NATIVE_TEST
enum tc_probe_result tc_health_connect_failure(int error) { return connect_failure(error); }
#endif

static void drain(struct probe *p, const struct target *t) {
    static const char tag[] = "TimeCapsuleSMB health probe\r\n";
    if (!t->smb) (void)send(p->fd, tag, sizeof(tag) - 1, 0);
    (void)shutdown(p->fd, SHUT_WR);
    p->result = TC_PROBE_OK;
    p->step = STEP_DRAINING;
}

static void connected(struct probe *p, const struct target *t) {
    if (t->smb) {
        ssize_t sent = send(p->fd, smb2_negotiate, sizeof(smb2_negotiate), 0);
        if (sent != (ssize_t)sizeof(smb2_negotiate)) {
            /* A full socket buffer this early is a server not reading; any
             * other failure, or a short send (errno untouched), a closed
             * connection. */
            finish(p, sent < 0 && errno == EAGAIN ? TC_PROBE_SILENT : TC_PROBE_CLOSED);
            return;
        }
    }
    p->step = STEP_READING;
    p->until = acp_monotonic_ms() + TC_HEALTH_PROBE_ANSWER_MS;
}

static void start(struct probe *p, const struct target *t) {
    struct sockaddr_storage ss;
    socklen_t sslen;
    memset(p, 0, sizeof(*p));
    memset(&ss, 0, sizeof(ss));
    if (t->family == AF_INET) {
        struct sockaddr_in *v4 = (struct sockaddr_in *)&ss;
        v4->sin_family = AF_INET;
        v4->sin_port = htons((unsigned short)t->port);
        v4->sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        sslen = sizeof(*v4);
    } else {
        struct sockaddr_in6 *v6 = (struct sockaddr_in6 *)&ss;
        v6->sin6_family = AF_INET6;
        v6->sin6_port = htons((unsigned short)t->port);
        v6->sin6_addr = in6addr_loopback;
        sslen = sizeof(*v6);
    }
    p->fd = socket(t->family, SOCK_STREAM, 0);
    if (p->fd < 0) {
        (void)tc_errno_record(TC_OP_SOCKET, errno);
        finish(p, TC_PROBE_SKIPPED);
        return;
    }
    /* A descriptor select() cannot watch is a process holding too many:
     * counted as a socket failure like EMFILE, so a leak in telemetry shows
     * as socket news instead of probes quietly skipped. */
    if (p->fd >= FD_SETSIZE) {
        (void)tc_errno_record(TC_OP_SOCKET, EMFILE);
        finish(p, TC_PROBE_SKIPPED);
        return;
    }
    if (fcntl(p->fd, F_SETFL, O_NONBLOCK) || fcntl(p->fd, F_SETFD, FD_CLOEXEC)) {
        (void)tc_errno_record(TC_OP_SOCKET, errno);
        finish(p, TC_PROBE_SKIPPED);
        return;
    }
    p->step = STEP_CONNECTING;
    p->until = acp_monotonic_ms() + TC_HEALTH_PROBE_CONNECT_MS;
    if (!connect(p->fd, (struct sockaddr *)&ss, sslen)) connected(p, t);
    else if (errno != EINPROGRESS) finish(p, connect_failure(errno));
}

static void advance(struct probe *p, const struct target *t, int ready) {
    long long now = acp_monotonic_ms();
    if (p->step == STEP_CONNECTING) {
        int error = 0;
        socklen_t length = sizeof(error);
        if (ready) {
            if (getsockopt(p->fd, SOL_SOCKET, SO_ERROR, &error, &length)) error = errno;
            if (error) finish(p, connect_failure(error));
            else connected(p, t);
        } else if (now >= p->until) {
            finish(p, TC_PROBE_TIMEOUT);
        }
        return;
    }
    if (p->step == STEP_DRAINING) {
        char scratch[512];
        ssize_t n = ready ? recv(p->fd, scratch, sizeof(scratch), 0) : 0;
        if (ready && n < 0 && (errno == EINTR || errno == EAGAIN)) return;
        if ((ready && n <= 0) || now >= p->until) finish(p, p->result);
        return;
    }
    if (p->step != STEP_READING) return;
    if (ready) {
        ssize_t n = recv(p->fd, p->reply + p->got, sizeof(p->reply) - p->got, 0);
        if (n < 0 && (errno == EINTR || errno == EAGAIN)) return;
        if (n <= 0) {
            finish(p, p->got ? TC_PROBE_SILENT : TC_PROBE_CLOSED);
            return;
        }
        p->got += (size_t)n;
        /* sshd's first byte is enough; smbd's reply must reach the SMB2
         * signature after the 4-byte session header. */
        if (!t->smb) drain(p, t);
        else if (p->got < sizeof(p->reply)) return;
        else if (!memcmp(p->reply + 4, "\xfeSMB", 4)) drain(p, t);
        else finish(p, TC_PROBE_SILENT);
    } else if (now >= p->until) {
        finish(p, TC_PROBE_SILENT);
    }
}

/* Stops a probe early: skipped, unless its answer is already in. */
static void abandon(struct probe *p) {
    finish(p, p->step == STEP_DRAINING ? p->result : TC_PROBE_SKIPPED);
}

/* Runs targets first to end (exclusive) together to the end. */
static void run_targets(struct probe *probes, size_t first, size_t end) {
    size_t i;
    for (i = first; i < end; i++) start(&probes[i], &targets[i]);
    for (;;) {
        fd_set reads, writes;
        struct timeval timeout;
        long long now = acp_monotonic_ms(), wait = 1000; /* recheck the stop flag every second */
        int top = -1, ready;
        FD_ZERO(&reads);
        FD_ZERO(&writes);
        for (i = first; i < end; i++) {
            struct probe *p = &probes[i];
            if (p->step == STEP_DONE) continue;
            if (telemetry_stop) {
                abandon(p);
                continue;
            }
            FD_SET(p->fd, p->step == STEP_CONNECTING ? &writes : &reads);
            if (p->fd > top) top = p->fd;
            if (p->until - now < wait) wait = p->until - now;
        }
        if (top < 0) return;
        if (wait < 0) wait = 0;
        timeout.tv_sec = (time_t)(wait / 1000);
        timeout.tv_usec = (suseconds_t)(wait % 1000) * 1000;
        ready = select(top + 1, &reads, &writes, NULL, &timeout);
        if (ready < 0 && errno != EINTR) {
            for (i = first; i < end; i++)
                if (probes[i].step != STEP_DONE) abandon(&probes[i]);
            return;
        }
        for (i = first; i < end; i++) {
            struct probe *p = &probes[i];
            if (p->step == STEP_DONE) continue;
            advance(p, &targets[i], ready > 0 && (FD_ISSET(p->fd, &reads) || FD_ISSET(p->fd, &writes)));
        }
    }
}

void tc_health_read_probes(struct tc_probe_round *out) {
    struct probe probes[TC_PROBES];
    size_t i;
    memset(probes, 0, sizeof(probes)); /* a probe never started stays skipped */
    run_targets(probes, TC_PROBE_SSH4, TC_PROBE_SMB6);
    if (!telemetry_stop) run_targets(probes, TC_PROBE_SMB6, TC_PROBES);
    for (i = 0; i < TC_PROBES; i++) out->result[i] = probes[i].result;
}
