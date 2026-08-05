#include "service.h"
int print_link_ipv4_cidrs(FILE *stream, const struct link_context_set *set) {
    int wrote = 0;
    size_t i;

    for (i = 0; i < set->count; i++) {
        size_t j;
        const struct link_context *link = &set->links[i];

        for (j = 0; j < link->ipv4_count; j++) {
            char cidr[INET_ADDRSTRLEN + 4];

            if (link_context_ipv4_cidr(cidr, sizeof(cidr), &link->ipv4[j]) != 0) {
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

int link_contexts_have_ipv4_addr(const struct link_context_set *set) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (set->links[i].ipv4_count > 0) {
            return 1;
        }
    }
    return 0;
}

int print_auto_ip_cidrs_with_provider(FILE *stream,
                                             collect_link_contexts_fn collect_contexts,
                                             void *userdata) {
    struct link_context_set links;

    if (collect_contexts == NULL) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }

    memset(&links, 0, sizeof(links));
    if (collect_contexts(&links, userdata) != 0) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    if (links.count == 0 || !link_contexts_have_ipv4_addr(&links)) {
        return EXIT_AUTO_IP_UNAVAILABLE;
    }
    if (links.truncated) {
        fprintf(stderr, "auto-ip: usable address link list exceeded static capacity\n");
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    sort_link_contexts(&links);
    if (print_link_ipv4_cidrs(stream, &links) != 0) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    return EXIT_OK;
}

int print_smb_bind_interfaces_with_policy(FILE *stream,
                                                 collect_link_contexts_fn collect_contexts,
                                                 void *userdata,
                                                 int lan_only) {
    struct link_context_set all_links;
    struct link_context_set bind_links;

    if (collect_contexts == NULL) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }

    memset(&all_links, 0, sizeof(all_links));
    memset(&bind_links, 0, sizeof(bind_links));
    if (collect_contexts(&all_links, userdata) != 0) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    if (all_links.count == 0) {
        return EXIT_AUTO_IP_UNAVAILABLE;
    }
    if (all_links.truncated) {
        fprintf(stderr, "auto-ip: Samba bind interface list exceeded static capacity\n");
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    filter_smb_bind_link_contexts(&bind_links, &all_links, lan_only, 0);
    if (lan_only && bind_links.count == 0) {
        /*
         * Some NetBSD 4 Time Capsules expose getifaddrs address rows without
         * interface names, so bridge0 appears as synthetic ip4-* links. In
         * that case keep LAN-only mode working by falling back to private LAN
         * Samba bind candidates instead of deferring startup forever.
         */
        filter_smb_bind_link_contexts(&bind_links, &all_links, lan_only, 1);
    }
    if (bind_links.count == 0) {
        return EXIT_AUTO_IP_UNAVAILABLE;
    }
    if (bind_links.truncated) {
        fprintf(stderr, "auto-ip: Samba bind interface list exceeded static capacity\n");
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    if (print_smb_link_bind_tokens(stream, &bind_links) != 0) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    return EXIT_OK;
}

int print_smb_bind_interfaces_with_provider(FILE *stream,
                                                   collect_link_contexts_fn collect_contexts,
                                                   void *userdata) {
    return print_smb_bind_interfaces_with_policy(stream, collect_contexts, userdata, 0);
}

int print_smb_bind_interfaces_lan_with_provider(FILE *stream,
                                                       collect_link_contexts_fn collect_contexts,
                                                       void *userdata) {
    return print_smb_bind_interfaces_with_policy(stream, collect_contexts, userdata, 1);
}
