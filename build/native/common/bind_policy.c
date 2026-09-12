#include "network.h"
TC_LOCAL int print_iface_context_cidrs(FILE *stream, const struct iface_context_set *set);
#include "log.h"
#define fprintf timestamped_fprintf
void filter_smb_bind_link_contexts(struct link_context_set *out,
                                                            const struct link_context_set *in,
                                                            int lan_only,
                                                            int unnamed_lan_fallback) {
    size_t i;

    memset(out, 0, sizeof(*out));
    for (i = 0; i < in->count; i++) {
        if (lan_only && in->links[i].is_wan) {
            continue;
        }
        if (lan_only) {
            if (unnamed_lan_fallback) {
                if (!link_context_is_unnamed_private_lan_fallback(&in->links[i])) {
                    continue;
                }
            } else if (!iface_name_is_strong_lan(in->links[i].name)) {
                continue;
            }
        }
        if (!link_context_has_samba_address(&in->links[i])) {
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

int print_smb_link_bind_tokens(FILE *stream, const struct link_context_set *set) {
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

TC_LOCAL int print_iface_context_cidrs(FILE *stream, const struct iface_context_set *set) {
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
