#ifndef TC_SERVICE_INSPECT_H
#define TC_SERVICE_INSPECT_H
#include "proctable.h"
#ifndef TC_FSTAT_PATH
#define TC_FSTAT_PATH "/usr/bin/fstat"
#endif

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
/* Reads a command line like tc_proctable_argv(): 0, 1 when the process has
 * exited, -1 on any other failure. */
typedef int (*tc_argv_fn)(pid_t, char *out, size_t size);
/* The managed-role processes in snapshot. Only roles that depend on the
 * command line read it; one that exited since the snapshot is left out, and
 * any other failure to read one fails the whole table (-1): an empty command
 * line would make the loopback diskd look foreign. */
int tc_process_table_build(struct tc_process_table *, const struct tc_proctable *snapshot, tc_argv_fn argv);
int tc_listener_present(const char *text, unsigned port);
/* Bit 1 = IPv4 wildcard, bit 2 = IPv6 wildcard. */
unsigned tc_wildcard_listener_families(const char *text, unsigned port);
int tc_process_listener(pid_t, unsigned port, int *listening);
int tc_process_wildcard_listeners(pid_t, unsigned port, unsigned *families);
/* Apple's wcifsnd must own its NBNS sockets and the private control listener. */
int tc_native_nbns_sockets_present(const char *fstat_text, pid_t pid, unsigned control_port);
#endif
