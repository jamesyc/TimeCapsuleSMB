#include <arpa/inet.h>
#include <string.h>
#include "mdns/mdns.h"

int main(void) {
    struct iface_context_set a;
    struct iface_context_set b;
    char synthetic_name[IFNAMSIZ];

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
    synthetic_ipv4_ifaddrs_name(synthetic_name, sizeof(synthetic_name), inet_addr("192.168.100.100"));
    if (strcmp(synthetic_name, "ip4-c0a86464") != 0) {
        return 7;
    }
    synthetic_ipv4_ifaddrs_name(synthetic_name, sizeof(synthetic_name), inet_addr("255.255.255.255"));
    if (strcmp(synthetic_name, "ip4-ffffffff") != 0 || strlen(synthetic_name) >= IFNAMSIZ) {
        return 8;
    }

    memset(&a, 0, sizeof(a));
    memset(&b, 0, sizeof(b));
    append_iface_context(&a, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&b, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    if (!iface_context_sets_equal(&a, &b)) {
        return 3;
    }
    append_iface_context(&b, "bcmeth0", inet_addr("192.168.1.217"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    if (iface_context_sets_equal(&a, &b)) {
        return 4;
    }
    b = a;
    b.contexts[0].netmask = inet_addr("255.255.0.0");
    if (iface_context_sets_equal(&a, &b)) {
        return 5;
    }
    b = a;
    b.count = 0;
    if (iface_context_sets_equal(&a, &b)) {
        return 6;
    }

    memset(&a, 0, sizeof(a));
    memset(&b, 0, sizeof(b));
    append_iface_context(&a, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&a, "bcmeth0", inet_addr("192.168.1.217"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&b, "bcmeth0", inet_addr("192.168.1.217"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&b, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    if (!iface_context_sets_equal(&a, &b)) {
        return 10;
    }

    memset(&a, 0, sizeof(a));
    append_iface_context(&a, "ppp0", inet_addr("10.0.1.2"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&a, "bridge0", inet_addr("203.0.113.5"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&a, "en0", inet_addr("192.168.1.2"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&a, "br1", inet_addr("192.168.1.3"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_iface_context(&a, "br0", inet_addr("192.168.1.4"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    sort_iface_contexts(&a);
    if (strcmp(a.contexts[0].name, "br0") != 0 ||
        strcmp(a.contexts[1].name, "br1") != 0 ||
        strcmp(a.contexts[2].name, "en0") != 0 ||
        strcmp(a.contexts[3].name, "bridge0") != 0 ||
        strcmp(a.contexts[4].name, "ppp0") != 0) {
        return 11;
    }
    return 0;
}
