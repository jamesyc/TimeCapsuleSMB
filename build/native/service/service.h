#ifndef TC_SERVICE_H
#define TC_SERVICE_H
#include "../common/plan.h"
#include "../common/exit_codes.h"
#define NT_HASH_MAX_PASSWORD_BYTES 4096
#ifndef TC_HOSTS_PATH
#define TC_HOSTS_PATH "/etc/hosts"
#endif
/* Maps hostname to 127.0.0.1 in path and drops our lines for other names.
 * Returns 1 when the file changed, 0 when it already matched, or -1 with errno.
 * Every ACPd checked (products 106 and 116 at 7.5.2-7.8.1, 119 and 120 at
 * 7.7.3-7.9.1) sets the hostname to lowercase [a-z0-9-] (at most 63 bytes)
 * or base-station-<6 hex digits>, and dhclient-script keeps only
 * [-.a-zA-Z0-9] of a DHCP host-name, so the name always fits one /etc/hosts
 * line. */
int tc_hosts_update(const char *path, const char *hostname);
/* The kernel hostname, or "" while it is unset. Host test builds read it from
 * the file named by TC_TEST_HOSTNAME instead. */
void tc_hostname_read(char *out, size_t size);
int print_link_plan(FILE *stream, const struct device_plan *plan);
int service_collect_plan(struct device_plan *plan, const char *facts_file);
int print_nt_hash_from_stdin(void);
int print_device_nt_hash(void);
int device_nt_hash(char out[33]);
struct tc_samba_identity {
    char netbios[16], server[256], model[48];
    int name_observed;
};
int tc_samba_identity_read(struct tc_samba_identity *out);
#endif
