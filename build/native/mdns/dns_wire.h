#ifndef TC_MDNS_DNS_WIRE_H
#define TC_MDNS_DNS_WIRE_H
#include "types.h"
int append_bytes(uint8_t *buf, size_t *off, size_t cap, const void *src, size_t len);
int append_u16(uint8_t *buf, size_t *off, size_t cap, uint16_t value);
int append_u32(uint8_t *buf, size_t *off, size_t cap, uint32_t value);
int validate_dns_name(const char *value, const char *field_name);
int encode_name(uint8_t *buf, size_t *off, size_t cap, const char *name);
int decode_name(const uint8_t *packet, size_t packet_len, size_t *cursor, char *out, size_t out_len);
int name_equals(const char *a, const char *b);
int append_host_address_records(uint8_t *buf,
                                       size_t *off,
                                       size_t cap,
                                       const char *owner,
                                       const struct link_context *link,
                                       int include_a,
                                       int include_aaaa,
                                       uint32_t ttl,
                                       int *answers);
int add_rr_ptr(uint8_t *buf, size_t *off, size_t cap, const char *owner, const char *target, uint32_t ttl);
int add_rr_txt_empty(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ttl);
#ifdef TC_NATIVE_TEST
int add_rr_txt_items(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ttl,
                            const char **strings, const uint8_t *lengths, size_t string_count);
#endif
int add_rr_txt_strings(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ttl,
                              const char **strings, size_t string_count);
int add_rr_srv(uint8_t *buf, size_t *off, size_t cap, const char *owner, const char *target, uint16_t port, uint32_t ttl);
#ifdef TC_NATIVE_TEST
int add_rr_a(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ipv4_addr, uint32_t ttl);
#endif
#ifdef TC_NATIVE_TEST
int add_rr_aaaa(uint8_t *buf, size_t *off, size_t cap, const char *owner, const struct in6_addr *ipv6_addr, uint32_t ttl);
#endif
#endif
