#include "../common/events.h"
#include "../common/loop.h"
#include "../common/log.h"
#include "../common/process.h"
#include "../common/worker.h"
#include "../samba/staging.h"
#include "../storage/settle.h"
#include "inspect.h"
#include <sys/file.h>
#include <sys/stat.h>

#define MANAGER_LOG TC_RAM_ROOT "/var/runtime.log"
#define SETTINGS_MS 30000
#define INVENTORY_MS 10000
#define AUDIT_MS 30000
#define JOB_RETRY_MS 5000
#ifndef TC_DISKD_PATH
#define TC_DISKD_PATH "/sbin/diskd"
#endif
enum { BLOCK_SMB = 1, BLOCK_RSYNC = 2, BLOCK_DISCOVERY = 4, BLOCK_TELEMETRY = 8 };
struct stale_process {
    pid_t pid;
    long long since;
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
    int smb_probe, rsync_probe;
    unsigned smb, rsync;
    pid_t smb_pid, rsync_pid;
};
struct mast_result {
    int status;
    size_t length;
    char text[TC_MAST_MAX + 1];
};
struct manager {
    struct tc_events events;
    struct plan_loop network;
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
    char bindings[TC_BIND_TOKENS_MAX], applied_bindings[TC_BIND_TOKENS_MAX];
    struct tc_share_set discovery_shares;
    char discovery_name[16];
    int discovery_diskless, discovery_afp, discovery_debug;
    int have_settings, have_bindings, have_applied, stopping, tune_ata;
    int storage_dirty, config_dirty, ownership_ready;
    unsigned blocked;
    struct stale_process stale[TC_PROCESS_MAX];
    size_t stale_count;
    int copy_smbd, copy_rsync, binary_valid, rsync_valid;
    unsigned revision, storage_revision, storage_generation, stage_revision;
    long long mast_at, settings_at, storage_at, stage_at, audit_at, bindings_at;
};

static void lower(long long *deadline, long long value) {
    if (value >= 0 && (*deadline < 0 || value < *deadline))
        *deadline = value;
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
    char hostname[256];
    if (!gethostname(hostname, sizeof(hostname))) {
        hostname[sizeof(hostname) - 1] = 0;
        if (tc_hosts_ensure(TC_HOSTS_PATH, hostname))
            fprintf(stderr, "settings: local hostname resolution could not update %s\n", TC_HOSTS_PATH);
    }
    if (tc_samba_settings_read(&result))
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
    if (m->copy_smbd && tc_samba_clear_locks())
        return tc_worker_finish(1);
    return tc_worker_finish(tc_samba_stage(&m->storage, &m->settings, m->bindings, m->copy_smbd, m->copy_rsync) ? 1 : 0);
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
    if (tc_process_table_read(&result.table))
        return tc_worker_finish(1);
    result.smb_pid = m->smb.child.pid;
    result.rsync_pid = m->rsync.child.pid;
    if (m->smb.child.pid && !m->smb.child.stopping)
        result.smb_probe = tc_process_listeners(m->smb.child.pid, 445, &result.smb) == 0;
    if (m->rsync.child.pid && !m->rsync.child.stopping)
        result.rsync_probe = tc_process_listeners(m->rsync.child.pid, 873, &result.rsync) == 0;
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
        c->telemetry = c->nbns = c->discovery_debug = 0;
        c->mount_attempts = c->mount_timeout = c->mount_poll = c->ata_idle = 0;
        memset(c->ata_standby, 0, sizeof(c->ata_standby));
    }
    return !memcmp(&first, &second, sizeof(first));
}
static void physical_event(struct manager *m, long long now) {
    invalidate_storage(m, now);
    m->mast_at = now;
    /* A cable bump can return the exact same UUID/dkN before MaSt is read.
     * Keep this check independently of topology equality. Samba inspects its
     * own retained bindings; the global event cannot identify a USB share. */
    if (!m->bindings_at) {
        m->bindings_at = now + TC_STORAGE_SETTLE_MS;
        m->storage_at = m->bindings_at;
    }
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
static unsigned required_families(const char *bindings) {
    unsigned families = 0;
    char copy[TC_BIND_TOKENS_MAX], *token, *save;
    snprintf(copy, sizeof(copy), "%s", bindings);
    for (token = strtok_r(copy, " ", &save); token; token = strtok_r(NULL, " ", &save)) {
        if (strchr(token, ':')) {
            if (strcmp(token, "::1/128"))
                families |= 2;
        } else if (strncmp(token, "127.", 4))
            families |= 1;
    }
    return families ? families : 1;
}
static void apply_audit(struct manager *m, long long now) {
    size_t i;
    int conflict = 0, controllers = 0, diskd = 0, external = 0;
    struct stale_process stale[TC_PROCESS_MAX];
    size_t stale_count = 0;
    m->blocked = 0;
    const struct tc_process_table *table = &m->audit_result.table;
    for (i = 0; i < table->count; i++) {
        if (table->processes[i].role == TC_PROC_DISCOVERY)
            controllers++;
        if (table->processes[i].role == TC_PROC_WCIFSFS)
            conflict = 1;
        if (table->processes[i].role == TC_PROC_DISKD || table->processes[i].role == TC_PROC_DISKD_LOOPBACK)
            diskd++;
    }
    for (i = 0; i < table->count; i++) {
        const struct tc_process_info *p = &table->processes[i];
        int stop = p->role == TC_PROC_WCIFSFS || p->role == TC_PROC_DISKD;
        if (p->role == TC_PROC_SMBD && p->group != m->smb.child.group)
            stop = 1;
        if (p->role == TC_PROC_RSYNC && p->group != m->rsync.child.group)
            stop = 1;
        if (p->role == TC_PROC_DISCOVERY && p->pid != m->discovery.child.pid)
            stop = 1;
        if (p->role == TC_PROC_TELEMETRY && p->pid != m->telemetry.child.pid)
            stop = 1;
        /* Never kill native NBNS independently of its live controller. */
        if (p->role == TC_PROC_WCIFSND && !controllers)
            stop = 1;
        if (stop) {
            size_t j;
            long long since = now;
            for (j = 0; j < m->stale_count; j++)
                if (m->stale[j].pid == p->pid)
                    since = m->stale[j].since;
            stale[stale_count].pid = p->pid;
            stale[stale_count++].since = since;
            int sig = now - since >= 10000 && p->role != TC_PROC_TELEMETRY ? SIGKILL : SIGTERM;
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
    if (conflict)
        stop_role(&m->discovery, now, 1);
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
        unsigned need = required_families(m->applied_bindings);
        if ((m->audit_result.smb & need) == need)
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
        m->audit_at = now + AUDIT_MS;
        if (tc_child_fork(&m->audit_job, audit_job, m, MANAGER_LOG, &m->audit_result, sizeof(m->audit_result),
                          now + 20000))
            m->audit_at = now + JOB_RETRY_MS;
    }
}
static void pump_stage(struct manager *m, long long now) {
    if (m->stage_job.group && tc_child_poll(&m->stage_job, now)) {
        if (tc_child_ok(&m->stage_job) && !m->stage_job.stopping && m->stage_revision == m->revision &&
            !tc_samba_publish(m->settings.config.rsync)) {
            m->applied_storage = m->storage;
            m->applied_settings = m->settings;
            strcpy(m->applied_bindings, m->bindings);
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
    if (!m->config_dirty || !m->have_settings || !m->have_bindings || m->storage.payload_index < 0 ||
        m->storage_dirty || m->topology.pending || m->storage_job.group || m->stage_job.group ||
        !m->ownership_ready || (m->blocked & (BLOCK_SMB | (m->settings.config.rsync ? BLOCK_RSYNC : 0))) ||
        now < m->stage_at)
        return;
    m->copy_smbd = !m->binary_valid || !m->have_applied || !payload_same(&m->storage, &m->applied_storage);
    m->copy_rsync =
        m->settings.config.rsync && (!m->rsync_valid || m->copy_smbd || !m->applied_settings.config.rsync);
    int bind_changed = m->have_applied && strcmp(m->bindings, m->applied_bindings);
    if (m->copy_smbd || bind_changed)
        stop_role(&m->smb, now, 1);
    if (m->copy_smbd || m->copy_rsync)
        stop_role(&m->rsync, now, 1);
    if ((m->copy_smbd || bind_changed) && m->smb.child.group)
        return;
    if ((m->copy_smbd || m->copy_rsync) && m->rsync.child.group)
        return;
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
    tc_child_stop(&m->network.collection_job, now, 1);
}
static int drained(struct manager *m, long long now) {
    struct tc_child *jobs[] = {&m->storage_job, &m->settings_job, &m->stage_job, &m->audit_job,
                               &m->mast_job, &m->network.collection_job};
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
    struct plan_options options = {0};
    int lock, result = 0;
    const char *facts_file = NULL;
    (void)argv;
#ifdef TC_NATIVE_TEST
    if (argc == 3 && !strcmp(argv[1], "--facts-file")) {
        facts_file = argv[2];
        argc = 1;
    }
#endif
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
#if defined(__NetBSD__)
    setproctitle("role=manager");
#endif
    plan_loop_init(&m->network, &options, facts_file);
    m->network.owned_collections = 1;
    m->storage_dirty = 1;
    timestamped_fprintf(stderr, "manager: starting native supervision\n");
    for (;;) {
        fd_set reads;
        int maxfd = -1;
        long long now = plan_loop_now_ms(), deadline = now + 1000;
        unsigned events = tc_events_take(&m->events);
        if (events & TC_EVENT_STOP)
            m->stopping = 1;
        poll_role(&m->smb, "smbd", now, 1);
        poll_role(&m->rsync, "rsync", now, 1);
        poll_role(&m->discovery, "discovery", now, 1);
        poll_role(&m->telemetry, "telemetry", now, 0);
        if (m->stopping || acp_stop_requested) {
            if (!m->stopping)
                m->stopping = 1;
            stopping(m, now);
            if (drained(m, now))
                break;
        } else {
            if (tc_events_disks(&m->events))
                physical_event(m, now);
            if (events & TC_EVENT_RELOAD) {
                if (storage_pending(m) || m->storage_dirty) {
                    m->storage_retry.at = now;
                    m->storage_at = now;
                }
                m->mast_at = m->settings_at = m->audit_at = now;
                plan_loop_request(&m->network, now);
            }
            pump_inventory(m, now);
            pump_settings(m, now);
            pump_storage(m, now);
            pump_audit(m, now);
            if (m->bindings_at && now >= m->bindings_at) {
                if (m->smb.child.pid && !m->smb.child.stopping)
                    kill(m->smb.child.pid, SIGHUP);
                m->bindings_at = 0;
            }
            pump_stage(m, now);
            reconcile_roles(m, now);
        }
        FD_ZERO(&reads);
        tc_events_prepare(&m->events, &reads, &maxfd);
        struct tc_child *children[] = {&m->smb.child,       &m->rsync.child,  &m->discovery.child,
                                       &m->telemetry.child, &m->settings_job, &m->storage_job,
                                       &m->stage_job,       &m->audit_job, &m->mast_job, &m->network.collection_job};
        size_t i;
        for (i = 0; i < sizeof(children) / sizeof(children[0]); i++)
            tc_child_prepare(children[i], &reads, &maxfd, &deadline);
        if (!m->stopping) {
            plan_loop_prepare(&m->network, now, &reads, &maxfd, &deadline);
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
            if (m->bindings_at)
                lower(&deadline, m->bindings_at);
        }
        if (plan_loop_wait(&reads, maxfd, now, deadline) < 0) {
            result = 1;
            m->stopping = 1;
        }
        if (!m->stopping && plan_loop_dispatch(&m->network, plan_loop_now_ms(), &reads)) {
            char bindings[TC_BIND_TOKENS_MAX];
            if (!device_plan_bind_tokens(&m->network.current, bindings, sizeof(bindings)) &&
                (!m->have_bindings || strcmp(bindings, m->bindings))) {
                strcpy(m->bindings, bindings);
                m->have_bindings = 1;
                changed(m, plan_loop_now_ms());
            }
        }
    }
    plan_loop_close(&m->network);
    tc_events_close(&m->events);
    tc_samba_discard();
    tc_child_close(&m->diskd.child);
    free(m);
    close(lock);
    return result;
}
