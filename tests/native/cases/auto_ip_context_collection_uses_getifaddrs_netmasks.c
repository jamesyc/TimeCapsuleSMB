#include <arpa/inet.h>
#include <ifaddrs.h>
#include <stdio.h>
#include <string.h>

int fake_getifaddrs(struct ifaddrs **out);
void fake_freeifaddrs(struct ifaddrs *list);

#include "mdns/mdns.h"

static struct ifaddrs fake_ifas[4];
static struct sockaddr_in fake_addrs[3];
static struct sockaddr_in fake_masks[3];

static void set_ipv4_sockaddr(struct sockaddr_in *sin, const char *addr) {
    memset(sin, 0, sizeof(*sin));
#if defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__DragonFly__)
    sin->sin_len = sizeof(*sin);
#endif
    sin->sin_family = AF_INET;
    sin->sin_addr.s_addr = inet_addr(addr);
}

int fake_getifaddrs(struct ifaddrs **out) {
    memset(fake_ifas, 0, sizeof(fake_ifas));
    memset(fake_addrs, 0, sizeof(fake_addrs));
    memset(fake_masks, 0, sizeof(fake_masks));

    set_ipv4_sockaddr(&fake_addrs[0], "10.0.1.1");
    set_ipv4_sockaddr(&fake_masks[0], "255.0.0.0");
    fake_ifas[0].ifa_next = &fake_ifas[1];
    fake_ifas[0].ifa_name = "bridge0";
    fake_ifas[0].ifa_flags = IFF_UP | IFF_RUNNING;
    fake_ifas[0].ifa_addr = (struct sockaddr *)(void *)&fake_addrs[0];
    fake_ifas[0].ifa_netmask = (struct sockaddr *)(void *)&fake_masks[0];

    set_ipv4_sockaddr(&fake_addrs[1], "192.168.1.217");
    set_ipv4_sockaddr(&fake_masks[1], "255.255.255.0");
    fake_ifas[1].ifa_next = &fake_ifas[2];
    fake_ifas[1].ifa_name = "bcmeth1";
    fake_ifas[1].ifa_flags = IFF_UP | IFF_RUNNING;
    fake_ifas[1].ifa_addr = (struct sockaddr *)(void *)&fake_addrs[1];
    fake_ifas[1].ifa_netmask = (struct sockaddr *)(void *)&fake_masks[1];

    set_ipv4_sockaddr(&fake_addrs[2], "10.2.3.4");
    set_ipv4_sockaddr(&fake_masks[2], "255.0.0.0");
    fake_ifas[2].ifa_next = NULL;
    fake_ifas[2].ifa_name = "down0";
    fake_ifas[2].ifa_flags = IFF_UP;
    fake_ifas[2].ifa_addr = (struct sockaddr *)(void *)&fake_addrs[2];
    fake_ifas[2].ifa_netmask = (struct sockaddr *)(void *)&fake_masks[2];

    *out = &fake_ifas[0];
    return 0;
}

void fake_freeifaddrs(struct ifaddrs *list) {
    (void)list;
}

int main(void) {
    struct iface_context_set iface_contexts;
    struct link_context_set link_contexts;
    char cidr[INET_ADDRSTRLEN + 4];

    if (collect_usable_iface_contexts(&iface_contexts) != 0 || iface_contexts.count != 2) {
        return 1;
    }
    if (strcmp(iface_contexts.contexts[0].name, "bridge0") != 0 ||
        iface_contexts.contexts[0].ipv4_addr != inet_addr("10.0.1.1") ||
        iface_contexts.contexts[0].netmask != inet_addr("255.0.0.0")) {
        return 2;
    }
    if (source_matches_context_subnet(inet_addr("10.44.55.66"), &iface_contexts.contexts[0]) != 1) {
        return 3;
    }
    if (source_matches_context_subnet(inet_addr("11.0.1.3"), &iface_contexts.contexts[0]) != 0) {
        return 4;
    }
    if (collect_usable_link_contexts(&link_contexts) != 0 || link_contexts.count != 2) {
        return 5;
    }
    if (link_context_ipv4_cidr(cidr, sizeof(cidr), &link_contexts.links[0].ipv4[0]) != 0 ||
        strcmp(cidr, "10.0.1.1/8") != 0) {
        return 6;
    }
    return 0;
}
