#include "network.h"
TC_LOCAL void relabel_synthetic_link_contexts_from_ifconfig(struct link_context_set *set);
TC_LOCAL int collect_link_contexts_with_policy(struct link_context_set *out, int require_running);
#include "log.h"
#define fprintf timestamped_fprintf
TC_LOCAL void relabel_synthetic_link_contexts_from_ifconfig(struct link_context_set *set) {
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

TC_LOCAL int collect_link_contexts_with_policy(struct link_context_set *out, int require_running) {
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
    mark_wan_link_contexts(out);
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

int collect_usable_link_contexts(struct link_context_set *out) {
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
