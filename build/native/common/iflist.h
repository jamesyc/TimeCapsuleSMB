#ifndef TC_IFLIST_H
#define TC_IFLIST_H
#include "platform.h"

/* Interface table built from sysctl(NET_RT_IFLIST) instead of getifaddrs().
 *
 * Apple's NetBSD 4 kernel emits a 152-byte struct if_msghdr while the SDK
 * libc expects 144, so getifaddrs() reads the AF_LINK sockaddr from inside
 * if_data: names are garbage and if_nametoindex() returns 0 (F4/M17). This
 * parser never interprets if_data. It locates the sockaddr_dl by scanning
 * for sdl_family == AF_LINK && sdl_index == ifm_index, and reads addresses
 * from RTM_NEWADDR rows keyed on the message's RTM_VERSION byte:
 *   version 3 (NetBSD 4): RTM_IFINFO 0xf, ifa_msghdr 20 bytes, index @12, RT_ROUNDUP 4
 *   version 4 (NetBSD 6/7): RTM_IFINFO 0x14, ifa_msghdr 24 bytes, index @16, RT_ROUNDUP 8
 * (the guide assumed one layout; the NetBSD 6 device proved otherwise, see
 * tests/native/fixtures/iflist/netbsd6-bridge.json). */

#define TC_MAX_LINKS 16
#define TC_MAX_ADDRS 64

struct if_link {
    char name[IFNAMSIZ];
    unsigned index;
    unsigned flags;
};

struct if_addr {
    unsigned owner_index;
    int family;              /* AF_INET or AF_INET6 */
    struct in_addr v4;
    struct in6_addr v6;      /* canonical: fe80 bytes 2-3 zeroed */
    unsigned prefix;
    unsigned scope;          /* fe80: embedded index, else owner_index */
    int link_local;
};

struct if_table {
    struct if_link links[TC_MAX_LINKS];
    size_t link_count;
    struct if_addr addrs[TC_MAX_ADDRS];
    size_t addr_count;
    int truncated;
};

int iflist_parse(const unsigned char *buf, size_t len, struct if_table *out);
int iflist_collect(struct if_table *out);
unsigned iflist_prefix_from_mask(const unsigned char *mask, size_t len);

#endif
