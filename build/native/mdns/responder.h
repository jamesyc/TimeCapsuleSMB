#ifndef TC_MDNS_RESPONDER_H
#define TC_MDNS_RESPONDER_H
#include "types.h"
#ifdef TC_NATIVE_TEST
int known_answer_ttl_is_fresh(uint32_t known_ttl, uint32_t advertised_ttl);
#endif
#ifdef TC_NATIVE_TEST
int planned_rr_rdata_equals(const struct planned_rr *rr, const uint8_t *rdata, uint16_t rdlength);
#endif
#ifdef TC_NATIVE_TEST
int planned_rr_add_raw(struct planned_rr_set *set,
                              int routes,
                              const char *owner,
                              uint16_t type,
                              uint16_t rrclass,
                              uint32_t ttl,
                              const uint8_t *rdata,
                              uint16_t rdlength);
#endif
int planned_rr_add_name(struct planned_rr_set *set,
                               int routes,
                               const char *owner,
                               uint16_t type,
                               uint16_t rrclass,
                               uint32_t ttl,
                               const char *target);
int planned_rr_add_srv(struct planned_rr_set *set,
                              int routes,
                              const char *owner,
                              const char *target,
                              uint16_t port,
                              uint32_t ttl);
int planned_rr_add_txt_items(struct planned_rr_set *set,
                                    int routes,
                                    const char *owner,
                                    const char **strings,
                                    const uint8_t *lengths,
                                    size_t string_count,
                                    uint32_t ttl);
int planned_rr_add_txt_empty(struct planned_rr_set *set, int routes, const char *owner, uint32_t ttl);
#ifdef TC_NATIVE_TEST
int planned_rr_add_a(struct planned_rr_set *set, int routes, const char *owner, uint32_t ipv4_addr, uint32_t ttl);
#endif
#ifdef TC_NATIVE_TEST
int planned_rr_add_aaaa(struct planned_rr_set *set,
                               int routes,
                               const char *owner,
                               const struct in6_addr *ipv6_addr,
                               uint32_t ttl);
#endif
int planned_rr_add_link_addresses(struct planned_rr_set *set,
                                         int routes,
                                         const char *owner,
                                         const struct link_context *link,
                                         int include_a,
                                         int include_aaaa,
                                         uint32_t ttl);
#ifdef TC_NATIVE_TEST
int planned_set_has_route(const struct planned_rr_set *set, int route);
#endif
#ifdef TC_NATIVE_TEST
int planned_set_has_any_route(const struct planned_rr_set *set);
#endif
#ifdef TC_NATIVE_TEST
int planned_rr_matches_known_answer(const struct planned_rr *rr,
                                           const char *owner,
                                           uint16_t type,
                                           uint16_t rrclass,
                                           const uint8_t *packet,
                                           size_t packet_len,
                                           size_t rdata_cursor,
                                           uint16_t rdlength);
#endif
#ifdef TC_NATIVE_TEST
void suppress_planned_known_answers(const uint8_t *packet,
                                           size_t packet_len,
                                           size_t cursor,
                                           uint16_t answer_count,
                                           struct planned_rr_set *planned);
#endif
#ifdef TC_NATIVE_TEST
uint16_t sockaddr_port_host(const struct sockaddr *addr);
#endif
#ifdef TC_NATIVE_TEST
int source_can_receive_unicast_response(const struct sockaddr *source,
                                               const struct link_context *response_link,
                                               unsigned int ingress_ifindex);
#endif
#ifdef TC_NATIVE_TEST
int plan_question_answers(struct planned_rr_set *planned,
                                 int route,
                                 const char *qname,
                                 uint16_t qtype,
                                 const struct config *cfg,
                                 const struct link_context *response_link,
                                 const char *instance_fqdn,
                                 const char *afp_instance_fqdn,
                                 const char *adisk_instance_fqdn,
                                 const char *device_info_instance_fqdn,
                                 const char *airport_instance_fqdn,
                                 const char *riousbprint_instance_fqdn,
                                 const char *pdl_datastream_instance_fqdn);
#endif
#ifdef TC_NATIVE_TEST
int add_planned_rr_to_packet(uint8_t *reply,
                                    size_t *off,
                                    size_t reply_cap,
                                    const struct planned_rr *rr,
                                    int legacy_unicast);
#endif
#ifdef TC_NATIVE_TEST
int build_planned_response_packet(uint8_t *reply,
                                         size_t reply_cap,
                                         size_t *reply_len,
                                         int *answer_count,
                                         uint16_t response_id,
                                         int route,
                                         int legacy_unicast,
                                         const struct response_question_section *questions,
                                         const struct planned_rr_set *planned);
#endif
#ifdef TC_NATIVE_TEST
void stored_question_section_as_response(const struct stored_question_section *stored,
                                                struct response_question_section *out);
#endif
#ifdef TC_NATIVE_TEST
int send_planned_response_route(int sockfd,
                                       const struct planned_rr_set *planned,
                                       int route,
                                       uint16_t response_id,
                                       const struct response_question_section *questions,
                                       const struct sockaddr *dest,
                                       socklen_t dest_len,
                                       int delay_multicast);
#endif
#ifdef TC_NATIVE_TEST
void clear_deferred_response(void);
#endif
void clear_deferred_response_for_sockfd(int sockfd);
#ifdef TC_NATIVE_TEST
int sockaddr_endpoint_equal(const struct sockaddr *a, socklen_t a_len,
                                   const struct sockaddr *b, socklen_t b_len);
#endif
#ifdef TC_NATIVE_TEST
int deferred_response_matches_source(int sockfd, const struct sockaddr *source, socklen_t source_len);
#endif
#ifdef TC_NATIVE_TEST
int copy_sockaddr_storage(struct sockaddr_storage *out,
                                 socklen_t *out_len,
                                 const struct sockaddr *src,
                                 socklen_t src_len);
#endif
#ifdef TC_NATIVE_TEST
int flush_deferred_response_now(void);
#endif
int flush_deferred_response_if_due(long long now_ms);
long long deferred_response_adjust_wait_ms(long long now_ms, long long wait_ms);
#ifdef TC_NATIVE_TEST
int defer_planned_response(int sockfd,
                                  uint16_t response_id,
                                  const struct sockaddr *multicast_dest,
                                  socklen_t multicast_dest_len,
                                  const struct sockaddr *source,
                                  socklen_t source_len,
                                  const struct response_question_section *questions,
                                  const struct planned_rr_set *planned);
#endif
int handle_query_any_scoped(int sockfd,
                                   const uint8_t *packet,
                                   size_t packet_len,
                                   const struct sockaddr *multicast_dest,
                                   socklen_t multicast_dest_len,
                                   const struct sockaddr *source,
                                   socklen_t source_len,
                                   unsigned int ingress_ifindex,
                                   const struct config *cfg,
                                   const struct link_context *response_link,
                                   enum mdns_service_scope scope);
int handle_query_scoped(int sockfd, const uint8_t *packet, size_t packet_len,
                               const struct sockaddr_in *multicast_dest, const struct sockaddr_in *source,
                               const struct config *cfg, const struct link_context *response_link,
                               enum mdns_service_scope scope);
#ifdef TC_NATIVE_TEST
int handle_query(int sockfd, const uint8_t *packet, size_t packet_len,
                                  const struct sockaddr_in *multicast_dest, const struct sockaddr_in *source,
                                  const struct config *cfg, const struct link_context *response_link);
#endif
#endif
