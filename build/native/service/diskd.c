#include "diskd.h"
#include "../storage/settle.h"
#include <stdio.h>
#include <string.h>

static void set_state(struct tc_diskd *s, enum tc_diskd_phase phase, enum tc_diskd_reason reason, long long now) {
    if (s->phase != phase || s->reason != reason) {
        if (s->reason == TC_DISKD_BUSY)
            s->held_ms += now - s->since;
        s->since = now;
    }
    s->phase = phase;
    s->reason = reason;
}
/* 1x, 3x, then 12x TC_STORAGE_RETRY_MS, like the storage retries. */
static long long backoff(unsigned *failures, long long now) {
    static const unsigned delays[] = {TC_STORAGE_RETRY_MS, 3 * TC_STORAGE_RETRY_MS, 12 * TC_STORAGE_RETRY_MS};
    long long due = now + delays[*failures];
    if (*failures < 2)
        (*failures)++;
    return due;
}
void tc_diskd_init(struct tc_diskd *s, long long now) {
    *s = (struct tc_diskd){.phase = TC_DISKD_INSPECT, .since = now, .generation = 1};
}
/* Apple keeps volume claims inside the diskd process: nothing the old owner
 * accepted carries over to the next one. Our loopback owner (starting or
 * ready) exiting means the next launch waits out a backoff, like a launch
 * that never exec'd, so a diskd that keeps dying is not relaunched every
 * pass; only an answer resets it. A stock owner exiting after TERM is the
 * handoff working: the launch follows at once. */
static unsigned retire(struct tc_diskd *s, long long now) {
    int ours = s->phase == TC_DISKD_STARTING || s->phase == TC_DISKD_READY;
    if (ours)
        s->retry_at = backoff(&s->launch_failures, now);
    s->owner = (struct tc_diskd_identity){0};
    s->generation++;
    s->probe_token++;
    s->probing = 0;
    s->no_answers = 0;
    s->probe_at = 0;
    set_state(s, TC_DISKD_INSPECT, ours ? TC_DISKD_RELAUNCH_WAIT : TC_DISKD_NONE, now);
    return TC_DISKD_RETIRE;
}
static enum tc_diskd_phase owner_phase(struct tc_diskd *s, const struct tc_diskd_observation *o) {
    s->owner = o->owner;
    return o->loopback ? TC_DISKD_STARTING : TC_DISKD_QUIESCE;
}
/* README "Disk owner handoff", Observation. diskd runs these tools through
 * `sh -c`, so the shell counts as one while it is diskd's child. */
static int disk_job_name(const char *name) {
    return !strncmp(name, "fsck", 4) || !strncmp(name, "newfs", 5) || !strcmp(name, "mount") ||
           !strcmp(name, "mount_hfs") || !strcmp(name, "umount") || !strcmp(name, "gpt");
}
int tc_diskd_row(const struct tc_proc *p) {
    return p && !strcmp(p->comm, "diskd");
}
/* A child diskd forks keeps its name and argv until it execs, so only a
 * diskd whose parent is not a diskd is an owner. */
void tc_diskd_scan(const struct tc_proctable *t, struct tc_diskd_identity current, struct tc_diskd_scan *out) {
    size_t i;
    memset(out, 0, sizeof(*out));
    for (i = 0; i < t->count; i++) {
        const struct tc_proc *p = &t->procs[i];
        int child_of_diskd;
        if (tc_proc_exited(p))
            continue;
        child_of_diskd = tc_diskd_row(tc_proctable_live(t, p->parent));
        if (disk_job_name(p->comm) || (child_of_diskd && !strcmp(p->comm, "sh")))
            out->disk_work = 1;
        if (!strcmp(p->comm, "acp"))
            out->acp_clients++;
        if (!tc_diskd_row(p) || child_of_diskd)
            continue;
        out->owners++;
        out->owner = (struct tc_diskd_identity){p->pid, p->born};
        out->signature += (unsigned long long)p->pid * 2654435761ULL + p->born;
        if (current.pid && p->pid == current.pid && p->born == current.born)
            out->current_present = 1;
    }
}
int tc_diskd_owner_idle(const struct tc_proctable *t, pid_t owner) {
    const struct tc_proc *p = tc_proctable_live(t, owner);
    size_t i;
    if (!p || (strcmp(p->wmesg, "kqread") && strcmp(p->wmesg, "kqueue")) || p->slept < TC_DISKD_IDLE_SECONDS)
        return 0;
    for (i = 0; i < t->count; i++)
        if (t->procs[i].parent == owner && !tc_proc_exited(&t->procs[i]))
            return 0;
    return 1;
}
unsigned tc_diskd_step(struct tc_diskd *s, const struct tc_diskd_observation *o, long long now) {
    enum tc_diskd_phase phase;
    enum tc_diskd_reason why = TC_DISKD_NONE;
    unsigned actions = 0;
    if (!o->kernel_valid) {
        set_state(s, s->phase, TC_DISKD_OBSERVATION, now);
        return 0;
    }
    /* An exit needs only the kernel table; what replaces it needs the audit. */
    if (s->owner.pid && !o->current_present)
        actions |= retire(s, now);
    phase = s->phase;
    if (!o->valid) {
        set_state(s, phase, TC_DISKD_OBSERVATION, now);
        return actions;
    }
    if (o->owners > 1) {
        /* Not ours to resolve: a second diskd that exits deletes the first's
         * RPC names (README F16), so nothing that needs `ready` starts while
         * it lasts; running services carry on. No answer from before or
         * during it counts, and our owner is asked again once it clears. */
        if (phase == TC_DISKD_READY || phase == TC_DISKD_STARTING) {
            phase = TC_DISKD_STARTING;
            s->probe_token++;
            s->probing = 0;
            s->probe_at = 0;
        }
        set_state(s, phase, TC_DISKD_CONFLICT, now);
        return actions;
    }
    if (phase == TC_DISKD_LAUNCHING && !o->launch)
        phase = o->owners ? owner_phase(s, o) : TC_DISKD_INSPECT;
    else if (phase == TC_DISKD_INSPECT && !o->launch) {
        if (o->owners)
            phase = owner_phase(s, o);
        else if (o->disk_work)
            why = TC_DISKD_MAINTENANCE;
        else if (o->mounts)
            /* A new diskd does not adopt a mount, and never retries a device
             * whose first mount failed (README F17). -1: cannot tell. */
            why = o->mounts > 0 ? TC_DISKD_RESIDUAL_MOUNTS : TC_DISKD_OBSERVATION;
        else if (now < s->retry_at)
            why = TC_DISKD_RELAUNCH_WAIT;
        else {
            phase = TC_DISKD_LAUNCHING;
            actions |= TC_DISKD_LAUNCH;
        }
    }
    /* Entered from another phase, or again after the last owner retired. */
    if (phase == TC_DISKD_QUIESCE && s->phase != TC_DISKD_QUIESCE)
        actions |= TC_DISKD_ENTER_QUIESCE;
    if (phase == TC_DISKD_QUIESCE && o->drained) {
        /* A TERM before diskd is back in its event loop, or while an acp call
         * may be forwarded to it, can leave its unregisters and an ACPd
         * forward waiting on each other for good (F19). */
        if (o->disk_work)
            why = TC_DISKD_MAINTENANCE;
        else if (o->acp_clients || !o->owner_idle || !o->fresh)
            why = TC_DISKD_BUSY;
        else {
            phase = TC_DISKD_STOPPING;
            actions |= TC_DISKD_TERM;
        }
    }
    if (phase == TC_DISKD_STOPPING && o->disk_work)
        why = TC_DISKD_MAINTENANCE; /* its TERM waits for the tool to return */
    if (phase == TC_DISKD_STARTING) {
        /* Disk work explains a slow or silent start (the initial scan
         * checks disks before RPCs are dispatched, README F18). */
        if (o->disk_work)
            why = TC_DISKD_MAINTENANCE;
        else if (s->no_answers >= 2)
            why = TC_DISKD_NO_ANSWER;
        if (!s->probing && now >= s->probe_at) {
            s->probing = 1;
            actions |= TC_DISKD_PROBE;
        }
    }
    set_state(s, phase, why, now);
    return actions;
}
void tc_diskd_launch_exited(struct tc_diskd *s, long long now) {
    s->retry_at = backoff(&s->launch_failures, now);
    if (s->phase == TC_DISKD_LAUNCHING)
        set_state(s, TC_DISKD_INSPECT, TC_DISKD_RELAUNCH_WAIT, now);
}
unsigned tc_diskd_owner_exited(struct tc_diskd *s, struct tc_diskd_identity owner, long long now) {
    if (!owner.pid || owner.pid != s->owner.pid || owner.born != s->owner.born)
        return 0;
    return retire(s, now);
}
void tc_diskd_probed(struct tc_diskd *s, unsigned token, int answered, long long now) {
    if (token != s->probe_token)
        return;
    s->probing = 0;
    if (s->phase != TC_DISKD_STARTING)
        return;
    if (answered) {
        s->no_answers = s->launch_failures = 0;
        s->retry_at = s->probe_at = 0;
        set_state(s, TC_DISKD_READY, TC_DISKD_NONE, now);
        return;
    }
    /* The next step sets the reason: a disk tool explains the silence first. */
    s->no_answers++;
    s->probe_at = now + TC_STORAGE_RETRY_MS;
}
int tc_diskd_ready(const struct tc_diskd *s) {
    return s->phase == TC_DISKD_READY;
}
int tc_diskd_word(const struct tc_diskd *s, char *out, size_t size) {
    int n = snprintf(out, size, "%s:%lld:%s", tc_diskd_phase_name(s->phase), s->since / 1000,
                     tc_diskd_reason_name(s->reason));
    if (s->held_ms >= 1000 && n >= 0 && (size_t)n < size)
        n += snprintf(out + n, size - (size_t)n, " held=%lld", s->held_ms / 1000);
    return n;
}
const char *tc_diskd_phase_name(enum tc_diskd_phase p) {
    static const char *const names[] = {"inspect", "quiesce", "stopping", "launching", "starting", "ready"};
    return names[p];
}
const char *tc_diskd_reason_name(enum tc_diskd_reason r) {
    static const char *const names[] = {"none",          "maintenance", "residual_mounts", "conflict", "no_answer",
                                        "relaunch_wait", "observation", "busy"};
    return names[r];
}
