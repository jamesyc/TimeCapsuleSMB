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
/* The rows the handoff's TERM and watch lines show (README "Disk owner
 * handoff"): "diskd=ROW acpd=ROW acpd_children=LIST acp=N". A ROW is
 * PID:STAT:WMESG:SLEPT:CPU_MS, or "none" when the process is gone. The ACPd
 * is the main one: a child ACPd under ACPd (the restart hang's) gives its
 * parent. LIST names ACPd's children that matter here, NAME:PID with ":Z"
 * for one ACPd has not reaped (exited ones, wcifsfs, wcifsnd, ACPd, diskd),
 * then "+N" for the rest (sshd and other daemons), or is "none". acp counts
 * live acp clients outside skip_group (the caller's own, 0 for none).
 * Returns like snprintf. */
int tc_proc_handoff_line(const struct tc_proctable *, struct tc_proc_identity owner, pid_t skip_group, char *out,
                         size_t size);
/* A row named diskd. */
int tc_proc_diskd(const struct tc_proc *);
/* The main ACPd: the first live row named ACPd, or its parent when that is
 * an ACPd too (the restart hang's child ACPd may come first). NULL: none. */
const struct tc_proc *tc_proc_main_acpd(const struct tc_proctable *);

/* Whether Apple's ACPd has been quiet, from its CPU time across the
 * manager's table reads (README "Disk owner handoff", F19). Its main thread
 * calls diskd synchronously, and a stock diskd TERMed meanwhile deadlocks
 * with it. An ACPd that has used almost no CPU for a while is not working on
 * such a call; one it waits in without using CPU passes all the same.
 * Updated only where the table is read, so it always describes that table.
 * Two states: no ACPd in the last table (acpd.pid 0), or tracking one. Host
 * tests shorten these. */
#ifndef TC_ACPD_QUIET_MS
#define TC_ACPD_QUIET_MS 3000 /* quiet this long before a TERM */
#endif
#ifndef TC_ACPD_QUIET_BUDGET_US
/* CPU per second between table reads that still counts as quiet. One ACP
 * read costs ACPd 1-2 ms (our own `acp -q` reads too), so most reads start
 * the stretch again; a lone local one can fit in the allowance of a 2 s gap.
 * Its idle timers cost about 10 ms every few seconds. */
#define TC_ACPD_QUIET_BUDGET_US 1000
#endif
#ifndef TC_ACPD_QUIET_GAP_MS
/* Reads further apart than this start over: tables come 1-2 s apart, and a
 * burst during a longer gap cannot be told from one just before the read. */
#define TC_ACPD_QUIET_GAP_MS 3500
#endif
struct tc_acpd_quiet {
    struct tc_proc_identity acpd; /* {0}: no ACPd in the last table */
    unsigned long long cpu_us;    /* its CPU time in the last table */
    long long seen_at;            /* when that table was read */
    long long since;              /* start of the current quiet stretch */
};
void tc_acpd_quiet_init(struct tc_acpd_quiet *);
/* A table read at now: the first rule that matches wins. No main ACPd: none.
 * Another ACPd (pid or start time), or one after none: since = now, as a
 * first look proves nothing. Its row runnable or on the CPU, the read more
 * than TC_ACPD_QUIET_GAP_MS after the last, CPU time gone back, or more than
 * TC_ACPD_QUIET_BUDGET_US of it per second since the last read: since = now.
 * A table the manager could not read changes nothing, and the next read's
 * gap rule covers it, so a gap never makes ACPd look quieter than it was. */
void tc_acpd_quiet_update(struct tc_acpd_quiet *, const struct tc_proctable *, long long now);
/* How long ACPd has been quiet at now, -1 with no ACPd. */
long long tc_acpd_quiet_ms(const struct tc_acpd_quiet *, long long now);
#endif
