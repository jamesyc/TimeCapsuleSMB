#ifndef TC_PROCTABLE_H
#define TC_PROCTABLE_H
#include "../common/platform.h"

/* The device's process table, read once per manager pass and shared by
 * buffer-stall recovery, stuck-process detection and the audit.
 *
 * tc_proctable_read() and tc_proctable_threads() read kernel process and
 * thread structures only (KERN_PROC2, KERN_LWP): they never touch another
 * process's memory, so the manager can call them while processes are stuck.
 * tc_proctable_argv() reads the target's memory (KERN_PROC_ARGS, uvm_io) and
 * blocks if that process holds its map stuck on the disk: only jobs call it.
 *
 * Host test builds read the file named by TC_TEST_PROCS instead, one line per
 * process:
 *     [live] PID PPID GROUP STAT FLAG SLEPT CPU_US WMESG COMM [ARGV...]
 * and one per thread of a multi-threaded process:
 *     lwp PID LID STAT FLAG SLEPT WMESG
 * STAT and FLAG are the kernel's values; WMESG is "-" for none. A "live" row
 * is dropped once PID no longer exists, as the kernel drops an exited one. */

/* Room for every process: kern.maxproc is 84 on both kernels. */
#define TC_PROCESS_MAX 128
#define TC_PROC_WMESG 9 /* kernel wait messages are 8 bytes, not NUL-terminated */
#define TC_PROC_COMM 17
/* The kernel's values on NetBSD 4 and 6 (sys/lwp.h, sys/proc.h). */
#define TC_LSSLEEP 3
#define TC_LSZOMB 5
#define TC_LSDEAD 6
#define TC_L_SINTR 0x80
#define TC_P_SYSTEM 0x200

struct tc_proc {
    pid_t pid, parent, group;
    int stat, flag, threads;
    unsigned slept;            /* seconds in this sleep: the kernel's l_slptime */
    unsigned long long cpu_us; /* CPU time so far */
    char wmesg[TC_PROC_WMESG], comm[TC_PROC_COMM];
};
struct tc_proctable {
    size_t count;
    struct tc_proc procs[TC_PROCESS_MAX];
};
struct tc_proc_thread {
    int lid, stat, flag;
    unsigned slept;
    char wmesg[TC_PROC_WMESG];
};
#define TC_THREAD_MAX 128

int tc_proctable_read(struct tc_proctable *);
/* The threads of a multi-threaded process; returns the count, or -1 when the
 * process is gone. */
int tc_proctable_threads(pid_t, struct tc_proc_thread *out, size_t max);
/* PID's command line, arguments joined by single spaces and truncated to
 * fit. Returns 0, 1 when PID has exited or become a zombie, or -1 on any
 * other failure. May block: jobs only. */
int tc_proctable_argv(pid_t, char *out, size_t size);
/* A zombie, or dying. */
int tc_proc_exited(const struct tc_proc *);
#endif
