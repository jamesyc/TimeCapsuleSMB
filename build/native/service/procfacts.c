#include "procfacts.h"
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
