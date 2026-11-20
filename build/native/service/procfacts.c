#include "procfacts.h"
#include <stdio.h>
#include <string.h>

/* diskd runs these through `sh -c`; the shell counts while it is diskd's child. */
static int disk_job_name(const char *name) {
    return !strncmp(name, "fsck", 4) || !strncmp(name, "newfs", 5) || !strcmp(name, "mount") ||
           !strcmp(name, "mount_hfs") || !strcmp(name, "umount") || !strcmp(name, "gpt");
}
int tc_proc_diskd(const struct tc_proc *p) {
    return p && !strcmp(p->comm, "diskd");
}
static int kevent_idle(const struct tc_proc *p) {
    return (!strcmp(p->wmesg, "kqread") || !strcmp(p->wmesg, "kqueue")) && p->slept >= TC_DISKD_IDLE_SECONDS;
}
static int has_child(const struct tc_proctable *t, pid_t pid) {
    size_t i;
    for (i = 0; i < t->count; i++)
        if (t->procs[i].parent == pid && !tc_proc_exited(&t->procs[i]))
            return 1;
    return 0;
}
void tc_proc_facts(const struct tc_proctable *t, const struct tc_proc_context *c, struct tc_proc_facts *out) {
    const struct tc_proc *p;
    size_t i;
    memset(out, 0, sizeof(*out));
    for (i = 0; i < t->count; i++) {
        int child_of_diskd;
        p = &t->procs[i];
        if (p->pid <= 1 || tc_proc_exited(p))
            continue;
        child_of_diskd = tc_proc_diskd(tc_proctable_live(t, p->parent));
        if (disk_job_name(p->comm) || (child_of_diskd && !strcmp(p->comm, "sh")))
            out->disk_work = 1;
        if (!strcmp(p->comm, "fsck_hfs") || !strcmp(p->comm, "newfs_hfs"))
            out->disk_repair = 1;
        if (!strcmp(p->comm, "acp"))
            out->acp_clients++;
        if (!strcmp(p->comm, "wcifsfs") || (!strcmp(p->comm, "wcifsnd") && tc_proc_foreign_wcifsnd(p->parent, c->discovery)))
            out->foreign_netbios = 1;
        /* A child diskd forks keeps its name and argv until it execs, so only
         * a diskd whose parent is not a diskd is an owner. */
        if (!tc_proc_diskd(p) || child_of_diskd)
            continue;
        out->owners++;
        out->owner = (struct tc_proc_identity){p->pid, p->born};
        out->signature += (unsigned long long)p->pid * 2654435761ULL + p->born;
        if (c->current.pid && p->pid == c->current.pid && p->born == c->current.born)
            out->current_present = 1;
        if (c->launch && p->pid == c->launch)
            out->launch_execd = (struct tc_proc_identity){p->pid, p->born};
    }
    if (out->owners == 1 && (p = tc_proctable_live(t, out->owner.pid)) && kevent_idle(p))
        out->owner_idle = !has_child(t, out->owner.pid);
}
const struct tc_proc *tc_proc_main_acpd(const struct tc_proctable *t) {
    const struct tc_proc *acpd = NULL, *p;
    size_t i;
    for (i = 0; i < t->count && !acpd; i++)
        if (!tc_proc_exited(&t->procs[i]) && !strcmp(t->procs[i].comm, "ACPd"))
            acpd = &t->procs[i];
    if (acpd && (p = tc_proctable_live(t, acpd->parent)) && !strcmp(p->comm, "ACPd"))
        acpd = p;
    return acpd;
}
void tc_acpd_quiet_init(struct tc_acpd_quiet *q) {
    *q = (struct tc_acpd_quiet){.seen_at = -1, .since = -1};
}
void tc_acpd_quiet_update(struct tc_acpd_quiet *q, const struct tc_proctable *t, long long now) {
    const struct tc_proc *acpd = tc_proc_main_acpd(t);
    if (!acpd) {
        tc_acpd_quiet_init(q);
        return;
    }
    /* The allowance grows with the time between reads, which the gap rule
     * caps, so one ACPd counts the same at either read spacing. */
    long long gap = now - q->seen_at;
    unsigned long long allowed = (unsigned long long)TC_ACPD_QUIET_BUDGET_US * (unsigned long long)(gap > 0 ? gap : 0);
    allowed /= 1000;
    if (acpd->pid != q->acpd.pid || acpd->born != q->acpd.born || acpd->stat == TC_LSRUN ||
        acpd->stat == TC_LSONPROC || gap > TC_ACPD_QUIET_GAP_MS || acpd->cpu_us < q->cpu_us ||
        acpd->cpu_us - q->cpu_us > allowed)
        q->since = now;
    q->acpd = (struct tc_proc_identity){acpd->pid, acpd->born};
    q->cpu_us = acpd->cpu_us;
    q->seen_at = now;
}
long long tc_acpd_quiet_ms(const struct tc_acpd_quiet *q, long long now) {
    return q->acpd.pid ? now - q->since : -1;
}
/* A row as PID:STAT:WMESG:SLEPT:CPU_MS, or "none". */
static void row_word(const struct tc_proc *p, char *out, size_t size) {
    if (!p)
        snprintf(out, size, "none");
    else
        snprintf(out, size, "%ld:%d:%s:%u:%llu", (long)p->pid, p->stat, p->wmesg[0] ? p->wmesg : "-", p->slept,
                 p->cpu_us / 1000);
}
int tc_proc_handoff_line(const struct tc_proctable *t, struct tc_proc_identity owner, pid_t skip_group, char *out,
                         size_t size) {
    const struct tc_proc *acpd = NULL, *p;
    char diskd[64], apple[64], children[160];
    size_t i, used = 0;
    int acp = 0, others = 0;
    for (i = 0; i < t->count; i++) {
        p = &t->procs[i];
        if (!tc_proc_exited(p) && !strcmp(p->comm, "acp") && !(skip_group && p->group == skip_group))
            acp++;
    }
    acpd = tc_proc_main_acpd(t);
    children[0] = 0;
    for (i = 0; acpd && i < t->count; i++) {
        p = &t->procs[i];
        if (p->parent != acpd->pid)
            continue;
        if (used < sizeof(children) && (tc_proc_exited(p) || !strncmp(p->comm, "wcifs", 5) ||
                                        !strcmp(p->comm, "ACPd") || tc_proc_diskd(p))) {
            int n = snprintf(children + used, sizeof(children) - used, "%s%s:%ld%s", used ? "," : "", p->comm,
                             (long)p->pid, tc_proc_exited(p) ? ":Z" : "");
            /* One that does not fit whole is counted instead. */
            if (n > 0 && used + (size_t)n < sizeof(children) - 8)
                used += (size_t)n;
            else {
                children[used] = 0;
                others++;
            }
        } else
            others++;
    }
    if (others)
        snprintf(children + used, sizeof(children) - used, "%s+%d", used ? "," : "", others);
    /* A reused PID is another process: the owner is gone. */
    p = owner.pid ? tc_proctable_live(t, owner.pid) : NULL;
    row_word(p && p->born == owner.born ? p : NULL, diskd, sizeof(diskd));
    row_word(acpd, apple, sizeof(apple));
    return snprintf(out, size, "diskd=%s acpd=%s acpd_children=%s acp=%d", diskd, apple,
                    children[0] ? children : "none", acp);
}
