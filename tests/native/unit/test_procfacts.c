/* What the manager reads from one kernel process table (service/procfacts.c):
 * README "Disk owner handoff", Observation, and the unmounted clock. */
#include "service/procfacts.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
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

static const char *line(struct tc_proc_identity owner, pid_t skip_group) {
    static char out[512];
    int n = tc_proc_handoff_line(&t, owner, skip_group, out, sizeof(out));
    assert(n > 0 && (size_t)n < sizeof(out));
    return out;
}
static struct tc_proc dead(pid_t pid, pid_t parent, const char *comm) {
    struct tc_proc p = row(pid, parent, comm, "", 0);
    p.stat = TC_LSZOMB;
    return p;
}
static void handoff_line_rows(void) {
    struct tc_proc diskd = row(229, 1, "diskd", "kqread", 4), acpd = row(190, 1, "ACPd", "netio", 48);
    diskd.cpu_us = 23456;
    acpd.cpu_us = 1111000;
    struct tc_proc acp_ours = row(800, 700, "acp", "select", 0), acp_user = row(801, 1, "acp", "select", 0);
    acp_ours.group = 700;
    const struct tc_proc rows[] = {
        diskd, acpd,
        dead(367, 190, "wcifsfs"),      /* ACPd has not reaped it */
        row(498, 190, "wcifsnd", "select", 2),
        row(402, 190, "sshd", "select", 9), /* counted only */
        row(403, 190, "sshd", "select", 9),
        acp_ours, acp_user, dead(802, 1, "acp"),
    };
    set(rows, sizeof(rows) / sizeof(rows[0]));
    /* The job's own acp (group 700) and a zombie acp are not clients. */
    assert(!strcmp(line(who(229), 700),
                   "diskd=229:3:kqread:4:23 acpd=190:3:netio:48:1111 "
                   "acpd_children=wcifsfs:367:Z,wcifsnd:498,+2 acp=1"));
    assert(strstr(line(who(229), 0), " acp=2"));
    /* A reused PID is another process, and no PID is no owner. */
    assert(!strncmp(line((struct tc_proc_identity){229, 1}, 0), "diskd=none ", 11));
    assert(!strncmp(line((struct tc_proc_identity){0, 0}, 0), "diskd=none ", 11));
}
static void handoff_line_picks_the_main_acpd(void) {
    /* The restart hang's child ACPd may come first in the table. */
    const struct tc_proc rows[] = {
        row(150, 190, "ACPd", "nanoslee", 30), row(190, 1, "ACPd", "netio", 5), row(229, 1, "diskd", "netio", 9),
    };
    set(rows, 3);
    assert(!strcmp(line(who(229), 0), "diskd=229:3:netio:9:0 acpd=190:3:netio:5:0 acpd_children=ACPd:150 acp=0"));
    /* No ACPd at all. */
    set(rows + 2, 1);
    assert(!strcmp(line(who(229), 0), "diskd=229:3:netio:9:0 acpd=none acpd_children=none acp=0"));
}
static void handoff_line_long_child_list(void) {
    static struct tc_proc rows[64];
    size_t i;
    rows[0] = row(190, 1, "ACPd", "netio", 5);
    for (i = 1; i < 64; i++)
        rows[i] = dead((pid_t)(1000 + i), 190, "sh");
    set(rows, 64);
    const char *out = line(who(229), 0), *children = strstr(out, "acpd_children=");
    const char *plus = strrchr(out, '+');
    /* Whole entries only, then the count of the rest; the line goes on. */
    assert(children && plus && strstr(out, " acp=0"));
    int listed = 0;
    for (const char *p = children; (p = strstr(p, "sh:")); p++)
        listed++;
    assert(listed > 0 && listed < 63 && listed + atoi(plus + 1) == 63);
    assert(plus[-1] == ',' && strstr(plus - 6, ":Z,+"));
    /* A buffer too small is cut like snprintf, and says how much it needed. */
    char small[16];
    assert(tc_proc_handoff_line(&t, who(229), 0, small, sizeof(small)) >= (int)sizeof(small));
    assert(strlen(small) == sizeof(small) - 1);
}
int main(void) {
    owners_and_helpers();
    our_launch_child();
    foreign_netbios();
    owner_idle_rule();
    handoff_line_rows();
    handoff_line_picks_the_main_acpd();
    handoff_line_long_child_list();
    return 0;
}
