#ifndef TC_AUTO_IP_COMMON_INC
#define TC_AUTO_IP_COMMON_INC

#if defined(__GNUC__)
#define TC_AUTO_IP_UNUSED __attribute__((unused))
#else
#define TC_AUTO_IP_UNUSED
#endif

#if defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__DragonFly__)
#include <net/if_dl.h>
#include <netinet6/in6_var.h>
#endif

#include <ifaddrs.h>

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
    char name[IFNAMSIZ];
};

struct ifconfig_address_owner_map {
    struct ifconfig_address_owner entries[MAX_IFCONFIG_OWNER_ENTRIES];
    size_t count;
};

static int TC_AUTO_IP_UNUSED runtime_ipv4_is_usable(uint32_t ipv4_addr) {
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

static int TC_AUTO_IP_UNUSED runtime_ipv4_is_bindable(uint32_t ipv4_addr) {
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

static int TC_AUTO_IP_UNUSED iface_flags_are_usable(int flags, int require_running) {
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

static int TC_AUTO_IP_UNUSED netmask_prefix_length(uint32_t netmask) {
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

static int TC_AUTO_IP_UNUSED iface_name_has_prefix(const char *name, const char *prefix) {
    size_t prefix_len = strlen(prefix);
    return strncmp(name, prefix, prefix_len) == 0;
}

static int TC_AUTO_IP_UNUSED iface_name_is_likely_lan(const char *name) {
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

static int TC_AUTO_IP_UNUSED iface_name_is_strong_lan(const char *name) {
    return iface_name_has_prefix(name, "bridge") ||
           iface_name_has_prefix(name, "br") ||
           iface_name_has_prefix(name, "lan");
}

static int TC_AUTO_IP_UNUSED iface_name_is_likely_wan_or_tunnel(const char *name) {
    return iface_name_has_prefix(name, "wan") ||
           iface_name_has_prefix(name, "ppp") ||
           iface_name_has_prefix(name, "tun") ||
           iface_name_has_prefix(name, "tap") ||
           iface_name_has_prefix(name, "gif") ||
           iface_name_has_prefix(name, "stf");
}

static int TC_AUTO_IP_UNUSED ipv4_is_rfc1918(uint32_t ipv4_addr) {
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

static int TC_AUTO_IP_UNUSED ipv4_is_link_local(uint32_t ipv4_addr) {
    uint32_t host_order = ntohl(ipv4_addr);
    return ((host_order >> 24) & 0xff) == 169 && ((host_order >> 16) & 0xff) == 254;
}

static uint32_t TC_AUTO_IP_UNUSED ipv4_link_local_netmask(void) {
    return htonl(0xffff0000U);
}

static uint32_t TC_AUTO_IP_UNUSED ipv4_private_fallback_netmask(uint32_t ipv4_addr) {
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

static uint32_t TC_AUTO_IP_UNUSED effective_ipv4_netmask(uint32_t ipv4_addr, uint32_t netmask) {
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

static int TC_AUTO_IP_UNUSED effective_ipv4_prefix_length(uint32_t ipv4_addr, uint32_t netmask) {
    uint32_t effective_netmask = effective_ipv4_netmask(ipv4_addr, netmask);

    if (effective_netmask == 0) {
        return 32;
    }
    return netmask_prefix_length(effective_netmask);
}

static int TC_AUTO_IP_UNUSED ipv6_is_unspecified_addr(const struct in6_addr *addr) {
    static const unsigned char zero[16] = {0};
    return memcmp(addr->s6_addr, zero, sizeof(zero)) == 0;
}

static int TC_AUTO_IP_UNUSED ipv6_is_loopback_addr(const struct in6_addr *addr) {
    static const unsigned char loopback[16] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1};
    return memcmp(addr->s6_addr, loopback, sizeof(loopback)) == 0;
}

static int TC_AUTO_IP_UNUSED ipv6_is_multicast_addr(const struct in6_addr *addr) {
    return addr->s6_addr[0] == 0xff;
}

static int TC_AUTO_IP_UNUSED ipv6_is_link_local_addr(const struct in6_addr *addr) {
    return addr->s6_addr[0] == 0xfe && (addr->s6_addr[1] & 0xc0) == 0x80;
}

static int TC_AUTO_IP_UNUSED ipv6_is_ula_addr(const struct in6_addr *addr) {
    return (addr->s6_addr[0] & 0xfe) == 0xfc;
}

static int TC_AUTO_IP_UNUSED runtime_ipv6_is_usable(const struct in6_addr *addr) {
    return !ipv6_is_unspecified_addr(addr) &&
           !ipv6_is_loopback_addr(addr) &&
           !ipv6_is_multicast_addr(addr);
}

static int TC_AUTO_IP_UNUSED runtime_ipv6_is_bindable(const struct in6_addr *addr) {
    return runtime_ipv6_is_usable(addr) && !ipv6_is_link_local_addr(addr);
}

static int TC_AUTO_IP_UNUSED ipv6_prefix_length_from_mask(const struct in6_addr *mask) {
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

static int TC_AUTO_IP_UNUSED ipv6_prefix_matches(const struct in6_addr *a,
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

static int TC_AUTO_IP_UNUSED iface_context_priority_score(const struct iface_context *ctx) {
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

static int TC_AUTO_IP_UNUSED iface_context_compare(const struct iface_context *a, const struct iface_context *b) {
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

static void TC_AUTO_IP_UNUSED sort_iface_contexts(struct iface_context_set *set) {
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

static int TC_AUTO_IP_UNUSED append_iface_context(struct iface_context_set *out,
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

static const char TC_AUTO_IP_UNUSED *usable_ifaddrs_name(const struct ifaddrs *ifa,
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

static void TC_AUTO_IP_UNUSED synthetic_ipv4_ifaddrs_name(char *buf, size_t buf_len, uint32_t ipv4_addr) {
    uint32_t host_order = ntohl(ipv4_addr);
    snprintf(buf,
             buf_len,
             "ip4-%08x",
             (unsigned int)host_order);
}

static void TC_AUTO_IP_UNUSED synthetic_ipv6_ifaddrs_name(char *buf,
                                                         size_t buf_len,
                                                         const struct sockaddr_in6 *sin6) {
    if (sin6->sin6_scope_id != 0) {
        snprintf(buf, buf_len, "ipv6-if%u", (unsigned int)sin6->sin6_scope_id);
        return;
    }
    snprintf(buf, buf_len, "ipv6");
}

static uint32_t TC_AUTO_IP_UNUSED getifaddrs_ipv4_netmask(const struct ifaddrs *ifa) {
    const struct sockaddr_in *netmask;

    if (ifa->ifa_addr == NULL ||
        ifa->ifa_addr->sa_family != AF_INET ||
        ifa->ifa_netmask == NULL) {
        return 0;
    }

    netmask = (const struct sockaddr_in *)(const void *)ifa->ifa_netmask;
    return netmask->sin_addr.s_addr;
}

static int TC_AUTO_IP_UNUSED getifaddrs_ipv6_prefix_len(const struct ifaddrs *ifa) {
    const struct sockaddr_in6 *netmask;

    if (ifa->ifa_addr == NULL ||
        ifa->ifa_addr->sa_family != AF_INET6 ||
        ifa->ifa_netmask == NULL) {
        return -1;
    }

    netmask = (const struct sockaddr_in6 *)(const void *)ifa->ifa_netmask;
    return ipv6_prefix_length_from_mask(&netmask->sin6_addr);
}

static int TC_AUTO_IP_UNUSED collect_iface_contexts_with_policy(struct iface_context_set *out, int require_running) {
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

static int TC_AUTO_IP_UNUSED collect_usable_iface_contexts(struct iface_context_set *out) {
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

static int TC_AUTO_IP_UNUSED iface_context_identity_equal(const struct iface_context *a,
                                                          const struct iface_context *b) {
    return strcmp(a->name, b->name) == 0 &&
           a->ipv4_addr == b->ipv4_addr &&
           a->netmask == b->netmask;
}

static int TC_AUTO_IP_UNUSED iface_context_set_contains(const struct iface_context_set *set,
                                                        const struct iface_context *ctx) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (iface_context_identity_equal(&set->contexts[i], ctx)) {
            return 1;
        }
    }
    return 0;
}

static int TC_AUTO_IP_UNUSED iface_context_sets_equal(const struct iface_context_set *a,
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

static int TC_AUTO_IP_UNUSED source_matches_context_subnet(uint32_t source_ipv4_addr,
                                                           const struct iface_context *ctx) {
    uint32_t netmask = effective_ipv4_netmask(ctx->ipv4_addr, ctx->netmask);

    if (netmask == 0) {
        return source_ipv4_addr == ctx->ipv4_addr;
    }
    return (source_ipv4_addr & netmask) == (ctx->ipv4_addr & netmask);
}

static int TC_AUTO_IP_UNUSED iface_context_cidr(char *out, size_t out_len, const struct iface_context *ctx) {
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

static int TC_AUTO_IP_UNUSED link_context_priority_score(const struct link_context *ctx) {
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

static int TC_AUTO_IP_UNUSED link_context_compare(const struct link_context *a, const struct link_context *b) {
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

static void TC_AUTO_IP_UNUSED sort_link_contexts(struct link_context_set *set) {
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

static struct link_context TC_AUTO_IP_UNUSED *find_or_add_link_context(struct link_context_set *out,
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

static int TC_AUTO_IP_UNUSED link_context_has_ipv4(const struct link_context *ctx, uint32_t addr) {
    size_t i;

    for (i = 0; i < ctx->ipv4_count; i++) {
        if (ctx->ipv4[i].addr == addr) {
            return 1;
        }
    }
    return 0;
}

static int TC_AUTO_IP_UNUSED link_context_has_ipv6(const struct link_context *ctx, const struct in6_addr *addr) {
    size_t i;

    for (i = 0; i < ctx->ipv6_count; i++) {
        if (memcmp(&ctx->ipv6[i].addr, addr, sizeof(*addr)) == 0) {
            return 1;
        }
    }
    return 0;
}

static int TC_AUTO_IP_UNUSED append_link_ipv4(struct link_context_set *out,
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

static int TC_AUTO_IP_UNUSED append_link_ipv6_with_transport(struct link_context_set *out,
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

static int TC_AUTO_IP_UNUSED append_link_ipv6(struct link_context_set *out,
                                             const char *name,
                                             const struct in6_addr *addr,
                                             int prefix_len,
                                             unsigned int scope_id,
                                             int flags) {
    return append_link_ipv6_with_transport(out, name, addr, prefix_len, scope_id, flags, 1);
}

static int TC_AUTO_IP_UNUSED link_ipv6_addr_is_samba_bindable(const struct link_ipv6_addr *addr) {
    return addr->prefix_len >= 0 && runtime_ipv6_is_bindable(&addr->addr);
}

static int TC_AUTO_IP_UNUSED link_ipv4_addr_is_samba_bindable(const struct link_ipv4_addr *addr) {
    return runtime_ipv4_is_bindable(addr->addr) && !ipv4_is_link_local(addr->addr);
}

static int TC_AUTO_IP_UNUSED link_context_has_samba_address(const struct link_context *ctx) {
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

static int TC_AUTO_IP_UNUSED link_context_has_private_lan_samba_address(const struct link_context *ctx) {
    size_t i;

    for (i = 0; i < ctx->ipv4_count; i++) {
        if (link_ipv4_addr_is_samba_bindable(&ctx->ipv4[i]) &&
            ipv4_is_rfc1918(ctx->ipv4[i].addr)) {
            return 1;
        }
    }
    for (i = 0; i < ctx->ipv6_count; i++) {
        if (link_ipv6_addr_is_samba_bindable(&ctx->ipv6[i]) &&
            ipv6_is_ula_addr(&ctx->ipv6[i].addr)) {
            return 1;
        }
    }
    return 0;
}

static int TC_AUTO_IP_UNUSED iface_name_is_synthetic_from_address(const char *name) {
    return iface_name_has_prefix(name, "ip4-") ||
           strcmp(name, "ipv6") == 0 ||
           iface_name_has_prefix(name, "ipv6-if");
}

static int TC_AUTO_IP_UNUSED link_context_is_unnamed_private_lan_fallback(const struct link_context *ctx) {
    return iface_name_is_synthetic_from_address(ctx->name) &&
           link_context_has_private_lan_samba_address(ctx);
}

static void TC_AUTO_IP_UNUSED ifconfig_owner_map_add_ipv4(struct ifconfig_address_owner_map *map,
                                                          const char *name,
                                                          const char *addr_text) {
    struct in_addr parsed;
    size_t i;

    if (name == NULL || name[0] == '\0' || addr_text == NULL || addr_text[0] == '\0') {
        return;
    }
    if (inet_pton(AF_INET, addr_text, &parsed) != 1) {
        return;
    }
    for (i = 0; i < map->count; i++) {
        if (map->entries[i].family == AF_INET &&
            map->entries[i].ipv4_addr == parsed.s_addr) {
            return;
        }
    }
    if (map->count >= MAX_IFCONFIG_OWNER_ENTRIES) {
        return;
    }
    map->entries[map->count].family = AF_INET;
    map->entries[map->count].ipv4_addr = parsed.s_addr;
    strncpy(map->entries[map->count].name, name, sizeof(map->entries[map->count].name) - 1);
    map->count++;
}

static void TC_AUTO_IP_UNUSED ifconfig_owner_map_add_ipv6(struct ifconfig_address_owner_map *map,
                                                          const char *name,
                                                          const char *addr_text) {
    struct in6_addr parsed;
    char normalized[INET6_ADDRSTRLEN + IFNAMSIZ];
    char *scope;
    size_t i;

    if (name == NULL || name[0] == '\0' || addr_text == NULL || addr_text[0] == '\0') {
        return;
    }
    strncpy(normalized, addr_text, sizeof(normalized) - 1);
    normalized[sizeof(normalized) - 1] = '\0';
    scope = strchr(normalized, '%');
    if (scope != NULL) {
        *scope = '\0';
    }
    if (inet_pton(AF_INET6, normalized, &parsed) != 1) {
        return;
    }
    for (i = 0; i < map->count; i++) {
        if (map->entries[i].family == AF_INET6 &&
            memcmp(&map->entries[i].ipv6_addr, &parsed, sizeof(parsed)) == 0) {
            return;
        }
    }
    if (map->count >= MAX_IFCONFIG_OWNER_ENTRIES) {
        return;
    }
    map->entries[map->count].family = AF_INET6;
    map->entries[map->count].ipv6_addr = parsed;
    strncpy(map->entries[map->count].name, name, sizeof(map->entries[map->count].name) - 1);
    map->count++;
}

static int TC_AUTO_IP_UNUSED copy_first_token(char *out,
                                              size_t out_len,
                                              const char *text) {
    const char *end;
    size_t len;

    if (out_len == 0 || text == NULL) {
        return 0;
    }
    while (*text != '\0' && isspace((unsigned char)*text)) {
        text++;
    }
    end = text;
    while (*end != '\0' && !isspace((unsigned char)*end)) {
        end++;
    }
    len = (size_t)(end - text);
    if (len == 0) {
        return 0;
    }
    if (len >= out_len) {
        len = out_len - 1;
    }
    memcpy(out, text, len);
    out[len] = '\0';
    return 1;
}

static void TC_AUTO_IP_UNUSED ifconfig_owner_map_parse_line(struct ifconfig_address_owner_map *map,
                                                            char *current_name,
                                                            size_t current_name_len,
                                                            const char *line) {
    const char *cursor;
    const char *colon;
    char token[INET6_ADDRSTRLEN + IFNAMSIZ];
    size_t name_len;

    if (line == NULL || line[0] == '\0') {
        return;
    }
    if (line[0] != ' ' && line[0] != '\t') {
        colon = strchr(line, ':');
        if (colon == NULL || colon == line || current_name_len == 0) {
            return;
        }
        name_len = (size_t)(colon - line);
        if (name_len >= current_name_len) {
            name_len = current_name_len - 1;
        }
        memcpy(current_name, line, name_len);
        current_name[name_len] = '\0';
        return;
    }

    cursor = line;
    while (*cursor != '\0' && isspace((unsigned char)*cursor)) {
        cursor++;
    }
    if (strncmp(cursor, "inet ", 5) == 0) {
        if (copy_first_token(token, sizeof(token), cursor + 5)) {
            ifconfig_owner_map_add_ipv4(map, current_name, token);
        }
    } else if (strncmp(cursor, "inet6 ", 6) == 0) {
        if (copy_first_token(token, sizeof(token), cursor + 6)) {
            ifconfig_owner_map_add_ipv6(map, current_name, token);
        }
    }
}

static int TC_AUTO_IP_UNUSED collect_ifconfig_address_owners(struct ifconfig_address_owner_map *map) {
    FILE *stream;
    char line[256];
    char current_name[IFNAMSIZ];

    memset(map, 0, sizeof(*map));
    current_name[0] = '\0';

    /*
     * NetBSD 4 getifaddrs can drop owner names from address rows entirely.
     * /sbin/ifconfig still reports address ownership, so use it only as a
     * bounded fallback to relabel synthetic ip4-* / ipv6 contexts.
     */
    stream = popen("/sbin/ifconfig -a", "r");
    if (stream == NULL) {
        return -1;
    }
    while (fgets(line, sizeof(line), stream) != NULL) {
        ifconfig_owner_map_parse_line(map, current_name, sizeof(current_name), line);
    }
    if (pclose(stream) == -1) {
        return -1;
    }
    return 0;
}

static const char TC_AUTO_IP_UNUSED *ifconfig_owner_for_ipv4(const struct ifconfig_address_owner_map *map,
                                                            uint32_t ipv4_addr) {
    size_t i;

    for (i = 0; i < map->count; i++) {
        if (map->entries[i].family == AF_INET &&
            map->entries[i].ipv4_addr == ipv4_addr) {
            return map->entries[i].name;
        }
    }
    return NULL;
}

static const char TC_AUTO_IP_UNUSED *ifconfig_owner_for_ipv6(const struct ifconfig_address_owner_map *map,
                                                            const struct in6_addr *ipv6_addr) {
    size_t i;

    for (i = 0; i < map->count; i++) {
        if (map->entries[i].family == AF_INET6 &&
            memcmp(&map->entries[i].ipv6_addr, ipv6_addr, sizeof(*ipv6_addr)) == 0) {
            return map->entries[i].name;
        }
    }
    return NULL;
}

static int TC_AUTO_IP_UNUSED link_context_set_has_synthetic_names(const struct link_context_set *set) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (iface_name_is_synthetic_from_address(set->links[i].name)) {
            return 1;
        }
    }
    return 0;
}

static void TC_AUTO_IP_UNUSED relabel_synthetic_link_contexts_from_ifconfig(struct link_context_set *set) {
    struct ifconfig_address_owner_map owners;
    struct link_context_set relabeled;
    size_t i;

    if (!link_context_set_has_synthetic_names(set)) {
        return;
    }
    if (collect_ifconfig_address_owners(&owners) != 0 || owners.count == 0) {
        return;
    }

    memset(&relabeled, 0, sizeof(relabeled));
    relabeled.truncated = set->truncated;
    for (i = 0; i < set->count; i++) {
        const struct link_context *ctx = &set->links[i];
        size_t j;

        for (j = 0; j < ctx->ipv4_count; j++) {
            const char *name = ctx->name;
            const char *owner;
            if (iface_name_is_synthetic_from_address(name)) {
                owner = ifconfig_owner_for_ipv4(&owners, ctx->ipv4[j].addr);
                if (owner != NULL && owner[0] != '\0') {
                    name = owner;
                }
            }
            append_link_ipv4(&relabeled,
                             name,
                             ctx->ipv4[j].addr,
                             ctx->ipv4[j].netmask,
                             ctx->flags);
        }
        for (j = 0; j < ctx->ipv6_count; j++) {
            const char *name = ctx->name;
            const char *owner;
            if (iface_name_is_synthetic_from_address(name)) {
                owner = ifconfig_owner_for_ipv6(&owners, &ctx->ipv6[j].addr);
                if (owner != NULL && owner[0] != '\0') {
                    name = owner;
                }
            }
            append_link_ipv6_with_transport(&relabeled,
                                            name,
                                            &ctx->ipv6[j].addr,
                                            ctx->ipv6[j].prefix_len,
                                            ctx->ipv6[j].scope_id,
                                            ctx->flags,
                                            ctx->mdns_ipv6_transport);
        }
    }
    if (relabeled.count > 0) {
        *set = relabeled;
    }
}

static int TC_AUTO_IP_UNUSED link_context_has_advertisable_ipv4(const struct link_context *ctx) {
    return ctx->ipv4_count > 0;
}

static int TC_AUTO_IP_UNUSED link_context_has_advertisable_ipv6(const struct link_context *ctx) {
    size_t i;

    for (i = 0; i < ctx->ipv6_count; i++) {
        if (link_ipv6_addr_is_samba_bindable(&ctx->ipv6[i])) {
            return 1;
        }
    }
    return 0;
}

static int TC_AUTO_IP_UNUSED link_context_has_advertisable_address(const struct link_context *ctx) {
    return link_context_has_advertisable_ipv4(ctx) ||
           link_context_has_advertisable_ipv6(ctx);
}

static int TC_AUTO_IP_UNUSED link_context_has_mdns_ipv4_transport(const struct link_context *ctx) {
    return ctx->mdns_ipv4_transport && ctx->ipv4_count > 0;
}

static void TC_AUTO_IP_UNUSED disable_link_contexts_mdns_ipv4_transport(struct link_context_set *set) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        set->links[i].mdns_ipv4_transport = 0;
        set->links[i].mdns_ipv4_transport_addr = 0;
    }
}

static int TC_AUTO_IP_UNUSED link_context_has_mdns_ipv6_transport(const struct link_context *ctx) {
    return ctx->mdns_ipv6_transport && ctx->ifindex != 0 && ctx->ipv6_count > 0;
}

static void TC_AUTO_IP_UNUSED disable_link_contexts_mdns_ipv6_transport(struct link_context_set *set) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        set->links[i].mdns_ipv6_transport = 0;
    }
}

static int TC_AUTO_IP_UNUSED link_context_is_advertise_eligible(const struct link_context *ctx) {
    return link_context_has_advertisable_address(ctx);
}

static void TC_AUTO_IP_UNUSED filter_advertise_link_contexts(struct link_context_set *out,
                                                             const struct link_context_set *in) {
    size_t i;

    memset(out, 0, sizeof(*out));
    for (i = 0; i < in->count; i++) {
        if (!link_context_is_advertise_eligible(&in->links[i])) {
            continue;
        }
        if (out->count >= MAX_IFACE_CONTEXTS) {
            out->truncated = 1;
            break;
        }
        out->links[out->count++] = in->links[i];
    }
    sort_link_contexts(out);
}

static int TC_AUTO_IP_UNUSED link_context_identity_equal(const struct link_context *a,
                                                        const struct link_context *b) {
    size_t i;

    if (strcmp(a->name, b->name) != 0 ||
        a->flags != b->flags ||
        a->ifindex != b->ifindex ||
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

static int TC_AUTO_IP_UNUSED link_context_set_contains(const struct link_context_set *set,
                                                      const struct link_context *ctx) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (link_context_identity_equal(&set->links[i], ctx)) {
            return 1;
        }
    }
    return 0;
}

static int TC_AUTO_IP_UNUSED link_context_sets_equal(const struct link_context_set *a,
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

static int TC_AUTO_IP_UNUSED link_context_ipv4_cidr(char *out,
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

static int TC_AUTO_IP_UNUSED link_context_ipv6_cidr(char *out,
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

static int TC_AUTO_IP_UNUSED print_smb_link_bind_tokens(FILE *stream, const struct link_context_set *set) {
    int wrote = 0;
    size_t i;

    for (i = 0; i < set->count; i++) {
        size_t j;
        const struct link_context *ctx = &set->links[i];
        for (j = 0; j < ctx->ipv4_count; j++) {
            char cidr[INET_ADDRSTRLEN + 4];
            if (!link_ipv4_addr_is_samba_bindable(&ctx->ipv4[j])) {
                continue;
            }
            if (link_context_ipv4_cidr(cidr, sizeof(cidr), &ctx->ipv4[j]) != 0) {
                return -1;
            }
            if (wrote && fputc(' ', stream) == EOF) {
                return -1;
            }
            if (fputs(cidr, stream) == EOF) {
                return -1;
            }
            wrote = 1;
        }
        for (j = 0; j < ctx->ipv6_count; j++) {
            char cidr[INET6_ADDRSTRLEN + 5];
            if (!link_ipv6_addr_is_samba_bindable(&ctx->ipv6[j])) {
                continue;
            }
            if (link_context_ipv6_cidr(cidr, sizeof(cidr), &ctx->ipv6[j]) != 0) {
                return -1;
            }
            if (wrote && fputc(' ', stream) == EOF) {
                return -1;
            }
            if (fputs(cidr, stream) == EOF) {
                return -1;
            }
            wrote = 1;
        }
    }
    if (!wrote) {
        return -1;
    }
    if (fputc('\n', stream) == EOF) {
        return -1;
    }
    return 0;
}

static int TC_AUTO_IP_UNUSED print_iface_context_cidrs(FILE *stream, const struct iface_context_set *set) {
    size_t i;

    if (set->count == 0) {
        return -1;
    }
    for (i = 0; i < set->count; i++) {
        char cidr[INET_ADDRSTRLEN + 4];
        if (iface_context_cidr(cidr, sizeof(cidr), &set->contexts[i]) != 0) {
            return -1;
        }
        if (i > 0 && fputc(' ', stream) == EOF) {
            return -1;
        }
        if (fputs(cidr, stream) == EOF) {
            return -1;
        }
    }
    if (fputc('\n', stream) == EOF) {
        return -1;
    }
    return 0;
}

static int TC_AUTO_IP_UNUSED collect_link_contexts_with_policy(struct link_context_set *out, int require_running) {
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
        int flags;
        char name_buf[IFNAMSIZ];
        const char *name;

        if (ifa->ifa_addr == NULL) {
            continue;
        }
        flags = (int)ifa->ifa_flags;
        /*
         * NetBSD 4 can report AF_LINK owner rows separately from unnamed
         * address rows. Preserve the owner name before filtering by flags so
         * bridge0/mgi1 ownership is not replaced with synthetic ip4-* names.
         */
        name = usable_ifaddrs_name(ifa, name_buf, sizeof(name_buf), current_name);
        if (name[0] != '\0' && name != current_name) {
            strncpy(current_name, name, sizeof(current_name) - 1);
            current_name[sizeof(current_name) - 1] = '\0';
        }
        if (!iface_flags_are_usable(flags, require_running)) {
            continue;
        }

        if (ifa->ifa_addr->sa_family == AF_INET) {
            struct sockaddr_in sin;
            memset(&sin, 0, sizeof(sin));
            memcpy(&sin, ifa->ifa_addr, sizeof(sin));
            if (name[0] == '\0') {
                synthetic_ipv4_ifaddrs_name(name_buf, sizeof(name_buf), sin.sin_addr.s_addr);
                name = name_buf;
            }
            append_link_ipv4(out,
                             name,
                             sin.sin_addr.s_addr,
                             getifaddrs_ipv4_netmask(ifa),
                             flags);
        } else if (ifa->ifa_addr->sa_family == AF_INET6) {
            struct sockaddr_in6 sin6;
            int prefix_len;

            memset(&sin6, 0, sizeof(sin6));
            memcpy(&sin6, ifa->ifa_addr, sizeof(sin6));
            prefix_len = getifaddrs_ipv6_prefix_len(ifa);
            if (name[0] == '\0') {
                synthetic_ipv6_ifaddrs_name(name_buf, sizeof(name_buf), &sin6);
                name = name_buf;
            }
            append_link_ipv6_with_transport(out,
                                            name,
                                            &sin6.sin6_addr,
                                            prefix_len,
                                            (unsigned int)sin6.sin6_scope_id,
                                            flags,
                                            1);
#if defined(AF_LINK) && (defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__DragonFly__))
        } else if (ifa->ifa_addr->sa_family == AF_LINK) {
            const struct sockaddr_dl *sdl = (const struct sockaddr_dl *)(const void *)ifa->ifa_addr;
            struct link_context *ctx = NULL;
            if (name[0] != '\0') {
                ctx = find_or_add_link_context(out, name, flags);
            }
            if (ctx != NULL && ctx->ifindex == 0) {
                ctx->ifindex = (unsigned int)sdl->sdl_index;
            }
#endif
        }
    }

    freeifaddrs(ifaddr_list);
    relabel_synthetic_link_contexts_from_ifconfig(out);
    {
        size_t i;
        size_t write_i = 0;
        for (i = 0; i < out->count; i++) {
            if (out->links[i].ipv4_count == 0 && out->links[i].ipv6_count == 0) {
                continue;
            }
            if (out->links[i].ifindex == 0) {
                out->links[i].ifindex = if_nametoindex(out->links[i].name);
            }
            if (write_i != i) {
                out->links[write_i] = out->links[i];
            }
            write_i++;
        }
        out->count = write_i;
    }
    sort_link_contexts(out);
    return 0;
}

static int TC_AUTO_IP_UNUSED collect_usable_link_contexts(struct link_context_set *out) {
    if (collect_link_contexts_with_policy(out, 1) != 0) {
        return -1;
    }
    if (out->count > 0) {
        return 0;
    }
    if (collect_link_contexts_with_policy(out, 0) != 0) {
        return -1;
    }
    if (out->count > 0) {
        fprintf(stderr, "auto-ip: no IFF_RUNNING usable address links found; using IFF_UP fallback links\n");
    }
    return 0;
}

static void TC_AUTO_IP_UNUSED log_link_contexts(const char *prefix, const struct link_context_set *set) {
    size_t i;

    fprintf(stderr, "%s: links=%lu\n", prefix, (unsigned long)set->count);
    for (i = 0; i < set->count; i++) {
        size_t j;
        char transport_ip_buf[INET_ADDRSTRLEN];
        const struct link_context *ctx = &set->links[i];
        fprintf(stderr, "%s: link[%lu] iface=%s flags=0x%x ifindex=%u mdns_ipv4=%d mdns_ipv4_addr=%s mdns_ipv6=%d score=%d ipv4=%lu ipv6=%lu\n",
                prefix,
                (unsigned long)i,
                ctx->name,
                (unsigned int)ctx->flags,
                ctx->ifindex,
                ctx->mdns_ipv4_transport,
                ctx->mdns_ipv4_transport_addr != 0 ? ipv4_to_string(ctx->mdns_ipv4_transport_addr, transport_ip_buf, sizeof(transport_ip_buf)) : "(auto)",
                ctx->mdns_ipv6_transport,
                link_context_priority_score(ctx),
                (unsigned long)ctx->ipv4_count,
                (unsigned long)ctx->ipv6_count);
        for (j = 0; j < ctx->ipv4_count; j++) {
            char ip_buf[INET_ADDRSTRLEN];
            char mask_buf[INET_ADDRSTRLEN];
            fprintf(stderr, "%s: link[%lu].ipv4[%lu]=%s netmask=%s\n",
                    prefix,
                    (unsigned long)i,
                    (unsigned long)j,
                    ipv4_to_string(ctx->ipv4[j].addr, ip_buf, sizeof(ip_buf)),
                    ipv4_to_string(ctx->ipv4[j].netmask, mask_buf, sizeof(mask_buf)));
        }
        for (j = 0; j < ctx->ipv6_count; j++) {
            char ip_buf[INET6_ADDRSTRLEN];
            const char *printed = inet_ntop(AF_INET6, &ctx->ipv6[j].addr, ip_buf, sizeof(ip_buf));
            fprintf(stderr, "%s: link[%lu].ipv6[%lu]=%s/%d scope=%u link_local=%d\n",
                    prefix,
                    (unsigned long)i,
                    (unsigned long)j,
                    printed != NULL ? printed : "invalid",
                    ctx->ipv6[j].prefix_len,
                    ctx->ipv6[j].scope_id,
                    ctx->ipv6[j].link_local);
        }
    }
}

static void TC_AUTO_IP_UNUSED log_iface_contexts(const char *prefix, const struct iface_context_set *set) {
    size_t i;

    fprintf(stderr, "%s: contexts=%lu\n", prefix, (unsigned long)set->count);
    for (i = 0; i < set->count; i++) {
        char ip_buf[INET_ADDRSTRLEN];
        char mask_buf[INET_ADDRSTRLEN];
        fprintf(stderr, "%s: context[%lu] iface=%s ip=%s netmask=%s flags=0x%x score=%d\n",
                prefix,
                (unsigned long)i,
                set->contexts[i].name,
                ipv4_to_string(set->contexts[i].ipv4_addr, ip_buf, sizeof(ip_buf)),
                ipv4_to_string(set->contexts[i].netmask, mask_buf, sizeof(mask_buf)),
                (unsigned int)set->contexts[i].flags,
                iface_context_priority_score(&set->contexts[i]));
    }
}

#undef TC_AUTO_IP_UNUSED

#endif
