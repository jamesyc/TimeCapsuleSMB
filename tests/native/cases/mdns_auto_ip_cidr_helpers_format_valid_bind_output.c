#include <arpa/inet.h>
#include <string.h>
#include "mdns/mdns.h"

int main(void) {
    struct iface_context_set set;
    char cidr[INET_ADDRSTRLEN + 4];
    struct in6_addr mask6;

    if (netmask_prefix_length(inet_addr("255.255.255.0")) != 24 ||
        netmask_prefix_length(inet_addr("255.255.0.0")) != 16 ||
        netmask_prefix_length(0) != 24 ||
        netmask_prefix_length(inet_addr("255.0.255.0")) != 24) {
        return 1;
    }
    if (netmask_prefix_length(ipv4_link_local_netmask()) != 16) {
        return 6;
    }
    if (inet_pton(AF_INET6, "ffff:ffff:ffff:ffff::", &mask6) != 1 ||
        ipv6_prefix_length_from_mask(&mask6) != 64) {
        return 7;
    }
    memset(&mask6, 0, sizeof(mask6));
    if (ipv6_prefix_length_from_mask(&mask6) != -1) {
        return 8;
    }
    if (inet_pton(AF_INET6, "ffff:ffff::ffff", &mask6) != 1 ||
        ipv6_prefix_length_from_mask(&mask6) != -1) {
        return 9;
    }

    memset(&set, 0, sizeof(set));
    if (print_iface_context_cidrs(stdout, &set) == 0) {
        return 5;
    }
    append_iface_context(&set, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&set, "bcmeth0", inet_addr("192.168.1.40"), 0, IFF_UP | IFF_RUNNING);
    append_iface_context(&set, "lo0", inet_addr("127.0.0.1"), inet_addr("255.0.0.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&set, "ll0", inet_addr("169.254.1.9"), inet_addr("255.255.0.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&set, "zero0", inet_addr("0.1.2.3"), inet_addr("255.0.0.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&set, "mcast0", inet_addr("224.0.0.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&set, "reserved0", inet_addr("240.0.0.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&set, "broadcast0", inet_addr("255.255.255.255"), inet_addr("255.255.255.255"), IFF_UP | IFF_RUNNING);
    if (set.count != 2) {
        return 2;
    }
    if (iface_context_cidr(cidr, sizeof(cidr), &set.contexts[1]) != 0 || strcmp(cidr, "192.168.1.40/24") != 0) {
        return 3;
    }
    if (print_iface_context_cidrs(stdout, &set) != 0) {
        return 4;
    }
    return 0;
}
