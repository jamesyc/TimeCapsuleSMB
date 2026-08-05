#ifndef TC_NETWORK_H
#define TC_NETWORK_H
#include "platform.h"

#if defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__DragonFly__)
#include <net/if_dl.h>
#include <netinet6/in6_var.h>
#endif

#include <ifaddrs.h>
#define MAX_IFACE_CONTEXTS 16

#ifndef MAX_LINK_IPV4_ADDRS
#define MAX_LINK_IPV4_ADDRS 8
#endif
#ifndef MAX_LINK_IPV6_ADDRS
#define MAX_LINK_IPV6_ADDRS 8
#endif
#ifndef MAX_IFCONFIG_OWNER_ENTRIES
#define MAX_IFCONFIG_OWNER_ENTRIES 64
#endif

struct iface_context {
    char name[IFNAMSIZ];
    uint32_t ipv4_addr;
    uint32_t netmask;
    int flags;
};

struct iface_context_set {
    struct iface_context contexts[MAX_IFACE_CONTEXTS];
    size_t count;
    int truncated;
};

struct link_ipv4_addr {
    uint32_t addr;
    uint32_t netmask;
};

struct link_ipv6_addr {
    struct in6_addr addr;
    unsigned int scope_id;
    int prefix_len;
    int link_local;
};

struct link_context {
    char name[IFNAMSIZ];
    int flags;
    unsigned int ifindex;
    int is_wan;
    int mdns_ipv4_transport;
    uint32_t mdns_ipv4_transport_addr;
    int mdns_ipv6_transport;
    struct link_ipv4_addr ipv4[MAX_LINK_IPV4_ADDRS];
    size_t ipv4_count;
    struct link_ipv6_addr ipv6[MAX_LINK_IPV6_ADDRS];
    size_t ipv6_count;
};

struct link_context_set {
    struct link_context links[MAX_IFACE_CONTEXTS];
    size_t count;
    int truncated;
};

struct ifconfig_address_owner {
    int family;
    uint32_t ipv4_addr;
    struct in6_addr ipv6_addr;
    unsigned int ipv6_ifindex;
    char name[IFNAMSIZ];
};

struct ifconfig_address_owner_map {
    struct ifconfig_address_owner entries[MAX_IFCONFIG_OWNER_ENTRIES];
    size_t count;
};

struct network_role_evidence {
    char default_iface[IFNAMSIZ];
    char default_ipv6_iface[IFNAMSIZ];
    char nat_iface[IFNAMSIZ];
};

int runtime_ipv4_is_usable(uint32_t ipv4_addr);
#ifdef TC_NATIVE_TEST
int runtime_ipv4_is_bindable(uint32_t ipv4_addr);
#endif
int iface_flags_are_usable(int flags, int require_running);
#ifdef TC_NATIVE_TEST
int netmask_prefix_length(uint32_t netmask);
#endif
#ifdef TC_NATIVE_TEST
int iface_name_has_prefix(const char *name, const char *prefix);
#endif
#ifdef TC_NATIVE_TEST
int iface_name_is_likely_lan(const char *name);
#endif
int iface_name_is_strong_lan(const char *name);
#ifdef TC_NATIVE_TEST
int iface_name_is_likely_wan_or_tunnel(const char *name);
#endif
int ipv4_is_rfc1918(uint32_t ipv4_addr);
int ipv4_is_link_local(uint32_t ipv4_addr);
#ifdef TC_NATIVE_TEST
uint32_t ipv4_link_local_netmask(void);
#endif
#ifdef TC_NATIVE_TEST
uint32_t ipv4_private_fallback_netmask(uint32_t ipv4_addr);
#endif
uint32_t effective_ipv4_netmask(uint32_t ipv4_addr, uint32_t netmask);
#ifdef TC_NATIVE_TEST
int effective_ipv4_prefix_length(uint32_t ipv4_addr, uint32_t netmask);
#endif
#ifdef TC_NATIVE_TEST
int ipv6_is_unspecified_addr(const struct in6_addr *addr);
#endif
#ifdef TC_NATIVE_TEST
int ipv6_is_loopback_addr(const struct in6_addr *addr);
#endif
#ifdef TC_NATIVE_TEST
int ipv6_is_multicast_addr(const struct in6_addr *addr);
#endif
int ipv6_is_link_local_addr(const struct in6_addr *addr);
#ifdef TC_NATIVE_TEST
int ipv6_is_ula_addr(const struct in6_addr *addr);
#endif
unsigned int ipv6_embedded_scope_id(const struct in6_addr *addr);
void ipv6_canonicalize_scoped_address(struct in6_addr *out,
                                                               const struct in6_addr *in);
#ifdef TC_NATIVE_TEST
int runtime_ipv6_is_usable(const struct in6_addr *addr);
#endif
int runtime_ipv6_is_bindable(const struct in6_addr *addr);
#ifdef TC_NATIVE_TEST
int ipv6_prefix_length_from_mask(const struct in6_addr *mask);
#endif
int ipv6_prefix_matches(const struct in6_addr *a,
                                                 const struct in6_addr *b,
                                                 int prefix_len);
int iface_context_priority_score(const struct iface_context *ctx);
#ifdef TC_NATIVE_TEST
int iface_context_compare(const struct iface_context *a, const struct iface_context *b);
#endif
#ifdef TC_NATIVE_TEST
void sort_iface_contexts(struct iface_context_set *set);
#endif
#ifdef TC_NATIVE_TEST
int append_iface_context(struct iface_context_set *out,
                                                  const char *name,
                                                  uint32_t ipv4_addr,
                                                  uint32_t netmask,
                                                  int flags);
#endif
const char *usable_ifaddrs_name(const struct ifaddrs *ifa,
                                                        char *buf,
                                                        size_t buf_len,
                                                        const char *fallback);
void synthetic_ipv4_ifaddrs_name(char *buf, size_t buf_len, uint32_t ipv4_addr);
void synthetic_ipv6_ifaddrs_name(char *buf,
                                                         size_t buf_len,
                                                         const struct sockaddr_in6 *sin6);
uint32_t getifaddrs_ipv4_netmask(const struct ifaddrs *ifa);
int getifaddrs_ipv6_prefix_len(const struct ifaddrs *ifa);
#ifdef TC_NATIVE_TEST
int collect_iface_contexts_with_policy(struct iface_context_set *out, int require_running);
#endif
#ifdef TC_NATIVE_TEST
int collect_usable_iface_contexts(struct iface_context_set *out);
#endif
#ifdef TC_NATIVE_TEST
int iface_context_identity_equal(const struct iface_context *a,
                                                          const struct iface_context *b);
#endif
#ifdef TC_NATIVE_TEST
int iface_context_set_contains(const struct iface_context_set *set,
                                                        const struct iface_context *ctx);
#endif
#ifdef TC_NATIVE_TEST
int iface_context_sets_equal(const struct iface_context_set *a,
                                                      const struct iface_context_set *b);
#endif
#ifdef TC_NATIVE_TEST
int source_matches_context_subnet(uint32_t source_ipv4_addr,
                                                           const struct iface_context *ctx);
#endif
int iface_context_cidr(char *out, size_t out_len, const struct iface_context *ctx);
int link_context_priority_score(const struct link_context *ctx);
#ifdef TC_NATIVE_TEST
int link_context_compare(const struct link_context *a, const struct link_context *b);
#endif
void sort_link_contexts(struct link_context_set *set);
struct link_context *find_or_add_link_context(struct link_context_set *out,
                                                                      const char *name,
                                                                      int flags);
#ifdef TC_NATIVE_TEST
int link_context_has_ipv4(const struct link_context *ctx, uint32_t addr);
#endif
#ifdef TC_NATIVE_TEST
int link_context_has_ipv6(const struct link_context *ctx, const struct in6_addr *addr);
#endif
int append_link_ipv4(struct link_context_set *out,
                                             const char *name,
                                             uint32_t ipv4_addr,
                                             uint32_t netmask,
                                             int flags);
int append_link_ipv6_with_transport(struct link_context_set *out,
                                                            const char *name,
                                                            const struct in6_addr *addr,
                                                            int prefix_len,
                                                            unsigned int scope_id,
                                                            int flags,
                                                            int mdns_ipv6_transport);
#ifdef TC_NATIVE_TEST
int append_link_ipv6(struct link_context_set *out,
                                             const char *name,
                                             const struct in6_addr *addr,
                                             int prefix_len,
                                             unsigned int scope_id,
                                             int flags);
#endif
int link_ipv6_addr_is_samba_bindable(const struct link_ipv6_addr *addr);
int link_ipv4_addr_is_samba_bindable(const struct link_ipv4_addr *addr);
int link_context_has_samba_address(const struct link_context *ctx);
#ifdef TC_NATIVE_TEST
int link_context_has_private_lan_samba_address(const struct link_context *ctx);
#endif
int iface_name_is_synthetic_from_address(const char *name);
int link_context_is_unnamed_private_lan_fallback(const struct link_context *ctx);
#ifdef TC_NATIVE_TEST
int link_context_identity_equal(const struct link_context *a,
                                                        const struct link_context *b);
#endif
int link_context_set_contains(const struct link_context_set *set,
                                                      const struct link_context *ctx);
int link_context_sets_equal(const struct link_context_set *a,
                                                    const struct link_context_set *b);
int link_context_ipv4_cidr(char *out,
                                                   size_t out_len,
                                                   const struct link_ipv4_addr *addr);
int link_context_ipv6_cidr(char *out,
                                                   size_t out_len,
                                                   const struct link_ipv6_addr *addr);
const char *ipv4_to_string(uint32_t ipv4_addr, char *out, size_t out_len);

void filter_smb_bind_link_contexts(struct link_context_set *out,
                                                            const struct link_context_set *in,
                                                            int lan_only,
                                                            int unnamed_lan_fallback);
int print_smb_link_bind_tokens(FILE *stream, const struct link_context_set *set);
#ifdef TC_NATIVE_TEST
int print_iface_context_cidrs(FILE *stream, const struct iface_context_set *set);
#endif

#ifdef TC_NATIVE_TEST
void ifconfig_owner_map_add_ipv4(struct ifconfig_address_owner_map *map,
                                                          const char *name,
                                                          const char *addr_text);
#endif
#ifdef TC_NATIVE_TEST
void ifconfig_owner_map_add_ipv6(struct ifconfig_address_owner_map *map,
                                                          const char *name,
                                                          const char *addr_text);
#endif
#ifdef TC_NATIVE_TEST
int copy_first_token(char *out,
                                              size_t out_len,
                                              const char *text);
#endif
#ifdef TC_NATIVE_TEST
int copy_last_token(char *out,
                                             size_t out_len,
                                             const char *text);
#endif
#ifdef TC_NATIVE_TEST
void network_role_evidence_parse_route_line(struct network_role_evidence *evidence,
                                                                      const char *line);
#endif
#ifdef TC_NATIVE_TEST
void network_role_evidence_parse_ipv6_route_line(struct network_role_evidence *evidence,
                                                                           const char *line);
#endif
#ifdef TC_NATIVE_TEST
void network_role_evidence_parse_pf_line(struct network_role_evidence *evidence,
                                                                   const char *line);
#endif
#ifdef TC_NATIVE_TEST
void parse_network_role_stream(FILE *stream,
                                                        struct network_role_evidence *evidence,
                                                        int mode);
#endif
#ifdef TC_NATIVE_TEST
void collect_network_role_evidence(struct network_role_evidence *evidence);
#endif
#ifdef TC_NATIVE_TEST
void ifconfig_owner_map_parse_line(struct ifconfig_address_owner_map *map,
                                                            char *current_name,
                                                            size_t current_name_len,
                                                            const char *line);
#endif
int collect_ifconfig_address_owners(struct ifconfig_address_owner_map *map);
const char *ifconfig_owner_for_ipv4(const struct ifconfig_address_owner_map *map,
                                                            uint32_t ipv4_addr);
const char *ifconfig_owner_for_ipv6(const struct ifconfig_address_owner_map *map,
                                                            const struct in6_addr *ipv6_addr);
#ifdef TC_NATIVE_TEST
int owner_map_has_addressed_strong_lan(const struct ifconfig_address_owner_map *owners);
#endif
#ifdef TC_NATIVE_TEST
int link_set_has_addressed_strong_lan(const struct link_context_set *links);
#endif
#ifdef TC_NATIVE_TEST
int link_context_owned_by_interface(const struct link_context *ctx,
                                                             const char *iface,
                                                             const struct ifconfig_address_owner_map *owners);
#endif
#ifdef TC_NATIVE_TEST
void mark_wan_interface(struct link_context_set *links,
                                                const char *wan_iface,
                                                const struct ifconfig_address_owner_map *owners);
#endif
#ifdef TC_NATIVE_TEST
void mark_wan_link_contexts_from_evidence(struct link_context_set *links,
                                                                  const struct network_role_evidence *evidence,
                                                                  const struct ifconfig_address_owner_map *owners);
#endif
void mark_wan_link_contexts(struct link_context_set *links);
int link_context_set_has_synthetic_names(const struct link_context_set *set);

#ifdef TC_NATIVE_TEST
void relabel_synthetic_link_contexts_from_ifconfig(struct link_context_set *set);
#endif
#ifdef TC_NATIVE_TEST
int collect_link_contexts_with_policy(struct link_context_set *out, int require_running);
#endif
int collect_usable_link_contexts(struct link_context_set *out);

int link_context_has_advertisable_ipv4(const struct link_context *ctx);
#ifdef TC_NATIVE_TEST
int link_context_has_advertisable_ipv6(const struct link_context *ctx);
#endif
#ifdef TC_NATIVE_TEST
int link_context_has_advertisable_address(const struct link_context *ctx);
#endif
int link_context_has_mdns_ipv4_transport(const struct link_context *ctx);
void disable_link_contexts_mdns_ipv4_transport(struct link_context_set *set);
int link_context_has_mdns_ipv6_transport(const struct link_context *ctx);
void disable_link_contexts_mdns_ipv6_transport(struct link_context_set *set);
#ifdef TC_NATIVE_TEST
int link_context_is_advertise_eligible(const struct link_context *ctx);
#endif
void filter_advertise_link_contexts(struct link_context_set *out,
                                                             const struct link_context_set *in);

void log_link_contexts(const char *prefix, const struct link_context_set *set);
#ifdef TC_NATIVE_TEST
void log_iface_contexts(const char *prefix, const struct iface_context_set *set);
#endif

#endif
