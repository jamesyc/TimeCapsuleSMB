#ifndef TC_MDNS_TRANSPORT_H
#define TC_MDNS_TRANSPORT_H
#include "types.h"
void scoped_mdns_dest6_for_link(struct sockaddr_in6 *out,
                                       const struct sockaddr_in6 *base,
                                       const struct link_context *link);
unsigned int ipv6_sockaddr_effective_ifindex(const struct sockaddr_in6 *addr);
int mdnsresponder_is_alive(void);
void sleep_millis(unsigned int delay_ms);
#ifdef TC_NATIVE_TEST
unsigned int random_multicast_response_delay_ms(void);
#endif
void delay_multicast_query_response(void);
long long monotonic_millis(void);
void kill_mdnsresponder(int sig);
#ifdef TC_NATIVE_TEST
int join_mdns_multicast_group(int sockfd, uint32_t ipv4_addr, const char *socket_role);
#endif
int set_outbound_multicast_interface(int sockfd, uint32_t ipv4_addr, const char *socket_role,
                                            int log_success, int log_errors);
#ifdef TC_NATIVE_TEST
void configure_unicast_response_hop_limit4(int sockfd);
#endif
#ifdef TC_NATIVE_TEST
void configure_unicast_response_hop_limit6(int sockfd);
#endif
#ifdef TC_NATIVE_TEST
int configure_multicast_socket_options(int sockfd);
#endif
#ifdef TC_NATIVE_TEST
int configure_outbound_multicast_socket(int sockfd, uint32_t ipv4_addr, const char *socket_role);
#endif
#ifdef TC_NATIVE_TEST
int open_bound_mdns_socket(int shared_bind, int log_bind_errors);
#endif
#ifdef TC_NATIVE_TEST
void drop_mdns_multicast_group_best_effort(int sockfd, uint32_t ipv4_addr, const char *socket_role);
#endif
#ifdef TC_NATIVE_TEST
int link_has_any_mdns_transport(const struct link_context *link);
#endif
#ifdef TC_NATIVE_TEST
void compact_link_contexts_for_mdns_transport(struct link_context_set *set);
#endif
#ifdef TC_NATIVE_TEST
int link_ipv4_source_score(uint32_t ipv4_addr);
#endif
uint32_t link_preferred_ipv4_source(const struct link_context *link);
uint32_t link_ipv4_source_for_peer(const struct link_context *link, uint32_t source_ipv4_addr);
int link_contexts_need_ipv4_socket(const struct link_context_set *set);
int link_contexts_need_ipv6_socket(const struct link_context_set *set);
void close_mdns_socket_pair(struct mdns_socket_pair *sockets);
#ifdef TC_NATIVE_TEST
void init_mdns_membership_delta(struct mdns_membership_delta *delta);
#endif
#ifdef TC_NATIVE_TEST
int record_mdns_membership_ipv4(struct mdns_membership_delta *delta, uint32_t ipv4_addr);
#endif
#ifdef TC_NATIVE_TEST
int record_mdns_membership_ipv6(struct mdns_membership_delta *delta, unsigned int ifindex, const char *ifname);
#endif
#ifdef TC_NATIVE_TEST
void rollback_mdns_membership_delta(struct mdns_socket_pair *sockets,
                                           const struct mdns_membership_delta *delta);
#endif
#ifdef TC_NATIVE_TEST
int open_bound_mdns_socket6(int shared_bind, int log_bind_errors);
#endif
ssize_t receive_ipv6_packet(int sockfd,
                                   uint8_t *packet,
                                   size_t packet_len,
                                   struct sockaddr_in6 *source,
                                   socklen_t *source_len,
                                   unsigned int *received_ifindex);
#ifdef TC_NATIVE_TEST
int join_mdns_multicast_group6(int sockfd, unsigned int ifindex, const char *ifname, const char *socket_role);
#endif
#ifdef TC_NATIVE_TEST
void drop_mdns_multicast_group6_best_effort(int sockfd, unsigned int ifindex, const char *ifname, const char *socket_role);
#endif
int set_outbound_multicast_interface6(int sockfd, unsigned int ifindex, const char *socket_role,
                                             int log_success, int log_errors);
#ifdef TC_NATIVE_TEST
int join_mdns_multicast_group_for_link4(int sockfd,
                                               struct link_context *link,
                                               const struct link_context_set *old_links,
                                               const char *socket_role,
                                               struct mdns_membership_delta *delta);
#endif
#ifdef TC_NATIVE_TEST
int configure_mdns_socket6_for_links(int sockfd, struct link_context_set *set, const char *socket_role);
#endif
#ifdef TC_NATIVE_TEST
int configure_mdns_socket4_for_links(int sockfd, struct link_context_set *set, const char *socket_role);
#endif
#ifdef TC_NATIVE_TEST
int link_set_has_ipv4_membership(const struct link_context_set *set, uint32_t ipv4_addr);
#endif
#ifdef TC_NATIVE_TEST
int link_set_has_ipv6_membership(const struct link_context_set *set, unsigned int ifindex);
#endif
#ifdef TC_NATIVE_TEST
int prepare_mdns_socket4_memberships(int sockfd,
                                            const struct link_context_set *old_links,
                                            struct link_context_set *new_links,
                                            const char *socket_role,
                                            struct mdns_membership_delta *delta);
#endif
#ifdef TC_NATIVE_TEST
int prepare_mdns_socket6_memberships(int sockfd,
                                            const struct link_context_set *old_links,
                                            struct link_context_set *new_links,
                                            const char *socket_role,
                                            struct mdns_membership_delta *delta);
#endif
#ifdef TC_NATIVE_TEST
int open_dualstack_mdns_sockets(int shared_bind,
                                       struct link_context_set *links,
                                       int log_bind_errors,
                                       struct mdns_socket_pair *out);
#endif
#ifdef TC_NATIVE_TEST
int open_dualstack_mdns_sockets_for_desired(int shared_bind,
                                                   const struct link_context_set *desired_links,
                                                   struct link_context_set *active_links,
                                                   int log_bind_errors,
                                                   struct mdns_socket_pair *out,
                                                   struct mdns_transport_status *status);
#endif
int acquire_dualstack_mdns_sockets(int shared_bind,
                                          const struct link_context_set *desired_links,
                                          struct link_context_set *active_links,
                                          struct mdns_socket_pair *out,
                                          struct mdns_transport_status *status);
int prepare_runtime_mdns_sockets_for_links(int shared_bind,
                                                  struct mdns_socket_pair *sockets,
                                                  const struct link_context_set *old_links,
                                                  struct link_context_set *new_links);
void retire_runtime_mdns_memberships_for_missing(struct mdns_socket_pair *sockets,
                                                        const struct link_context_set *old_links,
                                                        const struct link_context_set *new_links);
void close_unused_runtime_mdns_socket_families(struct mdns_socket_pair *sockets,
                                                      const struct link_context_set *links);
#endif
