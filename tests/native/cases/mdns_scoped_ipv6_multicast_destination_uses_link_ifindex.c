#include <arpa/inet.h>
#include <string.h>
#include "mdns/mdns.h"

int main(void) {
    struct sockaddr_in6 base;
    struct sockaddr_in6 scoped;
    struct sockaddr_in6 source;
    struct link_context link;
    struct link_context_set links;
    struct planned_rr_set planned;
    struct in6_addr lan_addr;
    struct in6_addr wan_addr;
    struct in6_addr canonical_wan;

    memset(&base, 0, sizeof(base));
    memset(&scoped, 0, sizeof(scoped));
    memset(&source, 0, sizeof(source));
    memset(&link, 0, sizeof(link));
    memset(&links, 0, sizeof(links));
    memset(&planned, 0, sizeof(planned));
    base.sin6_family = AF_INET6;
    base.sin6_port = htons(5353);
    if (inet_pton(AF_INET6, "ff02::fb", &base.sin6_addr) != 1) {
        return 1;
    }
    link.ifindex = 17;
    scoped_mdns_dest6_for_link(&scoped, &base, &link);
    if (scoped.sin6_family != AF_INET6 ||
        scoped.sin6_port != htons(5353) ||
        scoped.sin6_scope_id != 17 ||
        memcmp(&scoped.sin6_addr, &base.sin6_addr, sizeof(base.sin6_addr)) != 0) {
        return 2;
    }
    if (inet_pton(AF_INET6, "fe80:8::1", &lan_addr) != 1 ||
        inet_pton(AF_INET6, "fe80:1::1", &wan_addr) != 1 ||
        inet_pton(AF_INET6, "fe80::1", &canonical_wan) != 1 ||
        inet_pton(AF_INET6, "fe80:1::abcd", &source.sin6_addr) != 1) {
        return 3;
    }
    source.sin6_family = AF_INET6;
    append_link_ipv6(&links, "bridge0", &lan_addr, 64, 8, IFF_UP | IFF_RUNNING);
    append_link_ipv6(&links, "bcmeth1", &wan_addr, 64, 1, IFF_UP | IFF_RUNNING);
    if (ipv6_sockaddr_effective_ifindex(&source) != 1 ||
        select_response_link_ipv6(&links, &source, 0) != &links.links[1]) {
        return 4;
    }
    if (planned_rr_add_link_addresses(&planned,
                                      MDNS_REPLY_MULTICAST,
                                      "timecapsule.local.",
                                      &links.links[1],
                                      0,
                                      1,
                                      120) != 0 ||
        planned.count != 1 ||
        memcmp(planned.records[0].rdata, &canonical_wan, sizeof(canonical_wan)) != 0) {
        return 6;
    }
    memset(&source, 0, sizeof(source));
    source.sin6_family = AF_INET6;
    if (inet_pton(AF_INET6, "2001:db8::55", &source.sin6_addr) != 1 ||
        select_response_link_ipv6(&links, &source, 1) != &links.links[1] ||
        source.sin6_scope_id != 0) {
        return 5;
    }
    return 0;
}
