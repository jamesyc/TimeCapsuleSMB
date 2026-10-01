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

/* An interface that owns no address never takes a role, so only the
 * interfaces that own one must fit; the rest fill whatever room is left.
 * Two NetBSD 6 bridges reported iflist-truncated at the old limit of 16
 * (2026-10-01); which cause hit them is not known. */
#define TC_MAX_LINKS 32
#define TC_MAX_ADDRS 64

/* Why a snapshot is incomplete, in rising precedence: a malformed record
 * makes the counts behind the other two unreliable. */
enum iflist_truncation {
    IFLIST_COMPLETE = 0,
    IFLIST_TRUNC_ADDRS,      /* more addresses than TC_MAX_ADDRS */
    IFLIST_TRUNC_LINKS,      /* more address-owning interfaces than TC_MAX_LINKS */
    IFLIST_TRUNC_SOCKADDR    /* an address row's sockaddr runs past its message */
};

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
    enum iflist_truncation truncation;
    size_t kernel_link_count;     /* RTM_IFINFO rows in the kernel table */
    size_t kernel_addr_count;     /* IPv4/IPv6 address rows in the kernel table */
};

int iflist_parse(const unsigned char *buf, size_t len, struct if_table *out);
#ifdef TC_NATIVE_TEST
/* Facts-file spelling (facts.c): "sockaddr", "links", "addrs" or "none";
 * parsing anything else returns -1. Production code names causes through
 * the plan's reason codes instead. */
const char *iflist_truncation_name(enum iflist_truncation truncation);
int iflist_truncation_from_name(const char *name, enum iflist_truncation *out);
#endif
int iflist_collect(struct if_table *out);
unsigned iflist_prefix_from_mask(const unsigned char *mask, size_t len);

#endif
