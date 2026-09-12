#ifndef TC_MDNS_RUNTIME_H
#define TC_MDNS_RUNTIME_H
#include "types.h"
#ifdef TC_NATIVE_TEST
void on_signal(int signo);
#endif
#ifdef TC_NATIVE_TEST
int collect_usable_link_contexts_provider(struct link_context_set *out, void *userdata);
#endif
#ifdef TC_NATIVE_TEST
int collect_usable_advertise_link_contexts_provider(struct link_context_set *out, void *userdata);
#endif
#ifdef TC_NATIVE_TEST
void mdns_sleep_provider(unsigned int seconds, void *userdata);
#endif
#ifdef TC_NATIVE_TEST
int wait_for_auto_link_contexts_with_provider(struct link_context_set *out,
                                                     const char *role,
                                                     mdns_collect_link_contexts_fn collect_contexts,
                                                     mdns_sleep_fn sleep_fn,
                                                     void *userdata);
#endif
#ifdef TC_NATIVE_TEST
int wait_for_auto_advertise_link_contexts(struct link_context_set *out, const char *role);
#endif
#ifdef TC_NATIVE_TEST
int print_mdns_socket_families_with_provider(FILE *stream,
                                                    mdns_collect_link_contexts_fn collect_contexts,
                                                    void *userdata);
#endif
#ifdef TC_NATIVE_TEST
int link_context_topology_equal(const struct link_context *a, const struct link_context *b);
#endif
#ifdef TC_NATIVE_TEST
int link_context_set_contains_topology(const struct link_context_set *set,
                                              const struct link_context *ctx);
#endif
#ifdef TC_NATIVE_TEST
int link_context_topology_sets_equal(const struct link_context_set *a,
                                            const struct link_context_set *b);
#endif
enum mdns_service_scope mdns_service_scope_for_link(const struct link_context_set *links,
                                                           const struct link_context *link);
const char *mdns_service_scope_name(enum mdns_service_scope scope);
#ifdef TC_NATIVE_TEST
int apply_runtime_link_change(int shared_bind,
                                     struct mdns_socket_pair *sockets,
                                     struct link_context_set *active_links,
                                     const struct link_context_set *new_links,
                                     const struct sockaddr_in *dest4,
                                     const struct sockaddr_in6 *dest6,
                                     const struct config *cfg);
#endif
#ifdef TC_NATIVE_TEST
int recover_runtime_link_change_with_takeover(int shared_bind,
                                                     struct mdns_socket_pair *sockets,
                                                     struct link_context_set *active_links,
                                                     const struct link_context_set *desired_links,
                                                     const struct sockaddr_in *dest4,
                                                     const struct sockaddr_in6 *dest6,
                                                     const struct config *cfg,
                                                     struct mdns_transport_status *status);
#endif
int mdns_main(int argc, char **argv);
#endif
