#ifndef TC_SERVICE_H
#define TC_SERVICE_H
#include "../common/network.h"
typedef int (*collect_link_contexts_fn)(struct link_context_set *, void *);
#define EXIT_OK 0
#define EXIT_USAGE 3
#define EXIT_AUTO_IP_UNAVAILABLE 11
#define EXIT_AUTO_IP_PROBE_FAILED 13
#define NT_HASH_MAX_PASSWORD_BYTES 4096
int print_link_ipv4_cidrs(FILE *stream, const struct link_context_set *set);
int link_contexts_have_ipv4_addr(const struct link_context_set *set);
int print_auto_ip_cidrs_with_provider(FILE *stream,
                                             collect_link_contexts_fn collect_contexts,
                                             void *userdata);
int print_smb_bind_interfaces_with_policy(FILE *stream,
                                                 collect_link_contexts_fn collect_contexts,
                                                 void *userdata,
                                                 int lan_only);
int print_smb_bind_interfaces_with_provider(FILE *stream,
                                                   collect_link_contexts_fn collect_contexts,
                                                   void *userdata);
int print_smb_bind_interfaces_lan_with_provider(FILE *stream,
                                                       collect_link_contexts_fn collect_contexts,
                                                       void *userdata);
int print_nt_hash_from_stdin(void);
#endif
