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

#ifndef MAX_LINK_IPV4_ADDRS
#define MAX_LINK_IPV4_ADDRS 8
#endif
#ifndef MAX_LINK_IPV6_ADDRS
#define MAX_LINK_IPV6_ADDRS 8
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

struct ifconf_entry_view {
    char name[IFNAMSIZ];
    const struct sockaddr *addr;
    size_t addr_len;
    size_t step;
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

static int TC_AUTO_IP_UNUSED ifreq_table_uses_fixed_entries(size_t ifc_len) {
    /*
     * NetBSD 6/7 uses fixed-size struct ifreq records here, while NetBSD 4
     * returns the older variable-length layout with sa_len-sized sockaddr
     * payloads. The fixed layout has the large ifreq union; the old layout
     * stays close to IFNAMSIZ + struct sockaddr.
     */
    return sizeof(struct ifreq) > 64 && ifc_len > 0 && (ifc_len % sizeof(struct ifreq)) == 0;
}

static size_t TC_AUTO_IP_UNUSED ifreq_sockaddr_len(const struct ifreq *ifr, size_t remaining) {
    size_t sockaddr_len = sizeof(struct sockaddr);

    (void)ifr;
    if (remaining < IFNAMSIZ + sizeof(struct sockaddr)) {
        return 0;
    }
#if defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__DragonFly__)
    if (ifr->ifr_addr.sa_len > sockaddr_len) {
        sockaddr_len = ifr->ifr_addr.sa_len;
    }
#endif
    if (sockaddr_len > remaining - IFNAMSIZ) {
        return 0;
    }
    return sockaddr_len;
}

static size_t TC_AUTO_IP_UNUSED ifreq_entry_size(const struct ifreq *ifr, size_t remaining, int fixed_entries) {
    size_t sockaddr_len;
    size_t step;
    size_t align;

    if (fixed_entries) {
        if (remaining < sizeof(*ifr)) {
            return 0;
        }
        return sizeof(*ifr);
    }

    sockaddr_len = ifreq_sockaddr_len(ifr, remaining);
    if (sockaddr_len == 0) {
        return 0;
    }

    step = IFNAMSIZ + sockaddr_len;
    align = sizeof(long);
    if (align > 0 && (step % align) != 0) {
        step += align - (step % align);
    }
    if (step < sizeof(*ifr)) {
        step = sizeof(*ifr);
    }
    if (step > remaining) {
        return 0;
    }
    return step;
}

static int TC_AUTO_IP_UNUSED copy_ifreq_entry(struct ifreq *out, const char *cursor, size_t remaining) {
    size_t copy_len;

    if (remaining < IFNAMSIZ + sizeof(struct sockaddr)) {
        return -1;
    }

    memset(out, 0, sizeof(*out));
    copy_len = remaining < sizeof(*out) ? remaining : sizeof(*out);
    memcpy(out, cursor, copy_len);
    return 0;
}

static int TC_AUTO_IP_UNUSED ifconf_entry_view_from_cursor(struct ifconf_entry_view *out,
                                                           const char *cursor,
                                                           size_t remaining,
                                                           int fixed_entries) {
    struct ifreq ifr;
    size_t addr_len;
    size_t step;

    if (copy_ifreq_entry(&ifr, cursor, remaining) != 0) {
        return -1;
    }
    step = ifreq_entry_size(&ifr, remaining, fixed_entries);
    if (step == 0) {
        return -1;
    }
    addr_len = ifreq_sockaddr_len(&ifr, remaining);
    if (addr_len == 0) {
        return -1;
    }

    memset(out, 0, sizeof(*out));
    memcpy(out->name, ifr.ifr_name, IFNAMSIZ);
    out->name[IFNAMSIZ - 1] = '\0';
    out->addr = (const struct sockaddr *)(const void *)(cursor + IFNAMSIZ);
    out->addr_len = addr_len;
    out->step = step;
    return 0;
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

static int TC_AUTO_IP_UNUSED get_iface_flags(int sockfd, const char *name, int *flags_out) {
    struct ifreq flags_req;

    memset(&flags_req, 0, sizeof(flags_req));
    strncpy(flags_req.ifr_name, name, sizeof(flags_req.ifr_name) - 1);
    if (ioctl(sockfd, SIOCGIFFLAGS, &flags_req) != 0) {
        return -1;
    }
    *flags_out = flags_req.ifr_flags;
    return 0;
}

static uint32_t TC_AUTO_IP_UNUSED get_ipv4_netmask(int sockfd, const char *name, uint32_t ipv4_addr) {
    struct ifreq mask_req;

    if (ipv4_is_link_local(ipv4_addr)) {
        return ipv4_link_local_netmask();
    }

    memset(&mask_req, 0, sizeof(mask_req));
    strncpy(mask_req.ifr_name, name, sizeof(mask_req.ifr_name) - 1);
    if (ioctl(sockfd, SIOCGIFNETMASK, &mask_req) == 0) {
        struct sockaddr_in netmask_addr;
        memset(&netmask_addr, 0, sizeof(netmask_addr));
        memcpy(&netmask_addr, &mask_req.ifr_addr, sizeof(netmask_addr));
        if (netmask_addr.sin_family == AF_INET) {
            return netmask_addr.sin_addr.s_addr;
        }
    }
    return 0;
}

static int TC_AUTO_IP_UNUSED get_ipv6_prefix_len(int sockfd6, const char *name, const struct sockaddr_in6 *sin6) {
#if defined(SIOCGIFNETMASK_IN6)
    struct in6_ifreq mask_req;

    if (sockfd6 < 0) {
        return -1;
    }

    memset(&mask_req, 0, sizeof(mask_req));
    strncpy(mask_req.ifr_name, name, sizeof(mask_req.ifr_name) - 1);
    mask_req.ifr_ifru.ifru_addr = *sin6;
    if (ioctl(sockfd6, SIOCGIFNETMASK_IN6, &mask_req) == 0 &&
        mask_req.ifr_ifru.ifru_addr.sin6_family == AF_INET6) {
        return ipv6_prefix_length_from_mask(&mask_req.ifr_ifru.ifru_addr.sin6_addr);
    }
#else
    (void)sockfd6;
    (void)name;
    (void)sin6;
#endif
    return -1;
}

static int TC_AUTO_IP_UNUSED iface_context_priority_score(const struct iface_context *ctx) {
    int score = 0;
    int prefix = netmask_prefix_length(ctx->netmask);

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

static int TC_AUTO_IP_UNUSED collect_iface_contexts_with_policy(struct iface_context_set *out, int require_running) {
    int sockfd = -1;
    char buffer[8192];
    struct ifconf ifc;
    char *cursor;
    char *end;
    int fixed_entries;

    memset(out, 0, sizeof(*out));

    sockfd = socket(AF_INET, SOCK_DGRAM, 0);
    if (sockfd < 0) {
        perror("socket interface enumeration");
        return -1;
    }

    memset(&ifc, 0, sizeof(ifc));
    ifc.ifc_len = sizeof(buffer);
    ifc.ifc_buf = buffer;
    if (ioctl(sockfd, SIOCGIFCONF, &ifc) < 0) {
        perror("ioctl(SIOCGIFCONF)");
        close(sockfd);
        return -1;
    }

    cursor = ifc.ifc_buf;
    end = cursor + ifc.ifc_len;
    fixed_entries = ifreq_table_uses_fixed_entries((size_t)ifc.ifc_len);
    while (cursor < end) {
        struct ifconf_entry_view entry;
        struct sockaddr_in sin;
        int flags;
        size_t remaining = (size_t)(end - cursor);

        if (ifconf_entry_view_from_cursor(&entry, cursor, remaining, fixed_entries) != 0) {
            break;
        }

        if (entry.addr->sa_family == AF_INET && entry.addr_len >= sizeof(sin) &&
            get_iface_flags(sockfd, entry.name, &flags) == 0 &&
            iface_flags_are_usable(flags, require_running)) {
            memset(&sin, 0, sizeof(sin));
            memcpy(&sin, entry.addr, sizeof(sin));
            append_iface_context(out,
                                 entry.name,
                                 sin.sin_addr.s_addr,
                                 get_ipv4_netmask(sockfd, entry.name, sin.sin_addr.s_addr),
                                 flags);
        }
        cursor += entry.step;
    }

    close(sockfd);
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
    if (ctx->netmask == 0) {
        return source_ipv4_addr == ctx->ipv4_addr;
    }
    return (source_ipv4_addr & ctx->netmask) == (ctx->ipv4_addr & ctx->netmask);
}

static int TC_AUTO_IP_UNUSED iface_context_cidr(char *out, size_t out_len, const struct iface_context *ctx) {
    char ip_buf[INET_ADDRSTRLEN];
    int written;

    written = snprintf(out,
                       out_len,
                       "%s/%d",
                       ipv4_to_string(ctx->ipv4_addr, ip_buf, sizeof(ip_buf)),
                       netmask_prefix_length(ctx->netmask));
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

static int TC_AUTO_IP_UNUSED link_context_has_samba_address(const struct link_context *ctx) {
    size_t i;

    if (ctx->ipv4_count > 0) {
        return 1;
    }
    for (i = 0; i < ctx->ipv6_count; i++) {
        if (link_ipv6_addr_is_samba_bindable(&ctx->ipv6[i])) {
            return 1;
        }
    }
    return 0;
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
                       netmask_prefix_length(addr->netmask));
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
    int sockfd = -1;
    int sockfd6 = -1;
    char buffer[8192];
    struct ifconf ifc;
    char *cursor;
    char *end;
    int fixed_entries;

    memset(out, 0, sizeof(*out));

    sockfd = socket(AF_INET, SOCK_DGRAM, 0);
    if (sockfd < 0) {
        perror("socket interface enumeration");
        return -1;
    }
    sockfd6 = socket(AF_INET6, SOCK_DGRAM, 0);

    memset(&ifc, 0, sizeof(ifc));
    ifc.ifc_len = sizeof(buffer);
    ifc.ifc_buf = buffer;
    if (ioctl(sockfd, SIOCGIFCONF, &ifc) < 0) {
        perror("ioctl(SIOCGIFCONF)");
        if (sockfd6 >= 0) {
            close(sockfd6);
        }
        close(sockfd);
        return -1;
    }

    cursor = ifc.ifc_buf;
    end = cursor + ifc.ifc_len;
    fixed_entries = ifreq_table_uses_fixed_entries((size_t)ifc.ifc_len);
    while (cursor < end) {
        struct ifconf_entry_view entry;
        int flags = 0;
        size_t remaining = (size_t)(end - cursor);

        if (ifconf_entry_view_from_cursor(&entry, cursor, remaining, fixed_entries) != 0) {
            break;
        }

        if (get_iface_flags(sockfd, entry.name, &flags) != 0) {
            cursor += entry.step;
            continue;
        }
        if (!iface_flags_are_usable(flags, require_running)) {
            cursor += entry.step;
            continue;
        }

        if (entry.addr->sa_family == AF_INET && entry.addr_len >= sizeof(struct sockaddr_in)) {
            struct sockaddr_in sin;
            memset(&sin, 0, sizeof(sin));
            memcpy(&sin, entry.addr, sizeof(sin));
            append_link_ipv4(out,
                             entry.name,
                             sin.sin_addr.s_addr,
                             get_ipv4_netmask(sockfd, entry.name, sin.sin_addr.s_addr),
                             flags);
        } else if (entry.addr->sa_family == AF_INET6 && entry.addr_len >= sizeof(struct sockaddr_in6)) {
            struct sockaddr_in6 sin6;
            int prefix_len;

            memset(&sin6, 0, sizeof(sin6));
            memcpy(&sin6, entry.addr, sizeof(sin6));
            prefix_len = get_ipv6_prefix_len(sockfd6, entry.name, &sin6);
            append_link_ipv6_with_transport(out,
                                            entry.name,
                                            &sin6.sin6_addr,
                                            prefix_len,
                                            (unsigned int)sin6.sin6_scope_id,
                                            flags,
                                            sockfd6 >= 0);
#if defined(AF_LINK) && (defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__DragonFly__))
        } else if (entry.addr->sa_family == AF_LINK && entry.addr_len >= sizeof(struct sockaddr_dl)) {
            const struct sockaddr_dl *sdl = (const struct sockaddr_dl *)entry.addr;
            struct link_context *ctx = find_or_add_link_context(out, entry.name, flags);
            if (ctx != NULL && ctx->ifindex == 0) {
                ctx->ifindex = (unsigned int)sdl->sdl_index;
            }
#endif
        }
        cursor += entry.step;
    }

    if (sockfd6 >= 0) {
        close(sockfd6);
    }
    close(sockfd);
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
        const struct link_context *ctx = &set->links[i];
        fprintf(stderr, "%s: link[%lu] iface=%s flags=0x%x ifindex=%u mdns_ipv6=%d score=%d ipv4=%lu ipv6=%lu\n",
                prefix,
                (unsigned long)i,
                ctx->name,
                (unsigned int)ctx->flags,
                ctx->ifindex,
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
