/* What the manager reads from one kernel process table (service/procfacts.c):
 * README "Disk owner handoff", Observation, and the unmounted clock. */
#include "service/procfacts.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static struct tc_proc row(pid_t pid, pid_t parent, const char *comm, const char *wmesg, unsigned slept) {
    struct tc_proc p;
    memset(&p, 0, sizeof(p));
    p.pid = pid;
    p.parent = parent;
    p.group = pid;
    p.stat = TC_LSSLEEP;
    p.slept = slept;
    p.born = (unsigned long long)pid * 10;
    snprintf(p.comm, sizeof(p.comm), "%s", comm);
    snprintf(p.wmesg, sizeof(p.wmesg), "%s", wmesg);
    return p;
}
static struct tc_proc_identity who(pid_t pid) {
    return (struct tc_proc_identity){pid, (unsigned long long)pid * 10};
}
static struct tc_proctable t;
static void set(const struct tc_proc *rows, size_t count) {
    t.count = count;
    memcpy(t.procs, rows, count * sizeof(rows[0]));
}
static struct tc_proc_facts facts(pid_t discovery, pid_t launch, struct tc_proc_identity current) {
    struct tc_proc_context c = {discovery, launch, current};
    struct tc_proc_facts f;
    memset(&f, 0x5a, sizeof(f));
    tc_proc_facts(&t, &c, &f);
    return f;
}

static void owners_and_helpers(void) {
    struct tc_proc zombie = row(60, 1, "diskd", "", 0);
    zombie.stat = TC_LSZOMB;
    const struct tc_proc rows[] = {
        row(1, 0, "init", "wait", 9),
        row(229, 190, "diskd", "kqread", 4),  /* Apple's: ACPd's child */
        row(230, 229, "diskd", "-", 0),       /* forked, not exec'd: a helper */
        row(231, 229, "sh", "wait", 0),       /* the shell its tool runs in */
        row(40, 1, "sh", "wait", 0),          /* someone else's shell */
        row(41, 1, "acp", "netio", 0),
        row(42, 1, "acp", "netio", 0),
        zombie,
    };
    set(rows, sizeof(rows) / sizeof(rows[0]));
    struct tc_proc_facts f = facts(0, 0, who(229));
    assert(f.owners == 1 && f.owner.pid == 229 && f.owner.born == 2290 && f.current_present);
    assert(f.disk_work && !f.disk_repair && f.acp_clients == 2 && !f.foreign_netbios && !f.launch_execd.pid);
    unsigned long long one_owner = f.signature;
    /* A reused PID is not the current owner. */
    assert(!facts(0, 0, (struct tc_proc_identity){229, 1}).current_present);
    /* Disk tools anywhere; fsck_hfs and newfs_hfs are also repair. */
    t.procs[3] = row(231, 1, "fsck_hfs", "biowait", 0);
    f = facts(0, 0, who(229));
    assert(f.disk_work && f.disk_repair && f.signature == one_owner);
    t.procs[3] = row(231, 1, "newfs_hfs", "biowait", 0);
    assert(facts(0, 0, who(229)).disk_repair);
    const char *work_only[] = {"fsck_msdos", "mount_hfs", "mount", "umount", "gpt", "newfs_msdos"};
    for (size_t i = 0; i < sizeof(work_only) / sizeof(work_only[0]); i++) {
        t.procs[3] = row(231, 1, work_only[i], "biowait", 0);
        f = facts(0, 0, who(229));
        assert(f.disk_work && f.disk_repair == !strcmp(work_only[i], "fsck_hfs"));
    }
    /* Without them, nothing; a second owner changes the signature. */
    t.procs[2] = row(232, 1, "diskd", "kqread", 0);
    t.procs[3] = row(231, 1, "sh", "wait", 0);
    f = facts(0, 0, (struct tc_proc_identity){0, 0});
    assert(f.owners == 2 && !f.disk_work && !f.current_present && f.signature != one_owner && !f.owner_idle);
}
static void our_launch_child(void) {
    /* Before its exec it is our child named service (or still diskd's
     * fork); once its row shows diskd under the manager, it is an owner. */
    const struct tc_proc before[] = {row(500, 1, "service", "select", 0), row(501, 500, "service", "-", 0)};
    set(before, 2);
    assert(!facts(0, 501, who(0)).launch_execd.pid);
    const struct tc_proc after[] = {row(500, 1, "service", "select", 0), row(501, 500, "diskd", "kqread", 0)};
    set(after, 2);
    struct tc_proc_facts f = facts(0, 501, who(0));
    assert(f.launch_execd.pid == 501 && f.launch_execd.born == 5010 && f.owners == 1);
    /* No launch child: nothing to report, even with the same PID present. */
    assert(!facts(0, 0, who(0)).launch_execd.pid);
}
static void foreign_netbios(void) {
    const struct tc_proc ours[] = {row(700, 1, "service", "select", 0), row(701, 700, "wcifsnd", "select", 0)};
    set(ours, 2);
    assert(!facts(700, 0, who(0)).foreign_netbios);
    /* Not our discovery's child, or no discovery runs. */
    assert(facts(699, 0, who(0)).foreign_netbios && facts(0, 0, who(0)).foreign_netbios);
    const struct tc_proc apple[] = {row(190, 1, "ACPd", "kqread", 9), row(702, 190, "wcifsfs", "select", 0)};
    set(apple, 2);
    assert(facts(700, 0, who(0)).foreign_netbios);
    t.procs[1].stat = TC_LSZOMB; /* exited, not reaped */
    assert(!facts(700, 0, who(0)).foreign_netbios);
}
static void owner_idle_rule(void) {
    const struct tc_proc rows[] = {row(229, 190, "diskd", "kqread", 3), row(41, 1, "sh", "wait", 0)};
    set(rows, 2);
    /* Asleep in kevent with p_slptime 3 and no child; NetBSD 6 names it kqueue. */
    assert(facts(0, 0, who(229)).owner_idle);
    t.procs[0] = row(229, 190, "diskd", "kqueue", 7);
    assert(facts(0, 0, who(229)).owner_idle);
    /* p_slptime 2 can be just over a second: a scan may have just ended. */
    t.procs[0] = row(229, 190, "diskd", "kqread", 2);
    assert(!facts(0, 0, who(229)).owner_idle);
    t.procs[0] = row(229, 190, "diskd", "wait", 30); /* its scan waits on a tool */
    assert(!facts(0, 0, who(229)).owner_idle);
    t.procs[0] = row(229, 190, "diskd", "netio", 30); /* its own request to ACPd */
    assert(!facts(0, 0, who(229)).owner_idle);
    t.procs[0] = row(229, 190, "diskd", "kqread", 3);
    t.procs[1] = row(41, 229, "diskd", "-", 0); /* a forked child, before its exec */
    assert(!facts(0, 0, who(229)).owner_idle);
    /* A child that exited and is not reaped yet is no work. */
    t.procs[1].stat = TC_LSZOMB;
    assert(facts(0, 0, who(229)).owner_idle);
}

int main(void) {
    owners_and_helpers();
    our_launch_child();
    foreign_netbios();
    owner_idle_rule();
    return 0;
}
