#include <arpa/inet.h>
#include <ifaddrs.h>
#include <stdio.h>
#include <string.h>

int fake_getifaddrs(struct ifaddrs **out);
void fake_freeifaddrs(struct ifaddrs *list);
FILE *fake_popen(const char *command, const char *mode);
int fake_pclose(FILE *stream);

#include "mdns/mdns.h"
#undef EXIT_USAGE
#include "service/service.h"

static struct ifaddrs fake_ifas[3];
static struct sockaddr_in fake_addrs4[2];
static struct sockaddr_in fake_masks4[2];
static struct sockaddr_in6 fake_addr6;
static struct sockaddr_in6 fake_mask6;
static FILE *fake_ifconfig_stream;

static void set_ipv4_sockaddr(struct sockaddr_in *sin, const char *addr) {
    memset(sin, 0, sizeof(*sin));
#if defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__DragonFly__)
    sin->sin_len = sizeof(*sin);
#endif
    sin->sin_family = AF_INET;
    sin->sin_addr.s_addr = inet_addr(addr);
}

static void set_ipv6_sockaddr(struct sockaddr_in6 *sin6, const char *addr) {
    memset(sin6, 0, sizeof(*sin6));
#if defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__DragonFly__)
    sin6->sin6_len = sizeof(*sin6);
#endif
    sin6->sin6_family = AF_INET6;
    inet_pton(AF_INET6, addr, &sin6->sin6_addr);
}

static void set_ipv6_prefix64_mask(struct sockaddr_in6 *sin6) {
    memset(sin6, 0, sizeof(*sin6));
#if defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__DragonFly__)
    sin6->sin6_len = sizeof(*sin6);
#endif
    sin6->sin6_family = AF_INET6;
    memset(sin6->sin6_addr.s6_addr, 0xff, 8);
}

int fake_getifaddrs(struct ifaddrs **out) {
    memset(fake_ifas, 0, sizeof(fake_ifas));
    memset(fake_addrs4, 0, sizeof(fake_addrs4));
    memset(fake_masks4, 0, sizeof(fake_masks4));
    memset(&fake_addr6, 0, sizeof(fake_addr6));
    memset(&fake_mask6, 0, sizeof(fake_mask6));

    set_ipv4_sockaddr(&fake_addrs4[0], "192.168.1.193");
    set_ipv4_sockaddr(&fake_masks4[0], "255.255.255.0");
    fake_ifas[0].ifa_next = &fake_ifas[1];
    fake_ifas[0].ifa_name = "";
    fake_ifas[0].ifa_flags = IFF_UP | IFF_RUNNING;
    fake_ifas[0].ifa_addr = (struct sockaddr *)(void *)&fake_addrs4[0];
    fake_ifas[0].ifa_netmask = (struct sockaddr *)(void *)&fake_masks4[0];

    set_ipv6_sockaddr(&fake_addr6, "fdbb:5737:6e53:9bf7::40");
    set_ipv6_prefix64_mask(&fake_mask6);
    fake_ifas[1].ifa_next = &fake_ifas[2];
    fake_ifas[1].ifa_name = "";
    fake_ifas[1].ifa_flags = IFF_UP | IFF_RUNNING;
    fake_ifas[1].ifa_addr = (struct sockaddr *)(void *)&fake_addr6;
    fake_ifas[1].ifa_netmask = (struct sockaddr *)(void *)&fake_mask6;

    set_ipv4_sockaddr(&fake_addrs4[1], "10.0.1.1");
    set_ipv4_sockaddr(&fake_masks4[1], "255.255.255.0");
    fake_ifas[2].ifa_next = NULL;
    fake_ifas[2].ifa_name = "";
    fake_ifas[2].ifa_flags = IFF_UP | IFF_RUNNING;
    fake_ifas[2].ifa_addr = (struct sockaddr *)(void *)&fake_addrs4[1];
    fake_ifas[2].ifa_netmask = (struct sockaddr *)(void *)&fake_masks4[1];

    *out = &fake_ifas[0];
    return 0;
}

void fake_freeifaddrs(struct ifaddrs *list) {
    (void)list;
}

FILE *fake_popen(const char *command, const char *mode) {
    (void)command;
    if (strcmp(mode, "r") != 0) {
        return NULL;
    }
    fake_ifconfig_stream = tmpfile();
    if (fake_ifconfig_stream == NULL) {
        return NULL;
    }
    fputs(
        "mgi1: flags=8843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n"
        "\tinet6 fdbb:5737:6e53:9bf7::40 prefixlen 64 autoconf\n"
        "\tinet 192.168.1.193 netmask 0xffffff00 broadcast 192.168.1.255\n"
        "bridge0: flags=8043<UP,BROADCAST,RUNNING,MULTICAST> mtu 1500\n"
        "\tinet 10.0.1.1 netmask 0xffffff00 broadcast 10.0.1.255\n",
        fake_ifconfig_stream);
    rewind(fake_ifconfig_stream);
    return fake_ifconfig_stream;
}

int fake_pclose(FILE *stream) {
    return fclose(stream);
}

int main(void) {
    if (print_smb_bind_interfaces_with_provider(stdout, collect_usable_link_contexts_provider, NULL) != EXIT_OK) {
        return 1;
    }
    if (print_smb_bind_interfaces_lan_with_provider(stdout, collect_usable_link_contexts_provider, NULL) != EXIT_OK) {
        return 2;
    }
    return 0;
}
