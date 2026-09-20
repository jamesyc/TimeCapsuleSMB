#ifndef TC_SERVICE_INSPECT_H
#define TC_SERVICE_INSPECT_H
#include "../common/platform.h"
#define TC_PROCESS_MAX 128

enum tc_process_role {
    TC_PROC_OTHER,
    TC_PROC_SMBD,
    TC_PROC_DISCOVERY,
    TC_PROC_TELEMETRY,
    TC_PROC_RSYNC,
    TC_PROC_WCIFSFS,
    TC_PROC_WCIFSND,
    TC_PROC_DISKD,
    TC_PROC_DISKD_LOOPBACK
};
struct tc_process_info {
    pid_t pid, parent, group;
    enum tc_process_role role;
};
struct tc_process_table {
    struct tc_process_info processes[TC_PROCESS_MAX];
    size_t count;
};
int tc_process_table_parse(struct tc_process_table *, const char *text);
int tc_process_table_read(struct tc_process_table *);
/* Bit 1 = IPv4 TCP listener, bit 2 = IPv6 TCP listener. */
unsigned tc_listener_families(const char *text, unsigned port);
int tc_process_listeners(pid_t, unsigned port, unsigned *families);
#endif
