#include "network.h"
TC_LOCAL void log_iface_contexts(const char *prefix, const struct iface_context_set *set);
#include "log.h"
#define fprintf timestamped_fprintf
void log_link_contexts(const char *prefix, const struct link_context_set *set) {
    size_t i;

    fprintf(stderr, "%s: links=%lu\n", prefix, (unsigned long)set->count);
    for (i = 0; i < set->count; i++) {
        size_t j;
        char transport_ip_buf[INET_ADDRSTRLEN];
        const struct link_context *ctx = &set->links[i];
        fprintf(stderr, "%s: link[%lu] iface=%s role=%s flags=0x%x ifindex=%u mdns_ipv4=%d mdns_ipv4_addr=%s mdns_ipv6=%d score=%d ipv4=%lu ipv6=%lu\n",
                prefix,
                (unsigned long)i,
                ctx->name,
                ctx->is_wan ? "wan" : "lan-or-unknown",
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

TC_LOCAL void log_iface_contexts(const char *prefix, const struct iface_context_set *set) {
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
