#include "plan.h"

enum addr_kind addr4_kind(uint32_t network_order) {
    uint32_t host = ntohl(network_order);
    unsigned first = (host >> 24) & 0xff;
    unsigned second = (host >> 16) & 0xff;

    if (network_order == 0 || host == 0xffffffffU || first == 0 || first >= 224) {
        return ADDR_UNUSABLE;
    }
    if (first == 127) {
        return ADDR_LOOPBACK;
    }
    if (first == 169 && second == 254) {
        return ADDR_LINK_LOCAL;
    }
    if (first == 10 || (first == 172 && second >= 16 && second <= 31) || (first == 192 && second == 168)) {
        return ADDR_PRIVATE;
    }
    return ADDR_GLOBAL;
}

enum addr_kind addr6_kind(const struct in6_addr *addr) {
    static const unsigned char zero[16] = {0};
    static const unsigned char loopback[16] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1};
    const unsigned char *b = addr->s6_addr;

    if (memcmp(b, zero, 16) == 0 || b[0] == 0xff) {
        return ADDR_UNUSABLE;
    }
    if (memcmp(b, loopback, 16) == 0) {
        return ADDR_LOOPBACK;
    }
    if (b[0] == 0xfe && (b[1] & 0xc0) == 0x80) {
        return ADDR_LINK_LOCAL;
    }
    if ((b[0] & 0xfe) == 0xfc) {
        return ADDR_ULA;
    }
    return ADDR_GLOBAL;
}

/* Apple-identical (Q1): link-local IPv4 and scoped fe80 are service
 * addresses; unspecified, loopback, multicast and unscoped fe80 are not.
 * Loopback is added to Samba's bind list separately as 127.0.0.1/8 ::1/128. */
int addr_is_service_address(const struct if_addr *addr) {
    enum addr_kind kind;

    if (addr->family == AF_INET) {
        kind = addr4_kind(addr->v4.s_addr);
        return kind != ADDR_UNUSABLE && kind != ADDR_LOOPBACK;
    }
    if (addr->family == AF_INET6) {
        kind = addr6_kind(&addr->v6);
        if (kind == ADDR_LINK_LOCAL) {
            return addr->scope != 0;
        }
        return kind != ADDR_UNUSABLE && kind != ADDR_LOOPBACK;
    }
    return 0;
}

const char *addr_text(const struct if_addr *addr, char *out, size_t out_len) {
    const void *src = addr->family == AF_INET ? (const void *)&addr->v4 : (const void *)&addr->v6;
    if (inet_ntop(addr->family, src, out, (socklen_t)out_len) == NULL) {
        strncpy(out, "invalid", out_len - 1);
        out[out_len - 1] = '\0';
    }
    return out;
}

int bind_token_ipv4(char *out, size_t out_len, const struct if_addr *addr) {
    char text[INET_ADDRSTRLEN];
    int written;
    unsigned prefix = addr->prefix;

    if (prefix == 0 || prefix > 32) {
        prefix = 32;
    }
    written = snprintf(out, out_len, "%s/%u", addr_text(addr, text, sizeof(text)), prefix);
    return written < 0 || (size_t)written >= out_len ? -1 : 0;
}

/* Samba's IPv6 interface enumeration is IPv4-only, so fe80 tokens must use
 * NetBSD's embedded-scope form (fe80:<index hex>::iid/64), which the kernel
 * accepts on bind (M6/M7). GUA/ULA tokens are plain. */
int bind_token_ipv6(char *out, size_t out_len, const struct if_addr *addr) {
    struct in6_addr scoped = addr->v6;
    char text[INET6_ADDRSTRLEN];
    int written;
    unsigned prefix = addr->prefix;

    if (prefix == 0 || prefix > 128) {
        prefix = addr->link_local ? 64 : 128;
    }
    if (addr->link_local) {
        scoped.s6_addr[2] = (unsigned char)((addr->scope >> 8) & 0xff);
        scoped.s6_addr[3] = (unsigned char)(addr->scope & 0xff);
    }
    if (inet_ntop(AF_INET6, &scoped, text, sizeof(text)) == NULL) {
        return -1;
    }
    written = snprintf(out, out_len, "%s/%u", text, prefix);
    return written < 0 || (size_t)written >= out_len ? -1 : 0;
}
