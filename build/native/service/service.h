#ifndef TC_SERVICE_H
#define TC_SERVICE_H
#include "../common/plan.h"
#include "../common/exit_codes.h"
#define NT_HASH_MAX_PASSWORD_BYTES 4096
#ifndef TC_HOSTS_PATH
#define TC_HOSTS_PATH "/etc/hosts"
#endif
/* Maps hostname to 127.0.0.1 in path and drops our lines for other names.
 * Returns 1 when the file changed, 0 when it already matched, or -1 with errno
 * (EINVAL for a name that is empty or not a plain host name). */
int tc_hosts_update(const char *path, const char *hostname);
/* 1-255 characters, each A-Z a-z 0-9 . _ -: what one /etc/hosts line can hold. */
int tc_hostname_plain(const char *name);
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
