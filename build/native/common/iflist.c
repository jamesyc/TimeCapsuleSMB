#include "iflist.h"
#if defined(__NetBSD__) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__DragonFly__)
#include <sys/sysctl.h>
#include <net/route.h>
#define TC_HAVE_RT_IFLIST 1
#endif

#define TC_RTM_NEWADDR 0xc
#define TC_AF_LINK_WIRE 18
#define TC_AF_INET_WIRE 2
#define TC_AF_INET6_WIRE 24
#define TC_RTA_NETMASK_BIT 2
#define TC_RTA_IFA_BIT 5

struct iflist_layout {
    unsigned ifinfo_type;
    size_t ifa_header;
    size_t ifam_index_offset;
    size_t roundup;
};

/* Integer fields are the kernel's native byte order; the parser runs on the
 * same machine (or, in tests, on a little-endian host with little-endian
 * fixtures), so plain memcpy is the correct decode. */
static unsigned read_u16(const unsigned char *p) {
    uint16_t value;
    memcpy(&value, p, sizeof(value));
    return value;
}

static unsigned read_u32(const unsigned char *p) {
    uint32_t value;
    memcpy(&value, p, sizeof(value));
    return value;
}

static int layout_for_version(unsigned version, struct iflist_layout *out) {
    if (version == 3) {
        out->ifinfo_type = 0xf;
        out->ifa_header = 20;
        out->ifam_index_offset = 12;
        out->roundup = 4;
        return 0;
    }
    if (version == 4) {
        out->ifinfo_type = 0x14;
        out->ifa_header = 24;
        out->ifam_index_offset = 16;
        out->roundup = 8;
        return 0;
    }
    return -1;
}

unsigned iflist_prefix_from_mask(const unsigned char *mask, size_t len) {
    unsigned prefix = 0;
    size_t i;
    for (i = 0; i < len; i++) {
        int bit;
        for (bit = 7; bit >= 0; bit--) {
            if ((mask[i] >> bit) & 1) {
                prefix++;
            } else {
                return prefix;
            }
        }
    }
    return prefix;
}

static void parse_ifinfo(const unsigned char *msg, size_t msglen, struct if_table *out) {
    struct if_link *link;
    unsigned index = read_u16(msg + 12);
    size_t p;

    if (out->link_count >= TC_MAX_LINKS) {
        out->truncated = 1;
        return;
    }
    link = &out->links[out->link_count];
    memset(link, 0, sizeof(*link));
    link->index = index;
    link->flags = read_u32(msg + 8);
    /* The sockaddr_dl follows if_data, whose size we must not assume. Scan
     * for a plausible sockaddr_dl carrying this interface's index; if none
     * matches, keep the link unnamed rather than invent a name. */
    for (p = 16; p + 8 <= msglen; p++) {
        size_t sdl_len = msg[p];
        size_t nlen, alen;
        if (msg[p + 1] != TC_AF_LINK_WIRE || read_u16(msg + p + 2) != index) {
            continue;
        }
        if (sdl_len < 8 || sdl_len > msglen - p) {
            continue;
        }
        nlen = msg[p + 5];
        alen = msg[p + 6];
        if (8 + nlen + alen > sdl_len) {
            continue;
        }
        if (nlen >= sizeof(link->name)) {
            nlen = sizeof(link->name) - 1;
        }
        memcpy(link->name, msg + p + 8, nlen);
        link->name[nlen] = '\0';
        break;
    }
    out->link_count++;
}

static void parse_newaddr(const unsigned char *msg, size_t msglen, const struct iflist_layout *lay, struct if_table *out) {
    unsigned index = read_u16(msg + lay->ifam_index_offset);
    unsigned rta = read_u32(msg + 4);
    size_t p = lay->ifa_header;
    struct if_addr addr;
    int have_addr = 0;
    int have_mask = 0;
    unsigned prefix = 0;
    int bit;

    memset(&addr, 0, sizeof(addr));
    for (bit = 0; bit < 8 && p + 2 <= msglen; bit++) {
        size_t sa_len, family, consumed;
        if (!(rta & (1u << bit))) {
            continue;
        }
        sa_len = msg[p];
        family = msg[p + 1];
        consumed = sa_len == 0 ? lay->roundup : ((sa_len + lay->roundup - 1) / lay->roundup) * lay->roundup;
        if (consumed < lay->roundup) {
            consumed = lay->roundup;
        }
        if (sa_len > msglen - p) {
            /* A sockaddr that runs past its message is a malformed record,
             * not an absent address: the table is incomplete and the plan
             * must not validate on it (review 2, R6). */
            out->truncated = 1;
            return;
        }
        if (bit == TC_RTA_NETMASK_BIT) {
            /* NetBSD writes netmasks with sa_family 0 and a short sa_len
             * covering only the significant bytes. */
            if (family == TC_AF_INET6_WIRE && sa_len >= 24) {
                prefix = iflist_prefix_from_mask(msg + p + 8, 16);
            } else if (sa_len > 4 && sa_len <= 8) {
                prefix = iflist_prefix_from_mask(msg + p + 4, sa_len - 4);
            } else if (sa_len > 8) {
                prefix = iflist_prefix_from_mask(msg + p + 8, sa_len - 8 > 16 ? 16 : sa_len - 8);
            } else {
                prefix = 0;
            }
            have_mask = 1;
        } else if (bit == TC_RTA_IFA_BIT) {
            if (family == TC_AF_INET_WIRE && sa_len >= 8) {
                addr.family = AF_INET;
                memcpy(&addr.v4, msg + p + 4, 4);
                have_addr = 1;
            } else if (family == TC_AF_INET6_WIRE && sa_len >= 24) {
                addr.family = AF_INET6;
                memcpy(&addr.v6, msg + p + 8, 16);
                have_addr = 1;
            }
            /* AF_LINK rows (NetBSD 6 emits one per interface) and unknown
             * families are not addresses. */
        }
        p += consumed;
    }
    if (!have_addr) {
        return;
    }
    if (out->addr_count >= TC_MAX_ADDRS) {
        out->truncated = 1;
        return;
    }
    addr.owner_index = index;
    addr.scope = index;
    addr.prefix = have_mask ? prefix : (addr.family == AF_INET ? 32u : 128u);
    if (addr.family == AF_INET6 && addr.v6.s6_addr[0] == 0xfe && (addr.v6.s6_addr[1] & 0xc0) == 0x80) {
        unsigned embedded = ((unsigned)addr.v6.s6_addr[2] << 8) | addr.v6.s6_addr[3];
        addr.link_local = 1;
        if (embedded != 0) {
            addr.scope = embedded;
        }
        addr.v6.s6_addr[2] = 0;
        addr.v6.s6_addr[3] = 0;
    }
    out->addrs[out->addr_count++] = addr;
}

int iflist_parse(const unsigned char *buf, size_t len, struct if_table *out) {
    size_t p = 0;

    memset(out, 0, sizeof(*out));
    while (p < len) {
        size_t msglen;
        unsigned version, type;
        struct iflist_layout lay;

        if (len - p < 4) {
            return -1;
        }
        msglen = read_u16(buf + p);
        version = buf[p + 2];
        type = buf[p + 3];
        if (msglen < 4 || msglen > len - p) {
            return -1;
        }
        if (layout_for_version(version, &lay) != 0) {
            return -1;
        }
        if (type == lay.ifinfo_type) {
            if (msglen < 16) {
                return -1;
            }
            parse_ifinfo(buf + p, msglen, out);
        } else if (type == TC_RTM_NEWADDR) {
            if (msglen < lay.ifa_header) {
                return -1;
            }
            parse_newaddr(buf + p, msglen, &lay, out);
        }
        /* RTM_OIFINFO, RTM_IFANNOUNCE and anything else: skip by msglen. */
        p += msglen;
    }
    return 0;
}

int iflist_collect(struct if_table *out) {
#ifdef TC_HAVE_RT_IFLIST
    int mib[6];
    size_t len = 0;
    unsigned char *buf;
    int rc;
    int attempt;

    memset(out, 0, sizeof(*out));
    mib[0] = CTL_NET;
    mib[1] = PF_ROUTE;
    mib[2] = 0;
    mib[3] = 0;
    mib[4] = NET_RT_IFLIST;
    mib[5] = 0;
    /* The table can grow between the size query and the read; retry a few
     * times with headroom instead of publishing a truncated snapshot. */
    for (attempt = 0; attempt < 4; attempt++) {
        if (sysctl(mib, 6, NULL, &len, NULL, 0) < 0) {
            return -1;
        }
        len += len / 4 + 256;
        buf = malloc(len);
        if (buf == NULL) {
            return -1;
        }
        if (sysctl(mib, 6, buf, &len, NULL, 0) == 0) {
            rc = iflist_parse(buf, len, out);
            free(buf);
            return rc;
        }
        free(buf);
        if (errno != ENOMEM) {
            return -1;
        }
    }
    return -1;
#else
    memset(out, 0, sizeof(*out));
    errno = ENOSYS;
    return -1;
#endif
}
