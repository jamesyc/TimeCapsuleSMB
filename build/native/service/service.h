#ifndef TC_SERVICE_H
#define TC_SERVICE_H
#include "../common/plan.h"
#define EXIT_OK 0
#define EXIT_USAGE 3
#define EXIT_PLAN_FAILED 13
#define NT_HASH_MAX_PASSWORD_BYTES 4096
#define SERVICE_VERSION_CODE 30100
int print_smb_bind_interfaces(FILE *stream, const struct device_plan *plan);
int print_link_plan(FILE *stream, const struct device_plan *plan);
int service_collect_plan(struct device_plan *plan, const char *facts_file, struct device_plan *history);
int service_read_policy(FILE *stream, struct device_plan *history);
int service_print_policy(FILE *stream, const struct device_plan *history);
int print_nt_hash_from_stdin(void);
int print_device_nt_hash(void);
#endif
