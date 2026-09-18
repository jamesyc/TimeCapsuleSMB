/* Drives iflist_parse() for the fixture and synthetic-layout tests.
 *   iflist_parse <fixture.bin>   dump the parsed table (asserted against the manifest)
 *   iflist_parse --sdk-layout    synthetic RTM_VERSION 3 buffer with a 144-byte if_msghdr
 *   iflist_parse --unnamed       IFINFO without an AF_LINK sockaddr stays unnamed
 *   iflist_parse --truncated     a buffer cut inside a message is rejected
 *   iflist_parse --orphan-addr   NEWADDR whose index has no IFINFO keeps its owner index
 *   iflist_parse --bad-sockaddr  NEWADDR whose inner sockaddr claims more bytes than the message (v3)
 *   iflist_parse --bad-sockaddr6 same, RTM_VERSION 4 layout
 *   iflist_parse --version6      RTM_VERSION 4 (NetBSD 6) synthetic message pair */
#include "common/plan.h"

static size_t put_u16(unsigned char *p, unsigned v) { uint16_t x = (uint16_t)v; memcpy(p, &x, 2); return 2; }
static size_t put_u32(unsigned char *p, unsigned v) { uint32_t x = (uint32_t)v; memcpy(p, &x, 4); return 4; }

static void dump(const struct if_table *t) {
    size_t i;
    char text[INET6_ADDRSTRLEN];
    printf("links=%lu addrs=%lu truncated=%d\n", (unsigned long)t->link_count, (unsigned long)t->addr_count, t->truncated);
    for (i = 0; i < t->link_count; i++) {
        printf("link name=%s index=%u flags=0x%x\n", t->links[i].name, t->links[i].index, t->links[i].flags);
    }
    for (i = 0; i < t->addr_count; i++) {
        const struct if_addr *a = &t->addrs[i];
        printf("addr owner=%u family=%s addr=%s prefix=%u scope=%u link_local=%d\n", a->owner_index,
               a->family == AF_INET ? "inet" : "inet6", addr_text(a, text, sizeof(text)), a->prefix, a->scope, a->link_local);
    }
}

/* if_msghdr with the given header size (if_data follows at 16; the
 * sockaddr_dl follows the header). */
static size_t ifinfo(unsigned char *p, unsigned version, unsigned type, size_t header, unsigned index, unsigned flags,
                     const char *name, const unsigned char *mac, int with_sdl) {
    size_t nlen = strlen(name);
    size_t sdl_len = with_sdl ? 8 + nlen + (mac ? 6 : 0) : 0;
    size_t total = header + sdl_len;
    memset(p, 0, total);
    put_u16(p, (unsigned)total); p[2] = (unsigned char)version; p[3] = (unsigned char)type;
    put_u32(p + 4, 0x20); put_u32(p + 8, flags); put_u16(p + 12, index);
    if (with_sdl) {
        unsigned char *s = p + header;
        s[0] = (unsigned char)sdl_len; s[1] = 18; put_u16(s + 2, index); s[4] = 6; s[5] = (unsigned char)nlen; s[6] = mac ? 6 : 0; s[7] = 0;
        memcpy(s + 8, name, nlen);
        if (mac) memcpy(s + 8 + nlen, mac, 6);
    }
    return total;
}

static size_t round_up(size_t n, size_t unit) { return n < unit ? unit : ((n + unit - 1) / unit) * unit; }

static size_t newaddr4(unsigned char *p, unsigned version, size_t header, size_t unit, unsigned index,
                       const char *addr, unsigned prefix) {
    size_t q = header;
    unsigned char mask[4] = {0, 0, 0, 0};
    unsigned i;
    memset(p, 0, header);
    p[2] = (unsigned char)version; p[3] = 0xc;
    put_u32(p + 4, (1u << 2) | (1u << 5));
    put_u16(p + (version == 3 ? 12 : 16), index);
    /* NetBSD-style short netmask: sa_len covers only significant bytes. */
    for (i = 0; i < prefix; i++) mask[i / 8] |= (unsigned char)(0x80 >> (i % 8));
    p[q] = 8; p[q + 1] = 0; memcpy(p + q + 4, mask, 4); q += round_up(8, unit);
    p[q] = 16; p[q + 1] = 2; inet_pton(AF_INET, addr, p + q + 4); q += round_up(16, unit);
    put_u16(p, (unsigned)q);
    return q;
}

static size_t newaddr6(unsigned char *p, unsigned version, size_t header, size_t unit, unsigned index,
                       const char *addr, unsigned prefix, unsigned embedded_scope) {
    size_t q = header;
    unsigned i;
    memset(p, 0, header);
    p[2] = (unsigned char)version; p[3] = 0xc;
    put_u32(p + 4, (1u << 2) | (1u << 5));
    put_u16(p + (version == 3 ? 12 : 16), index);
    p[q] = 28; p[q + 1] = 24;
    for (i = 0; i < prefix; i++) p[q + 8 + i / 8] |= (unsigned char)(0x80 >> (i % 8));
    q += round_up(28, unit);
    p[q] = 28; p[q + 1] = 24; inet_pton(AF_INET6, addr, p + q + 8);
    if (embedded_scope) { p[q + 10] = (unsigned char)(embedded_scope >> 8); p[q + 11] = (unsigned char)embedded_scope; }
    q += round_up(28, unit);
    put_u16(p, (unsigned)q);
    return q;
}

int main(int argc, char **argv) {
    static unsigned char buf[65536];
    struct if_table table;
    size_t len = 0;
    static const unsigned char mac[6] = {2, 0, 0, 0, 0, 1};
    if (argc != 2) return 2;
    if (!strcmp(argv[1], "--sdk-layout")) {
        /* The SDK's 144-byte if_msghdr followed by the sockaddr_dl, plus the
         * addresses: same parse result as the 152-byte kernel layout. */
        len += ifinfo(buf + len, 3, 0xf, 144, 9, 0xe043, "bridge0", mac, 1);
        len += newaddr4(buf + len, 3, 20, 4, 9, "192.0.2.10", 24);
        len += newaddr6(buf + len, 3, 20, 4, 9, "fe80::ff:fe00:1", 64, 9);
        len += ifinfo(buf + len, 3, 0xf, 152, 10, 0xe002, "bridge1", mac, 1);
    } else if (!strcmp(argv[1], "--unnamed")) {
        len += ifinfo(buf + len, 3, 0xf, 152, 4, 0x8843, "", NULL, 0);
        len += newaddr4(buf + len, 3, 20, 4, 4, "10.0.0.4", 8);
    } else if (!strcmp(argv[1], "--truncated")) {
        len += ifinfo(buf + len, 3, 0xf, 152, 9, 0xe043, "bridge0", mac, 1);
        len -= 5;
    } else if (!strcmp(argv[1], "--bad-sockaddr") || !strcmp(argv[1], "--bad-sockaddr6")) {
        /* Outer msglen intact; the first sockaddr's sa_len runs past the
         * message. Review 2 R6: the table must be incomplete, not a
         * validated subset missing the LAN address. */
        int v6 = argv[1][14] == '6';
        size_t start;
        len += ifinfo(buf + len, v6 ? 4 : 3, v6 ? 0x14 : 0xf, v6 ? 160 : 152, 9, 0xe043, "bridge0", mac, 1);
        start = len;
        len += newaddr4(buf + len, v6 ? 4 : 3, v6 ? 24 : 20, v6 ? 8 : 4, 9, "192.0.2.10", 24);
        buf[start + (v6 ? 24 : 20)] = 255;
        len += newaddr4(buf + len, v6 ? 4 : 3, v6 ? 24 : 20, v6 ? 8 : 4, 9, "10.9.9.9", 8);
    } else if (!strcmp(argv[1], "--orphan-addr")) {
        len += ifinfo(buf + len, 3, 0xf, 152, 9, 0xe043, "bridge0", mac, 1);
        len += newaddr4(buf + len, 3, 20, 4, 42, "172.16.42.1", 24);
    } else if (!strcmp(argv[1], "--version6")) {
        len += ifinfo(buf + len, 4, 0x14, 160, 13, 0xffffe043, "bridge0", mac, 1);
        len += newaddr4(buf + len, 4, 24, 8, 13, "192.0.2.218", 24);
        len += newaddr6(buf + len, 4, 24, 8, 13, "fe80::82ea:96ff:fee6:5868", 64, 13);
        /* NetBSD 6 also emits an AF_LINK RTA_IFA row per interface: not an address. */
        memset(buf + len, 0, 64); buf[len + 2] = 4; buf[len + 3] = 0xc; put_u32(buf + len + 4, (1u << 2) | (1u << 5));
        put_u16(buf + len + 16, 13); buf[len + 24] = 15; buf[len + 32] = 21; buf[len + 33] = 18; put_u16(buf + len, 64); len += 64;
    } else {
        FILE *fp = fopen(argv[1], "rb");
        if (fp == NULL) { perror(argv[1]); return 1; }
        len = fread(buf, 1, sizeof(buf), fp);
        fclose(fp);
    }
    if (iflist_parse(buf, len, &table) != 0) {
        puts("parse=error");
        return 0;
    }
    puts("parse=ok");
    dump(&table);
    return 0;
}
