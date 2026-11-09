#ifndef TC_DISKD_H
#define TC_DISKD_H
#include <sys/types.h>

#include "proctable.h"

/* Hands the disks from Apple's stock diskd to the manager's loopback diskd.
 * Apple's diskd handles SIGTERM in its event loop, after any tool it started
 * returns: it unregisters its RPC names, then unmounts its volumes. A TERM
 * that arrives before diskd is back in that loop can deadlock its unregisters
 * with ACPd for good (README F19), so the manager sends TERM once, only while
 * the owner idles in the loop, never SIGKILL, and never takes over because a
 * timer expired. This machine does no I/O: the manager gives it one
 * observation per pass and carries out the actions it returns. The phase
 * table is "Disk owner handoff" in ../README.md. */
enum tc_diskd_phase {
    TC_DISKD_INSPECT,
    TC_DISKD_QUIESCE,
    TC_DISKD_STOPPING,
    TC_DISKD_LAUNCHING,
    TC_DISKD_STARTING,
    TC_DISKD_READY
};
/* Why the phase is held back. Each name claims only what is listed here. */
enum tc_diskd_reason {
    TC_DISKD_NONE,            /* nothing holds the phase back */
    TC_DISKD_MAINTENANCE,     /* a disk tool, or a shell a diskd started, runs */
    TC_DISKD_RESIDUAL_MOUNTS, /* no diskd runs and an HFS data volume is still mounted */
    TC_DISKD_CONFLICT,        /* two or more independent diskd run */
    /* The last two readiness probes in a row got no answer: acp exited
     * non-zero (any error, -6727 "not registered" included), ran past its
     * limit or was killed, or the manager could not start the probe. Which
     * one is in the manager log, not here. */
    TC_DISKD_NO_ANSWER,
    /* No diskd runs and the next launch waits out its backoff: the last
     * launch exited before exec, or our loopback diskd exited. */
    TC_DISKD_RELAUNCH_WAIT,
    /* This pass could not tell: no process table, an unreadable mount list,
     * or one owner whose role no audit has read yet. */
    TC_DISKD_OBSERVATION,
    /* The stock owner is not idle in its event loop in a table read this
     * pass (owner_idle, fresh below), or an acp client runs (acp_clients), so
     * its TERM waits (F19). */
    TC_DISKD_BUSY
};
/* The stock owner's kevent sleep, in the kernel's p_slptime, before its TERM.
 * The kernel adds one each second to every sleeping thread, the first time as
 * soon as the next second starts, so 3 means asleep for more than 2 s. */
#define TC_DISKD_IDLE_SECONDS 3
/* PID and kernel start time: a reused PID is another owner. */
struct tc_diskd_identity {
    pid_t pid;
    unsigned long long born;
};
struct tc_diskd_observation {
    int kernel_valid;    /* this pass read the kernel process table */
    int valid;           /* the owner's role is known: always with 0 or 2+ owners */
    int owners;          /* diskd rows whose parent is not a diskd row */
    struct tc_diskd_identity owner; /* when owners == 1 */
    int loopback;        /* that owner's role from its argv */
    int current_present; /* the machine's current owner is among the owners */
    int launch;          /* our launch child has not exec'd yet */
    int disk_work;       /* a disk tool, or a shell diskd started */
    int mounts;          /* HFS data volumes mounted: 1, 0, or -1 unreadable */
    int drained;         /* nothing of ours or foreign holds a file on the volumes (manager.c observe_diskd) */
    int owner_idle; /* the single stock owner may take its TERM (tc_diskd_owner_idle) */
    /* The kernel table was read this pass. Passes come sooner than table
     * reads (a child's exit, an audit), and a TERM must not follow an idle
     * seen up to a read interval ago: a scan or forwarded call can have begun
     * since (F19). */
    int fresh;
    /* acp CLI processes running. ACPd forwards a MaSt read or a diskd.* call
     * from one to diskd and holds it in flight until diskd answers; a TERM
     * then can deadlock the same way (F19). Mostly our own short `acp -q`
     * reads of ACPd's properties, which cost a pass at most. */
    int acp_clients;
};
/* ENTER_QUIESCE: the machine just entered `quiesce`. The manager then stops what
 * of its own holds the volumes, once; nothing restarts it outside `ready`. */
enum tc_diskd_action {
    TC_DISKD_TERM = 1,
    TC_DISKD_LAUNCH = 2,
    TC_DISKD_PROBE = 4,
    TC_DISKD_RETIRE = 8,
    TC_DISKD_ENTER_QUIESCE = 16
};
struct tc_diskd {
    enum tc_diskd_phase phase;
    enum tc_diskd_reason reason;
    long long since; /* when the phase or the reason last changed */
    long long held_ms; /* how long `busy` held a TERM back, spells that ended */
    struct tc_diskd_identity owner;
    unsigned generation; /* advances whenever an owner is retired */
    unsigned probe_token; /* a probe's reply counts only for this token */
    int probing;
    long long retry_at, probe_at;
    unsigned launch_failures, no_answers;
};
void tc_diskd_init(struct tc_diskd *, long long now);
unsigned tc_diskd_step(struct tc_diskd *, const struct tc_diskd_observation *, long long now);
/* Our launch child exited before it became the owner. */
void tc_diskd_launch_exited(struct tc_diskd *, long long now);
/* Our exec'd child was reaped: when it is the owner, retire it now rather
 * than at the next process sample. Returns TC_DISKD_RETIRE or 0. */
unsigned tc_diskd_owner_exited(struct tc_diskd *, struct tc_diskd_identity, long long now);
/* A probe started when probe_token had this value; a reply under an older
 * token (owner retired, or a conflict since) is ignored. answered: acp exited
 * 0, so diskd dispatched the call (a handler error still counts). 0: any
 * other end, including a probe the manager could not start. */
void tc_diskd_probed(struct tc_diskd *, unsigned token, int answered, long long now);
int tc_diskd_ready(const struct tc_diskd *);

/* What one pass's kernel process table says about diskd, before the audit
 * and the manager's own state: README "Disk owner handoff", Observation. */
struct tc_diskd_scan {
    int owners;                     /* diskd rows whose parent is not a diskd row */
    struct tc_diskd_identity owner; /* the last of them; the one when owners == 1 */
    int current_present;            /* `current` is among them */
    unsigned long long signature;   /* changes whenever the set of owners does */
    int disk_work;                  /* a disk tool, or a shell a diskd started */
    int acp_clients;                /* acp CLI processes */
};
void tc_diskd_scan(const struct tc_proctable *, struct tc_diskd_identity current, struct tc_diskd_scan *);
/* Whether stock owner PID may take its TERM (F19): asleep in kevent (wmesg
 * kqread on NetBSD 4, kqueue on NetBSD 6) with p_slptime of at least
 * TC_DISKD_IDLE_SECONDS, and no child. Its disk scans run outside that sleep
 * and the sleep time restarts on every wakeup, so it has had nothing to do
 * for that long. */
int tc_diskd_owner_idle(const struct tc_proctable *, pid_t);
/* A row named diskd. */
int tc_diskd_row(const struct tc_proc *);
/* The title's handoff word, PHASE:SINCE:REASON, then " held=S" once `busy`
 * has held a TERM back for a second or more (spells that ended). Cut to fit
 * like snprintf, and like it returns size or more when cut. */
int tc_diskd_word(const struct tc_diskd *, char *out, size_t size);
const char *tc_diskd_phase_name(enum tc_diskd_phase);
const char *tc_diskd_reason_name(enum tc_diskd_reason);
#endif
