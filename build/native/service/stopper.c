#include "stopper.h"
#include <signal.h>
#include <string.h>

/* The block bits a process holds back while it runs, or 0 when it is ours or
 * no target. *killable: SIGKILL may follow. */
static unsigned policy(const struct tc_process_info *p, const struct tc_stop_ours *o, int *killable) {
    *killable = 1;
    switch (p->role) {
    case TC_PROC_WCIFSFS:
        return BLOCK_SMB | BLOCK_DISCOVERY; /* Apple's file server on TCP 445 */
    case TC_PROC_WCIFSND:
        return tc_proc_foreign_wcifsnd(p->parent, o->discovery) ? BLOCK_DISCOVERY : 0;
    case TC_PROC_SMBD:
        return p->group != o->smb_group ? BLOCK_SMB : 0;
    case TC_PROC_RSYNC:
        return p->group != o->rsync_group ? BLOCK_RSYNC : 0;
    case TC_PROC_DISCOVERY:
        return p->pid != o->discovery ? BLOCK_DISCOVERY : 0;
    case TC_PROC_TELEMETRY:
        *killable = 0;
        return p->pid != o->telemetry ? BLOCK_TELEMETRY : 0;
    default:
        return 0; /* the stock diskd is the handoff's */
    }
}
size_t tc_stopper_step(struct tc_stopper *s, const struct tc_process_table *table, const struct tc_stop_ours *ours,
                       long long now, struct tc_stop_signal *out, unsigned *blocked) {
    static struct tc_stop_entry next[TC_PROCESS_MAX];
    size_t i, j, count = 0;
    *blocked = 0;
    for (i = 0; i < table->count; i++) {
        const struct tc_process_info *p = &table->processes[i];
        struct tc_stop_entry e = {{p->pid, p->born}, now, 0};
        int killable;
        unsigned block = policy(p, ours, &killable);
        if (!block)
            continue;
        for (j = 0; j < s->count; j++)
            if (s->entries[j].id.pid == p->pid && s->entries[j].id.born == p->born)
                e = s->entries[j];
        out[count] = (struct tc_stop_signal){p->pid, p->group, p->role, SIGTERM, 0, now - e.since};
        if (killable && now - e.since >= TC_STALE_KILL_MS) {
            out[count].sig = SIGKILL;
            out[count].first_kill = !e.killed;
            e.killed = 1;
        }
        next[count++] = e;
        *blocked |= block;
    }
    memcpy(s->entries, next, count * sizeof(next[0]));
    s->count = count;
    return count;
}
