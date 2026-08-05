#include "network.h"
TC_LOCAL void ifconfig_owner_map_add_ipv4(struct ifconfig_address_owner_map *map,
                                                          const char *name,
                                                          const char *addr_text);
TC_LOCAL void ifconfig_owner_map_add_ipv6(struct ifconfig_address_owner_map *map,
                                                          const char *name,
                                                          const char *addr_text);
TC_LOCAL int copy_first_token(char *out,
                                              size_t out_len,
                                              const char *text);
TC_LOCAL int copy_last_token(char *out,
                                             size_t out_len,
                                             const char *text);
TC_LOCAL void network_role_evidence_parse_route_line(struct network_role_evidence *evidence,
                                                                      const char *line);
TC_LOCAL void network_role_evidence_parse_ipv6_route_line(struct network_role_evidence *evidence,
                                                                           const char *line);
TC_LOCAL void network_role_evidence_parse_pf_line(struct network_role_evidence *evidence,
                                                                   const char *line);
TC_LOCAL void parse_network_role_stream(FILE *stream,
                                                        struct network_role_evidence *evidence,
                                                        int mode);
TC_LOCAL void collect_network_role_evidence(struct network_role_evidence *evidence);
TC_LOCAL void ifconfig_owner_map_parse_line(struct ifconfig_address_owner_map *map,
                                                            char *current_name,
                                                            size_t current_name_len,
                                                            const char *line);
TC_LOCAL int owner_map_has_addressed_strong_lan(const struct ifconfig_address_owner_map *owners);
TC_LOCAL int link_set_has_addressed_strong_lan(const struct link_context_set *links);
TC_LOCAL int link_context_owned_by_interface(const struct link_context *ctx,
                                                             const char *iface,
                                                             const struct ifconfig_address_owner_map *owners);
TC_LOCAL void mark_wan_interface(struct link_context_set *links,
                                                const char *wan_iface,
                                                const struct ifconfig_address_owner_map *owners);
TC_LOCAL void mark_wan_link_contexts_from_evidence(struct link_context_set *links,
                                                                  const struct network_role_evidence *evidence,
                                                                  const struct ifconfig_address_owner_map *owners);
#include "log.h"
#define fprintf timestamped_fprintf
TC_LOCAL void ifconfig_owner_map_add_ipv4(struct ifconfig_address_owner_map *map,
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

TC_LOCAL void ifconfig_owner_map_add_ipv6(struct ifconfig_address_owner_map *map,
                                                          const char *name,
                                                          const char *addr_text) {
    struct in6_addr parsed;
    struct in6_addr canonical;
    unsigned int ifindex = 0;
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
        ifindex = if_nametoindex(scope + 1);
    }
    if (inet_pton(AF_INET6, normalized, &parsed) != 1) {
        return;
    }
    if (ifindex == 0) {
        ifindex = ipv6_embedded_scope_id(&parsed);
    }
    if (ifindex == 0) {
        ifindex = if_nametoindex(name);
    }
    ipv6_canonicalize_scoped_address(&canonical, &parsed);
    for (i = 0; i < map->count; i++) {
        if (map->entries[i].family == AF_INET6 &&
            memcmp(&map->entries[i].ipv6_addr, &canonical, sizeof(canonical)) == 0 &&
            map->entries[i].ipv6_ifindex == ifindex &&
            strcmp(map->entries[i].name, name) == 0) {
            return;
        }
    }
    if (map->count >= MAX_IFCONFIG_OWNER_ENTRIES) {
        return;
    }
    map->entries[map->count].family = AF_INET6;
    map->entries[map->count].ipv6_addr = canonical;
    map->entries[map->count].ipv6_ifindex = ifindex;
    strncpy(map->entries[map->count].name, name, sizeof(map->entries[map->count].name) - 1);
    map->count++;
}

TC_LOCAL int copy_first_token(char *out,
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

TC_LOCAL int copy_last_token(char *out,
                                             size_t out_len,
                                             const char *text) {
    const char *end;
    const char *start;
    size_t len;

    if (out_len == 0 || text == NULL) {
        return 0;
    }
    end = text + strlen(text);
    while (end > text && isspace((unsigned char)end[-1])) {
        end--;
    }
    start = end;
    while (start > text && !isspace((unsigned char)start[-1])) {
        start--;
    }
    len = (size_t)(end - start);
    if (len == 0) {
        return 0;
    }
    if (len >= out_len) {
        len = out_len - 1;
    }
    memcpy(out, start, len);
    out[len] = '\0';
    return 1;
}

TC_LOCAL void network_role_evidence_parse_route_line(struct network_role_evidence *evidence,
                                                                      const char *line) {
    const char *cursor = line;

    while (cursor != NULL && *cursor != '\0' && isspace((unsigned char)*cursor)) {
        cursor++;
    }
    if (cursor == NULL || strncmp(cursor, "default", 7) != 0 ||
        (cursor[7] != '\0' && !isspace((unsigned char)cursor[7]))) {
        return;
    }
    (void)copy_last_token(evidence->default_iface, sizeof(evidence->default_iface), cursor);
}

TC_LOCAL void network_role_evidence_parse_ipv6_route_line(struct network_role_evidence *evidence,
                                                                           const char *line) {
    const char *cursor = line;

    while (cursor != NULL && *cursor != '\0' && isspace((unsigned char)*cursor)) {
        cursor++;
    }
    if (cursor == NULL ||
        !((strncmp(cursor, "default", 7) == 0 &&
           (cursor[7] == '\0' || isspace((unsigned char)cursor[7]))) ||
          (strncmp(cursor, "::/0", 4) == 0 &&
           (cursor[4] == '\0' || isspace((unsigned char)cursor[4]))))) {
        return;
    }
    (void)copy_last_token(evidence->default_ipv6_iface,
                          sizeof(evidence->default_ipv6_iface),
                          cursor);
}

TC_LOCAL void network_role_evidence_parse_pf_line(struct network_role_evidence *evidence,
                                                                   const char *line) {
    const char *cursor = line;

    while (cursor != NULL && *cursor != '\0' && isspace((unsigned char)*cursor)) {
        cursor++;
    }
    if (cursor == NULL || strncmp(cursor, "nat on ", 7) != 0) {
        return;
    }
    (void)copy_first_token(evidence->nat_iface, sizeof(evidence->nat_iface), cursor + 7);
}

TC_LOCAL void parse_network_role_stream(FILE *stream,
                                                        struct network_role_evidence *evidence,
                                                        int mode) {
    char line[512];

    if (stream == NULL) {
        return;
    }
    while (fgets(line, sizeof(line), stream) != NULL) {
        if (mode == 1) {
            network_role_evidence_parse_pf_line(evidence, line);
        } else if (mode == 2) {
            network_role_evidence_parse_ipv6_route_line(evidence, line);
        } else {
            network_role_evidence_parse_route_line(evidence, line);
        }
    }
}

TC_LOCAL void collect_network_role_evidence(struct network_role_evidence *evidence) {
    FILE *stream;

    memset(evidence, 0, sizeof(*evidence));
    stream = popen("/usr/bin/netstat -rn -f inet 2>/dev/null || /bin/netstat -rn -f inet 2>/dev/null", "r");
    if (stream != NULL) {
        parse_network_role_stream(stream, evidence, 0);
        (void)pclose(stream);
    }
    stream = popen("/usr/bin/netstat -rn -f inet6 2>/dev/null || /bin/netstat -rn -f inet6 2>/dev/null", "r");
    if (stream != NULL) {
        parse_network_role_stream(stream, evidence, 2);
        (void)pclose(stream);
    }
    stream = popen("/sbin/pfctl -sn 2>/dev/null", "r");
    if (stream != NULL) {
        parse_network_role_stream(stream, evidence, 1);
        (void)pclose(stream);
    }
}

TC_LOCAL void ifconfig_owner_map_parse_line(struct ifconfig_address_owner_map *map,
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

int collect_ifconfig_address_owners(struct ifconfig_address_owner_map *map) {
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

const char *ifconfig_owner_for_ipv4(const struct ifconfig_address_owner_map *map,
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

const char *ifconfig_owner_for_ipv6(const struct ifconfig_address_owner_map *map,
                                                            const struct in6_addr *ipv6_addr) {
    struct in6_addr canonical;
    unsigned int ifindex = ipv6_embedded_scope_id(ipv6_addr);
    size_t i;

    ipv6_canonicalize_scoped_address(&canonical, ipv6_addr);
    for (i = 0; i < map->count; i++) {
        if (map->entries[i].family == AF_INET6 &&
            memcmp(&map->entries[i].ipv6_addr, &canonical, sizeof(canonical)) == 0 &&
            (ifindex == 0 || map->entries[i].ipv6_ifindex == 0 ||
             map->entries[i].ipv6_ifindex == ifindex)) {
            return map->entries[i].name;
        }
    }
    return NULL;
}

TC_LOCAL int owner_map_has_addressed_strong_lan(const struct ifconfig_address_owner_map *owners) {
    size_t i;

    for (i = 0; i < owners->count; i++) {
        const struct ifconfig_address_owner *entry = &owners->entries[i];
        if (!iface_name_is_strong_lan(entry->name)) {
            continue;
        }
        if (entry->family == AF_INET && runtime_ipv4_is_usable(entry->ipv4_addr)) {
            return 1;
        }
        if (entry->family == AF_INET6 && runtime_ipv6_is_bindable(&entry->ipv6_addr)) {
            return 1;
        }
    }
    return 0;
}

TC_LOCAL int link_set_has_addressed_strong_lan(const struct link_context_set *links) {
    size_t i;

    for (i = 0; i < links->count; i++) {
        if (iface_name_is_strong_lan(links->links[i].name) &&
            link_context_has_samba_address(&links->links[i])) {
            return 1;
        }
    }
    return 0;
}

TC_LOCAL int link_context_owned_by_interface(const struct link_context *ctx,
                                                             const char *iface,
                                                             const struct ifconfig_address_owner_map *owners) {
    size_t i;

    if (iface == NULL || iface[0] == '\0') {
        return 0;
    }
    if (strcmp(ctx->name, iface) == 0) {
        return 1;
    }
    for (i = 0; i < ctx->ipv4_count; i++) {
        const char *owner = ifconfig_owner_for_ipv4(owners, ctx->ipv4[i].addr);
        if (owner != NULL && strcmp(owner, iface) == 0) {
            return 1;
        }
    }
    for (i = 0; i < ctx->ipv6_count; i++) {
        const char *owner = ifconfig_owner_for_ipv6(owners, &ctx->ipv6[i].addr);
        if (owner != NULL && strcmp(owner, iface) == 0) {
            return 1;
        }
    }
    return 0;
}

TC_LOCAL void mark_wan_interface(struct link_context_set *links,
                                                const char *wan_iface,
                                                const struct ifconfig_address_owner_map *owners) {
    size_t i;

    if (wan_iface == NULL || wan_iface[0] == '\0') {
        return;
    }
    for (i = 0; i < links->count; i++) {
        if (link_context_owned_by_interface(&links->links[i], wan_iface, owners)) {
            links->links[i].is_wan = 1;
        }
    }
}

TC_LOCAL void mark_wan_link_contexts_from_evidence(struct link_context_set *links,
                                                                  const struct network_role_evidence *evidence,
                                                                  const struct ifconfig_address_owner_map *owners) {
    int has_lan;
    size_t i;

    for (i = 0; i < links->count; i++) {
        links->links[i].is_wan = 0;
    }
    has_lan = link_set_has_addressed_strong_lan(links) ||
              owner_map_has_addressed_strong_lan(owners);
    if (evidence->nat_iface[0] != '\0' &&
        (evidence->default_iface[0] == '\0' ||
         strcmp(evidence->nat_iface, evidence->default_iface) == 0)) {
        mark_wan_interface(links, evidence->nat_iface, owners);
    }
    if (has_lan && evidence->default_iface[0] != '\0' &&
        !iface_name_is_strong_lan(evidence->default_iface)) {
        mark_wan_interface(links, evidence->default_iface, owners);
    }
    if (has_lan && evidence->default_ipv6_iface[0] != '\0' &&
        !iface_name_is_strong_lan(evidence->default_ipv6_iface)) {
        mark_wan_interface(links, evidence->default_ipv6_iface, owners);
    }
}

int link_context_set_has_synthetic_names(const struct link_context_set *set);

void mark_wan_link_contexts(struct link_context_set *links) {
    struct network_role_evidence evidence;
    struct ifconfig_address_owner_map owners;
    static int logged_ambiguous_private_links = 0;
    size_t private_synthetic_count = 0;
    size_t wan_count = 0;
    size_t i;

    memset(&owners, 0, sizeof(owners));
    if (link_context_set_has_synthetic_names(links)) {
        (void)collect_ifconfig_address_owners(&owners);
    }
    collect_network_role_evidence(&evidence);
    mark_wan_link_contexts_from_evidence(links, &evidence, &owners);
    for (i = 0; i < links->count; i++) {
        if (links->links[i].is_wan) {
            wan_count++;
        }
        if (link_context_is_unnamed_private_lan_fallback(&links->links[i])) {
            private_synthetic_count++;
        }
    }
    if (!logged_ambiguous_private_links && wan_count == 0 && private_synthetic_count > 1) {
        fprintf(stderr,
                "auto-ip: routing evidence could not distinguish %lu unnamed private links; preserving fail-open LAN fallback\n",
                (unsigned long)private_synthetic_count);
        logged_ambiguous_private_links = 1;
    }
}

int link_context_set_has_synthetic_names(const struct link_context_set *set) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (iface_name_is_synthetic_from_address(set->links[i].name)) {
            return 1;
        }
    }
    return 0;
}
