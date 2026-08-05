#ifndef TC_NBNS_PROTOCOL_H
#define TC_NBNS_PROTOCOL_H
#include "types.h"
#ifdef TC_NATIVE_TEST
void normalize_netbios_name(char out[16], const char *name);
#endif
int validate_netbios_name(const char *name);
#ifdef TC_NATIVE_TEST
int decode_netbios_question_name(const uint8_t *encoded, size_t encoded_len, char out[16], uint8_t *suffix);
#endif
#ifdef TC_NATIVE_TEST
int names_match(const char configured[16], const char queried[16]);
#endif
#ifdef TC_NATIVE_TEST
int name_is_wildcard(const char queried[16]);
#endif
#ifdef TC_NATIVE_TEST
int parse_question_name(const uint8_t *buf,
                               size_t len,
                               size_t question_name_off,
                               char out[16],
                               uint8_t *suffix,
                               size_t *question_name_end_off);
#endif
#ifdef TC_NATIVE_TEST
int build_resource_response(uint8_t *out,
                                   size_t out_len,
                                   const uint8_t *request,
                                   size_t request_len,
                                   size_t question_name_off,
                                   size_t question_name_end_off,
                                   uint16_t response_flags,
                                   uint16_t answer_count,
                                   uint16_t rr_type_value,
                                   uint32_t ttl,
                                   const uint8_t *rdata,
                                   uint16_t rdata_len);
#endif
#ifdef TC_NATIVE_TEST
int build_positive_response(uint8_t *out,
                                   size_t out_len,
                                   const uint8_t *request,
                                   size_t request_len,
                                   size_t question_name_off,
                                   size_t question_name_end_off,
                                   uint32_t ttl,
                                   uint32_t ipv4_addr);
#endif
#ifdef TC_NATIVE_TEST
int build_negative_query_response(uint8_t *out,
                                         size_t out_len,
                                         const uint8_t *request,
                                         size_t request_len,
                                         size_t question_name_off,
                                         size_t question_name_end_off);
#endif
#ifdef TC_NATIVE_TEST
void append_node_status_name(uint8_t *out, const char normalized_name[16], uint8_t suffix);
#endif
#ifdef TC_NATIVE_TEST
int build_node_status_response(uint8_t *out,
                                      size_t out_len,
                                      const uint8_t *request,
                                      size_t request_len,
                                      size_t question_name_off,
                                      size_t question_name_end_off,
                                      const char *netbios_name);
#endif
int maybe_respond_to_query_addr(int sock,
                                       const struct config *cfg,
                                       const uint8_t *buf,
                                       size_t len,
                                       const struct sockaddr *peer,
                                       socklen_t peer_len);
#endif
