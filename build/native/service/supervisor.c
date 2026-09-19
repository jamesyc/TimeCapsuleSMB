#include "service.h"
#include "supervisor.h"
#include "../common/config.h"
#include "../common/ipc.h"
#include "../storage/mast.h"
#include "../storage/shares.h"
#include "../samba/runtime.h"

#ifndef TC_SERVICE_STATE_DIR
#define TC_SERVICE_STATE_DIR "/mnt/Memory/timecapsulesmb"
#endif
#ifndef TC_SERVICE_LOCK_PATH
#define TC_SERVICE_LOCK_PATH TC_SERVICE_STATE_DIR "/service.lock"
#endif
#ifndef TC_SERVICE_SOCKET_PATH
#define TC_SERVICE_SOCKET_PATH TC_SERVICE_STATE_DIR "/service.sock"
#endif
#ifndef TC_SERVICE_LOG_PATH
#define TC_SERVICE_LOG_PATH TC_SERVICE_STATE_DIR "/service.log"
#endif
#ifndef TC_XATTR_RECEIPT_PATH
#define TC_XATTR_RECEIPT_PATH "/mnt/Flash/xattr-upgrade.state"
#endif

enum child_state { CHILD_DISABLED, CHILD_STOPPED, CHILD_STARTING, CHILD_READY, CHILD_DEGRADED };
#define TC_CHILD_COUNT 5

struct supervised_child {
    const char *name;
    uint16_t role;
    int enabled;
    pid_t pid;
    int fd;
    enum child_state state;
    unsigned failures;
    long long restart_at;
};

struct supervisor {
    const char *program;
    uint64_t instance;
    uint64_t generation;
    int lock_fd;
    int listen_fd;
    int stop;
    int reload;
    char netbios[16];
    struct tc_runtime_config config;
    struct tc_inventory inventory;
    struct tc_share_set shares;
    struct device_plan plan;
    struct device_plan last_validated;
    int have_plan;
    unsigned long service_signature;
    long long storage_poll_at;
    struct supervised_child children[TC_CHILD_COUNT];
};

static volatile sig_atomic_t supervisor_signal;
static void close_other_descriptors(int keep);
static void supervisor_on_signal(int signal_number) {
    supervisor_signal = signal_number;
}

static const char *child_state_name(enum child_state state) {
    switch (state) {
    case CHILD_DISABLED: return "disabled";
    case CHILD_STARTING: return "starting";
    case CHILD_READY: return "ready";
    case CHILD_DEGRADED: return "degraded";
    default: return "stopped";
    }
}

static int receipt_allows_runtime(void) {
    FILE *file = fopen(TC_XATTR_RECEIPT_PATH, "r");
    char line[256];
    int state_seen = 0, valid = 1;
    if (file == NULL) return errno == ENOENT ? 1 : 0;
    while (fgets(line, sizeof(line), file) != NULL) {
        line[strcspn(line, "\r\n")] = '\0';
        if (!strncmp(line, "state=", 6)) {
            if (state_seen || strcmp(line + 6, "complete")) valid = 0;
            state_seen = 1;
        }
    }
    if (ferror(file)) valid = 0;
    fclose(file);
    return valid && state_seen;
}

static int acquire_singleton(void) {
    struct flock lock;
    int fd;
    if (mkdir(TC_SERVICE_STATE_DIR, 0700) != 0 && errno != EEXIST) return -1;
    if (chmod(TC_SERVICE_STATE_DIR, 0700) != 0) return -1;
    fd = open(TC_SERVICE_LOCK_PATH, O_RDWR | O_CREAT, 0600);
    if (fd < 0) return -1;
    memset(&lock, 0, sizeof(lock));
    lock.l_type = F_WRLCK; lock.l_whence = SEEK_SET;
    if (fcntl(fd, F_SETLK, &lock) != 0) { close(fd); return -1; }
    return fd;
}

static void start_logging(void) {
    struct stat status;
    time_t now = time(NULL);
    struct tm *local = localtime(&now);
    char timestamp[32] = "0000-00-00 00:00:00";
    int fd = open(TC_SERVICE_LOG_PATH, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd < 0) return;
    if (fstat(fd, &status) == 0 && status.st_size > 1024 * 1024) (void)ftruncate(fd, 0);
    (void)dup2(fd, STDOUT_FILENO); (void)dup2(fd, STDERR_FILENO);
    if (fd > STDERR_FILENO) close(fd);
    if (local != NULL) (void)strftime(timestamp, sizeof(timestamp), "%Y-%m-%d %H:%M:%S", local);
    fprintf(stderr, "%s service starting epoch=%ld pid=%ld\n", timestamp, (long)now, (long)getpid());
}

#ifndef TC_NATIVE_TEST
static int run_and_wait(char *const arguments[]) {
    pid_t child = fork();
    int status;
    if (child == 0) { execv(arguments[0], arguments); _exit(127); }
    if (child < 0) return -1;
    while (waitpid(child, &status, 0) < 0) if (errno != EINTR) return -1;
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

static void stop_firmware_conflicts(const struct tc_runtime_config *config) {
    char *wcifsfs[] = {"/usr/bin/pkill", "-x", "wcifsfs", NULL};
    char *afp[] = {"/usr/bin/pkill", "-x", "afpserver", NULL};
    (void)run_and_wait(wcifsfs);
    if (!config->advertise_afp) (void)run_and_wait(afp);
}

static void stop_surviving_managed_processes(void) {
    static const char *const names[] = {"smbd", "rsync", "wcifsnd", "wcifsfs", "discoveryd", "telemetry"};
    size_t i;
    for (i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
        char *arguments[] = {"/usr/bin/pkill", "-x", (char *)names[i], NULL};
        (void)run_and_wait(arguments);
    }
    sleep(1);
    for (i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
        char *arguments[] = {"/usr/bin/pkill", "-KILL", "-x", (char *)names[i], NULL};
        (void)run_and_wait(arguments);
    }
}

static void relaunch_diskd_loopback(void) {
    char *stop[] = {"/usr/bin/pkill", "-x", "diskd", NULL};
    pid_t child;
    (void)run_and_wait(stop);
    child = fork();
    if (child == 0) {
        close_other_descriptors(-1);
        execl("/sbin/diskd", "diskd", "-i", "lo0", "-d", "local.", (char *)NULL);
        _exit(127);
    }
}
#endif

static int open_admin_socket(void) {
    struct sockaddr_un address;
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    if (strlen(TC_SERVICE_SOCKET_PATH) >= sizeof(address.sun_path)) { close(fd); errno = ENAMETOOLONG; return -1; }
    strcpy(address.sun_path, TC_SERVICE_SOCKET_PATH);
    unlink(TC_SERVICE_SOCKET_PATH);
    if (bind(fd, (struct sockaddr *)&address, sizeof(address)) != 0 ||
        chmod(TC_SERVICE_SOCKET_PATH, 0600) != 0 || listen(fd, 4) != 0 ||
        tc_ipc_configure_fd(fd, 1, 1) != 0) {
        close(fd); unlink(TC_SERVICE_SOCKET_PATH); return -1;
    }
    return fd;
}

static void close_other_descriptors(int keep) {
    long limit = sysconf(_SC_OPEN_MAX);
    int fd;
    if (limit < 0 || limit > 4096) limit = 256;
    for (fd = 3; fd < limit; fd++) if (fd != keep) close(fd);
}

static int spawn_child(struct supervisor *supervisor, struct supervised_child *child) {
    int channels[2] = {-1, -1};
    pid_t pid;
    char fd_text[16];
    char rsync_config[512];
    char *arguments[128];
    const char *executable = supervisor->program;
    int controlled = child->role == TC_ROLE_MDNS || child->role == TC_ROLE_NETBIOS || child->role == TC_ROLE_TELEMETRY;
    int count = 0;
    size_t i;
    if (!child->enabled) { child->state = CHILD_DISABLED; return 0; }
    if (controlled && socketpair(AF_UNIX, SOCK_STREAM, 0, channels) != 0) return -1;
    if (controlled && (tc_ipc_configure_fd(channels[0], 1, 1) != 0 ||
        tc_ipc_configure_fd(channels[1], 0, 0) != 0)) {
        close(channels[0]); close(channels[1]); return -1;
    }
    if (controlled) {
        snprintf(fd_text, sizeof(fd_text), "%d", channels[1]);
        arguments[count++] = (char *)supervisor->program;
        arguments[count++] = (char *)child->name;
        if ((child->role == TC_ROLE_MDNS || child->role == TC_ROLE_NETBIOS) && supervisor->shares.count == 0)
            arguments[count++] = "--diskless";
        if (child->role == TC_ROLE_NETBIOS) {
            arguments[count++] = "--netbios-name"; arguments[count++] = supervisor->netbios;
        }
        if (child->role == TC_ROLE_MDNS) for (i = 0; i < supervisor->shares.count; i++) {
            struct tc_share *share = &supervisor->shares.values[i];
            arguments[count++] = "--adisk-share"; arguments[count++] = share->name;
            arguments[count++] = share->device; arguments[count++] = share->uuid;
            arguments[count++] = "0x82";
        }
        if (child->role == TC_ROLE_TELEMETRY) arguments[count++] = "--daemon";
        arguments[count++] = "--control-fd"; arguments[count++] = fd_text;
    } else if (child->role == TC_ROLE_SAMBA) {
        executable = TC_SAMBA_BIN;
        arguments[count++] = (char *)executable; arguments[count++] = "-F";
        arguments[count++] = "--no-process-group"; arguments[count++] = "-s";
        arguments[count++] = TC_SAMBA_CONF;
    } else if (child->role == TC_ROLE_RSYNC) {
        executable = TC_RSYNC_BIN;
        if (snprintf(rsync_config, sizeof(rsync_config), "%s/rsyncd.conf", supervisor->config.payload_dir) >=
            (int)sizeof(rsync_config)) return -1;
        arguments[count++] = (char *)executable; arguments[count++] = "--daemon";
        arguments[count++] = "--no-detach"; arguments[count++] = "--config";
        arguments[count++] = rsync_config;
    }
    arguments[count] = NULL;
    pid = fork();
    if (pid == 0) {
        setpgid(0, 0);
        signal(SIGTERM, SIG_DFL); signal(SIGINT, SIG_DFL); signal(SIGCHLD, SIG_DFL); signal(SIGPIPE, SIG_DFL);
        if (controlled) close(channels[0]);
        close_other_descriptors(controlled ? channels[1] : -1);
        execv(executable, arguments);
        _exit(127);
    }
    if (controlled) close(channels[1]);
    if (pid < 0) { if (controlled) close(channels[0]); return -1; }
    child->pid = pid; child->fd = controlled ? channels[0] : -1; child->state = CHILD_STARTING;
    if (controlled && tc_ipc_send(child->fd, TC_IPC_INIT, child->role, supervisor->instance,
                    supervisor->generation, NULL, 0) != 0) {
        kill(-pid, SIGTERM); close(child->fd); child->fd = -1; return -1;
    }
    return 0;
}

static void schedule_restart(struct supervised_child *child, long long now) {
    unsigned delay;
    child->pid = 0;
    if (child->fd >= 0) close(child->fd);
    child->fd = -1; child->state = child->enabled ? CHILD_STOPPED : CHILD_DISABLED;
    child->failures++;
    delay = child->failures > 6 ? 60U : 1U << (child->failures - 1);
    child->restart_at = now + (long long)delay * 1000;
}

static void stop_child(struct supervised_child *child);

static void reap_children(struct supervisor *supervisor, long long now) {
    int status;
    pid_t pid;
    while ((pid = waitpid(-1, &status, WNOHANG)) > 0) {
        size_t i;
        for (i = 0; i < sizeof(supervisor->children) / sizeof(supervisor->children[0]); i++) {
            if (supervisor->children[i].pid == pid) {
                uint16_t role = supervisor->children[i].role;
                schedule_restart(&supervisor->children[i], now);
                if (role == TC_ROLE_SAMBA) {
                    size_t j;
                    for (j = 0; j < TC_CHILD_COUNT; j++)
                        if (supervisor->children[j].role == TC_ROLE_MDNS ||
                            supervisor->children[j].role == TC_ROLE_NETBIOS)
                            stop_child(&supervisor->children[j]);
                }
            }
        }
    }
}

static void stop_child(struct supervised_child *child) {
    int status, attempts;
    if (child->pid <= 0) return;
    (void)tc_ipc_send(child->fd, TC_IPC_STOP, child->role, 1, 1, NULL, 0);
    kill(-child->pid, SIGTERM);
    for (attempts = 0; attempts < 20; attempts++) {
        pid_t waited = waitpid(child->pid, &status, WNOHANG);
        if (waited == child->pid || (waited < 0 && errno == ECHILD)) break;
        usleep(100000);
    }
    if (attempts == 20) { kill(-child->pid, SIGKILL); (void)waitpid(child->pid, &status, 0); }
    if (child->fd >= 0) close(child->fd);
    child->pid = 0; child->fd = -1; child->state = CHILD_STOPPED;
}

static int child_can_start(const struct supervisor *supervisor,
                           const struct supervised_child *child) {
    size_t i;
    if (child->role != TC_ROLE_MDNS && child->role != TC_ROLE_NETBIOS) return 1;
    for (i = 0; i < TC_CHILD_COUNT; i++)
        if (supervisor->children[i].role == TC_ROLE_SAMBA)
            return !supervisor->children[i].enabled || supervisor->children[i].state == CHILD_READY;
    return 0;
}

static int start_child_checked(struct supervisor *supervisor,
                               struct supervised_child *child) {
    if (spawn_child(supervisor, child) != 0) return -1;
    if (child->role == TC_ROLE_SAMBA) {
        if (tc_samba_listener_ready(10) != 0) {
            stop_child(child);
            return -1;
        }
        child->state = CHILD_READY;
        child->failures = 0;
    } else if (child->role == TC_ROLE_RSYNC) {
        sleep(1);
        if (kill(child->pid, 0) != 0) { stop_child(child); return -1; }
        child->state = CHILD_READY;
        child->failures = 0;
    }
    return 0;
}

static void admin_reply(struct supervisor *supervisor, int client, const char *command) {
    char response[1024];
    size_t used = 0, i;
    if (!strcmp(command, "stop")) supervisor->stop = 1;
    else if (!strcmp(command, "reload")) supervisor->reload = 1;
    used += (size_t)snprintf(response + used, sizeof(response) - used,
                             "instance=%llu generation=%llu\n",
                             (unsigned long long)supervisor->instance,
                             (unsigned long long)supervisor->generation);
    for (i = 0; i < sizeof(supervisor->children) / sizeof(supervisor->children[0]) && used < sizeof(response); i++) {
        struct supervised_child *child = &supervisor->children[i];
        used += (size_t)snprintf(response + used, sizeof(response) - used,
            "role=%s state=%s pid=%ld failures=%u\n", child->name,
            child_state_name(child->state), (long)child->pid, child->failures);
    }
    (void)write(client, response, used < sizeof(response) ? used : sizeof(response));
}

static void accept_admin(struct supervisor *supervisor) {
    int client = accept(supervisor->listen_fd, NULL, NULL);
    char command[32];
    ssize_t got;
    if (client < 0) return;
    got = read(client, command, sizeof(command) - 1);
    if (got > 0) {
        command[got] = '\0'; command[strcspn(command, "\r\n ")] = '\0';
        if (!strcmp(command, "status") || !strcmp(command, "reload") || !strcmp(command, "stop"))
            admin_reply(supervisor, client, command);
    }
    close(client);
}

static unsigned long hash_bytes(unsigned long hash, const void *data, size_t length) {
    const unsigned char *bytes = data;
    while (length--) { hash ^= *bytes++; hash *= 16777619UL; }
    return hash;
}

static unsigned long controller_signature(const struct tc_share_set *shares,
                                          const struct device_plan *plan) {
    char bind[TC_BIND_TOKENS_MAX];
    unsigned long hash = 2166136261UL;
    size_t i;
    if (device_plan_bind_tokens(plan, bind, sizeof(bind)) != 0) return 0;
    hash = hash_bytes(hash, bind, strlen(bind));
    for (i = 0; i < shares->count; i++) {
        hash = hash_bytes(hash, shares->values[i].name, strlen(shares->values[i].name));
        hash = hash_bytes(hash, shares->values[i].path, strlen(shares->values[i].path));
        hash = hash_bytes(hash, shares->values[i].uuid, strlen(shares->values[i].uuid));
    }
    return hash ? hash : 1;
}

/* Returns 1 with a prepared Samba generation, 0 for a confirmed no-service
 * state, and -1 for an observation failure that must retain current policy. */
static int collect_controller_state(struct supervisor *supervisor) {
    struct tc_inventory inventory;
    struct tc_share_set shares;
    struct device_plan plan;
    struct plan_options options;
    unsigned long signature;
    size_t i;
    if (tc_mast_collect(&inventory) != 0) return -1;
    for (i = 0; i < inventory.count; i++) (void)tc_storage_activate(&inventory.volumes[i]);
    if (tc_shares_build(&shares, &inventory, supervisor->config.internal_root) != 0) return -1;
    memset(&options, 0, sizeof(options));
    options.diskless = shares.count == 0;
    if (device_plan_collect(&plan, supervisor->have_plan ? &supervisor->last_validated : NULL, &options) != 0 ||
        plan.status.cold_start) return -1;
    signature = controller_signature(&shares, &plan);
    if (!signature) return -1;
    if (shares.count == 0 || !supervisor->config.payload_dir[0]) {
        supervisor->inventory = inventory; supervisor->shares = shares; supervisor->plan = plan;
        if (plan.status.validated) { supervisor->last_validated = plan; supervisor->have_plan = 1; }
        supervisor->service_signature = signature;
        return 0;
    }
    if (signature != supervisor->service_signature &&
        tc_samba_prepare(&supervisor->config, &plan, &shares) != 0) return -1;
    supervisor->inventory = inventory; supervisor->shares = shares; supervisor->plan = plan;
    if (plan.status.validated) { supervisor->last_validated = plan; supervisor->have_plan = 1; }
    if (plan.id.netbios[0]) strncpy(supervisor->netbios, plan.id.netbios, sizeof(supervisor->netbios) - 1);
    else strcpy(supervisor->netbios, "TIMECAPSULE");
    supervisor->service_signature = signature;
    return 1;
}

static void set_service_enablement(struct supervisor *supervisor, int prepared) {
    size_t i;
    for (i = 0; i < TC_CHILD_COUNT; i++) {
        struct supervised_child *child = &supervisor->children[i];
        int enabled = child->enabled;
        if (child->role == TC_ROLE_SAMBA || child->role == TC_ROLE_MDNS)
            enabled = prepared;
        else if (child->role == TC_ROLE_NETBIOS)
            enabled = prepared && supervisor->config.nbns;
        else if (child->role == TC_ROLE_RSYNC)
            enabled = prepared && supervisor->config.rsync;
        else if (child->role == TC_ROLE_TELEMETRY)
            enabled = supervisor->config.telemetry;
#ifdef TC_NATIVE_TEST
        if (child->role == TC_ROLE_MDNS && !prepared) enabled = 1;
#endif
        if (!enabled && child->pid > 0) stop_child(child);
        child->enabled = enabled;
        if (!enabled) child->state = CHILD_DISABLED;
    }
}

static void restart_service_generation(struct supervisor *supervisor, int prepared) {
    size_t i;
    for (i = 0; i < TC_CHILD_COUNT; i++)
        if (supervisor->children[i].role != TC_ROLE_TELEMETRY) stop_child(&supervisor->children[i]);
    set_service_enablement(supervisor, prepared);
    supervisor->generation++;
}

int tc_supervisor_run(const char *program) {
    struct supervisor supervisor;
    size_t i;
    int prepared;
    memset(&supervisor, 0, sizeof(supervisor));
    supervisor.program = program;
    supervisor.instance = ((uint64_t)time(NULL) << 32) ^ (uint64_t)getpid();
    if (!supervisor.instance) supervisor.instance = 1;
    supervisor.generation = 1;
    supervisor.lock_fd = acquire_singleton();
    if (supervisor.lock_fd < 0) { fputs("service: supervisor already running or lock unavailable\n", stderr); return 1; }
    start_logging();
    if (!receipt_allows_runtime()) { fputs("service: incomplete or invalid xattr migration receipt\n", stderr); close(supervisor.lock_fd); return 1; }
    supervisor.listen_fd = open_admin_socket();
    if (supervisor.listen_fd < 0) { close(supervisor.lock_fd); return 1; }
    if (tc_runtime_config_load(&supervisor.config) != 0) {
        fputs("service: invalid runtime configuration; file service disabled\n", stderr);
        memset(&supervisor.config, 0, sizeof(supervisor.config));
        supervisor.config.telemetry = 1;
    }
#ifndef TC_NATIVE_TEST
    stop_surviving_managed_processes();
    stop_firmware_conflicts(&supervisor.config);
    relaunch_diskd_loopback();
#endif
    supervisor.children[0] = (struct supervised_child){"mdns", TC_ROLE_MDNS, 0, 0, -1, CHILD_STOPPED, 0, 0};
    supervisor.children[1] = (struct supervised_child){"netbios", TC_ROLE_NETBIOS, 0, 0, -1, CHILD_STOPPED, 0, 0};
    supervisor.children[2] = (struct supervised_child){"telemetry", TC_ROLE_TELEMETRY, 0, 0, -1, CHILD_STOPPED, 0, 0};
    supervisor.children[3] = (struct supervised_child){"samba", TC_ROLE_SAMBA, 0, 0, -1, CHILD_STOPPED, 0, 0};
    supervisor.children[4] = (struct supervised_child){"rsync", TC_ROLE_RSYNC, 0, 0, -1, CHILD_STOPPED, 0, 0};
    prepared = collect_controller_state(&supervisor);
    set_service_enablement(&supervisor, prepared == 1);
    supervisor.storage_poll_at = acp_monotonic_ms() + 10000;
    signal(SIGTERM, supervisor_on_signal); signal(SIGINT, supervisor_on_signal);
    signal(SIGHUP, supervisor_on_signal); signal(SIGCHLD, supervisor_on_signal); signal(SIGPIPE, SIG_IGN);
    for (i = 0; i < TC_CHILD_COUNT; i++) if (supervisor.children[i].enabled &&
        child_can_start(&supervisor, &supervisor.children[i]) &&
        start_child_checked(&supervisor, &supervisor.children[i]) != 0)
        schedule_restart(&supervisor.children[i], acp_monotonic_ms());
    while (!supervisor.stop) {
        fd_set reads;
        struct timeval timeout;
        int maxfd = supervisor.listen_fd;
        long long now = acp_monotonic_ms();
        FD_ZERO(&reads); FD_SET(supervisor.listen_fd, &reads);
        for (i = 0; i < TC_CHILD_COUNT; i++) {
            struct supervised_child *child = &supervisor.children[i];
            if (child->fd >= 0) { FD_SET(child->fd, &reads); if (child->fd > maxfd) maxfd = child->fd; }
            if (child->enabled && child->pid == 0 && now >= child->restart_at &&
                child_can_start(&supervisor, child) && start_child_checked(&supervisor, child) != 0)
                schedule_restart(child, now);
        }
        timeout.tv_sec = 1; timeout.tv_usec = 0;
        if (select(maxfd + 1, &reads, NULL, NULL, &timeout) > 0) {
            if (FD_ISSET(supervisor.listen_fd, &reads)) accept_admin(&supervisor);
            for (i = 0; i < TC_CHILD_COUNT; i++) if (supervisor.children[i].fd >= 0 && FD_ISSET(supervisor.children[i].fd, &reads)) {
                struct tc_ipc_message message;
                int rc = tc_ipc_recv(supervisor.children[i].fd, &message);
                if (rc == 1 && message.instance == supervisor.instance && message.role == supervisor.children[i].role) {
                    if (message.type == TC_IPC_READY) { supervisor.children[i].state = CHILD_READY; supervisor.children[i].failures = 0; }
                    else if (message.type == TC_IPC_DEGRADED) supervisor.children[i].state = CHILD_DEGRADED;
                }
            }
        }
        if (supervisor_signal == SIGTERM || supervisor_signal == SIGINT) supervisor.stop = 1;
        if (supervisor_signal == SIGHUP) supervisor.reload = 1;
        if (supervisor_signal == SIGCHLD) reap_children(&supervisor, acp_monotonic_ms());
        supervisor_signal = 0;
        reap_children(&supervisor, acp_monotonic_ms());
        now = acp_monotonic_ms();
        if (now >= supervisor.storage_poll_at) {
            unsigned long previous_signature = supervisor.service_signature;
            int refreshed = collect_controller_state(&supervisor);
            supervisor.storage_poll_at = now + 10000;
#ifndef TC_NATIVE_TEST
            stop_firmware_conflicts(&supervisor.config);
#endif
            if (refreshed >= 0 && supervisor.service_signature != previous_signature)
                restart_service_generation(&supervisor, refreshed == 1);
        }
        if (supervisor.reload) {
            struct tc_runtime_config updated;
            supervisor.reload = 0;
            if (tc_runtime_config_load(&updated) == 0) supervisor.config = updated;
            supervisor.service_signature = 0;
            prepared = collect_controller_state(&supervisor);
            if (prepared >= 0) restart_service_generation(&supervisor, prepared == 1);
            for (i = 0; i < TC_CHILD_COUNT; i++) if (supervisor.children[i].fd >= 0)
                (void)tc_ipc_send(supervisor.children[i].fd, TC_IPC_REFRESH,
                    supervisor.children[i].role, supervisor.instance, supervisor.generation, NULL, 0);
        }
    }
    for (i = 0; i < TC_CHILD_COUNT; i++) stop_child(&supervisor.children[i]);
    close(supervisor.listen_fd); unlink(TC_SERVICE_SOCKET_PATH); close(supervisor.lock_fd);
    return 0;
}

int tc_supervisor_command(const char *command) {
    struct sockaddr_un address;
    char buffer[2048];
    ssize_t got;
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) return 1;
    memset(&address, 0, sizeof(address)); address.sun_family = AF_UNIX;
    if (strlen(TC_SERVICE_SOCKET_PATH) >= sizeof(address.sun_path)) { close(fd); return 1; }
    strcpy(address.sun_path, TC_SERVICE_SOCKET_PATH);
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) != 0 ||
        write(fd, command, strlen(command)) != (ssize_t)strlen(command)) { close(fd); return 1; }
    shutdown(fd, SHUT_WR);
    while ((got = read(fd, buffer, sizeof(buffer))) > 0)
        if (write(STDOUT_FILENO, buffer, (size_t)got) != got) { close(fd); return 1; }
    close(fd);
    return got < 0 ? 1 : 0;
}
