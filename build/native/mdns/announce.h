#ifndef TC_MDNS_ANNOUNCE_H
#define TC_MDNS_ANNOUNCE_H
#include "types.h"
typedef int (*generated_record_adder)(uint8_t *buf,
                                      size_t *off,
                                      size_t cap,
                                      const struct config *cfg,
                                      uint32_t ttl,
                                      int *answers);
#ifdef TC_NATIVE_TEST
void format_dest_addr(const struct sockaddr_in *dest, char *buf, size_t buf_size);
#endif
void format_sockaddr_addr(const struct sockaddr *dest, char *buf, size_t buf_size);
void log_packet_build_failure(const char *stage, const char *step, size_t packet_len, int answers);
#ifdef TC_NATIVE_TEST
void log_packet_send_failure_detail_any(const char *stage, const struct sockaddr *dest, size_t packet_len,
                                               int answers, int saved_errno);
#endif
int send_dns_packet_any(const char *stage, int sockfd, const uint8_t *buf, size_t packet_len,
                               const struct sockaddr *dest, socklen_t dest_len,
                               int answers);
#ifdef TC_NATIVE_TEST
void init_announcement_packet(size_t *off, int *answers);
#endif
#ifdef TC_NATIVE_TEST
int finalize_and_send_announcement_packet_any(int sockfd,
                                                     uint8_t *buf,
                                                     size_t off,
                                                     int answers,
                                                     const struct sockaddr *dest,
                                                     socklen_t dest_len);
#endif
#ifdef TC_NATIVE_TEST
int append_generated_records_with_flush(int sockfd,
                                               uint8_t *buf,
                                               size_t *off,
                                               size_t cap,
                                               int *answers,
                                               const struct sockaddr *dest,
                                               socklen_t dest_len,
                                               const struct config *cfg,
                                               uint32_t ttl,
                                               generated_record_adder add_records,
                                               const char *failure_stage);
#endif
#ifdef TC_NATIVE_TEST
int append_generated_apple_records(int sockfd,
                                          uint8_t *buf,
                                          size_t *off,
                                          size_t cap,
                                          int *answers,
                                          const struct sockaddr *dest,
                                          socklen_t dest_len,
                                          const struct config *cfg,
                                          uint32_t ttl);
#endif
#ifdef TC_NATIVE_TEST
int append_generated_base_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg,
                                         const struct link_context *response_link,
                                         int include_a, int include_aaaa,
                                         uint32_t ttl, int *answers);
#endif
#ifdef TC_NATIVE_TEST
int send_announcement_any_scoped(int sockfd,
                                        const struct sockaddr *dest,
                                        socklen_t dest_len,
                                        const struct config *cfg,
                                        const struct link_context *response_link,
                                        uint32_t ttl,
                                        enum mdns_service_scope scope);
#endif
#ifdef TC_NATIVE_TEST
int send_announcement_any(int sockfd,
                                 const struct sockaddr *dest,
                                 socklen_t dest_len,
                                 const struct config *cfg,
                                 const struct link_context *response_link,
                                 uint32_t ttl);
#endif
#ifdef TC_NATIVE_TEST
int send_announcement(int sockfd, const struct sockaddr_in *dest, const struct config *cfg,
                                       const struct link_context *response_link, uint32_t ttl);
#endif
int source_matches_link_ipv4_subnet(uint32_t source_ipv4_addr, const struct link_context *link);
const struct link_context *select_response_link_ipv4(const struct link_context_set *links,
                                                            const struct sockaddr_in *source);
const struct link_context *select_response_link_ipv6(const struct link_context_set *links,
                                                            const struct sockaddr_in6 *source,
                                                            unsigned int ingress_ifindex);
#ifdef TC_NATIVE_TEST
int set_link_outbound_interface4(int sockfd, const struct link_context *link);
#endif
int set_link_outbound_interface4_for_peer(int sockfd, const struct link_context *link, uint32_t source_ipv4_addr);
int set_link_outbound_interface6(int sockfd, const struct link_context *link);
#ifdef TC_NATIVE_TEST
void send_link_announcement_pair(const struct mdns_socket_pair *sockets,
                                        const struct link_context *link,
                                        const struct sockaddr_in *dest4,
                                        const struct sockaddr_in6 *dest6,
                                        const struct config *cfg,
                                        uint32_t ttl,
                                        enum mdns_service_scope scope,
                                        const char *stage);
#endif
void announce_all_links(const struct mdns_socket_pair *sockets,
                               const struct link_context_set *links,
                               const struct sockaddr_in *dest4,
                               const struct sockaddr_in6 *dest6,
                               const struct config *cfg,
                               const char *stage);
void send_link_goodbyes(const struct mdns_socket_pair *sockets,
                               const struct link_context_set *links,
                               const struct sockaddr_in *dest4,
                               const struct sockaddr_in6 *dest6,
                               const struct config *cfg);
void send_link_goodbyes_for_missing(const struct mdns_socket_pair *sockets,
                                           const struct link_context_set *old_links,
                                           const struct link_context_set *new_links,
                                           const struct sockaddr_in *dest4,
                                           const struct sockaddr_in6 *dest6,
                                           const struct config *cfg);
#endif
