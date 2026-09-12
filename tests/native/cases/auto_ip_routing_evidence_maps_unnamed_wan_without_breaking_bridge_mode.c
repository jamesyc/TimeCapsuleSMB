#include <arpa/inet.h>
#include <string.h>
#include "mdns/mdns.h"

static void add_owner(struct ifconfig_address_owner_map *owners, const char *name, const char *address) {
    ifconfig_owner_map_add_ipv4(owners, name, address);
}

static void add_owner6(struct ifconfig_address_owner_map *owners, const char *name, const char *address) {
    ifconfig_owner_map_add_ipv6(owners, name, address);
}

int main(void) {
    struct network_role_evidence evidence;
    struct ifconfig_address_owner_map owners;
    struct ifconfig_address_owner_map scoped_owners;
    struct link_context_set links;
    struct link_context_set lan_links;
    struct link_context_set old_links;
    struct in6_addr kame_wan;
    struct in6_addr lan_ula;
    struct in6_addr wan_ula;

    memset(&evidence, 0, sizeof(evidence));
    network_role_evidence_parse_route_line(
        &evidence,
        "default            192.168.1.1        UGS        13     2885      -  mgi1\n");
    network_role_evidence_parse_pf_line(
        &evidence,
        "nat on mgi1 inet from 10.0.1.0/24 to any -> (mgi1:0)\n");
    network_role_evidence_parse_ipv6_route_line(
        &evidence,
        "default                            fe80::1                        UGS         -        -  mgi1\n");
    if (strcmp(evidence.default_iface, "mgi1") != 0 ||
        strcmp(evidence.default_ipv6_iface, "mgi1") != 0 ||
        strcmp(evidence.nat_iface, "mgi1") != 0) {
        return 1;
    }

    memset(&owners, 0, sizeof(owners));
    add_owner(&owners, "bridge0", "10.0.1.1");
    add_owner(&owners, "mgi1", "192.168.1.218");
    add_owner6(&owners, "mgi1", "fe80::82ea:96ff:fee6:5868%mgi1");
    if (inet_pton(AF_INET6, "fe80:1::82ea:96ff:fee6:5868", &kame_wan) != 1 ||
        ifconfig_owner_for_ipv6(&owners, &kame_wan) == NULL ||
        strcmp(ifconfig_owner_for_ipv6(&owners, &kame_wan), "mgi1") != 0) {
        return 8;
    }
    memset(&scoped_owners, 0, sizeof(scoped_owners));
    add_owner6(&scoped_owners, "bridge0", "fe80::1%bridge0");
    add_owner6(&scoped_owners, "mgi1", "fe80::1%mgi1");
    scoped_owners.entries[0].ipv6_ifindex = 8;
    scoped_owners.entries[1].ipv6_ifindex = 1;
    if (inet_pton(AF_INET6, "fe80:1::1", &kame_wan) != 1 ||
        ifconfig_owner_for_ipv6(&scoped_owners, &kame_wan) == NULL ||
        strcmp(ifconfig_owner_for_ipv6(&scoped_owners, &kame_wan), "mgi1") != 0) {
        return 11;
    }

    memset(&links, 0, sizeof(links));
    append_link_ipv4(&links, "ip4-0a000101", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_link_ipv4(&links, "ip4-c0a801da", inet_addr("192.168.1.218"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    mark_wan_link_contexts_from_evidence(&links, &evidence, &owners);
    if (links.links[0].is_wan || !links.links[1].is_wan) {
        return 2;
    }
    filter_smb_bind_link_contexts(&lan_links, &links, 1, 1);
    if (lan_links.count != 1 || lan_links.links[0].ipv4[0].addr != inet_addr("10.0.1.1")) {
        return 3;
    }
    old_links = links;
    old_links.links[1].is_wan = 0;
    if (link_context_sets_equal(&old_links, &links) ||
        link_context_topology_sets_equal(&old_links, &links)) {
        return 6;
    }

    memset(&evidence, 0, sizeof(evidence));
    network_role_evidence_parse_route_line(
        &evidence,
        "default            192.168.1.1        UGS        13     2885      -  mgi1\n");
    links = old_links;
    mark_wan_link_contexts_from_evidence(&links, &evidence, &owners);
    if (links.links[0].is_wan || !links.links[1].is_wan) {
        return 7;
    }

    memset(&evidence, 0, sizeof(evidence));
    network_role_evidence_parse_ipv6_route_line(
        &evidence,
        "::/0                               fe80::1                        UGS         -        -  mgi1\n");
    memset(&links, 0, sizeof(links));
    if (inet_pton(AF_INET6, "fdbb:1::1", &lan_ula) != 1 ||
        inet_pton(AF_INET6, "fdcc:2::1", &wan_ula) != 1) {
        return 9;
    }
    append_link_ipv6(&links, "bridge0", &lan_ula, 64, 8, IFF_UP | IFF_RUNNING);
    append_link_ipv6(&links, "mgi1", &wan_ula, 64, 1, IFF_UP | IFF_RUNNING);
    mark_wan_link_contexts_from_evidence(&links, &evidence, &owners);
    if (links.links[0].is_wan || !links.links[1].is_wan) {
        return 10;
    }

    memset(&evidence, 0, sizeof(evidence));
    network_role_evidence_parse_route_line(
        &evidence,
        "default            192.168.1.1        UGS         1       54      -  bridge0\n");
    memset(&links, 0, sizeof(links));
    append_link_ipv4(&links, "bridge0", inet_addr("192.168.1.218"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    mark_wan_link_contexts_from_evidence(&links, &evidence, &owners);
    if (links.links[0].is_wan) {
        return 4;
    }

    memset(&evidence, 0, sizeof(evidence));
    memset(&links, 0, sizeof(links));
    append_link_ipv4(&links, "ip4-0a000101", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_link_ipv4(&links, "ip4-c0a801da", inet_addr("192.168.1.218"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    mark_wan_link_contexts_from_evidence(&links, &evidence, &owners);
    filter_smb_bind_link_contexts(&lan_links, &links, 1, 1);
    if (lan_links.count != 2) {
        return 5;
    }
    return 0;
}
