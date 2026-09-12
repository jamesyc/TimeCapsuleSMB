#include <arpa/inet.h>
#include <ifaddrs.h>
#include <stdio.h>
#include <string.h>

int fake_getifaddrs(struct ifaddrs **out);
void fake_freeifaddrs(struct ifaddrs *list);

#include "mdns/mdns.h"

static struct ifaddrs fake_ifas[3];
static struct sockaddr_in fake_addrs[3];
static struct sockaddr_in fake_masks[3];

static void set_ipv4_sockaddr(struct sockaddr_in *sin, const char *addr, int family) {
    memset(sin, 0, sizeof(*sin));
#if defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__DragonFly__)
    sin->sin_len = sizeof(*sin);
#endif
    sin->sin_family = family;
    sin->sin_addr.s_addr = inet_addr(addr);
}

int fake_getifaddrs(struct ifaddrs **out) {
    memset(fake_ifas, 0, sizeof(fake_ifas));
    memset(fake_addrs, 0, sizeof(fake_addrs));
    memset(fake_masks, 0, sizeof(fake_masks));

    set_ipv4_sockaddr(&fake_addrs[0], "192.168.1.217", AF_INET);
    set_ipv4_sockaddr(&fake_masks[0], "255.255.255.0", 0);
    fake_ifas[0].ifa_next = &fake_ifas[1];
    fake_ifas[0].ifa_name = "";
    fake_ifas[0].ifa_flags = IFF_UP | IFF_RUNNING;
    fake_ifas[0].ifa_addr = (struct sockaddr *)(void *)&fake_addrs[0];
    fake_ifas[0].ifa_netmask = (struct sockaddr *)(void *)&fake_masks[0];

    set_ipv4_sockaddr(&fake_addrs[1], "10.0.1.1", AF_INET);
    set_ipv4_sockaddr(&fake_masks[1], "255.0.0.0", 0);
    fake_ifas[1].ifa_next = &fake_ifas[2];
    fake_ifas[1].ifa_name = "";
    fake_ifas[1].ifa_flags = IFF_UP | IFF_RUNNING;
    fake_ifas[1].ifa_addr = (struct sockaddr *)(void *)&fake_addrs[1];
    fake_ifas[1].ifa_netmask = (struct sockaddr *)(void *)&fake_masks[1];

    set_ipv4_sockaddr(&fake_addrs[2], "169.254.155.207", AF_INET);
    set_ipv4_sockaddr(&fake_masks[2], "255.255.0.0", 0);
    fake_ifas[2].ifa_next = NULL;
    fake_ifas[2].ifa_name = "";
    fake_ifas[2].ifa_flags = IFF_UP | IFF_RUNNING;
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
    const struct iface_context *ten_iface = NULL;
    const struct link_context *ten_link = NULL;
    char cidr[INET_ADDRSTRLEN + 4];
    size_t i;

    if (collect_usable_iface_contexts(&iface_contexts) != 0 || iface_contexts.count != 2) {
        return 1;
    }
    for (i = 0; i < iface_contexts.count; i++) {
        if (iface_contexts.contexts[i].ipv4_addr == inet_addr("10.0.1.1")) {
            ten_iface = &iface_contexts.contexts[i];
        }
    }
    if (ten_iface == NULL ||
        strcmp(ten_iface->name, "") == 0 ||
        ten_iface->netmask != inet_addr("255.0.0.0")) {
        return 2;
    }
    if (collect_usable_link_contexts(&link_contexts) != 0 || link_contexts.count != 3) {
        return 3;
    }
    for (i = 0; i < link_contexts.count; i++) {
        if (link_contexts.links[i].ipv4_count > 0 &&
            link_contexts.links[i].ipv4[0].addr == inet_addr("10.0.1.1")) {
            ten_link = &link_contexts.links[i];
        }
    }
    if (ten_link == NULL) {
        return 4;
    }
    if (source_matches_link_ipv4_subnet(inet_addr("10.44.55.66"), ten_link) != 1) {
        return 5;
    }
    if (link_ipv4_source_for_peer(ten_link, inet_addr("10.44.55.66")) != inet_addr("10.0.1.1")) {
        return 6;
    }
    if (link_context_ipv4_cidr(cidr, sizeof(cidr), &ten_link->ipv4[0]) != 0 ||
        strcmp(cidr, "10.0.1.1/8") != 0) {
        return 7;
    }
    return 0;
}
