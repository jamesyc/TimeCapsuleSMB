#include "runtime.h"
#include "../common/log.h"

enum tc_samba_sysctl {
    TC_SYSCTL_SBMAX, TC_SYSCTL_MAXFILES, TC_SYSCTL_MAXPROC,
    TC_SYSCTL_SENDBUF_AUTO, TC_SYSCTL_SENDBUF_MAX, TC_SYSCTLS
};
enum tc_sysctl_policy { TC_SYSCTL_MINIMUM, TC_SYSCTL_EXACT };

static const char *const tc_samba_sysctl_names[TC_SYSCTLS] = {
    "kern.sbmax", "kern.maxfiles", "kern.maxproc",
    "net.inet.tcp.sendbuf_auto", "net.inet.tcp.sendbuf_max"
};

/* Unit tests replace only the syscall and NetBSD declarations, exercising
 * this same MIB discovery/read/write path on the host. */
#if (defined(__NetBSD__) && !defined(TC_NATIVE_TEST)) || defined(TC_SYSCTL_NATIVE_TEST)
#ifndef TC_SYSCTL_NATIVE_TEST
#include <sys/sysctl.h>
#endif
static const int kern_nodes[TC_SYSCTLS] = {
    [TC_SYSCTL_SBMAX] = KERN_SBMAX,
    [TC_SYSCTL_MAXFILES] = KERN_MAXFILES,
    [TC_SYSCTL_MAXPROC] = KERN_MAXPROC
};
static int tcp_nodes[TC_SYSCTLS];
/* TCP autosizing nodes have dynamic numbers. Query just the TCP subtree,
 * rather than linking sysctlbyname's much larger libc tree learner. Cache
 * only node numbers in this process; no runtime state file is needed. */
static int tcp_number(enum tc_samba_sysctl setting, int *number) {
    int mib[] = {CTL_NET, PF_INET, IPPROTO_TCP, CTL_QUERY};
    struct sysctlnode query, *nodes;
    size_t length = 0, i;
    memset(&query, 0, sizeof(query));
    query.sysctl_flags = SYSCTL_VERSION;
    if (sysctl(mib, 4, NULL, &length, &query, sizeof(query)))
        return -1;
    nodes = malloc(length);
    if (!nodes)
        return -1;
    size_t capacity = length;
    int result = sysctl(mib, 4, nodes, &length, &query, sizeof(query));
    if (!result) {
        result = -1;
        errno = ENOENT;
        for (i = 0; i < length / sizeof(*nodes) && i < capacity / sizeof(*nodes); i++)
            if (!strcmp(nodes[i].sysctl_name, strrchr(tc_samba_sysctl_names[setting], '.') + 1)) {
                *number = nodes[i].sysctl_num;
                result = 0;
                break;
            }
    }
    int saved = errno;
    free(nodes);
    errno = saved;
    return result;
}
static int sysctl_value(enum tc_samba_sysctl setting, int *old, const int *value) {
    int mib[4] = {CTL_KERN, 0, 0, 0};
    unsigned count = 2;
    size_t length = sizeof(int);
    if (setting == TC_SYSCTL_SENDBUF_AUTO || setting == TC_SYSCTL_SENDBUF_MAX) {
        int *number = &tcp_nodes[setting];
        if (!*number && tcp_number(setting, number))
            return -1;
        mib[0] = CTL_NET;
        mib[1] = PF_INET;
        mib[2] = IPPROTO_TCP;
        mib[3] = *number;
        count = 4;
    } else
        mib[1] = kern_nodes[setting];
    return sysctl(mib, count, old, old ? &length : NULL, value, value ? sizeof(*value) : 0);
}
static int sysctl_get(enum tc_samba_sysctl setting, int *value) {
    return sysctl_value(setting, value, NULL);
}
static int sysctl_set(enum tc_samba_sysctl setting, int value) {
    return sysctl_value(setting, NULL, &value);
}
#elif defined(TC_NATIVE_TEST)
/* TC_TEST_KERN names a directory with one file per sysctl's final component
 * holding its value; a read-only file refuses writes as the kernel would. */
static FILE *sysctl_open(enum tc_samba_sysctl setting, const char *mode) {
    char path[512];
    const char *dir = getenv("TC_TEST_KERN");
    const char *name = strrchr(tc_samba_sysctl_names[setting], '.') + 1;
    if (!dir || snprintf(path, sizeof(path), "%s/%s", dir, name) >= (int)sizeof(path)) {
        errno = ENOENT;
        return NULL;
    }
    return fopen(path, mode);
}
static int sysctl_get(enum tc_samba_sysctl setting, int *value) {
    FILE *stream = sysctl_open(setting, "r");
    int n = stream ? fscanf(stream, "%d", value) : 0;
    if (stream)
        fclose(stream);
    return n == 1 ? 0 : -1;
}
static int sysctl_set(enum tc_samba_sysctl setting, int value) {
    FILE *stream = sysctl_open(setting, "w");
    if (!stream)
        return -1;
    fprintf(stream, "%d\n", value);
    return fclose(stream);
}
#else
static int sysctl_get(enum tc_samba_sysctl setting, int *value) {
    (void)setting;
    (void)value;
    errno = ENOSYS;
    return -1;
}
static int sysctl_set(enum tc_samba_sysctl setting, int value) {
    (void)setting;
    (void)value;
    errno = ENOSYS;
    return -1;
}
#endif

/* Kernel setup failures stay nonfatal and are reported individually, so one
 * unavailable setting does not prevent applying the others or starting smbd. */
static void prepare_sysctl(enum tc_samba_sysctl setting, unsigned wanted, enum tc_sysctl_policy policy) {
    int current;
    if (!sysctl_get(setting, &current)) {
        if (current == (int)wanted || (policy == TC_SYSCTL_MINIMUM && current > (int)wanted))
            return;
        if (!sysctl_set(setting, (int)wanted))
            return;
    }
    timestamped_fprintf(stderr, "manager: could not set %s to %u: %s\n",
                        tc_samba_sysctl_names[setting], wanted, strerror(errno));
}

void tc_samba_prepare_kernel(int netbsd4) {
    prepare_sysctl(TC_SYSCTL_SBMAX, TC_SMBD_SBMAX(netbsd4), TC_SYSCTL_MINIMUM);
    prepare_sysctl(TC_SYSCTL_MAXFILES, TC_MAXFILES, TC_SYSCTL_MINIMUM);
    prepare_sysctl(TC_SYSCTL_MAXPROC, TC_MAXPROC(netbsd4), TC_SYSCTL_EXACT);
    if (!netbsd4) {
        prepare_sysctl(TC_SYSCTL_SENDBUF_AUTO, 1, TC_SYSCTL_EXACT);
        prepare_sysctl(TC_SYSCTL_SENDBUF_MAX, TC_SMBD_SNDBUF_MAX_NETBSD6, TC_SYSCTL_EXACT);
    }
}
