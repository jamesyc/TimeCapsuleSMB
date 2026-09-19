#include "service.h"
#include "supervisor.h"
#include "../common/config.h"
#include "../common/ipc.h"

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
    struct supervised_child children[3];
};

static volatile sig_atomic_t supervisor_signal;
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

static int load_enabled(const char *key, int fallback) {
    char value[TC_CONFIG_VALUE_MAX];
    int rc = config_read_value(TC_FLASH_CONFIG_PATH, key, value, sizeof(value));
    return rc == 1 ? fallback : rc < 0 ? -1 : config_bool_value(value, -1);
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
    int channels[2];
    pid_t pid;
    char fd_text[16];
    char *arguments[10];
    int count = 0;
    if (!child->enabled) { child->state = CHILD_DISABLED; return 0; }
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, channels) != 0) return -1;
    if (tc_ipc_configure_fd(channels[0], 1, 1) != 0 ||
        tc_ipc_configure_fd(channels[1], 0, 0) != 0) {
        close(channels[0]); close(channels[1]); return -1;
    }
    snprintf(fd_text, sizeof(fd_text), "%d", channels[1]);
    arguments[count++] = (char *)supervisor->program;
    arguments[count++] = (char *)child->name;
    if (child->role == TC_ROLE_MDNS || child->role == TC_ROLE_NETBIOS) arguments[count++] = "--diskless";
    if (child->role == TC_ROLE_NETBIOS) {
        arguments[count++] = "--netbios-name"; arguments[count++] = supervisor->netbios;
    }
    if (child->role == TC_ROLE_TELEMETRY) arguments[count++] = "--daemon";
    arguments[count++] = "--control-fd"; arguments[count++] = fd_text; arguments[count] = NULL;
    pid = fork();
    if (pid == 0) {
        setpgid(0, 0);
        signal(SIGTERM, SIG_DFL); signal(SIGINT, SIG_DFL); signal(SIGCHLD, SIG_DFL); signal(SIGPIPE, SIG_DFL);
        close(channels[0]); close_other_descriptors(channels[1]);
        execv(supervisor->program, arguments);
        _exit(127);
    }
    close(channels[1]);
    if (pid < 0) { close(channels[0]); return -1; }
    child->pid = pid; child->fd = channels[0]; child->state = CHILD_STARTING;
    if (tc_ipc_send(child->fd, TC_IPC_INIT, child->role, supervisor->instance,
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

static void reap_children(struct supervisor *supervisor, long long now) {
    int status;
    pid_t pid;
    while ((pid = waitpid(-1, &status, WNOHANG)) > 0) {
        size_t i;
        for (i = 0; i < sizeof(supervisor->children) / sizeof(supervisor->children[0]); i++) {
            if (supervisor->children[i].pid == pid) schedule_restart(&supervisor->children[i], now);
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

static void configure_identity(struct supervisor *supervisor) {
    struct device_plan plan;
    struct plan_options options;
    memset(&plan, 0, sizeof(plan));
    memset(&options, 0, sizeof(options));
    options.diskless = 1;
    if (device_plan_collect(&plan, NULL, &options) == 0 && plan.id.netbios[0])
        strncpy(supervisor->netbios, plan.id.netbios, sizeof(supervisor->netbios) - 1);
    else strcpy(supervisor->netbios, "TIMECAPSULE");
}

int tc_supervisor_run(const char *program) {
    struct supervisor supervisor;
    size_t i;
    int telemetry;
    memset(&supervisor, 0, sizeof(supervisor));
    supervisor.program = program;
    supervisor.instance = ((uint64_t)time(NULL) << 32) ^ (uint64_t)getpid();
    if (!supervisor.instance) supervisor.instance = 1;
    supervisor.generation = 1;
    supervisor.lock_fd = acquire_singleton();
    if (supervisor.lock_fd < 0) { fputs("service: supervisor already running or lock unavailable\n", stderr); return 1; }
    if (!receipt_allows_runtime()) { fputs("service: incomplete or invalid xattr migration receipt\n", stderr); close(supervisor.lock_fd); return 1; }
    supervisor.listen_fd = open_admin_socket();
    if (supervisor.listen_fd < 0) { close(supervisor.lock_fd); return 1; }
    configure_identity(&supervisor);
    telemetry = load_enabled("TELEMETRY", 1);
    supervisor.children[0] = (struct supervised_child){"mdns", TC_ROLE_MDNS, 1, 0, -1, CHILD_STOPPED, 0, 0};
    supervisor.children[1] = (struct supervised_child){"netbios", TC_ROLE_NETBIOS, load_enabled("NBNS_ENABLED", 1) == 1, 0, -1, CHILD_STOPPED, 0, 0};
    supervisor.children[2] = (struct supervised_child){"telemetry", TC_ROLE_TELEMETRY, telemetry == 1, 0, -1, CHILD_STOPPED, 0, 0};
    signal(SIGTERM, supervisor_on_signal); signal(SIGINT, supervisor_on_signal);
    signal(SIGHUP, supervisor_on_signal); signal(SIGCHLD, supervisor_on_signal); signal(SIGPIPE, SIG_IGN);
    for (i = 0; i < 3; i++) if (spawn_child(&supervisor, &supervisor.children[i]) != 0)
        schedule_restart(&supervisor.children[i], acp_monotonic_ms());
    while (!supervisor.stop) {
        fd_set reads;
        struct timeval timeout;
        int maxfd = supervisor.listen_fd;
        long long now = acp_monotonic_ms();
        FD_ZERO(&reads); FD_SET(supervisor.listen_fd, &reads);
        for (i = 0; i < 3; i++) {
            struct supervised_child *child = &supervisor.children[i];
            if (child->fd >= 0) { FD_SET(child->fd, &reads); if (child->fd > maxfd) maxfd = child->fd; }
            if (child->enabled && child->pid == 0 && now >= child->restart_at && spawn_child(&supervisor, child) != 0)
                schedule_restart(child, now);
        }
        timeout.tv_sec = 1; timeout.tv_usec = 0;
        if (select(maxfd + 1, &reads, NULL, NULL, &timeout) > 0) {
            if (FD_ISSET(supervisor.listen_fd, &reads)) accept_admin(&supervisor);
            for (i = 0; i < 3; i++) if (supervisor.children[i].fd >= 0 && FD_ISSET(supervisor.children[i].fd, &reads)) {
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
        if (supervisor.reload) {
            supervisor.reload = 0; supervisor.generation++;
            for (i = 0; i < 3; i++) if (supervisor.children[i].fd >= 0)
                (void)tc_ipc_send(supervisor.children[i].fd, TC_IPC_REFRESH,
                    supervisor.children[i].role, supervisor.instance, supervisor.generation, NULL, 0);
        }
    }
    for (i = 0; i < 3; i++) stop_child(&supervisor.children[i]);
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
