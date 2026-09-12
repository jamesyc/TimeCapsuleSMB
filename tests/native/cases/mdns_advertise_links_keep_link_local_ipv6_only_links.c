#include <arpa/inet.h>
#include <string.h>
#include "mdns/mdns.h"

int main(void) {
    struct link_context_set all_links;
    struct link_context_set advertise_links;
    struct in6_addr ll1;
    struct in6_addr ll2;

    memset(&all_links, 0, sizeof(all_links));
    if (inet_pton(AF_INET6, "fe80::1", &ll1) != 1 ||
        inet_pton(AF_INET6, "fe80::2", &ll2) != 1) {
        return 1;
    }

    append_link_ipv6(&all_links, "bridge0", &ll1, 64, 7, IFF_UP | IFF_RUNNING);
    append_link_ipv4(&all_links, "bridge1", inet_addr("192.168.1.40"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_link_ipv6(&all_links, "bridge1", &ll2, 64, 8, IFF_UP | IFF_RUNNING);
    filter_advertise_link_contexts(&advertise_links, &all_links);

    if (all_links.count != 2 || advertise_links.count != 2) {
        return 2;
    }
    if (strcmp(advertise_links.links[0].name, "bridge1") != 0 ||
        strcmp(advertise_links.links[1].name, "bridge0") != 0) {
        return 3;
    }
    if (!link_contexts_need_ipv4_socket(&advertise_links) ||
        !link_contexts_need_ipv6_socket(&advertise_links)) {
        return 4;
    }
    return 0;
}
