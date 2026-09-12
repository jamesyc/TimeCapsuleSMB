#ifndef TC_MDNS_DIAGNOSTICS_H
#define TC_MDNS_DIAGNOSTICS_H
#include "types.h"
void log_startup_config(const struct config *cfg);
void log_send_failure(const char *stage, const struct sockaddr_in *dest, const char *detail);
void remember_last_send_failure(const char *stage, int saved_errno);
#ifdef TC_NATIVE_TEST
void log_mdns_counters(const char *reason);
#endif
#ifdef TC_NATIVE_TEST
void remember_logged_mdns_counters(long long now_ms);
#endif
#ifdef TC_NATIVE_TEST
int mdns_counters_changed_since_log(void);
#endif
void log_mdns_counters_force(const char *reason);
void maybe_log_mdns_counters(const char *reason, long long now_ms);
int note_mdns_ipv4_packet_received(void);
int note_mdns_ipv6_packet_received(void);
void log_mdns_receive_counters(const char *first_packet_reason,
                                      int first_packet,
                                      unsigned long query_matches_before,
                                      long long now_ms);
#ifdef TC_NATIVE_TEST
void mdns_transport_requirements_from_links(const struct link_context_set *desired_links,
                                                   struct mdns_transport_requirements *requirements);
#endif
void mdns_transport_status_from_links(const struct link_context_set *desired_links,
                                             const struct link_context_set *active_links,
                                             const struct mdns_socket_pair *sockets,
                                             struct mdns_transport_status *status);
int mdns_transport_has_active_socket(const struct mdns_transport_status *status);
int mdns_transport_missing_required(const struct mdns_transport_status *status);
int mdns_transport_is_healthy(const struct mdns_transport_status *status);
#ifdef TC_NATIVE_TEST
const char *mdns_transport_health_label(const struct mdns_transport_status *status);
#endif
#ifdef TC_NATIVE_TEST
void mdns_first_active_ipv4(char *out, size_t out_len, const struct link_context_set *active_links);
#endif
#ifdef TC_NATIVE_TEST
void mdns_first_active_ipv6(char *out, size_t out_len, const struct link_context_set *active_links);
#endif
void log_mdns_transport_status(const char *reason,
                                      const struct link_context_set *active_links,
                                      const struct mdns_transport_status *status);
void log_served_records(const struct config *cfg);
#endif
