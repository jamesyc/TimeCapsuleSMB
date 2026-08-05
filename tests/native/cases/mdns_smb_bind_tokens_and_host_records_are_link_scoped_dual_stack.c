#include <arpa/inet.h>
#include <string.h>
#include "mdns/mdns.h"

static int buffer_contains(const uint8_t *buf, size_t len, const void *needle, size_t needle_len) {
    size_t i;
    for (i = 0; i + needle_len <= len; i++) {
        if (memcmp(buf + i, needle, needle_len) == 0) {
            return 1;
        }
    }
    return 0;
}

int main(void) {
    struct link_context_set set;
    struct in6_addr ula;
    struct in6_addr unknown_prefix;
    struct in6_addr ll;
    struct in6_addr canonical_ll;
    uint8_t packet[512];
    size_t off;
    int answers;

    memset(&set, 0, sizeof(set));
    if (inet_pton(AF_INET6, "fdbb:1111:2222:3333::40", &ula) != 1 ||
        inet_pton(AF_INET6, "fdbb:1111:2222:3333::41", &unknown_prefix) != 1 ||
        inet_pton(AF_INET6, "fe80:7::40", &ll) != 1 ||
        inet_pton(AF_INET6, "fe80::40", &canonical_ll) != 1) {
        return 1;
    }
    append_link_ipv4(&set, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_link_ipv4(&set, "bridge0", inet_addr("169.254.1.9"), inet_addr("255.255.0.0"), IFF_UP | IFF_RUNNING);
    append_link_ipv4(&set, "lo0", inet_addr("127.0.0.1"), inet_addr("255.0.0.0"), IFF_UP | IFF_RUNNING);
    append_link_ipv6(&set, "bridge0", &ula, 64, 7, IFF_UP | IFF_RUNNING);
    append_link_ipv6(&set, "bridge0", &unknown_prefix, -1, 7, IFF_UP | IFF_RUNNING);
    append_link_ipv6(&set, "bridge0", &ll, 64, 7, IFF_UP | IFF_RUNNING);

    if (set.count != 1 || set.links[0].ipv4_count != 2 || set.links[0].ipv6_count != 3) {
        return 2;
    }
    if (print_smb_link_bind_tokens(stdout, &set) != 0) {
        return 3;
    }

    memset(packet, 0, sizeof(packet));
    off = 0;
    answers = 0;
    if (append_host_address_records(packet, &off, sizeof(packet), "timecapsule.local.", &set.links[0], 1, 1, 120, &answers) != 0) {
        return 4;
    }
    if (answers != 4 ||
        !buffer_contains(packet, off, &set.links[0].ipv4[0].addr, sizeof(set.links[0].ipv4[0].addr)) ||
        !buffer_contains(packet, off, &set.links[0].ipv4[1].addr, sizeof(set.links[0].ipv4[1].addr)) ||
        !buffer_contains(packet, off, &ula, sizeof(ula)) ||
        !buffer_contains(packet, off, &canonical_ll, sizeof(canonical_ll)) ||
        buffer_contains(packet, off, &ll, sizeof(ll)) ||
        buffer_contains(packet, off, &unknown_prefix, sizeof(unknown_prefix))) {
        return 5;
    }
    return 0;
}
