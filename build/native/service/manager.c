#include "../common/acp.h"
#include "../common/events.h"
#include "../common/log.h"
#include "../common/process.h"
#include "../common/worker.h"
#include "../samba/staging.h"
#include "../storage/settle.h"
#include "bufstall.h"
#include "inspect.h"
#include "proctable.h"
#include "stuck.h"
#include <sys/file.h>
#include <sys/stat.h>

#define MANAGER_LOG TC_RAM_ROOT "/var/runtime.log"
#define SETTINGS_MS 30000
#define INVENTORY_MS 10000
#define AUDIT_MS 30000
/* Host tests shorten these two; the device uses the defaults. */
#ifndef JOB_RETRY_MS
#define JOB_RETRY_MS 5000
#endif
#ifndef TC_STALE_KILL_MS
#define TC_STALE_KILL_MS 10000
#endif
/* The loop wakes at least this often without an event; buffer-stall sampling
 * (bufstall.h) runs on these passes. Host tests shorten it with the stall
 * timings. */
#ifndef TC_MANAGER_PASS_MS
#define TC_MANAGER_PASS_MS 1000
#endif
#ifndef TC_DISKD_PATH
#define TC_DISKD_PATH "/sbin/diskd"
#endif
/* Cool down after delivery; failed attempts retain the report and retry. */
#ifndef TC_BUFSTALL_REPORT_MS
#define TC_BUFSTALL_REPORT_MS 3600000
#endif
#ifndef TC_BUFSTALL_REPORT_RETRY_MS
#define TC_BUFSTALL_REPORT_RETRY_MS 60000
#endif
#ifndef TC_BUFSTALL_REPORT_TIMEOUT_MS
#define TC_BUFSTALL_REPORT_TIMEOUT_MS 180000
#endif
/* Stuck-process heartbeats waiting for the report job; each episode sends at
 * most two, so a few cover several at once. */
#define STUCK_REPORT_QUEUE 4
/* Stuck processes named in the manager's process title, longest first. */
#define STUCK_TITLE_MAX 4
enum { BLOCK_SMB = 1, BLOCK_RSYNC = 2, BLOCK_DISCOVERY = 4, BLOCK_TELEMETRY = 8 };
struct stale_process {
    pid_t pid;
    long long since;
    int killed;
};

struct managed {
    struct tc_child child;
    long long started, retry, ready_at;
    unsigned failures;
    int ready, requested_stop;
};
struct role_launch {
    char *const *argv;
    const char *log;
    const struct tc_volume *volume;
    int unbounded;
};
struct audit_result {
    struct tc_process_table table;
    int smb_probe, rsync_probe, rsync;
    unsigned smb;
    pid_t smb_pid, rsync_pid;
};
struct mast_result {
    int status;
    size_t length;
    char text[TC_MAST_MAX + 1];
};
struct manager {
    struct tc_events events;
    struct tc_storage_settle topology;
    struct tc_storage_retry storage_retry;
    uint32_t storage_request;
    struct tc_storage_snapshot storage, applied_storage, storage_result;
    struct tc_samba_settings settings, applied_settings, settings_result;
    struct managed smb, discovery, telemetry, rsync, diskd;
    struct tc_child storage_job, settings_job, stage_job, audit_job;
    struct audit_result audit_result;
    struct tc_child mast_job;
    struct mast_result mast_result;
    struct tc_share_set discovery_shares;
    char discovery_name[16];
    int discovery_diskless, discovery_afp, discovery_debug;
    int have_settings, have_applied, stopping, tune_ata;
    int storage_dirty, config_dirty, ownership_ready;
    unsigned blocked;
    struct stale_process stale[TC_PROCESS_MAX];
    size_t stale_count;
    int copy_smbd, copy_rsync, binary_valid, rsync_valid;
    unsigned revision, storage_revision, storage_generation, stage_revision;
    long long mast_at, settings_at, storage_at, stage_at, audit_at;
    /* The device hostname staging last mapped: empty until first found.
     * Staging, and so Samba, waits while the kernel hostname is unset. */
    char hostname[256];
    int hostname_waiting;
    long long hostname_wait_since;
    /* When this manager started, on the monotonic clock (-1 if unreadable):
     * the title's started=, from which doctor measures the startup age. */
    long long started_ms;
    /* The process table, read once per pass (proctable.h) for buffer-stall
     * recovery, stuck-process detection and the next audit. */
    struct tc_proctable procs;
    long long sample_at;
    int procs_valid, procs_unreadable;
    /* Buffer-cache stall recovery (bufstall.h); process-local by design. */
    struct tc_bufstall bufstall;
    struct tc_child report_job;
    long long report_at;
    int bufstall_unreadable, bufstall_write_failed, bufstall_wake_failed;
    /* Ended episodes not yet reported, merged: worst outcome, longest wait. */
    enum tc_bufstall_outcome report_outcome;
    long long report_longest;
    /* The in-flight snapshot is separate from episodes ending during it. */
    enum tc_bufstall_outcome report_inflight_outcome;
    long long report_inflight_longest;
    /* Processes stuck in the kernel (stuck.h); process-local by design. */
    struct tc_stuck stuck;
    long long stuck_report_at;
    int stuck_truncated_logged, stuck_reports_dropped;
    /* Heartbeat reasons not yet sent, oldest first, and the one in flight
     * (empty when the in-flight report, if any, is a buffer-stall one). */
    char stuck_reports[STUCK_REPORT_QUEUE][64];
    size_t stuck_report_count;
    char stuck_inflight[64];
};

static void lower(long long *deadline, long long value) {
    if (value >= 0 && (*deadline < 0 || value < *deadline))
        *deadline = value;
}
/* Doctor reads started= and stuck=PID:COMM:WAIT:SECONDS[,...] from the
 * title (stuck.h tc_manager_title; ps shows a sleep time of at most 127 s,
 * and none for a wait that keeps waking). */
static void set_manager_title(const struct manager *m) {
    char title[256];
    tc_manager_title(title, sizeof(title), m->started_ms, m->hostname_waiting, &m->stuck, STUCK_TITLE_MAX,
                     acp_monotonic_ms());
#if defined(__NetBSD__)
    setproctitle("%s", title);
#elif defined(TC_NATIVE_TEST)
    {
        /* Host tests read each title ps would show on a device. */
        const char *root = getenv("TC_TEST_ROOT");
        char path[1024];
        FILE *titles;
        if (root && snprintf(path, sizeof(path), "%s/titles", root) < (int)sizeof(path) && (titles = fopen(path, "a"))) {
            fprintf(titles, "%s\n", title);
            fclose(titles);
        }
    }
#else
    (void)title;
#endif
}
static void changed(struct manager *m, long long now);
/* ACPd sets the hostname a few seconds after the manager starts and gives no
 * notice (sethostname() only); the loop wakes at least once a second, so each
 * pass checks it. smbd resolves this name at every login (issue #54), so
 * staging, and with it Samba, waits for it indefinitely. */
static void observe_hostname(struct manager *m, long long now) {
    char name[256];
    tc_hostname_read(name, sizeof(name));
    if (!name[0]) {
        if (!m->hostname_waiting) {
            if (m->hostname[0])
                timestamped_fprintf(stderr, "manager: hostname is no longer set; Samba keeps running, new staging waits\n");
            else
                timestamped_fprintf(stderr, "manager: waiting for the device hostname before starting Samba\n");
            m->hostname_waiting = 1;
            m->hostname_wait_since = now;
            set_manager_title(m);
        }
        return;
    }
    if (!m->hostname[0]) {
        timestamped_fprintf(stderr, "manager: hostname found: %s after %lld ms\n", name, now - m->hostname_wait_since);
    } else if (strcmp(name, m->hostname)) {
        timestamped_fprintf(stderr, "manager: hostname changed from %s to %s\n", m->hostname, name);
        changed(m, now); /* Restage: map the new name and reload Samba. */
    } else if (m->hostname_waiting) {
        timestamped_fprintf(stderr, "manager: hostname found again: %s after %lld ms\n", name, now - m->hostname_wait_since);
    }
    if (strcmp(name, m->hostname))
        strcpy(m->hostname, name);
    if (m->hostname_waiting) {
        m->hostname_waiting = 0;
        set_manager_title(m);
    }
}
static void changed(struct manager *m, long long now) {
    m->revision++;
    m->config_dirty = 1;
    if (m->stage_job.group)
        tc_child_stop(&m->stage_job, now, 1);
}
static void stop_role(struct managed *role, long long now, int allow_kill) {
    role->ready = 0;
    if (role->child.group)
        role->requested_stop = 1;
    tc_child_stop(&role->child, now, allow_kill);
}
static void poll_role(struct managed *role, const char *name, long long now, int allow_kill) {
    if (!role->child.group || !tc_child_poll(&role->child, now))
        return;
    if (!role->requested_stop) {
        if (now - role->started >= 60000)
            role->failures = 0;
        if (role->failures < 6)
            role->failures++;
        role->retry = now + (1000LL << role->failures);
        timestamped_fprintf(stderr, "manager: %s exited; retry in %lld ms\n", name, role->retry - now);
    }
    tc_child_close(&role->child);
    role->child.allow_kill = allow_kill;
    role->requested_stop = 0;
    role->ready = 0;
}
static int launch_role(void *opaque) {
    const struct role_launch *launch = opaque;
    struct stat st;
    int guard = -1, fd;
    /* Only a launch touches payload logs; healthy audits remain RAM-only.
     * The child does slow filesystem work so the manager stays responsive. */
    if (launch->volume) {
        guard = tc_storage_guard(launch->volume);
        if (guard < 0) {
            fprintf(stderr, "launch: log volume unavailable: %s\n", launch->log);
            return 1;
        }
    }
    if (!launch->unbounded && tc_log_trim(launch->log))
        fprintf(stderr, "launch: unable to trim %s: %s\n", launch->log, strerror(errno));
    fd = open(launch->log, O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW, 0600);
    if (fd < 0 || fstat(fd, &st) || !S_ISREG(st.st_mode) ||
        (guard >= 0 && !tc_storage_guard_valid(launch->volume, guard))) {
        fprintf(stderr, "launch: log destination unavailable: %s\n", launch->log);
        if (fd >= 0) close(fd);
        if (guard >= 0) close(guard);
        return 1;
    }
    if (guard >= 0) close(guard);
    if (dup2(fd, STDOUT_FILENO) < 0 || dup2(fd, STDERR_FILENO) < 0) { close(fd); return 1; }
    if (fd > STDERR_FILENO) close(fd);
    execv(launch->argv[0], launch->argv);
    fprintf(stderr, "exec %s: %s\n", launch->argv[0], strerror(errno));
    return 127;
}
static int start_role(struct managed *role, char *const argv[], const char *log, long long now,
                      int allow_kill, const struct tc_volume *volume, int unbounded) {
    struct role_launch launch = {argv, log, volume, unbounded};
    if (role->child.group || now < role->retry)
        return -1;
    if (tc_child_fork(&role->child, launch_role, &launch, MANAGER_LOG, NULL, 0, 0)) {
        role->retry = now + JOB_RETRY_MS;
        return -1;
    }
    role->child.allow_kill = allow_kill;
    role->started = now;
    role->ready_at = now + 10000;
    return 0;
}
static int settings_job(void *opaque) {
    struct manager *m = opaque;
    struct tc_samba_settings result;
    tc_worker_begin("settings");
    /* The hostname staging maps, so a read after it is set derives the same
     * NetBIOS name staging gave Samba (see pump_stage) and changes nothing. */
    if (tc_samba_settings_read(&result, m->hostname))
        return tc_worker_finish(1);
    /* Hostname/model fallbacks are useful at cold boot. A later ACP failure
     * must not rename a working server or replace its known hardware model. */
    if (m->have_settings) {
        if (!result.identity.name_observed)
            result.identity = m->settings.identity;
        else if (!strcmp(result.identity.model, "MacSamba"))
            strcpy(result.identity.model, m->settings.identity.model);
    }
    return tc_worker_finish(tc_worker_result(&result, sizeof(result)) ? 1 : 0);
}
static int storage_job(void *opaque) {
    struct manager *m = opaque;
    struct tc_storage_snapshot result;
    tc_worker_begin("storage");
    if (tc_storage_prepare(&result, &m->topology.stable, &m->storage, &m->settings.config, m->tune_ata, m->storage_request))
        return tc_worker_finish(1);
    return tc_worker_finish(tc_worker_result(&result, sizeof(result)) ? 1 : 0);
}
static int stage_job(void *opaque) {
    struct manager *m = opaque;
    tc_worker_begin("stage");
    /* Samba logins stall without this mapping, so a failed write fails
     * staging, which retries. */
    if (tc_hosts_update(TC_HOSTS_PATH, m->hostname) < 0) {
        fprintf(stderr, "stage: could not update %s for %s: %s\n", TC_HOSTS_PATH, m->hostname, strerror(errno));
        return tc_worker_finish(1);
    }
    if (m->copy_smbd && tc_samba_clear_locks())
        return tc_worker_finish(1);
    return tc_worker_finish(tc_samba_stage(&m->storage, &m->settings, m->copy_smbd, m->copy_rsync) ? 1 : 0);
}
static int audit_job(void *opaque) {
    struct manager *m = opaque;
    struct audit_result result;
    tc_worker_begin("audit");
    memset(&result, 0, sizeof(result));
    (void)tc_log_trim(MANAGER_LOG);
    (void)tc_log_trim(TC_RAM_ROOT "/var/discovery.log");
    (void)tc_log_trim(TC_RAM_ROOT "/var/telemetry.log");
    (void)tc_log_trim(TC_RAM_ROOT "/var/rsync.log");
#ifdef TC_NATIVE_TEST
    {
        /* Host tests count audits. */
        const char *root = getenv("TC_TEST_ROOT");
        char path[1024];
        FILE *events;
        if (root && snprintf(path, sizeof(path), "%s/record-audit", root) < (int)sizeof(path) && !access(path, F_OK) &&
            snprintf(path, sizeof(path), "%s/events", root) < (int)sizeof(path) && (events = fopen(path, "a"))) {
            fputs("{\"kind\": \"command\", \"role\": \"audit\"}\n", events);
            fclose(events);
        }
    }
#endif
    /* The manager's table for this pass, inherited by fork. Reading command
     * lines can block on a process stuck on the disk, so only this job does. */
    if (tc_process_table_build(&result.table, &m->procs, tc_proctable_argv))
        return tc_worker_finish(1);
    result.smb_pid = m->smb.child.pid;
    result.rsync_pid = m->rsync.child.pid;
    if (m->smb.child.pid && !m->smb.child.stopping)
        result.smb_probe = tc_process_wildcard_listeners(m->smb.child.pid, 445, &result.smb) == 0;
    if (m->rsync.child.pid && !m->rsync.child.stopping)
        result.rsync_probe = tc_process_listener(m->rsync.child.pid, 873, &result.rsync) == 0;
    return tc_worker_finish(tc_worker_result(&result, sizeof(result)) ? 1 : 0);
}
static int payload_same(const struct tc_storage_snapshot *a, const struct tc_storage_snapshot *b) {
    return a->payload_index >= 0 && b->payload_index >= 0 && !strcmp(a->payload, b->payload) && !strcmp(a->smbd_source, b->smbd_source) &&
           !strcmp(a->inventory.volumes[a->payload_index].uuid, b->inventory.volumes[b->payload_index].uuid);
}
static uint32_t storage_pending(const struct manager *m) {
    return m->storage.retry_prepare | m->storage.retry_payload;
}
static int storage_projection_same(const struct tc_storage_snapshot *a, const struct tc_storage_snapshot *b) {
    return tc_shares_equal(&a->shares, &b->shares) &&
           ((a->payload_index < 0 && b->payload_index < 0) || payload_same(a, b));
}
static void invalidate_storage(struct manager *m, long long now) {
    changed(m, now);
    m->storage_generation++;
    m->storage_dirty = 1;
    m->storage_at = now;
    memset(&m->storage_retry, 0, sizeof(m->storage_retry));
    if (m->storage_job.group)
        tc_child_stop(&m->storage_job, now, 1);
}
static int samba_settings_equal(const struct tc_samba_settings *a, const struct tc_samba_settings *b) {
    struct tc_samba_settings first = *a, second = *b;
    struct tc_runtime_config *configs[] = {&first.config, &second.config};
    size_t i;
    for (i = 0; i < 2; i++) {
        struct tc_runtime_config *c = configs[i];
        c->telemetry = c->discovery_debug = 0;
        c->mount_attempts = c->mount_timeout = c->mount_poll = c->ata_idle = 0;
        memset(c->ata_standby, 0, sizeof(c->ata_standby));
    }
    return !memcmp(&first, &second, sizeof(first));
}
static void physical_event(struct manager *m, long long now) {
    invalidate_storage(m, now);
    m->mast_at = now;
    m->storage_at = now + TC_STORAGE_SETTLE_MS;
}
static int mast_job(void *unused) {
    struct mast_result result = {0};
    struct acp_request request = {0};
    (void)unused;
    tc_worker_begin("inventory");
    request.key = "MaSt";
    request.form = ACP_ARRAY;
    request.multiline = 1;
    request.output = result.text;
    request.capacity = sizeof(result.text);
    (void)acp_collect_run(&request, 1, 20000, 20000);
    result.status = request.status;
    result.length = request.length;
    return tc_worker_finish(tc_worker_result(&result, sizeof(result)) ? 1 : 0);
}
static void pump_inventory(struct manager *m, long long now) {
    if (m->mast_job.group && tc_child_poll(&m->mast_job, now)) {
        struct tc_inventory inventory;
        if (tc_child_ok(&m->mast_job) && !m->mast_job.stopping &&
            m->mast_job.used == sizeof(m->mast_result) && m->mast_result.status == ACP_OK &&
            !tc_mast_parse(&inventory, m->mast_result.text, m->mast_result.length)) {
            if (tc_storage_observe(&m->topology, &inventory, now))
                invalidate_storage(m, now);
            else if (!m->topology.pending && !m->storage_dirty && !m->storage_job.group &&
                     tc_storage_refresh_needed(&m->storage, &m->topology.stable))
                invalidate_storage(m, now);
            if (m->topology.pending)
                lower(&m->mast_at, m->topology.confirm_at > now ? m->topology.confirm_at : now + 1000);
        } else {
            timestamped_fprintf(stderr, "manager: MaSt unavailable; retaining last valid inventory\n");
            m->mast_at = now + JOB_RETRY_MS;
        }
        tc_child_close(&m->mast_job);
    }
    if (!m->mast_job.group && now >= m->mast_at) {
        m->mast_at = now + INVENTORY_MS;
        if (tc_child_fork(&m->mast_job, mast_job, NULL, MANAGER_LOG, &m->mast_result,
                          sizeof(m->mast_result), now + 25000))
            m->mast_at = now + JOB_RETRY_MS;
    }
}
static void pump_settings(struct manager *m, long long now) {
    if (m->settings_job.group && tc_child_poll(&m->settings_job, now)) {
        if (tc_child_ok(&m->settings_job) && m->settings_job.used == sizeof(m->settings_result)) {
            if (!m->have_settings || memcmp(&m->settings, &m->settings_result, sizeof(m->settings))) {
                int storage_changed =
                    !m->have_settings ||
                    m->settings.config.internal_root != m->settings_result.config.internal_root ||
                    m->settings.config.rsync != m->settings_result.config.rsync ||
                    m->settings.config.advertise_afp != m->settings_result.config.advertise_afp;
                if (!m->have_settings || m->settings.config.ata_idle != m->settings_result.config.ata_idle ||
                    strcmp(m->settings.config.ata_standby, m->settings_result.config.ata_standby)) {
                    m->tune_ata = 1;
                    storage_changed = 1;
                }
                if (!m->have_settings || !samba_settings_equal(&m->settings, &m->settings_result))
                    changed(m, now);
                m->settings = m->settings_result;
                m->have_settings = 1;
                if (storage_changed)
                    invalidate_storage(m, now);
            }
        } else
            m->settings_at = now + JOB_RETRY_MS;
        tc_child_close(&m->settings_job);
    }
    if (!m->settings_job.group && now >= m->settings_at) {
        m->settings_at = now + SETTINGS_MS;
        if (tc_child_fork(&m->settings_job, settings_job, m, MANAGER_LOG, &m->settings_result,
                          sizeof(m->settings_result), now + 65000))
            m->settings_at = now + JOB_RETRY_MS;
    }
}
static void pump_storage(struct manager *m, long long now) {
    if (m->storage_job.group && tc_child_poll(&m->storage_job, now)) {
        if (tc_child_ok(&m->storage_job) && !m->storage_job.stopping &&
            m->storage_job.used == sizeof(m->storage_result) &&
            m->storage_revision == m->storage_generation) {
            int projected_change = !storage_projection_same(&m->storage, &m->storage_result);
            m->storage = m->storage_result;
            m->storage_dirty = 0;
            m->storage_at = 0;
            m->tune_ata = 0;
            tc_storage_retry_finish(&m->storage_retry, now, storage_pending(m) != 0);
            if (projected_change) changed(m, now);
            if (m->storage.payload_index < 0) {
                /* Explicit product policy: a missing payload stops Samba,
                 * even though its executable still exists on the RAM disk. */
                stop_role(&m->smb, now, 1);
                stop_role(&m->rsync, now, 1);
                m->have_applied = 0;
            }
        } else {
            tc_storage_retry_finish(&m->storage_retry, now, 1);
            m->storage_at = m->storage_retry.at;
        }
        tc_child_close(&m->storage_job);
    }
    if ((m->storage_dirty || (storage_pending(m) && now >= m->storage_retry.at)) && m->have_settings && m->topology.initialized && !m->topology.pending &&
        !m->storage_job.group && !m->stage_job.group && now >= m->storage_at) {
        m->storage_revision = m->storage_generation;
        m->storage_request = m->storage_dirty ? UINT32_MAX : storage_pending(m);
        /* At most 16 disks, each with a bounded native activation. Child
         * death and TERM remain responsive throughout this setup job. */
        long long budget = 10000 + (long long)m->topology.stable.count *
                                       (m->settings.config.mount_attempts *
                                            (30000LL + m->settings.config.mount_timeout * 1000LL) +
                                        45000);
        if (tc_child_fork(&m->storage_job, storage_job, m, MANAGER_LOG, &m->storage_result,
                          sizeof(m->storage_result), now + budget))
            m->storage_at = now + JOB_RETRY_MS;
    }
}
static const char *role_name(enum tc_process_role role) {
    static const char *const names[] = {"process", "smbd", "discovery", "telemetry", "rsync",
                                        "wcifsfs", "wcifsnd", "diskd", "diskd"};
    return (size_t)role < sizeof(names) / sizeof(names[0]) ? names[role] : "process";
}
static void apply_audit(struct manager *m, long long now) {
    size_t i;
    int diskd = 0, external = 0;
    struct stale_process stale[TC_PROCESS_MAX];
    size_t stale_count = 0;
    m->blocked = 0;
    const struct tc_process_table *table = &m->audit_result.table;
#ifdef TC_AUDIT_TEST_LOG
    /* Device validation builds only: the classified table, to compare with ps. */
    for (i = 0; i < table->count; i++)
        timestamped_fprintf(stderr, "audit: pid %ld parent %ld group %ld role %s\n", (long)table->processes[i].pid,
                            (long)table->processes[i].parent, (long)table->processes[i].group,
                            role_name(table->processes[i].role));
#endif
    for (i = 0; i < table->count; i++) {
        const struct tc_process_info *p = &table->processes[i];
        /* Discovery verifies its own child's sockets before registering names.
         * Clear only a foreign child; discovery can keep Bonjour alive
         * and retry native NBNS locally without surrendering a working socket. */
        int stop = p->role == TC_PROC_WCIFSFS || p->role == TC_PROC_DISKD ||
                   (p->role == TC_PROC_WCIFSND && p->parent != m->discovery.child.pid);
        if (p->role == TC_PROC_DISKD || p->role == TC_PROC_DISKD_LOOPBACK)
            diskd++;
        if (p->role == TC_PROC_SMBD && p->group != m->smb.child.group)
            stop = 1;
        if (p->role == TC_PROC_RSYNC && p->group != m->rsync.child.group)
            stop = 1;
        if (p->role == TC_PROC_DISCOVERY && p->pid != m->discovery.child.pid)
            stop = 1;
        if (p->role == TC_PROC_TELEMETRY && p->pid != m->telemetry.child.pid)
            stop = 1;
        if (stop) {
            size_t j;
            long long since = now;
            int killed = 0;
            for (j = 0; j < m->stale_count; j++)
                if (m->stale[j].pid == p->pid) {
                    since = m->stale[j].since;
                    killed = m->stale[j].killed;
                }
            int sig = now - since >= TC_STALE_KILL_MS && p->role != TC_PROC_TELEMETRY ? SIGKILL : SIGTERM;
            /* SIGTERM is routine cleanup. A process that outlives it is
             * rare and worth one line; later audits resend SIGKILL quietly. */
            if (sig == SIGKILL && !killed) {
                timestamped_fprintf(stderr, "manager: foreign %s pid %d (group %d) ignored SIGTERM for %lld ms; sending SIGKILL\n",
                                    role_name(p->role), (int)p->pid, (int)p->group, now - since);
                killed = 1;
            }
            stale[stale_count].pid = p->pid;
            stale[stale_count].since = since;
            stale[stale_count++].killed = killed;
            kill(p->pid, sig);
            external = 1;
            if (p->role == TC_PROC_TELEMETRY)
                m->blocked |= BLOCK_TELEMETRY;
            else if (p->role == TC_PROC_RSYNC)
                m->blocked |= BLOCK_RSYNC;
            else if (p->role == TC_PROC_SMBD)
                m->blocked |= BLOCK_SMB;
            else if (p->role == TC_PROC_WCIFSFS)
                m->blocked |= BLOCK_SMB | BLOCK_DISCOVERY;
            else
                m->blocked |= BLOCK_DISCOVERY;
        }
    }
    memcpy(m->stale, stale, stale_count * sizeof(stale[0]));
    m->stale_count = stale_count;
    if (external) {
        m->audit_at = now + 1000;
    }
    m->ownership_ready = 1;
    if (!diskd && !m->diskd.child.group) {
        char *argv[] = {TC_DISKD_PATH, "-i", "lo0", "-d", "local.", NULL};
        if (!start_role(&m->diskd, argv, MANAGER_LOG, now, 1, NULL, 0))
            m->mast_at = now;
        else if (m->diskd.retry > now)
            lower(&m->audit_at, m->diskd.retry);
    }
    if (m->smb.child.pid && m->smb.child.pid == m->audit_result.smb_pid && !m->smb.child.stopping &&
        m->audit_result.smb_probe) {
        if ((m->audit_result.smb & 3) == 3)
            m->smb.ready = 1;
        else if (now >= m->smb.ready_at) {
            timestamped_fprintf(stderr, "manager: smbd missing required listeners; restarting\n");
            stop_role(&m->smb, now, 1);
            m->smb.retry = now + JOB_RETRY_MS;
        }
    }
    if (m->rsync.child.pid && m->rsync.child.pid == m->audit_result.rsync_pid && !m->rsync.child.stopping &&
        m->audit_result.rsync_probe) {
        if (m->audit_result.rsync)
            m->rsync.ready = 1;
        else if (now >= m->rsync.ready_at) {
            stop_role(&m->rsync, now, 1);
            m->rsync.retry = now + JOB_RETRY_MS;
        }
    }
    if ((m->smb.child.pid && !m->smb.ready) || (m->rsync.child.pid && !m->rsync.ready))
        m->audit_at = now + 1000;
}
static void pump_audit(struct manager *m, long long now) {
    int had_diskd = m->diskd.child.group != 0;
    poll_role(&m->diskd, "diskd", now, 1);
    if (had_diskd && !m->diskd.child.group)
        m->audit_at = m->diskd.retry;
    if (m->audit_job.group && tc_child_poll(&m->audit_job, now)) {
        if (tc_child_ok(&m->audit_job) && m->audit_job.used == sizeof(m->audit_result))
            apply_audit(m, now);
        else
            m->audit_at = now + JOB_RETRY_MS;
        tc_child_close(&m->audit_job);
    }
    if (!m->audit_job.group && now >= m->audit_at) {
        /* The audit classifies a process table read in this pass, as ps ran
         * at its start before: an audit asked for by an event must see what
         * changed. An older table asks the next pass for a fresh read; an
         * unreadable one waits rather than spinning on a past deadline. */
        if (!m->procs_valid) {
            m->audit_at = now + JOB_RETRY_MS;
            return;
        }
        if (m->sample_at != now) {
            m->sample_at = 0;
            return;
        }
        m->audit_at = now + AUDIT_MS;
        if (tc_child_fork(&m->audit_job, audit_job, m, MANAGER_LOG, &m->audit_result, sizeof(m->audit_result),
                          now + 20000))
            m->audit_at = now + JOB_RETRY_MS;
    }
}
/* Put Apple's low-water mark back when the manager exits. */
static void bufstall_restore(void) {
    struct tc_bufstall_sample sample;
    uint64_t apple;
    memset(&sample, 0, sizeof(sample));
    if (tc_bufstall_read_vm(&sample))
        return;
    apple = tc_bufstall_default_lowater(sample.hiwater);
    if (sample.lowater <= apple)
        return;
    if (tc_bufstall_set_lowater(apple))
        timestamped_fprintf(stderr, "manager: could not restore vm.bufmem_lowater to %llu at stop: %s\n",
                            (unsigned long long)apple, strerror(errno));
    else
        timestamped_fprintf(stderr, "manager: restored vm.bufmem_lowater from %llu to %llu at stop\n",
                            (unsigned long long)sample.lowater, (unsigned long long)apple);
}
static void drop_stuck_report(struct manager *m, const char *reason) {
    /* Logged once until a report fits again. */
    if (!m->stuck_reports_dropped)
        timestamped_fprintf(stderr, "manager: stuck: report queue full; dropping %s\n", reason);
    m->stuck_reports_dropped = 1;
}
static void push_stuck_report(struct manager *m, const char *reason) {
    if (m->stuck_report_count < STUCK_REPORT_QUEUE) {
        snprintf(m->stuck_reports[m->stuck_report_count++], sizeof(m->stuck_reports[0]), "%s", reason);
        m->stuck_reports_dropped = 0;
    } else {
        drop_stuck_report(m, reason);
    }
}
static void pop_stuck_report(struct manager *m) {
    if (!m->stuck_report_count)
        return;
    memmove(m->stuck_reports[0], m->stuck_reports[1], (m->stuck_report_count - 1) * sizeof(m->stuck_reports[0]));
    m->stuck_report_count--;
}
/* Buffer-stall and stuck-process heartbeats share one report job, the one
 * step here that starts a process, so never while stopping and never during a
 * buffer stall, when the new process could itself wait for a buffer, hold the
 * report slot and keep the manager from stopping. A stuck-process report goes
 * out while the process is still stuck (that stall may end only with a power
 * cycle), unless a buffer stall is in progress. */
static void pump_reports(struct manager *m, long long now) {
    char reason[64];
    char *argv[] = {TC_SERVICE_BIN, "telemetry", "--report", reason, NULL};
    int bufstall_due;
    if (m->report_job.group && tc_child_poll(&m->report_job, now)) {
        int delivered = tc_child_ok(&m->report_job) && !m->report_job.stopping;
        if (m->stuck_inflight[0]) {
            if (!delivered) {
                /* Back to the front: reports go out in order. A queue that
                 * filled meanwhile loses its newest. */
                if (m->stuck_report_count == STUCK_REPORT_QUEUE)
                    drop_stuck_report(m, m->stuck_reports[--m->stuck_report_count]);
                memmove(m->stuck_reports[1], m->stuck_reports[0], m->stuck_report_count * sizeof(m->stuck_reports[0]));
                memcpy(m->stuck_reports[0], m->stuck_inflight, sizeof(m->stuck_inflight));
                m->stuck_report_count++;
                m->stuck_report_at = now + TC_BUFSTALL_REPORT_RETRY_MS;
            }
            m->stuck_inflight[0] = 0;
        } else if (delivered) {
            m->report_at = now + TC_BUFSTALL_REPORT_MS;
        } else {
            if (m->report_outcome < m->report_inflight_outcome)
                m->report_outcome = m->report_inflight_outcome;
            if (m->report_longest < m->report_inflight_longest)
                m->report_longest = m->report_inflight_longest;
            m->report_at = now + TC_BUFSTALL_REPORT_RETRY_MS;
        }
        m->report_inflight_outcome = TC_BUFSTALL_NO_EPISODE;
        m->report_inflight_longest = 0;
        tc_child_close(&m->report_job);
    }
    if (m->stopping || acp_stop_requested || !m->have_settings || m->report_job.group)
        return;
    bufstall_due = m->report_outcome && !m->bufstall.outcome && now >= m->report_at;
    if (!m->settings.config.telemetry) {
        if (bufstall_due) {
            m->report_outcome = TC_BUFSTALL_NO_EPISODE;
            m->report_longest = 0;
        }
        m->stuck_report_count = 0;
        return;
    }
    if (bufstall_due)
        snprintf(reason, sizeof(reason), "bufstall:%s:%lld", tc_bufstall_outcome_name(m->report_outcome),
                 m->report_longest / 1000);
    else if (m->stuck_report_count && !m->bufstall.outcome && now >= m->stuck_report_at)
        snprintf(reason, sizeof(reason), "%s", m->stuck_reports[0]);
    else
        return;
    /* --report never starts signed jobs, so its whole group may be killed
     * after its deadline or at stop without interrupting a debug program. */
    if (!tc_child_exec(&m->report_job, argv, TC_RAM_ROOT "/var/telemetry.log")) {
        m->report_job.deadline = now + TC_BUFSTALL_REPORT_TIMEOUT_MS;
        if (bufstall_due) {
            m->report_inflight_outcome = m->report_outcome;
            m->report_inflight_longest = m->report_longest;
            m->report_outcome = TC_BUFSTALL_NO_EPISODE;
            m->report_longest = 0;
        } else {
            memcpy(m->stuck_inflight, reason, sizeof(m->stuck_inflight));
            pop_stuck_report(m);
        }
    } else if (bufstall_due) {
        m->report_at = now + TC_BUFSTALL_REPORT_RETRY_MS;
    } else {
        m->stuck_report_at = now + TC_BUFSTALL_REPORT_RETRY_MS;
    }
}
static const char *group_role(const struct manager *m, pid_t group) {
    const struct {
        const struct tc_child *child;
        const char *role;
    } owners[] = {
        {&m->smb.child, "smbd"},         {&m->discovery.child, "discovery"}, {&m->telemetry.child, "telemetry"},
        {&m->rsync.child, "rsync"},      {&m->diskd.child, "diskd"},         {&m->storage_job, "storage"},
        {&m->settings_job, "settings"},  {&m->stage_job, "stage"},           {&m->audit_job, "audit"},
        {&m->mast_job, "inventory"},     {&m->report_job, "report"},
    };
    size_t i;
    for (i = 0; group > 0 && i < sizeof(owners) / sizeof(owners[0]); i++)
        if (owners[i].child->group == group)
            return owners[i].role;
    return NULL;
}
static void stuck_name(const struct manager *m, const struct tc_stuck_thread *t, char *out, size_t size) {
    const char *role = group_role(m, t->group);
    int n = snprintf(out, size, "pid %ld", (long)t->pid);
    if (n > 0 && (size_t)n < size && t->lid)
        n += snprintf(out + n, size - (size_t)n, " thread %d", t->lid);
    if (n > 0 && (size_t)n < size)
        snprintf(out + n, size - (size_t)n, " (%s%s%s)", t->comm, role ? ", role " : "", role ? role : "");
}
static void stuck_changed(const struct tc_stuck_event *e, void *context) {
    struct manager *m = context;
    char name[96], reason[64], comm[TC_STUCK_COMM], wmesg[TC_STUCK_WMESG];
    long long seconds = e->duration_ms / 1000;
    stuck_name(m, &e->entry.thread, name, sizeof(name));
    tc_stuck_word(comm, sizeof(comm), e->entry.thread.comm);
    tc_stuck_word(wmesg, sizeof(wmesg), e->entry.thread.wmesg);
    if (e->change == TC_STUCK_STARTED) {
        timestamped_fprintf(stderr, "manager: stuck: %s asleep uninterruptibly on %s for %lld s without "
                                    "running\n", name, wmesg, seconds);
    } else if (e->change == TC_STUCK_REPORT) {
        snprintf(reason, sizeof(reason), "stuck:%s:%s:%lld", comm, wmesg, seconds);
        push_stuck_report(m, reason);
    } else {
        timestamped_fprintf(stderr, "manager: stuck: %s no longer stuck after %lld s\n", name, seconds);
        if (e->entry.reported) {
            snprintf(reason, sizeof(reason), "stuck-cleared:%s:%s:%lld", comm, wmesg, seconds);
            push_stuck_report(m, reason);
        }
    }
}
/* Reporting only: nothing in user space can wake or kill these (stuck.h). */
static void sample_stuck(struct manager *m, long long now) {
    static struct tc_stuck_sample sample;
    size_t count, i, stuck = 0;
    tc_stuck_read(&sample, &m->procs);
    if (sample.truncated && !m->stuck_truncated_logged)
        timestamped_fprintf(stderr, "manager: stuck: more than %d uninterruptible sleepers; following the first %d\n",
                            TC_STUCK_MAX, TC_STUCK_MAX);
    m->stuck_truncated_logged = sample.truncated;
    count = tc_stuck_step(&m->stuck, &sample, now, stuck_changed, m);
    for (i = 0; i < m->stuck.count; i++)
        stuck += m->stuck.entries[i].stuck != 0;
    /* While anything is stuck, every sample: the title shows seconds. */
    if (count || stuck)
        set_manager_title(m);
}
/* Detection, the raise, the wake and the restore are system calls in this
 * process: none of them forks, so they work with a full process table. They
 * also run while stopping, when stuck Samba workers would block the drain. */
static void sample_bufstall(struct manager *m, long long now) {
    struct tc_bufstall_sample sample;
    enum tc_bufstall_action action;
    uint64_t lowater;
    long long longest = 0;
    int failed = 0, error = 0;
    size_t i;
    if (tc_bufstall_read(&sample, &m->procs)) {
        if (!m->bufstall_unreadable)
            timestamped_fprintf(stderr, "manager: cannot read buffer-cache state (%s); buffer-stall recovery "
                                        "waits until it can\n", strerror(errno));
        m->bufstall_unreadable = 1;
        return;
    }
    if (m->bufstall_unreadable)
        timestamped_fprintf(stderr, "manager: buffer-cache state readable again\n");
    m->bufstall_unreadable = 0;
    action = tc_bufstall_step(&m->bufstall, &sample, now, &lowater);
    if (action == TC_BUFSTALL_NONE)
        return;
    /* Fix first and log after: NetBSD 4's /mnt/Memory is MFS, whose writes
     * can themselves wait for a buffer. */
    if (action == TC_BUFSTALL_RAISE || action == TC_BUFSTALL_STUCK || action == TC_BUFSTALL_RESOLVED ||
        action == TC_BUFSTALL_RESTORE) {
        failed = tc_bufstall_set_lowater(lowater) != 0;
        error = errno;
    }
    if (action == TC_BUFSTALL_RAISE && failed)
        tc_bufstall_failed(&m->bufstall, now);
    if ((action == TC_BUFSTALL_RAISE && !failed) || action == TC_BUFSTALL_WAKE) {
        if (tc_bufstall_wake()) {
            if (!m->bufstall_wake_failed)
                timestamped_fprintf(stderr, "manager: buffer-stall wake could not read %s: %s\n", TC_BUFWAKE_DIR,
                                    strerror(errno));
            m->bufstall_wake_failed = 1;
        } else {
            m->bufstall_wake_failed = 0;
        }
    }
    for (i = 0; i < m->bufstall.seen_count; i++)
        if (now - m->bufstall.seen[i].since > longest)
            longest = now - m->bufstall.seen[i].since;
    if (failed) {
        /* A refused restore is retried every sample: log the first. */
        if (!m->bufstall_write_failed)
            timestamped_fprintf(stderr, "manager: could not set vm.bufmem_lowater to %llu: %s\n",
                                (unsigned long long)lowater, strerror(error));
        m->bufstall_write_failed = 1;
    } else if (action != TC_BUFSTALL_WAKE && action != TC_BUFSTALL_CAPPED && action != TC_BUFSTALL_ENDED) {
        m->bufstall_write_failed = 0;
    }
    if (action == TC_BUFSTALL_RAISE && !failed)
        timestamped_fprintf(stderr,
                            "manager: buffer stall: %lu processes waiting for buffers, longest %lld ms; raised "
                            "vm.bufmem_lowater from %llu to %llu (bufmem %llu, hiwater %llu)\n",
                            (unsigned long)sample.count, longest, (unsigned long long)sample.lowater,
                            (unsigned long long)lowater, (unsigned long long)sample.bufmem,
                            (unsigned long long)sample.hiwater);
    else if (action == TC_BUFSTALL_CAPPED)
        timestamped_fprintf(stderr,
                            "manager: buffer stall: %lu processes waiting for buffers, longest %lld ms; bufmem %llu "
                            "is at the high-water mark %llu, so raising vm.bufmem_lowater cannot help; checking "
                            "again every %d s\n",
                            (unsigned long)sample.count, longest, (unsigned long long)sample.bufmem,
                            (unsigned long long)sample.hiwater, TC_BUFSTALL_HOLD_MS / 1000);
    else if (action == TC_BUFSTALL_STUCK && !failed)
        timestamped_fprintf(stderr,
                            "manager: buffer stall: %lu processes still waiting %lld ms into the raise; restored "
                            "vm.bufmem_lowater to %llu; raising again in %d s\n",
                            (unsigned long)sample.count, now - m->bufstall.raised_at, (unsigned long long)lowater,
                            TC_BUFSTALL_HOLD_MS / 1000);
    else if (action == TC_BUFSTALL_RESTORE && !failed)
        timestamped_fprintf(stderr, "manager: restored vm.bufmem_lowater from %llu to %llu\n",
                            (unsigned long long)sample.lowater, (unsigned long long)lowater);
    if (action == TC_BUFSTALL_RESOLVED || action == TC_BUFSTALL_ENDED) {
        const char *outcome = tc_bufstall_outcome_name(m->bufstall.ended_outcome);
        long long seconds = m->bufstall.ended_longest / 1000;
        if (action == TC_BUFSTALL_RESOLVED && !failed)
            timestamped_fprintf(stderr, "manager: buffer stall over (%s, longest wait %lld s); restored "
                                        "vm.bufmem_lowater to %llu\n",
                                outcome, seconds, (unsigned long long)lowater);
        else if (action == TC_BUFSTALL_ENDED)
            timestamped_fprintf(stderr, "manager: buffer stall over (%s, longest wait %lld s)\n", outcome, seconds);
        if (m->report_outcome < m->bufstall.ended_outcome)
            m->report_outcome = m->bufstall.ended_outcome;
        if (m->report_longest < m->bufstall.ended_longest)
            m->report_longest = m->bufstall.ended_longest;
    }
}
/* One process-table read per pass (proctable.h), shared by buffer-stall
 * recovery, stuck-process detection and the next audit. Reading is system
 * calls in this process, no fork, so it works with a full process table; it
 * runs while stopping too, when stuck Samba workers hold up the drain. */
static void pump_sample(struct manager *m, long long now) {
    pump_reports(m, now);
    if (now - m->sample_at < TC_BUFSTALL_SAMPLE_MS && m->sample_at)
        return;
    m->sample_at = now;
    if (tc_proctable_read(&m->procs)) {
        if (!m->procs_unreadable)
            timestamped_fprintf(stderr, "manager: cannot read the process table (%s); buffer-stall recovery, "
                                        "stuck-process detection and audits wait until it can\n", strerror(errno));
        m->procs_unreadable = 1;
        m->procs_valid = 0;
        return;
    }
    if (m->procs_unreadable)
        timestamped_fprintf(stderr, "manager: process table readable again\n");
    m->procs_unreadable = 0;
    m->procs_valid = 1;
    sample_bufstall(m, now);
    sample_stuck(m, now);
}
static void pump_stage(struct manager *m, long long now) {
    if (m->stage_job.group && tc_child_poll(&m->stage_job, now)) {
        if (tc_child_ok(&m->stage_job) && !m->stage_job.stopping && m->stage_revision == m->revision &&
            !tc_samba_publish(m->settings.config.rsync)) {
            m->applied_storage = m->storage;
            m->applied_settings = m->settings;
            m->have_applied = 1;
            m->config_dirty = 0;
            m->binary_valid = 1;
            if (m->settings.config.rsync)
                m->rsync_valid = 1;
            if (m->smb.child.pid && !m->smb.child.stopping)
                kill(m->smb.child.pid, SIGHUP);
            if (m->rsync.child.pid)
                stop_role(&m->rsync, now, 1);
        } else {
            tc_samba_discard();
            m->stage_at = now + JOB_RETRY_MS;
            timestamped_fprintf(stderr, "manager: staging failed or superseded; will retry\n");
        }
        tc_child_close(&m->stage_job);
    }
    if (!m->config_dirty || !m->have_settings || m->storage.payload_index < 0 || !m->hostname[0] || m->hostname_waiting ||
        m->storage_dirty || m->topology.pending || m->storage_job.group || m->stage_job.group ||
        !m->ownership_ready || (m->blocked & (BLOCK_SMB | (m->settings.config.rsync ? BLOCK_RSYNC : 0))) ||
        now < m->stage_at)
        return;
    m->copy_smbd = !m->binary_valid || !m->have_applied || !payload_same(&m->storage, &m->applied_storage);
    m->copy_rsync =
        m->settings.config.rsync && (!m->rsync_valid || m->copy_smbd || !m->applied_settings.config.rsync);
    if (m->copy_smbd)
        stop_role(&m->smb, now, 1);
    if (m->copy_smbd || m->copy_rsync)
        stop_role(&m->rsync, now, 1);
    if (m->copy_smbd && m->smb.child.group)
        return;
    if ((m->copy_smbd || m->copy_rsync) && m->rsync.child.group)
        return;
    /* Samba and discovery take their NetBIOS name from the hostname staged
     * here, not from the settings read: a read before ACPd sets the hostname
     * (the first one at boot) falls back to syNm. Setting it in m->settings
     * keeps the next read, derived from the same hostname, equal to it, and a
     * rename takes its new name in this same restage even while ACP reads
     * fail. Keep the read's name only if the hostname yields none. */
    char netbios[sizeof(m->settings.identity.netbios)];
    if (!normalize_netbios_name(netbios, sizeof(netbios), m->hostname))
        strcpy(m->settings.identity.netbios, netbios);
    m->stage_revision = m->revision;
    /* Applied config can still describe the old generation after an aborted
     * replacement. Track the RAM images separately so reverting the desired
     * payload cannot mistake a removed/partial image for that old generation. */
    if (m->copy_smbd)
        m->binary_valid = 0;
    if (m->copy_rsync)
        m->rsync_valid = 0;
    if (tc_child_fork(&m->stage_job, stage_job, m, MANAGER_LOG, NULL, 0, now + 120000))
        m->stage_at = now + JOB_RETRY_MS;
}
static void reconcile_discovery(struct manager *m, long long now) {
    int diskless = !m->have_applied || !m->smb.ready;
    const struct tc_share_set empty = {0};
    const struct tc_share_set *shares = diskless ? &empty : &m->applied_storage.shares;
    const char *name = diskless ? "" : m->applied_settings.identity.netbios;
    int afp = !diskless          ? m->applied_settings.config.advertise_afp
              : m->have_settings ? m->settings.config.advertise_afp
                                 : 0;
    int debug = m->have_settings ? m->settings.config.discovery_debug : 0;
    if (m->discovery.child.group && (m->discovery_diskless != diskless || strcmp(name, m->discovery_name) ||
                                     !tc_shares_equal(shares, &m->discovery_shares) ||
                                     afp != m->discovery_afp || debug != m->discovery_debug))
        stop_role(&m->discovery, now, 1);
    if (!m->discovery.child.group && m->ownership_ready && !(m->blocked & BLOCK_DISCOVERY)) {
        char *argv[8 + TC_MAX_VOLUMES * 5];
        char log[352];
        size_t n = 0, i;
        argv[n++] = TC_SERVICE_BIN;
        argv[n++] = "discovery";
        if (diskless)
            argv[n++] = "--diskless";
        else {
            argv[n++] = "--netbios-name";
            argv[n++] = (char *)name;
        }
        if (debug)
            argv[n++] = "--debug-logging";
        for (i = 0; i < shares->count; i++) {
            argv[n++] = "--adisk-share";
            argv[n++] = (char *)shares->values[i].name;
            argv[n++] = (char *)shares->values[i].device;
            argv[n++] = (char *)shares->values[i].uuid;
            argv[n++] = afp ? "0x83" : "0x82";
        }
        argv[n] = NULL;
        if (diskless)
            snprintf(log, sizeof(log), "%s", TC_RAM_ROOT "/var/discovery.log");
        else
            snprintf(log, sizeof(log), "%s/logs/discovery.log", m->applied_storage.payload);
        const struct tc_volume *volume = diskless ? NULL :
            &m->applied_storage.inventory.volumes[m->applied_storage.payload_index];
        int unbounded = !diskless && (m->settings.config.debug || m->settings.config.discovery_debug);
        if (!start_role(&m->discovery, argv, log, now, 1, volume, unbounded)) {
            m->discovery_diskless = diskless;
            m->discovery_shares = *shares;
            strcpy(m->discovery_name, name);
            m->discovery_afp = afp;
            m->discovery_debug = debug;
        }
    }
}
static void reconcile_roles(struct manager *m, long long now) {
    if (m->have_applied && !m->config_dirty && !m->storage_dirty && m->storage.payload_index >= 0 &&
        m->ownership_ready) {
        char log[352];
        snprintf(log, sizeof(log), "%s/logs/smbd-console.log", m->applied_storage.payload);
        char *argv[] = {TC_SMBD_BIN, "-F", "--no-process-group", "-s", TC_SMBD_CONF, NULL};
        const struct tc_volume *volume = &m->applied_storage.inventory.volumes[m->applied_storage.payload_index];
        int unbounded = m->applied_settings.config.debug || m->applied_settings.config.discovery_debug;
        if (!(m->blocked & BLOCK_SMB) && !m->smb.child.group && !start_role(&m->smb, argv, log, now, 1, volume, unbounded))
            m->audit_at = now + 1000;
        if (m->settings.config.rsync && !(m->blocked & BLOCK_RSYNC)) {
            char *rsync[] = {TC_RSYNC_BIN, "--daemon", "--no-detach", "--config=" TC_RSYNC_CONF, NULL};
            if (!m->rsync.child.group && !start_role(&m->rsync, rsync, TC_RAM_ROOT "/var/rsync.log", now, 1, NULL, 0))
                m->audit_at = now + 1000;
        }
    }
    if (!m->have_settings || !m->settings.config.rsync)
        stop_role(&m->rsync, now, 1);
    if ((!m->have_settings || !m->settings.config.rsync || m->storage.payload_index < 0) &&
        !m->rsync.child.group && !(m->blocked & BLOCK_RSYNC) && m->rsync_valid) {
        if ((!unlink(TC_RSYNC_BIN) || errno == ENOENT) && (!unlink(TC_RSYNC_CONF) || errno == ENOENT))
            m->rsync_valid = 0;
    }
    if (m->have_settings && m->settings.config.telemetry && m->ownership_ready &&
        !(m->blocked & BLOCK_TELEMETRY)) {
        char *argv[] = {TC_SERVICE_BIN, "telemetry", "--daemon", NULL};
        if (!m->telemetry.child.group)
            (void)start_role(&m->telemetry, argv, TC_RAM_ROOT "/var/telemetry.log", now, 0, NULL, 0);
    } else
        stop_role(&m->telemetry, now, 0);
    reconcile_discovery(m, now);
}
static void stopping(struct manager *m, long long now) {
    stop_role(&m->smb, now, 1);
    stop_role(&m->rsync, now, 1);
    stop_role(&m->discovery, now, 1);
    stop_role(&m->telemetry, now, 0);
    tc_child_stop(&m->storage_job, now, 1);
    tc_child_stop(&m->settings_job, now, 1);
    tc_child_stop(&m->stage_job, now, 1);
    tc_child_stop(&m->audit_job, now, 1);
    tc_child_stop(&m->mast_job, now, 1);
    tc_child_stop(&m->report_job, now, 1);
}
static int drained(struct manager *m, long long now) {
    struct tc_child *jobs[] = {&m->storage_job, &m->settings_job, &m->stage_job, &m->audit_job,
                               &m->mast_job, &m->report_job};
    size_t i;
    for (i = 0; i < sizeof(jobs) / sizeof(jobs[0]); i++) {
        if (jobs[i]->group && tc_child_poll(jobs[i], now))
            tc_child_close(jobs[i]);
        if (jobs[i]->group)
            return 0;
    }
    return !m->smb.child.group && !m->rsync.child.group && !m->discovery.child.group &&
           !m->telemetry.child.group;
}
int tc_manager_main(int argc, char **argv) {
    struct manager *m;
    int lock, result = 0;
    (void)argv;
    if (argc != 1)
        return 2;
    /* The installed image is a stable existing inode; no PID/lock marker file.
     * All exec children close this FD. Deployment stops us before replacement. */
    lock = open(TC_SERVICE_BIN, O_RDONLY);
    if (lock < 0 || flock(lock, LOCK_EX | LOCK_NB)) {
        if (lock >= 0)
            close(lock);
        return 1;
    }
    fcntl(lock, F_SETFD, FD_CLOEXEC);
    m = calloc(1, sizeof(*m));
    if (!m) {
        close(lock);
        return 1;
    }
    m->storage.payload_index = m->applied_storage.payload_index = -1;
    if (tc_events_init(&m->events)) {
        free(m);
        close(lock);
        return 1;
    }
    m->started_ms = m->hostname_wait_since = acp_monotonic_ms();
    set_manager_title(m);
    m->storage_dirty = 1;
    timestamped_fprintf(stderr, "manager: starting native supervision\n");
    for (;;) {
        fd_set reads;
        int maxfd = -1;
        long long now = acp_monotonic_ms(), deadline = now + TC_MANAGER_PASS_MS;
        unsigned events = tc_events_take(&m->events);
        if (events & TC_EVENT_STOP)
            m->stopping = 1;
        poll_role(&m->smb, "smbd", now, 1);
        poll_role(&m->rsync, "rsync", now, 1);
        poll_role(&m->discovery, "discovery", now, 1);
        poll_role(&m->telemetry, "telemetry", now, 0);
        pump_sample(m, now);
        if (m->stopping || acp_stop_requested) {
            if (!m->stopping)
                m->stopping = 1;
            stopping(m, now);
            if (drained(m, now))
                break;
        } else {
            observe_hostname(m, now);
            if (tc_events_disks(&m->events))
                physical_event(m, now);
            if (events & TC_EVENT_RELOAD) {
                if (storage_pending(m) || m->storage_dirty) {
                    m->storage_retry.at = now;
                    m->storage_at = now;
                }
                m->mast_at = m->settings_at = m->audit_at = now;
            }
            pump_inventory(m, now);
            pump_settings(m, now);
            pump_storage(m, now);
            pump_audit(m, now);
            pump_stage(m, now);
            reconcile_roles(m, now);
        }
        FD_ZERO(&reads);
        tc_events_prepare(&m->events, &reads, &maxfd);
        struct tc_child *children[] = {&m->smb.child,       &m->rsync.child,  &m->discovery.child,
                                       &m->telemetry.child, &m->settings_job, &m->storage_job,
                                       &m->stage_job,       &m->audit_job, &m->mast_job,
                                       &m->report_job};
        size_t i;
        for (i = 0; i < sizeof(children) / sizeof(children[0]); i++)
            tc_child_prepare(children[i], &reads, &maxfd, &deadline);
        if (!m->stopping) {
            if (!m->mast_job.group)
                lower(&deadline, m->mast_at);
            if (!m->settings_job.group)
                lower(&deadline, m->settings_at);
            if (!m->audit_job.group)
                lower(&deadline, m->audit_at);
            if (!m->storage_job.group && !m->stage_job.group && m->have_settings &&
                m->topology.initialized && !m->topology.pending) {
                if (m->storage_dirty) lower(&deadline, m->storage_at);
                else if (storage_pending(m))
                    lower(&deadline, m->storage_retry.at > m->storage_at ? m->storage_retry.at : m->storage_at);
            }
        }
        if (tc_wait_until(&reads, maxfd, now, deadline) < 0) {
            result = 1;
            m->stopping = 1;
        }
    }
    tc_events_close(&m->events);
    bufstall_restore();
    tc_samba_discard();
    tc_child_close(&m->diskd.child);
    free(m);
    close(lock);
    return result;
}
