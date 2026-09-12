#include "network.h"
TC_LOCAL int link_context_has_advertisable_ipv6(const struct link_context *ctx);
TC_LOCAL int link_context_has_advertisable_address(const struct link_context *ctx);
TC_LOCAL int link_context_is_advertise_eligible(const struct link_context *ctx);
#include "log.h"
#define fprintf timestamped_fprintf
int link_context_has_advertisable_ipv4(const struct link_context *ctx) {
    return ctx->ipv4_count > 0;
}

TC_LOCAL int link_context_has_advertisable_ipv6(const struct link_context *ctx) {
    size_t i;

    if (!ctx->mdns_ipv6_transport || ctx->ifindex == 0) {
        return 0;
    }
    for (i = 0; i < ctx->ipv6_count; i++) {
        if (link_ipv6_addr_is_samba_bindable(&ctx->ipv6[i])) {
            return 1;
        }
    }
    return 0;
}

TC_LOCAL int link_context_has_advertisable_address(const struct link_context *ctx) {
    return link_context_has_advertisable_ipv4(ctx) ||
           link_context_has_advertisable_ipv6(ctx);
}

int link_context_has_mdns_ipv4_transport(const struct link_context *ctx) {
    return ctx->mdns_ipv4_transport && ctx->ipv4_count > 0;
}

void disable_link_contexts_mdns_ipv4_transport(struct link_context_set *set) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        set->links[i].mdns_ipv4_transport = 0;
        set->links[i].mdns_ipv4_transport_addr = 0;
    }
}

int link_context_has_mdns_ipv6_transport(const struct link_context *ctx) {
    return ctx->mdns_ipv6_transport && ctx->ifindex != 0 && ctx->ipv6_count > 0;
}

void disable_link_contexts_mdns_ipv6_transport(struct link_context_set *set) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        set->links[i].mdns_ipv6_transport = 0;
    }
}

TC_LOCAL int link_context_is_advertise_eligible(const struct link_context *ctx) {
    return link_context_has_advertisable_address(ctx);
}

void filter_advertise_link_contexts(struct link_context_set *out,
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
