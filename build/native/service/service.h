#ifndef TC_SERVICE_H
#define TC_SERVICE_H
#include "../common/plan.h"
#define EXIT_OK 0
#define EXIT_USAGE 3
#define EXIT_PLAN_FAILED 13
#define NT_HASH_MAX_PASSWORD_BYTES 4096
#ifndef TC_HOSTS_PATH
#define TC_HOSTS_PATH "/etc/hosts"
#endif
int tc_hosts_ensure(const char *path, const char *hostname);
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
