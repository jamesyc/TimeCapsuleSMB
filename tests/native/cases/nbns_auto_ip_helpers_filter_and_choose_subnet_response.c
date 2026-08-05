#include <arpa/inet.h>
#include <string.h>
#include "nbns/nbns.h"

int main(void) {
    struct link_context_set links;
    struct link_context_set nbns_links;
    struct link_context_set single_link;
    struct link_context_set v6_only_links;
    struct link_context_set links_a;
    struct link_context_set links_b;
    struct in6_addr v6_addr;

    if (AUTO_IP_STARTUP_POLL_SECONDS != 2 || AUTO_IP_STABLE_POLL_SECONDS != 30) {
        return 10;
    }

    if (runtime_ipv4_is_usable(inet_addr("0.1.2.3")) ||
        runtime_ipv4_is_usable(inet_addr("127.0.0.1")) ||
        runtime_ipv4_is_usable(inet_addr("169.254.1.9")) ||
        runtime_ipv4_is_usable(inet_addr("224.0.0.1")) ||
        runtime_ipv4_is_usable(inet_addr("240.0.0.1")) ||
        runtime_ipv4_is_usable(inet_addr("255.255.255.255")) ||
        runtime_ipv4_is_usable(0) ||
        !runtime_ipv4_is_usable(inet_addr("10.0.1.1"))) {
        return 1;
    }
    if (!iface_flags_are_usable(IFF_UP | IFF_RUNNING, 1) ||
        iface_flags_are_usable(IFF_UP, 1) ||
        !iface_flags_are_usable(IFF_UP, 0) ||
        iface_flags_are_usable(IFF_UP | IFF_LOOPBACK, 0) ||
        iface_flags_are_usable(IFF_RUNNING, 0)) {
        return 2;
    }

    memset(&links, 0, sizeof(links));
    append_link_ipv4(&links, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_link_ipv4(&links, "bcmeth0", inet_addr("192.168.50.2"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    filter_nbns_link_contexts(&nbns_links, &links);
    if (nbns_links.count != 1 || strcmp(nbns_links.links[0].name, "bridge0") != 0) {
        return 29;
    }
    if (choose_response_ipv4_from_links(&links, inet_addr("192.168.50.99")) != inet_addr("192.168.50.2")) {
        return 3;
    }
    if (choose_response_ipv4_from_links(&links, inet_addr("172.16.1.5")) != 0) {
        return 4;
    }

    memset(&links, 0, sizeof(links));
    append_link_ipv4(&links, "bridge0", inet_addr("10.0.1.1"), 0, IFF_UP | IFF_RUNNING);
    append_link_ipv4(&links, "bcmeth0", inet_addr("192.168.1.217"), 0, IFF_UP | IFF_RUNNING);
    if (choose_response_ipv4_from_links(&links, inet_addr("10.0.1.3")) != inet_addr("10.0.1.1")) {
        return 14;
    }
    if (choose_response_ipv4_from_links(&links, inet_addr("10.44.55.66")) != inet_addr("10.0.1.1")) {
        return 17;
    }
    if (choose_response_ipv4_from_links(&links, inet_addr("192.168.1.40")) != inet_addr("192.168.1.217")) {
        return 15;
    }
    if (choose_response_ipv4_from_links(&links, inet_addr("172.16.1.5")) != 0) {
        return 16;
    }

    memset(&single_link, 0, sizeof(single_link));
    append_link_ipv4(&single_link, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    if (choose_response_ipv4_from_links(&single_link, inet_addr("172.16.1.5")) != inet_addr("10.0.1.1")) {
        return 13;
    }

    memset(&links, 0, sizeof(links));
    append_link_ipv4(&links, "bridge0", inet_addr("10.0.1.1"), 0, IFF_UP | IFF_RUNNING);
    if (inet_pton(AF_INET6, "fd00::1", &v6_addr) != 1) {
        return 20;
    }
    append_link_ipv6(&links, "bridge0", &v6_addr, 64, 0, IFF_UP | IFF_RUNNING);
    keep_only_nbns_ipv4_link_contexts(&links);
    if (!link_contexts_need_nbns_ipv4_socket(&links)) {
        return 21;
    }
    if (links.count != 1 || links.links[0].ipv6_count != 0 || links.links[0].mdns_ipv6_transport != 0) {
        return 22;
    }
    if (choose_response_ipv4_from_links(&links, inet_addr("172.16.1.5")) != inet_addr("10.0.1.1")) {
        return 23;
    }

    memset(&v6_only_links, 0, sizeof(v6_only_links));
    append_link_ipv6(&v6_only_links, "bridge0", &v6_addr, 64, 0, IFF_UP | IFF_RUNNING);
    keep_only_nbns_ipv4_link_contexts(&v6_only_links);
    if (v6_only_links.count != 0) {
        return 24;
    }
    if (link_contexts_need_nbns_ipv4_socket(&v6_only_links)) {
        return 25;
    }

    memset(&links_a, 0, sizeof(links_a));
    memset(&links_b, 0, sizeof(links_b));
    append_link_ipv4(&links_a, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_link_ipv6(&links_a, "bridge0", &v6_addr, 64, 0, IFF_UP | IFF_RUNNING);
    append_link_ipv6(&links_b, "bridge0", &v6_addr, 64, 0, IFF_UP | IFF_RUNNING);
    append_link_ipv4(&links_b, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    if (!link_context_sets_equal(&links_a, &links_b)) {
        return 27;
    }
    links_b.links[0].ipv6[0].prefix_len = 48;
    if (link_context_sets_equal(&links_a, &links_b)) {
        return 28;
    }
    return 0;
}
