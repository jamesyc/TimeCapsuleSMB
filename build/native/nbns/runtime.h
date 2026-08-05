#ifndef TC_NBNS_RUNTIME_H
#define TC_NBNS_RUNTIME_H
#include "types.h"
#ifdef TC_NATIVE_TEST
void on_signal(int signo);
#endif
#ifdef TC_NATIVE_TEST
void keep_only_nbns_ipv4_link_contexts(struct link_context_set *set);
#endif
#ifdef TC_NATIVE_TEST
int link_contexts_need_nbns_ipv4_socket(const struct link_context_set *set);
#endif
#ifdef TC_NATIVE_TEST
void filter_nbns_link_contexts(struct link_context_set *out,
                                      const struct link_context_set *all_links);
#endif
#ifdef TC_NATIVE_TEST
int collect_usable_nbns_link_contexts(struct link_context_set *out);
#endif
#ifdef TC_NATIVE_TEST
int print_nbns_socket_families(FILE *stream);
#endif
#ifdef TC_NATIVE_TEST
int wait_for_auto_link_contexts(struct link_context_set *out);
#endif
#ifdef TC_NATIVE_TEST
void log_nbns_ipv4_link_miss(const struct link_context_set *links, uint32_t peer_addr);
#endif
#ifdef TC_NATIVE_TEST
int source_matches_link_ipv4_subnet(uint32_t source_ipv4_addr,
                                           const struct link_ipv4_addr *addr);
#endif
#ifdef TC_NATIVE_TEST
uint32_t choose_response_ipv4_from_links(const struct link_context_set *links, uint32_t peer_addr);
#endif
#ifdef TC_NATIVE_TEST
int refresh_auto_link_contexts_if_needed(struct link_context_set *contexts,
                                                time_t *last_link_poll);
#endif
#ifdef TC_NATIVE_TEST
int open_nbns_ipv4_socket(void);
#endif
int nbns_main(int argc, char **argv);
#ifdef TC_NATIVE_TEST
void write_stdout_line(const char *value);
#endif
#ifdef TC_NATIVE_TEST
void write_stdout_int_line(int value);
#endif
ssize_t sendto_retry(int sockfd, const void *buf, size_t len, int flags,
                            const struct sockaddr *dest, socklen_t dest_len);
#ifdef TC_NATIVE_TEST
void usage(const char *prog);
#endif
#endif
