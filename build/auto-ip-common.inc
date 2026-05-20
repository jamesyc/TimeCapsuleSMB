#ifndef TC_AUTO_IP_COMMON_INC
#define TC_AUTO_IP_COMMON_INC

#if defined(__GNUC__)
#define TC_AUTO_IP_UNUSED __attribute__((unused))
#else
#define TC_AUTO_IP_UNUSED
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

static size_t TC_AUTO_IP_UNUSED ifreq_entry_size(const struct ifreq *ifr, size_t remaining, int fixed_entries) {
    size_t sockaddr_len = sizeof(struct sockaddr);
    size_t step;
    size_t align;

    if (fixed_entries) {
        if (remaining < sizeof(*ifr)) {
            return 0;
        }
        return sizeof(*ifr);
    }

    if (remaining < IFNAMSIZ + sizeof(struct sockaddr)) {
        return 0;
    }
#if defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__DragonFly__)
    if (ifr->ifr_addr.sa_len > sockaddr_len) {
        sockaddr_len = ifr->ifr_addr.sa_len;
    }
#endif

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
        struct ifreq ifr_entry;
        struct ifreq flags_req;
        struct ifreq mask_req;
        struct sockaddr_in sin;
        int flags;
        uint32_t netmask = 0;
        size_t remaining = (size_t)(end - cursor);
        size_t step;

        if (copy_ifreq_entry(&ifr_entry, cursor, remaining) != 0) {
            break;
        }
        step = ifreq_entry_size(&ifr_entry, remaining, fixed_entries);
        if (step == 0) {
            break;
        }

        if (ifr_entry.ifr_addr.sa_family == AF_INET) {
            memset(&flags_req, 0, sizeof(flags_req));
            strncpy(flags_req.ifr_name, ifr_entry.ifr_name, sizeof(flags_req.ifr_name) - 1);
            if (ioctl(sockfd, SIOCGIFFLAGS, &flags_req) == 0) {
                flags = flags_req.ifr_flags;
                if (iface_flags_are_usable(flags, require_running)) {
                    memset(&mask_req, 0, sizeof(mask_req));
                    strncpy(mask_req.ifr_name, ifr_entry.ifr_name, sizeof(mask_req.ifr_name) - 1);
                    if (ioctl(sockfd, SIOCGIFNETMASK, &mask_req) == 0) {
                        struct sockaddr_in netmask_addr;
                        memset(&netmask_addr, 0, sizeof(netmask_addr));
                        memcpy(&netmask_addr, &mask_req.ifr_addr, sizeof(netmask_addr));
                        netmask = netmask_addr.sin_addr.s_addr;
                    }
                    memset(&sin, 0, sizeof(sin));
                    memcpy(&sin, &ifr_entry.ifr_addr, sizeof(sin));
                    append_iface_context(out, ifr_entry.ifr_name, sin.sin_addr.s_addr, netmask, flags);
                }
            }
        }
        cursor += step;
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
