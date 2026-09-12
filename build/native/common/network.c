#include "network.h"
TC_LOCAL int runtime_ipv4_is_bindable(uint32_t ipv4_addr);
TC_LOCAL int netmask_prefix_length(uint32_t netmask);
TC_LOCAL int iface_name_has_prefix(const char *name, const char *prefix);
TC_LOCAL int iface_name_is_likely_lan(const char *name);
TC_LOCAL int iface_name_is_likely_wan_or_tunnel(const char *name);
TC_LOCAL uint32_t ipv4_link_local_netmask(void);
TC_LOCAL uint32_t ipv4_private_fallback_netmask(uint32_t ipv4_addr);
TC_LOCAL int effective_ipv4_prefix_length(uint32_t ipv4_addr, uint32_t netmask);
TC_LOCAL int ipv6_is_unspecified_addr(const struct in6_addr *addr);
TC_LOCAL int ipv6_is_loopback_addr(const struct in6_addr *addr);
TC_LOCAL int ipv6_is_multicast_addr(const struct in6_addr *addr);
TC_LOCAL int ipv6_is_ula_addr(const struct in6_addr *addr);
TC_LOCAL int runtime_ipv6_is_usable(const struct in6_addr *addr);
TC_LOCAL int ipv6_prefix_length_from_mask(const struct in6_addr *mask);
TC_LOCAL int iface_context_compare(const struct iface_context *a, const struct iface_context *b);
TC_LOCAL void sort_iface_contexts(struct iface_context_set *set);
TC_LOCAL int append_iface_context(struct iface_context_set *out,
                                                  const char *name,
                                                  uint32_t ipv4_addr,
                                                  uint32_t netmask,
                                                  int flags);
TC_LOCAL int collect_iface_contexts_with_policy(struct iface_context_set *out, int require_running);
TC_LOCAL int collect_usable_iface_contexts(struct iface_context_set *out);
TC_LOCAL int iface_context_identity_equal(const struct iface_context *a,
                                                          const struct iface_context *b);
TC_LOCAL int iface_context_set_contains(const struct iface_context_set *set,
                                                        const struct iface_context *ctx);
TC_LOCAL int iface_context_sets_equal(const struct iface_context_set *a,
                                                      const struct iface_context_set *b);
TC_LOCAL int source_matches_context_subnet(uint32_t source_ipv4_addr,
                                                           const struct iface_context *ctx);
TC_LOCAL int link_context_compare(const struct link_context *a, const struct link_context *b);
TC_LOCAL int link_context_has_ipv4(const struct link_context *ctx, uint32_t addr);
TC_LOCAL int link_context_has_ipv6(const struct link_context *ctx, const struct in6_addr *addr);
TC_LOCAL int append_link_ipv6(struct link_context_set *out,
                                             const char *name,
                                             const struct in6_addr *addr,
                                             int prefix_len,
                                             unsigned int scope_id,
                                             int flags);
TC_LOCAL int link_context_has_private_lan_samba_address(const struct link_context *ctx);
TC_LOCAL int link_context_identity_equal(const struct link_context *a,
                                                        const struct link_context *b);
#include "log.h"
#define fprintf timestamped_fprintf
int runtime_ipv4_is_usable(uint32_t ipv4_addr) {
    uint32_t host_order = ntohl(ipv4_addr);
    unsigned int first_octet = (unsigned int)((host_order >> 24) & 0xff);
    unsigned int second_octet = (unsigned int)((host_order >> 16) & 0xff);

    if (ipv4_addr == 0 || host_order == 0xffffffffU) {
        return 0;
    }
    if (first_octet == 0 || first_octet == 127) {
        return 0;
    }
    if (first_octet == 169 && second_octet == 254) {
        return 0;
    }
    if (first_octet >= 224) {
        return 0;
    }
    return 1;
}

TC_LOCAL int runtime_ipv4_is_bindable(uint32_t ipv4_addr) {
    uint32_t host_order = ntohl(ipv4_addr);
    unsigned int first_octet = (unsigned int)((host_order >> 24) & 0xff);

    if (ipv4_addr == 0 || host_order == 0xffffffffU) {
        return 0;
    }
    if (first_octet == 0 || first_octet == 127) {
        return 0;
    }
    if (first_octet >= 224) {
        return 0;
    }
    return 1;
}

int iface_flags_are_usable(int flags, int require_running) {
    if ((flags & IFF_UP) == 0) {
        return 0;
    }
    if ((flags & IFF_LOOPBACK) != 0) {
        return 0;
    }
    if (require_running && (flags & IFF_RUNNING) == 0) {
        return 0;
    }
    return 1;
}

TC_LOCAL int netmask_prefix_length(uint32_t netmask) {
    uint32_t mask = ntohl(netmask);
    int prefix = 0;
    int saw_zero = 0;
    int bit;

    if (mask == 0) {
        return 24;
    }

    for (bit = 31; bit >= 0; bit--) {
        if ((mask & (1U << bit)) != 0) {
            if (saw_zero) {
                return 24;
            }
            prefix++;
        } else {
            saw_zero = 1;
        }
    }

    return prefix;
}

TC_LOCAL int iface_name_has_prefix(const char *name, const char *prefix) {
    size_t prefix_len = strlen(prefix);
    return strncmp(name, prefix, prefix_len) == 0;
}

TC_LOCAL int iface_name_is_likely_lan(const char *name) {
    return iface_name_has_prefix(name, "bridge") ||
           iface_name_has_prefix(name, "br") ||
           iface_name_has_prefix(name, "lan") ||
           iface_name_has_prefix(name, "bcmeth") ||
           iface_name_has_prefix(name, "eth") ||
           iface_name_has_prefix(name, "en") ||
           iface_name_has_prefix(name, "wlan") ||
           iface_name_has_prefix(name, "ath") ||
           iface_name_has_prefix(name, "bwl");
}

int iface_name_is_strong_lan(const char *name) {
    return iface_name_has_prefix(name, "bridge") ||
           iface_name_has_prefix(name, "br") ||
           iface_name_has_prefix(name, "lan");
}

TC_LOCAL int iface_name_is_likely_wan_or_tunnel(const char *name) {
    return iface_name_has_prefix(name, "wan") ||
           iface_name_has_prefix(name, "ppp") ||
           iface_name_has_prefix(name, "tun") ||
           iface_name_has_prefix(name, "tap") ||
           iface_name_has_prefix(name, "gif") ||
           iface_name_has_prefix(name, "stf");
}

int ipv4_is_rfc1918(uint32_t ipv4_addr) {
    uint32_t host_order = ntohl(ipv4_addr);
    unsigned int first_octet = (unsigned int)((host_order >> 24) & 0xff);
    unsigned int second_octet = (unsigned int)((host_order >> 16) & 0xff);

    if (first_octet == 10) {
        return 1;
    }
    if (first_octet == 172 && second_octet >= 16 && second_octet <= 31) {
        return 1;
    }
    if (first_octet == 192 && second_octet == 168) {
        return 1;
    }
    return 0;
}

int ipv4_is_link_local(uint32_t ipv4_addr) {
    uint32_t host_order = ntohl(ipv4_addr);
    return ((host_order >> 24) & 0xff) == 169 && ((host_order >> 16) & 0xff) == 254;
}

TC_LOCAL uint32_t ipv4_link_local_netmask(void) {
    return htonl(0xffff0000U);
}

TC_LOCAL uint32_t ipv4_private_fallback_netmask(uint32_t ipv4_addr) {
    uint32_t host_order = ntohl(ipv4_addr);
    unsigned int first_octet = (unsigned int)((host_order >> 24) & 0xff);
    unsigned int second_octet = (unsigned int)((host_order >> 16) & 0xff);

    if (first_octet == 10) {
        return htonl(0xff000000U);
    }
    if (first_octet == 172 && second_octet >= 16 && second_octet <= 31) {
        return htonl(0xfff00000U);
    }
    if (first_octet == 192 && second_octet == 168) {
        return htonl(0xffffff00U);
    }
    return 0;
}

uint32_t effective_ipv4_netmask(uint32_t ipv4_addr, uint32_t netmask) {
    if (netmask != 0) {
        return netmask;
    }
    if (ipv4_is_link_local(ipv4_addr)) {
        return ipv4_link_local_netmask();
    }
    if (ipv4_is_rfc1918(ipv4_addr)) {
        return ipv4_private_fallback_netmask(ipv4_addr);
    }
    return 0;
}

TC_LOCAL int effective_ipv4_prefix_length(uint32_t ipv4_addr, uint32_t netmask) {
    uint32_t effective_netmask = effective_ipv4_netmask(ipv4_addr, netmask);

    if (effective_netmask == 0) {
        return 32;
    }
    return netmask_prefix_length(effective_netmask);
}

TC_LOCAL int ipv6_is_unspecified_addr(const struct in6_addr *addr) {
    static const unsigned char zero[16] = {0};
    return memcmp(addr->s6_addr, zero, sizeof(zero)) == 0;
}

TC_LOCAL int ipv6_is_loopback_addr(const struct in6_addr *addr) {
    static const unsigned char loopback[16] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1};
    return memcmp(addr->s6_addr, loopback, sizeof(loopback)) == 0;
}

TC_LOCAL int ipv6_is_multicast_addr(const struct in6_addr *addr) {
    return addr->s6_addr[0] == 0xff;
}

int ipv6_is_link_local_addr(const struct in6_addr *addr) {
    return addr->s6_addr[0] == 0xfe && (addr->s6_addr[1] & 0xc0) == 0x80;
}

TC_LOCAL int ipv6_is_ula_addr(const struct in6_addr *addr) {
    return (addr->s6_addr[0] & 0xfe) == 0xfc;
}

unsigned int ipv6_embedded_scope_id(const struct in6_addr *addr) {
    if (!ipv6_is_link_local_addr(addr)) {
        return 0;
    }
    return ((unsigned int)addr->s6_addr[2] << 8) |
           (unsigned int)addr->s6_addr[3];
}

void ipv6_canonicalize_scoped_address(struct in6_addr *out,
                                                               const struct in6_addr *in) {
    *out = *in;
    if (ipv6_is_link_local_addr(out)) {
        out->s6_addr[2] = 0;
        out->s6_addr[3] = 0;
    }
}

TC_LOCAL int runtime_ipv6_is_usable(const struct in6_addr *addr) {
    return !ipv6_is_unspecified_addr(addr) &&
           !ipv6_is_loopback_addr(addr) &&
           !ipv6_is_multicast_addr(addr);
}

int runtime_ipv6_is_bindable(const struct in6_addr *addr) {
    return runtime_ipv6_is_usable(addr);
}

TC_LOCAL int ipv6_prefix_length_from_mask(const struct in6_addr *mask) {
    int prefix = 0;
    int saw_zero = 0;
    size_t i;

    for (i = 0; i < sizeof(mask->s6_addr); i++) {
        unsigned char byte = mask->s6_addr[i];
        int bit;
        for (bit = 7; bit >= 0; bit--) {
            if ((byte & (1U << bit)) != 0) {
                if (saw_zero) {
                    return -1;
                }
                prefix++;
            } else {
                saw_zero = 1;
            }
        }
    }
    return prefix == 0 ? -1 : prefix;
}

int ipv6_prefix_matches(const struct in6_addr *a,
                                                 const struct in6_addr *b,
                                                 int prefix_len) {
    int full_bytes;
    int remaining_bits;
    unsigned char mask;

    if (prefix_len < 0) {
        return 0;
    }
    if (prefix_len == 0) {
        return 1;
    }
    if (prefix_len > 128) {
        prefix_len = 128;
    }
    full_bytes = prefix_len / 8;
    remaining_bits = prefix_len % 8;
    if (full_bytes > 0 && memcmp(a->s6_addr, b->s6_addr, (size_t)full_bytes) != 0) {
        return 0;
    }
    if (remaining_bits == 0) {
        return 1;
    }
    mask = (unsigned char)(0xffU << (8 - remaining_bits));
    return (a->s6_addr[full_bytes] & mask) == (b->s6_addr[full_bytes] & mask);
}

int iface_context_priority_score(const struct iface_context *ctx) {
    int score = 0;
    int prefix = effective_ipv4_prefix_length(ctx->ipv4_addr, ctx->netmask);

    if (ipv4_is_rfc1918(ctx->ipv4_addr)) {
        score -= 4000;
    }
    if (iface_name_is_strong_lan(ctx->name)) {
        score -= 3000;
    } else if (iface_name_is_likely_lan(ctx->name)) {
        score -= 2500;
    }
    if (iface_name_is_likely_wan_or_tunnel(ctx->name)) {
        score += 5000;
    }
    if (prefix == 24) {
        score -= 200;
    } else if (prefix >= 16 && prefix <= 30) {
        score -= 100;
    } else if (prefix >= 31) {
        score += 400;
    }
    if ((ctx->flags & IFF_RUNNING) == 0) {
        score += 500;
    }
    return score;
}

TC_LOCAL int iface_context_compare(const struct iface_context *a, const struct iface_context *b) {
    int a_score = iface_context_priority_score(a);
    int b_score = iface_context_priority_score(b);
    int name_cmp;
    uint32_t a_ip;
    uint32_t b_ip;
    uint32_t a_mask;
    uint32_t b_mask;

    if (a_score != b_score) {
        return a_score < b_score ? -1 : 1;
    }
    name_cmp = strcmp(a->name, b->name);
    if (name_cmp != 0) {
        return name_cmp;
    }
    a_ip = ntohl(a->ipv4_addr);
    b_ip = ntohl(b->ipv4_addr);
    if (a_ip != b_ip) {
        return a_ip < b_ip ? -1 : 1;
    }
    a_mask = ntohl(a->netmask);
    b_mask = ntohl(b->netmask);
    if (a_mask != b_mask) {
        return a_mask < b_mask ? -1 : 1;
    }
    return 0;
}

TC_LOCAL void sort_iface_contexts(struct iface_context_set *set) {
    size_t i;

    for (i = 1; i < set->count; i++) {
        struct iface_context current = set->contexts[i];
        size_t j = i;
        while (j > 0 && iface_context_compare(&current, &set->contexts[j - 1]) < 0) {
            set->contexts[j] = set->contexts[j - 1];
            j--;
        }
        set->contexts[j] = current;
    }
}

TC_LOCAL int append_iface_context(struct iface_context_set *out,
                                                  const char *name,
                                                  uint32_t ipv4_addr,
                                                  uint32_t netmask,
                                                  int flags) {
    size_t i;
    struct iface_context *ctx;

    if (!runtime_ipv4_is_usable(ipv4_addr)) {
        return 0;
    }
    for (i = 0; i < out->count; i++) {
        if (out->contexts[i].ipv4_addr == ipv4_addr) {
            return 0;
        }
    }
    if (out->count >= MAX_IFACE_CONTEXTS) {
        out->truncated = 1;
        return 0;
    }

    ctx = &out->contexts[out->count++];
    memset(ctx, 0, sizeof(*ctx));
    strncpy(ctx->name, name, sizeof(ctx->name) - 1);
    ctx->ipv4_addr = ipv4_addr;
    ctx->netmask = netmask;
    ctx->flags = flags;
    return 1;
}

const char *usable_ifaddrs_name(const struct ifaddrs *ifa,
                                                        char *buf,
                                                        size_t buf_len,
                                                        const char *fallback) {
    /*
     * AF_LINK exists on BSD-derived targets, where unnamed address rows can
     * still expose the interface name through sockaddr_dl. Linux omits that
     * path, so the scratch buffer is intentionally unused there.
     */
    (void)buf;
    (void)buf_len;

    if (ifa->ifa_name != NULL && ifa->ifa_name[0] != '\0') {
        return ifa->ifa_name;
    }
#if defined(AF_LINK) && (defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__DragonFly__))
    if (ifa->ifa_addr != NULL && ifa->ifa_addr->sa_family == AF_LINK) {
        const struct sockaddr_dl *sdl = (const struct sockaddr_dl *)(const void *)ifa->ifa_addr;
        if (sdl->sdl_nlen > 0 && buf_len > 0) {
            size_t copy_len = (size_t)sdl->sdl_nlen;
            if (copy_len >= buf_len) {
                copy_len = buf_len - 1;
            }
            memcpy(buf, sdl->sdl_data, copy_len);
            buf[copy_len] = '\0';
            return buf;
        }
    }
#endif
    if (fallback != NULL && fallback[0] != '\0') {
        return fallback;
    }
    return "";
}

void synthetic_ipv4_ifaddrs_name(char *buf, size_t buf_len, uint32_t ipv4_addr) {
    uint32_t host_order = ntohl(ipv4_addr);
    snprintf(buf,
             buf_len,
             "ip4-%08x",
             (unsigned int)host_order);
}

void synthetic_ipv6_ifaddrs_name(char *buf,
                                                         size_t buf_len,
                                                         const struct sockaddr_in6 *sin6) {
    if (sin6->sin6_scope_id != 0) {
        snprintf(buf, buf_len, "ipv6-if%u", (unsigned int)sin6->sin6_scope_id);
        return;
    }
    snprintf(buf, buf_len, "ipv6");
}

uint32_t getifaddrs_ipv4_netmask(const struct ifaddrs *ifa) {
    const struct sockaddr_in *netmask;

    if (ifa->ifa_addr == NULL ||
        ifa->ifa_addr->sa_family != AF_INET ||
        ifa->ifa_netmask == NULL) {
        return 0;
    }

    netmask = (const struct sockaddr_in *)(const void *)ifa->ifa_netmask;
    return netmask->sin_addr.s_addr;
}

int getifaddrs_ipv6_prefix_len(const struct ifaddrs *ifa) {
    const struct sockaddr_in6 *netmask;

    if (ifa->ifa_addr == NULL ||
        ifa->ifa_addr->sa_family != AF_INET6 ||
        ifa->ifa_netmask == NULL) {
        return -1;
    }

    netmask = (const struct sockaddr_in6 *)(const void *)ifa->ifa_netmask;
    return ipv6_prefix_length_from_mask(&netmask->sin6_addr);
}

TC_LOCAL int collect_iface_contexts_with_policy(struct iface_context_set *out, int require_running) {
    struct ifaddrs *ifaddr_list = NULL;
    struct ifaddrs *ifa;
    char current_name[IFNAMSIZ];

    memset(out, 0, sizeof(*out));
    current_name[0] = '\0';

    if (getifaddrs(&ifaddr_list) != 0) {
        perror("getifaddrs interface enumeration");
        return -1;
    }

    for (ifa = ifaddr_list; ifa != NULL; ifa = ifa->ifa_next) {
        struct sockaddr_in sin;
        char name_buf[IFNAMSIZ];
        const char *name;

        if (ifa->ifa_addr == NULL) {
            continue;
        }
        name = usable_ifaddrs_name(ifa, name_buf, sizeof(name_buf), current_name);
        if (name[0] != '\0' && name != current_name) {
            strncpy(current_name, name, sizeof(current_name) - 1);
            current_name[sizeof(current_name) - 1] = '\0';
        }
        if (ifa->ifa_addr->sa_family != AF_INET ||
            !iface_flags_are_usable((int)ifa->ifa_flags, require_running)) {
            continue;
        }

        memset(&sin, 0, sizeof(sin));
        memcpy(&sin, ifa->ifa_addr, sizeof(sin));
        if (name[0] == '\0') {
            synthetic_ipv4_ifaddrs_name(name_buf, sizeof(name_buf), sin.sin_addr.s_addr);
            name = name_buf;
        }
        append_iface_context(out,
                             name,
                             sin.sin_addr.s_addr,
                             getifaddrs_ipv4_netmask(ifa),
                             (int)ifa->ifa_flags);
    }

    freeifaddrs(ifaddr_list);
    sort_iface_contexts(out);
    return 0;
}

TC_LOCAL int collect_usable_iface_contexts(struct iface_context_set *out) {
    if (collect_iface_contexts_with_policy(out, 1) != 0) {
        return -1;
    }
    if (out->count > 0) {
        return 0;
    }
    if (collect_iface_contexts_with_policy(out, 0) != 0) {
        return -1;
    }
    if (out->count > 0) {
        fprintf(stderr, "auto-ip: no IFF_RUNNING usable IPv4 found; using IFF_UP fallback contexts\n");
    }
    return 0;
}

TC_LOCAL int iface_context_identity_equal(const struct iface_context *a,
                                                          const struct iface_context *b) {
    return strcmp(a->name, b->name) == 0 &&
           a->ipv4_addr == b->ipv4_addr &&
           a->netmask == b->netmask;
}

TC_LOCAL int iface_context_set_contains(const struct iface_context_set *set,
                                                        const struct iface_context *ctx) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (iface_context_identity_equal(&set->contexts[i], ctx)) {
            return 1;
        }
    }
    return 0;
}

TC_LOCAL int iface_context_sets_equal(const struct iface_context_set *a,
                                                      const struct iface_context_set *b) {
    size_t i;

    if (a->count != b->count) {
        return 0;
    }
    for (i = 0; i < a->count; i++) {
        if (!iface_context_set_contains(b, &a->contexts[i])) {
            return 0;
        }
    }
    return 1;
}

TC_LOCAL int source_matches_context_subnet(uint32_t source_ipv4_addr,
                                                           const struct iface_context *ctx) {
    uint32_t netmask = effective_ipv4_netmask(ctx->ipv4_addr, ctx->netmask);

    if (netmask == 0) {
        return source_ipv4_addr == ctx->ipv4_addr;
    }
    return (source_ipv4_addr & netmask) == (ctx->ipv4_addr & netmask);
}

int iface_context_cidr(char *out, size_t out_len, const struct iface_context *ctx) {
    char ip_buf[INET_ADDRSTRLEN];
    int written;

    written = snprintf(out,
                       out_len,
                       "%s/%d",
                       ipv4_to_string(ctx->ipv4_addr, ip_buf, sizeof(ip_buf)),
                       effective_ipv4_prefix_length(ctx->ipv4_addr, ctx->netmask));
    if (written < 0 || (size_t)written >= out_len) {
        return -1;
    }
    return 0;
}

int link_context_priority_score(const struct link_context *ctx) {
    int score = 0;

    if (ctx->ipv4_count > 0) {
        if (ipv4_is_rfc1918(ctx->ipv4[0].addr)) {
            score -= 4000;
        }
        if (ipv4_is_link_local(ctx->ipv4[0].addr)) {
            score += 200;
        }
    } else {
        score += 1000;
    }
    if (iface_name_is_strong_lan(ctx->name)) {
        score -= 3000;
    } else if (iface_name_is_likely_lan(ctx->name)) {
        score -= 2500;
    }
    if (iface_name_is_likely_wan_or_tunnel(ctx->name)) {
        score += 5000;
    }
    if ((ctx->flags & IFF_RUNNING) == 0) {
        score += 500;
    }
    return score;
}

TC_LOCAL int link_context_compare(const struct link_context *a, const struct link_context *b) {
    int a_score = link_context_priority_score(a);
    int b_score = link_context_priority_score(b);
    int name_cmp;

    if (a_score != b_score) {
        return a_score < b_score ? -1 : 1;
    }
    name_cmp = strcmp(a->name, b->name);
    if (name_cmp != 0) {
        return name_cmp;
    }
    if (a->ipv4_count > 0 && b->ipv4_count > 0 && a->ipv4[0].addr != b->ipv4[0].addr) {
        return ntohl(a->ipv4[0].addr) < ntohl(b->ipv4[0].addr) ? -1 : 1;
    }
    return 0;
}

void sort_link_contexts(struct link_context_set *set) {
    size_t i;

    for (i = 1; i < set->count; i++) {
        struct link_context current = set->links[i];
        size_t j = i;
        while (j > 0 && link_context_compare(&current, &set->links[j - 1]) < 0) {
            set->links[j] = set->links[j - 1];
            j--;
        }
        set->links[j] = current;
    }
}

struct link_context *find_or_add_link_context(struct link_context_set *out,
                                                                      const char *name,
                                                                      int flags) {
    size_t i;
    struct link_context *ctx;

    for (i = 0; i < out->count; i++) {
        if (strcmp(out->links[i].name, name) == 0) {
            if (out->links[i].flags == 0) {
                out->links[i].flags = flags;
            }
            return &out->links[i];
        }
    }
    if (out->count >= MAX_IFACE_CONTEXTS) {
        out->truncated = 1;
        return NULL;
    }
    ctx = &out->links[out->count++];
    memset(ctx, 0, sizeof(*ctx));
    strncpy(ctx->name, name, sizeof(ctx->name) - 1);
    ctx->flags = flags;
    return ctx;
}

TC_LOCAL int link_context_has_ipv4(const struct link_context *ctx, uint32_t addr) {
    size_t i;

    for (i = 0; i < ctx->ipv4_count; i++) {
        if (ctx->ipv4[i].addr == addr) {
            return 1;
        }
    }
    return 0;
}

TC_LOCAL int link_context_has_ipv6(const struct link_context *ctx, const struct in6_addr *addr) {
    size_t i;

    for (i = 0; i < ctx->ipv6_count; i++) {
        if (memcmp(&ctx->ipv6[i].addr, addr, sizeof(*addr)) == 0) {
            return 1;
        }
    }
    return 0;
}

int append_link_ipv4(struct link_context_set *out,
                                             const char *name,
                                             uint32_t ipv4_addr,
                                             uint32_t netmask,
                                             int flags) {
    struct link_context *ctx;
    size_t pos;

    if (!runtime_ipv4_is_bindable(ipv4_addr)) {
        return 0;
    }
    ctx = find_or_add_link_context(out, name, flags);
    if (ctx == NULL || link_context_has_ipv4(ctx, ipv4_addr)) {
        return 0;
    }
    if (ctx->ipv4_count >= MAX_LINK_IPV4_ADDRS) {
        out->truncated = 1;
        return 0;
    }
    pos = ctx->ipv4_count++;
    ctx->ipv4[pos].addr = ipv4_addr;
    ctx->ipv4[pos].netmask = netmask;
    ctx->mdns_ipv4_transport = 1;
    if (ctx->ipv4_count == 1) {
        ctx->flags = flags;
    }
    return 1;
}

int append_link_ipv6_with_transport(struct link_context_set *out,
                                                            const char *name,
                                                            const struct in6_addr *addr,
                                                            int prefix_len,
                                                            unsigned int scope_id,
                                                            int flags,
                                                            int mdns_ipv6_transport) {
    struct link_context *ctx;
    size_t pos;

    if (!runtime_ipv6_is_usable(addr)) {
        return 0;
    }
    ctx = find_or_add_link_context(out, name, flags);
    if (ctx == NULL || link_context_has_ipv6(ctx, addr)) {
        return 0;
    }
    if (ctx->ipv6_count >= MAX_LINK_IPV6_ADDRS) {
        out->truncated = 1;
        return 0;
    }
    if (ctx->ifindex == 0 && scope_id != 0) {
        ctx->ifindex = scope_id;
    }
    if (mdns_ipv6_transport) {
        ctx->mdns_ipv6_transport = 1;
    }
    pos = ctx->ipv6_count++;
    ctx->ipv6[pos].addr = *addr;
    ctx->ipv6[pos].scope_id = scope_id;
    ctx->ipv6[pos].prefix_len = prefix_len >= 0 && prefix_len <= 128 ? prefix_len : -1;
    ctx->ipv6[pos].link_local = ipv6_is_link_local_addr(addr);
    return 1;
}

TC_LOCAL int append_link_ipv6(struct link_context_set *out,
                                             const char *name,
                                             const struct in6_addr *addr,
                                             int prefix_len,
                                             unsigned int scope_id,
                                             int flags) {
    return append_link_ipv6_with_transport(out, name, addr, prefix_len, scope_id, flags, 1);
}

int link_ipv6_addr_is_samba_bindable(const struct link_ipv6_addr *addr) {
    return addr->prefix_len >= 0 && runtime_ipv6_is_bindable(&addr->addr);
}

int link_ipv4_addr_is_samba_bindable(const struct link_ipv4_addr *addr) {
    return runtime_ipv4_is_bindable(addr->addr) && !ipv4_is_link_local(addr->addr);
}

int link_context_has_samba_address(const struct link_context *ctx) {
    size_t i;

    for (i = 0; i < ctx->ipv4_count; i++) {
        if (link_ipv4_addr_is_samba_bindable(&ctx->ipv4[i])) {
            return 1;
        }
    }
    for (i = 0; i < ctx->ipv6_count; i++) {
        if (link_ipv6_addr_is_samba_bindable(&ctx->ipv6[i])) {
            return 1;
        }
    }
    return 0;
}

TC_LOCAL int link_context_has_private_lan_samba_address(const struct link_context *ctx) {
    size_t i;

    for (i = 0; i < ctx->ipv4_count; i++) {
        if (link_ipv4_addr_is_samba_bindable(&ctx->ipv4[i]) &&
            ipv4_is_rfc1918(ctx->ipv4[i].addr)) {
            return 1;
        }
    }
    for (i = 0; i < ctx->ipv6_count; i++) {
        if (link_ipv6_addr_is_samba_bindable(&ctx->ipv6[i]) &&
            (ipv6_is_ula_addr(&ctx->ipv6[i].addr) ||
             ipv6_is_link_local_addr(&ctx->ipv6[i].addr))) {
            return 1;
        }
    }
    return 0;
}

int iface_name_is_synthetic_from_address(const char *name) {
    return iface_name_has_prefix(name, "ip4-") ||
           strcmp(name, "ipv6") == 0 ||
           iface_name_has_prefix(name, "ipv6-if");
}

int link_context_is_unnamed_private_lan_fallback(const struct link_context *ctx) {
    return iface_name_is_synthetic_from_address(ctx->name) &&
           link_context_has_private_lan_samba_address(ctx);
}

TC_LOCAL int link_context_identity_equal(const struct link_context *a,
                                                        const struct link_context *b) {
    size_t i;

    if (strcmp(a->name, b->name) != 0 ||
        a->flags != b->flags ||
        a->ifindex != b->ifindex ||
        a->is_wan != b->is_wan ||
        a->mdns_ipv4_transport != b->mdns_ipv4_transport ||
        a->mdns_ipv6_transport != b->mdns_ipv6_transport ||
        a->ipv4_count != b->ipv4_count ||
        a->ipv6_count != b->ipv6_count) {
        return 0;
    }
    for (i = 0; i < a->ipv4_count; i++) {
        if (a->ipv4[i].addr != b->ipv4[i].addr ||
            a->ipv4[i].netmask != b->ipv4[i].netmask) {
            return 0;
        }
    }
    for (i = 0; i < a->ipv6_count; i++) {
        if (memcmp(&a->ipv6[i].addr, &b->ipv6[i].addr, sizeof(a->ipv6[i].addr)) != 0 ||
            a->ipv6[i].scope_id != b->ipv6[i].scope_id ||
            a->ipv6[i].prefix_len != b->ipv6[i].prefix_len ||
            a->ipv6[i].link_local != b->ipv6[i].link_local) {
            return 0;
        }
    }
    return 1;
}

int link_context_set_contains(const struct link_context_set *set,
                                                      const struct link_context *ctx) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (link_context_identity_equal(&set->links[i], ctx)) {
            return 1;
        }
    }
    return 0;
}

int link_context_sets_equal(const struct link_context_set *a,
                                                    const struct link_context_set *b) {
    size_t i;

    if (a->count != b->count) {
        return 0;
    }
    for (i = 0; i < a->count; i++) {
        if (!link_context_set_contains(b, &a->links[i])) {
            return 0;
        }
    }
    return 1;
}

int link_context_ipv4_cidr(char *out,
                                                   size_t out_len,
                                                   const struct link_ipv4_addr *addr) {
    char ip_buf[INET_ADDRSTRLEN];
    int written;

    written = snprintf(out,
                       out_len,
                       "%s/%d",
                       ipv4_to_string(addr->addr, ip_buf, sizeof(ip_buf)),
                       effective_ipv4_prefix_length(addr->addr, addr->netmask));
    if (written < 0 || (size_t)written >= out_len) {
        return -1;
    }
    return 0;
}

int link_context_ipv6_cidr(char *out,
                                                   size_t out_len,
                                                   const struct link_ipv6_addr *addr) {
    char ip_buf[INET6_ADDRSTRLEN];
    int written;

    if (inet_ntop(AF_INET6, &addr->addr, ip_buf, sizeof(ip_buf)) == NULL) {
        return -1;
    }
    if (addr->prefix_len < 0 || addr->prefix_len > 128) {
        return -1;
    }
    written = snprintf(out, out_len, "%s/%d", ip_buf, addr->prefix_len);
    if (written < 0 || (size_t)written >= out_len) {
        return -1;
    }
    return 0;
}

const char *ipv4_to_string(uint32_t ipv4_addr, char *out, size_t out_len) {
    struct in_addr addr;

    addr.s_addr = ipv4_addr;
    if (inet_ntop(AF_INET, &addr, out, out_len) == NULL) {
        strncpy(out, "invalid", out_len - 1);
        out[out_len - 1] = '\0';
    }
    return out;
}
