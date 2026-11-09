#ifndef TC_PROCFACTS_H
#define TC_PROCFACTS_H
#include "proctable.h"
#include "inspect.h"

/* What the manager reads from one kernel process table, in one place and as
 * one pure function: README "Disk owner handoff", Observation, and the
 * unmounted clock. Command lines are not read here (that can block); the
 * audit (inspect.c) reads them in a job. */

/* The stock owner's kevent sleep, in the kernel's p_slptime, before its TERM.
 * The kernel adds one each second to every sleeping thread, the first time as
 * soon as the next second starts, so 3 means asleep for more than 2 s. */
#define TC_DISKD_IDLE_SECONDS 3

/* The manager's own state the facts depend on, as it is when they are used:
 * a launch child can be reaped, and discovery restarted, within a pass. */
struct tc_proc_context {
    pid_t discovery;                  /* our discovery, 0 when none runs */
    pid_t launch;                     /* our diskd launch child, 0 when none */
    struct tc_proc_identity current; /* the handoff's owner */
};
struct tc_proc_facts {
    int owners;                       /* diskd rows whose parent is not a diskd row */
    struct tc_proc_identity owner;   /* the last of them; the one when owners == 1 */
    int current_present;              /* the context's current owner is among them */
    unsigned long long signature;     /* changes whenever the set of owners does */
    /* The single owner asleep in kevent (wmesg kqread on NetBSD 4, kqueue on
     * NetBSD 6) with p_slptime of at least TC_DISKD_IDLE_SECONDS, and no
     * child. Its disk scans run outside that sleep and the sleep time
     * restarts on every wakeup (F19). Whether it is stock is the audit's. */
    int owner_idle;
    /* Our launch child once its row shows diskd and its parent is not a
     * diskd: it has exec'd and is an owner. {0} until then. */
    struct tc_proc_identity launch_execd;
    /* A disk tool (fsck*, newfs*, mount, mount_hfs, umount, gpt) anywhere, or
     * a shell a diskd started to run one: holds the handoff (`maintenance`). */
    int disk_work;
    /* fsck_hfs or newfs_hfs only: a check or format, which pauses the
     * unmounted clock. Narrower than disk_work on purpose: the quick check
     * after each claim and a long repair are no hang; a mount is. */
    int disk_repair;
    /* acp CLI processes. ACPd forwards a MaSt read or a diskd.* call from one
     * to diskd and holds it in flight until diskd answers (F19). */
    int acp_clients;
    /* Apple's wcifsfs, or a wcifsnd that is not our discovery's child. ACPd
     * starts them after the last audit, so the drain checks each table. */
    int foreign_netbios;
};
void tc_proc_facts(const struct tc_proctable *, const struct tc_proc_context *, struct tc_proc_facts *);
/* A row named diskd. */
int tc_proc_diskd(const struct tc_proc *);
#endif
