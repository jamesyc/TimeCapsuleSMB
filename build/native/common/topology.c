#include "plan.h"

const char *link_role_name(enum link_role role) {
    switch (role) {
    case LINK_ROLE_LAN: return "lan";
    case LINK_ROLE_WAN: return "wan";
    case LINK_ROLE_GUEST: return "guest";
    default: return "isolated";
    }
}

const char *router_mode_name(enum router_mode mode) {
    switch (mode) {
    case ROUTER_MODE_BRIDGE: return "bridge";
    case ROUTER_MODE_DHCP: return "dhcp";
    case ROUTER_MODE_NAT: return "nat";
    default: return "unknown";
    }
}

/* (raNA,raDS): (0,0) bridge, (0,1) DHCP-only, (1,1) NAT. (1,0) was never
 * measured and unavailable inputs decode nothing: both are UNKNOWN. */
enum router_mode router_mode_from_facts(const struct device_facts *facts) {
    struct acp_bool rana = acp_bool(&facts->acp[ACP_KEY_raNA]);
    struct acp_bool rads = acp_bool(&facts->acp[ACP_KEY_raDS]);

    if (!rana.available || !rads.available) {
        return ROUTER_MODE_UNKNOWN;
    }
    if (!rana.value && !rads.value) {
        return ROUTER_MODE_BRIDGE;
    }
    if (!rana.value && rads.value) {
        return ROUTER_MODE_DHCP;
    }
    if (rana.value && rads.value) {
        return ROUTER_MODE_NAT;
    }
    return ROUTER_MODE_UNKNOWN;
}

int link_owns_ipv4(const struct link_plan *link, uint32_t network_order) {
    size_t i;
    for (i = 0; i < link->addr_count; i++) {
        if (link->addrs[i].family == AF_INET && link->addrs[i].v4.s_addr == network_order) {
            return 1;
        }
    }
    return 0;
}

int link_plan_has_service_address(const struct link_plan *link) {
    size_t i;
    for (i = 0; i < link->addr_count; i++) {
        if (addr_is_service_address(&link->addrs[i])) {
            return 1;
        }
    }
    return 0;
}

static size_t owners_of_ipv4(const struct device_facts *facts, uint32_t network_order) {
    size_t i;
    unsigned seen[TC_MAX_ADDRS];
    size_t seen_count = 0;

    for (i = 0; i < facts->ifs.addr_count; i++) {
        const struct if_addr *addr = &facts->ifs.addrs[i];
        size_t j;
        int duplicate = 0;
        if (addr->family != AF_INET || addr->v4.s_addr != network_order) {
            continue;
        }
        for (j = 0; j < seen_count; j++) {
            if (seen[j] == addr->owner_index) {
                duplicate = 1;
            }
        }
        if (!duplicate) {
            seen[seen_count++] = addr->owner_index;
        }
    }
    return seen_count;
}

/* Validation domain (a): the mode decodes, the interface table was read,
 * every hint key was actually read, and every ACP hint IP is owned by at
 * most one link. An ACP_UNAVAILABLE hint is an observation (the key is not
 * set: no guest network, no WAN link-local) and may move a role; an
 * ACP_ABORT hint (timeout, exec failure, budget exhausted) says nothing,
 * so treating it as "not configured" would revoke a LAN grant on a slow
 * ACPd and call the result validated (review finding 1). */
int topology_ownership_coherent(const struct device_facts *facts, const char **reason) {
    static const int hint_keys[4] = { ACP_KEY_laIP, ACP_KEY_waIP, ACP_KEY_waLL, ACP_KEY_gnRo };
    static const char *const hint_names[4] = { "laIP", "waIP", "waLL", "gnRo" };
    size_t i;

    *reason = "";
    if (!facts->ifs_ok) {
        *reason = "iflist";
        return 0;
    }
    if (facts->ifs.truncated) {
        *reason = "iflist-truncated";
        return 0;
    }
    if (router_mode_from_facts(facts) == ROUTER_MODE_UNKNOWN) {
        *reason = "mode";
        return 0;
    }
    for (i = 0; i < 4; i++) {
        const struct acp_value *value = &facts->acp[hint_keys[i]];
        struct acp_ipv4 hint = acp_ipv4(value);
        if (value->status == ACP_ABORT) {
            *reason = hint_names[i];
            return 0;
        }
        if (value->status == ACP_OK && !hint.available) {
            /* acp answered, but not with an address: a malformed value is
             * a failed read, not an observation that the key is unset
             * (review 2, R5). Only ACP_UNAVAILABLE means "not set". */
            *reason = hint_names[i];
            return 0;
        }
        if (hint.available && owners_of_ipv4(facts, hint.addr) > 1) {
            *reason = hint_names[i];
            return 0;
        }
    }
    return 1;
}

/* Rules 1-5 of guide B.5 for a coherent known-mode snapshot. Order matters:
 * in bridge/DHCP one bridge owns laIP, waIP and waLL together. */
void topology_assign_roles(struct device_plan *plan, const struct device_facts *facts) {
    struct acp_ipv4 laip = acp_ipv4(&facts->acp[ACP_KEY_laIP]);
    struct acp_ipv4 waip = acp_ipv4(&facts->acp[ACP_KEY_waIP]);
    struct acp_ipv4 wall = acp_ipv4(&facts->acp[ACP_KEY_waLL]);
    struct acp_ipv4 gnro = acp_ipv4(&facts->acp[ACP_KEY_gnRo]);
    size_t i;

    for (i = 0; i < plan->link_count; i++) {
        struct link_plan *link = &plan->links[i];
        link->retained = 0;
        if (gnro.available && link_owns_ipv4(link, gnro.addr)) {
            link->role = LINK_ROLE_GUEST;
        } else if (laip.available && link_owns_ipv4(link, laip.addr)) {
            link->role = LINK_ROLE_LAN;
        } else if (plan->mode == ROUTER_MODE_NAT &&
                   ((waip.available && link_owns_ipv4(link, waip.addr)) ||
                    (wall.available && link_owns_ipv4(link, wall.addr)))) {
            link->role = LINK_ROLE_WAN;
        } else {
            link->role = LINK_ROLE_ISOLATED;
        }
    }
}
