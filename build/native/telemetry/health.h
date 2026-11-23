#ifndef TC_TELEMETRY_HEALTH_H
#define TC_TELEMETRY_HEALTH_H
#include "telemetry.h"
#include "errclass.h"
#include "../service/proctable.h"

/* Device resource health for heartbeats, in three layers:
 *
 *   readers (health_read.c)   one sample of kernel counters, the process
 *                             table and the kernel message buffer, and one
 *                             round of loopback probes of sshd and smbd.
 *                             I/O only: no state, no fork, no ps, nothing
 *                             that reads another process's memory or
 *                             command line (KERN_PROC_ARGS takes the
 *                             target's map lock; see service/proctable.h).
 *   detectors (health.c)      pure state machines over those readings:
 *                             threshold (process and file tables),
 *                             probe_track (per probe target), kmsg_scan
 *                             (new kernel messages); each documented beside
 *                             its code. They note events in the ledger.
 *   JSON (health_json.c)      the health object, from the state.
 *
 * The ledger keeps two sets of event bits: news (everything since the last
 * delivered heartbeat) and pending (the requesting events among them, which
 * ask the planner for an extra heartbeat). The JSON writer returns the bits
 * it wrote as a token; only a delivered heartbeat clears them, so an event
 * noted while a heartbeat is in flight goes in the next one.
 *
 * Everything lives in the caller's struct tc_health, in process memory only:
 * the outage usually ends with an unplug that loses it, which is why the
 * first crossing toward a limit asks for a heartbeat at once. */

#ifndef TC_HEALTH_SAMPLE_SECONDS
#define TC_HEALTH_SAMPLE_SECONDS 15
#endif
/* Probe rounds, in the daemon; one-shot modes run one. */
#ifndef TC_HEALTH_PROBE_SECONDS
#define TC_HEALTH_PROBE_SECONDS 300
#endif
#ifndef TC_HEALTH_SSH_PORT
#define TC_HEALTH_SSH_PORT 22
#endif
#ifndef TC_HEALTH_SMB_PORT
#define TC_HEALTH_SMB_PORT 445
#endif
#ifndef TC_HEALTH_PROBE_CONNECT_MS
#define TC_HEALTH_PROBE_CONNECT_MS 2000
#endif
#ifndef TC_HEALTH_PROBE_ANSWER_MS
#define TC_HEALTH_PROBE_ANSWER_MS 3000
#endif
/* A round probes IPv4 (sshd and smbd at once), then smbd over IPv6: at most
 * one probe per service in flight, so a round adds at most two processes to
 * the device's table (an sshd and an smbd child), and takes at most this
 * long. sshd is probed over IPv4 only, an exception to dual stack the
 * maintainer approved: sshd logs every probe, and ACPd keeps those lines in
 * its log (acp logm, AirPort Utility's log view), and one family shows a
 * wedged sshd as well as two. */
#define TC_HEALTH_PROBE_ROUND_MAX_MS (2 * (TC_HEALTH_PROBE_CONNECT_MS + TC_HEALTH_PROBE_ANSWER_MS))
/* High at kern.maxproc - 10 processes, rearmed at kern.maxproc - 20; files
 * at 90 % and 80 % of kern.maxfiles; near full within 2 of kern.maxproc. */
#define TC_HEALTH_PROC_HIGH_MARGIN 10
#define TC_HEALTH_PROC_REARM_MARGIN 20
#define TC_HEALTH_PROC_FULL_MARGIN 2
/* A crossing needs this many samples in a row past the mark. */
#define TC_HEALTH_HIGH_SAMPLES 2
/* A probe triggers at this many closed or down results in a row, or this
 * many failures of any kind (silent included) in a row. */
#define TC_HEALTH_PROBE_HARD_FAILURES 2
#define TC_HEALTH_PROBE_ANY_FAILURES 3
/* The largest health object (test_health.c "worst"); telemetry.h budgets
 * the payload with it. */
#define TC_HEALTH_JSON_MAX 5632

/* The events, by bit. Requesting events also ask for an extra heartbeat;
 * the rest ride along: RAM and mbufs until heartbeats show what is normal
 * for them, fork, socket and acp failures (the planner's retries handle
 * those), kernel warnings that are not a full table, and messages already
 * in the buffer when this process first read it. */
enum tc_health_event {
    TC_EV_PROCS = 1u << 0, TC_EV_FILES = 1u << 1, TC_EV_KMSG = 1u << 2, TC_EV_PROBE = 1u << 3,
    TC_EV_KMSG_WARNING = 1u << 4, TC_EV_RAM = 1u << 5, TC_EV_MBUF = 1u << 6, TC_EV_FORK = 1u << 7,
    TC_EV_SOCKET = 1u << 8, TC_EV_ACP_TIMEOUT = 1u << 9, TC_EV_KMSG_AT_START = 1u << 10
};
#define TC_EV_COUNT 11
#define TC_EV_REQUESTING (TC_EV_PROCS | TC_EV_FILES | TC_EV_KMSG | TC_EV_PROBE)

/* ---- readings ---- */
#define TC_TOP_GROUPS 8
#define TC_TOP_WAITS 6
#define TC_TOP_LARGEST 3
#define TC_TOP_FDS 3

/* Kernel counters; -1 (or have_* 0) when the read failed. */
struct tc_counters {
    int maxproc, maxfiles, files, nmbclusters;
    int have_mem, have_mbuf;
    long long pagesize, free, active, inactive, freetarg, anon, file, fltnoram, pdscans;
    unsigned long mbuf_drops, mbuf_waits;
};
struct tc_proc_group { char comm[TC_PROC_COMM]; unsigned depth, count; };
struct tc_wait_count { char wmesg[TC_PROC_WMESG]; unsigned count; };
struct tc_proc_large { char comm[TC_PROC_COMM]; unsigned rss_pages; };
struct tc_proc_fds { char comm[TC_PROC_COMM]; unsigned fds; };
/* One look at the process table, small enough to keep three copies of. */
struct tc_snapshot {
    long at;
    int read;          /* the table was read */
    unsigned procs;    /* every row: what kern.maxproc counts */
    unsigned system;   /* kernel threads (processes on NetBSD 4) */
    unsigned zombies;
    unsigned blocked;  /* uninterruptible sleep */
    int smbd_children; /* children of smbd's listener; -1 with no smbd */
    int sshd_up, smbd_up; /* a live listener: a daemon whose parent is not the same daemon */
    size_t groups, waits, largest, most_fds;
    struct tc_proc_group group[TC_TOP_GROUPS];
    struct tc_wait_count wait[TC_TOP_WAITS];
    struct tc_proc_large large[TC_TOP_LARGEST];
    struct tc_proc_fds fd[TC_TOP_FDS];
};
struct tc_reading {
    struct tc_counters counters;
    struct tc_snapshot snapshot;
    const char *kmsg;  /* the message buffer with NULs dropped, oldest first */
    long kmsg_len;     /* -1 when it could not be read */
};

enum tc_probe_result { TC_PROBE_SKIPPED, TC_PROBE_OK, TC_PROBE_CLOSED, TC_PROBE_SILENT, TC_PROBE_DOWN, TC_PROBE_ABSENT };
enum { TC_PROBE_SSH4, TC_PROBE_SMB4, TC_PROBE_SMB6, TC_PROBES };
struct tc_probe_round { enum tc_probe_result result[TC_PROBES]; };

/* Readers (health_read.c). The kmsg text stays valid until the next call. */
void tc_health_read_sample(struct tc_reading *out, long now);
void tc_health_read_probes(struct tc_probe_round *out);

/* ---- detectors (health.c) ---- */
struct tc_threshold { unsigned over; int high; unsigned events; };
/* One sample: 1 when it crosses (armed, TC_HEALTH_HIGH_SAMPLES in a row at
 * or past high), after which it rearms only at or below rearm. */
int tc_threshold_step(struct tc_threshold *t, long value, long high, long rearm, int armed);

struct tc_probe_track {
    enum tc_probe_result last;
    int seen_ok, triggered;
    unsigned streak, hard_streak;
    unsigned ok, closed, silent, down;
    long first_fail_at, last_fail_at;
};
/* One probe result: 1 when it triggers (once per episode). expected says
 * the service is running (its listener in the process table), so a refused
 * connection is a failure rather than a service that is off. */
int tc_probe_track_step(struct tc_probe_track *p, enum tc_probe_result r, int expected, long now);

#define TC_KMSG_PATTERNS 4
#define TC_KMSG_TAIL 128
struct tc_kmsg_sighting { long first, last; unsigned n; int at_start; };
struct tc_kmsg_state {
    char tail[TC_KMSG_TAIL];
    size_t tail_len;
    int baseline;   /* a buffer has been read once */
    long bytes;     /* the last read's length; -1 unreadable */
    struct tc_kmsg_sighting seen[TC_KMSG_PATTERNS];
};
/* Scans the text that is new since the last call; returns event bits. */
unsigned tc_kmsg_scan(struct tc_kmsg_state *k, const char *text, size_t len, long now);
extern const char *const tc_kmsg_names[TC_KMSG_PATTERNS];

struct tc_health {
    int sampled;
    struct tc_counters now;
    struct tc_snapshot current, peak, first_high;
    int have_first_high;
    unsigned near_full;
    struct tc_threshold procs, files;
    int peak_files;
    long peak_files_at;
    long long min_free;
    int have_min_free;
    long min_free_at;
    long long last_fltnoram;
    unsigned long last_mbuf_drops;
    int have_last_mem, have_last_mbuf;
    struct tc_kmsg_state kmsg;
    struct tc_probe_track probe[TC_PROBES];
    int probed;
    unsigned probe_rounds_skipped;
    struct tc_failure_counts failures_seen;
    unsigned acp_timeouts_seen;
    unsigned pending, news; /* the ledger */
};

void tc_health_sample(struct tc_health *h, const struct tc_reading *r, long now, int armed);
void tc_health_probes(struct tc_health *h, const struct tc_probe_round *round, long now);
/* A probe round may run: not while the last sample shows the table within
 * TC_HEALTH_PROC_FULL_MARGIN of kern.maxproc, where the probe's own sshd
 * or smbd child could be the process that refuses a client. Closer than
 * TC_HEALTH_PROC_HIGH_MARGIN the probes still run: that is when they are
 * evidence. */
int tc_health_probe_allowed(const struct tc_health *h);
void tc_health_probes_skipped(struct tc_health *h);
/* Notes fork, socket and acp-timeout failures counted since the last call. */
void tc_health_note_failures(struct tc_health *h, unsigned acp_timeouts);
int tc_health_requests(const struct tc_health *h);
/* A delivered heartbeat carried the bits in token. */
void tc_health_delivered(struct tc_health *h, unsigned token);
extern const char *const tc_health_event_names[TC_EV_COUNT];
extern const char *const tc_probe_names[TC_PROBES];

/* ---- JSON (health_json.c) ---- */
/* Appends ,"health":{...} at json + used; returns the new length, with
 * *token the event bits written, or -1 when it does not fit in cap. */
int tc_health_append_json(char *json, size_t cap, size_t used, const struct tc_health *h, unsigned acp_timeouts,
                          unsigned acp_budget_exhausted, unsigned *token);
#endif
