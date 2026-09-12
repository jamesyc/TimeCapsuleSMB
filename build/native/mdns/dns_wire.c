#include "mdns.h"
TC_LOCAL int add_rr_txt_items(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ttl,
                            const char **strings, const uint8_t *lengths, size_t string_count);
TC_LOCAL int add_rr_a(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ipv4_addr, uint32_t ttl);
TC_LOCAL int add_rr_aaaa(uint8_t *buf, size_t *off, size_t cap, const char *owner, const struct in6_addr *ipv6_addr, uint32_t ttl);
int append_bytes(uint8_t *buf, size_t *off, size_t cap, const void *src, size_t len) {
    if (*off + len > cap) {
        return -1;
    }
    memcpy(buf + *off, src, len);
    *off += len;
    return 0;
}

int append_u16(uint8_t *buf, size_t *off, size_t cap, uint16_t value) {
    uint16_t net = htons(value);
    return append_bytes(buf, off, cap, &net, sizeof(net));
}

int append_u32(uint8_t *buf, size_t *off, size_t cap, uint32_t value) {
    uint32_t net = htonl(value);
    return append_bytes(buf, off, cap, &net, sizeof(net));
}

int validate_dns_name(const char *value, const char *field_name) {
    const unsigned char *p;
    size_t label_len = 0;
    size_t total_len;

    if (value == NULL || value[0] == '\0') {
        fprintf(stderr, "%s must not be empty\n", field_name);
        return -1;
    }

    total_len = strlen(value);
    if (total_len >= MAX_NAME) {
        fprintf(stderr, "%s must be %d bytes or fewer\n", field_name, MAX_NAME - 1);
        return -1;
    }

    for (p = (const unsigned char *)value; *p != '\0'; p++) {
        if (*p < 0x20 || *p == 0x7f) {
            fprintf(stderr, "%s contains an invalid control character\n", field_name);
            return -1;
        }
        if (*p == '.') {
            if (label_len == 0) {
                if (*(p + 1) == '\0' && p != (const unsigned char *)value) {
                    return 0;
                }
                fprintf(stderr, "%s contains an empty label\n", field_name);
                return -1;
            }
            if (label_len > MAX_LABEL) {
                fprintf(stderr, "%s contains a label longer than %d bytes\n", field_name, MAX_LABEL);
                return -1;
            }
            label_len = 0;
            continue;
        }
        label_len++;
        if (label_len > MAX_LABEL) {
            fprintf(stderr, "%s contains a label longer than %d bytes\n", field_name, MAX_LABEL);
            return -1;
        }
    }

    if (label_len == 0) {
        if (total_len > 1 && value[total_len - 1] == '.') {
            return 0;
        }
        fprintf(stderr, "%s contains an empty label\n", field_name);
        return -1;
    }

    return 0;
}

int encode_name(uint8_t *buf, size_t *off, size_t cap, const char *name) {
    size_t name_i = 0;
    size_t label_len = 0;
    uint8_t label[MAX_LABEL];

    if (name == NULL || name[0] == '\0') {
        return -1;
    }

    while (name[name_i] != '\0') {
        unsigned char ch = (unsigned char)name[name_i++];
        if (ch == '\\') {
            if (name[name_i] == '\0') {
                return -1;
            }
            ch = (unsigned char)name[name_i++];
        } else if (ch == '.') {
            uint8_t wire_len;
            if (label_len == 0) {
                if (name[name_i] == '\0') {
                    break;
                }
                return -1;
            }
            wire_len = (uint8_t)label_len;
            if (append_bytes(buf, off, cap, &wire_len, 1) != 0 ||
                append_bytes(buf, off, cap, label, label_len) != 0) {
                return -1;
            }
            label_len = 0;
            continue;
        }
        if (label_len >= MAX_LABEL) {
            return -1;
        }
        label[label_len++] = ch;
    }

    if (label_len > 0) {
        uint8_t wire_len = (uint8_t)label_len;
        if (append_bytes(buf, off, cap, &wire_len, 1) != 0 ||
            append_bytes(buf, off, cap, label, label_len) != 0) {
            return -1;
        }
    }

    return append_bytes(buf, off, cap, "\0", 1);
}

int decode_name(const uint8_t *packet, size_t packet_len, size_t *cursor, char *out, size_t out_len) {
    size_t pos = *cursor;
    size_t out_pos = 0;
    int jumped = 0;
    size_t jump_count = 0;
    size_t next_cursor = pos;

    while (pos < packet_len) {
        uint8_t len = packet[pos];

        if (len == 0) {
            if (!jumped) {
                next_cursor = pos + 1;
            }
            if (out_pos == 0) {
                if (out_len < 2) {
                    return -1;
                }
                out[out_pos++] = '.';
            }
            out[out_pos] = '\0';
            *cursor = next_cursor;
            return 0;
        }

        if ((len & 0xC0) == 0xC0) {
            uint16_t ptr;
            if (pos + 1 >= packet_len) {
                return -1;
            }
            ptr = (uint16_t)(((len & 0x3F) << 8) | packet[pos + 1]);
            if (ptr >= packet_len || jump_count++ > 16) {
                return -1;
            }
            if (!jumped) {
                next_cursor = pos + 2;
            }
            pos = ptr;
            jumped = 1;
            continue;
        }

        if (len > 63 || pos + 1 + len > packet_len) {
            return -1;
        }

        if (out_pos != 0) {
            if (out_pos + 1 >= out_len) {
                return -1;
            }
            out[out_pos++] = '.';
        }
        {
            size_t label_i;
            for (label_i = 0; label_i < len; label_i++) {
                unsigned char ch = packet[pos + 1 + label_i];
                if (ch == '.' || ch == '\\') {
                    if (out_pos + 2 >= out_len) {
                        return -1;
                    }
                    out[out_pos++] = '\\';
                    out[out_pos++] = (char)ch;
                } else {
                    if (out_pos + 1 >= out_len) {
                        return -1;
                    }
                    out[out_pos++] = (char)ch;
                }
            }
        }
        pos += 1 + len;
        if (!jumped) {
            next_cursor = pos;
        }
    }

    return -1;
}

int name_equals(const char *a, const char *b) {
    size_t a_len = strlen(a);
    size_t b_len = strlen(b);
    while (a_len > 0 && a[a_len - 1] == '.') {
        a_len--;
    }
    while (b_len > 0 && b[b_len - 1] == '.') {
        b_len--;
    }
    return a_len == b_len && strncasecmp(a, b, a_len) == 0;
}

int append_host_address_records(uint8_t *buf,
                                       size_t *off,
                                       size_t cap,
                                       const char *owner,
                                       const struct link_context *link,
                                       int include_a,
                                       int include_aaaa,
                                       uint32_t ttl,
                                       int *answers) {
    size_t i;

    if (owner == NULL || owner[0] == '\0' || link == NULL) {
        return 0;
    }
    if (include_a) {
        for (i = 0; i < link->ipv4_count; i++) {
            if (add_rr_a(buf, off, cap, owner, link->ipv4[i].addr, ttl) != 0) {
                return -1;
            }
            *answers += 1;
        }
    }
    if (include_aaaa) {
        for (i = 0; i < link->ipv6_count; i++) {
            struct in6_addr canonical;
            if (!link_ipv6_addr_is_samba_bindable(&link->ipv6[i])) {
                continue;
            }
            if (link->ipv6[i].link_local && link->ifindex == 0) {
                continue;
            }
            ipv6_canonicalize_scoped_address(&canonical, &link->ipv6[i].addr);
            if (add_rr_aaaa(buf, off, cap, owner, &canonical, ttl) != 0) {
                return -1;
            }
            *answers += 1;
        }
    }
    return 0;
}

int add_rr_ptr(uint8_t *buf, size_t *off, size_t cap, const char *owner, const char *target, uint32_t ttl) {
    size_t rdlength_pos;
    size_t rdata_start;
    if (encode_name(buf, off, cap, owner) != 0 ||
        append_u16(buf, off, cap, DNS_TYPE_PTR) != 0 ||
        append_u16(buf, off, cap, DNS_CLASS_IN) != 0 ||
        append_u32(buf, off, cap, ttl) != 0) {
        return -1;
    }
    rdlength_pos = *off;
    if (append_u16(buf, off, cap, 0) != 0) {
        return -1;
    }
    rdata_start = *off;
    if (encode_name(buf, off, cap, target) != 0) {
        return -1;
    }
    {
        uint16_t rdlength = htons((uint16_t)(*off - rdata_start));
        memcpy(buf + rdlength_pos, &rdlength, sizeof(rdlength));
    }
    return 0;
}

int add_rr_txt_empty(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ttl) {
    static const uint8_t empty_txt[] = {0x00};
    if (encode_name(buf, off, cap, owner) != 0 ||
        append_u16(buf, off, cap, DNS_TYPE_TXT) != 0 ||
        append_u16(buf, off, cap, DNS_CLASS_IN_UNIQUE) != 0 ||
        append_u32(buf, off, cap, ttl) != 0 ||
        append_u16(buf, off, cap, (uint16_t)sizeof(empty_txt)) != 0 ||
        append_bytes(buf, off, cap, empty_txt, sizeof(empty_txt)) != 0) {
        return -1;
    }
    return 0;
}

TC_LOCAL int add_rr_txt_items(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ttl,
                            const char **strings, const uint8_t *lengths, size_t string_count) {
    size_t rdlength_pos;
    size_t rdata_start;
    size_t i;

    if (encode_name(buf, off, cap, owner) != 0 ||
        append_u16(buf, off, cap, DNS_TYPE_TXT) != 0 ||
        append_u16(buf, off, cap, DNS_CLASS_IN_UNIQUE) != 0 ||
        append_u32(buf, off, cap, ttl) != 0) {
        return -1;
    }

    rdlength_pos = *off;
    if (append_u16(buf, off, cap, 0) != 0) {
        return -1;
    }
    rdata_start = *off;

    for (i = 0; i < string_count; i++) {
        uint8_t len;
        size_t slen = lengths != NULL ? lengths[i] : strlen(strings[i]);
        if (slen > 255) {
            return -1;
        }
        len = (uint8_t)slen;
        if (append_bytes(buf, off, cap, &len, 1) != 0 ||
            append_bytes(buf, off, cap, strings[i], slen) != 0) {
            return -1;
        }
    }

    {
        uint16_t rdlength = htons((uint16_t)(*off - rdata_start));
        memcpy(buf + rdlength_pos, &rdlength, sizeof(rdlength));
    }
    return 0;
}

int add_rr_txt_strings(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ttl,
                              const char **strings, size_t string_count) {
    return add_rr_txt_items(buf, off, cap, owner, ttl, strings, NULL, string_count);
}

int add_rr_srv(uint8_t *buf, size_t *off, size_t cap, const char *owner, const char *target, uint16_t port, uint32_t ttl) {
    size_t rdlength_pos;
    size_t rdata_start;
    if (encode_name(buf, off, cap, owner) != 0 ||
        append_u16(buf, off, cap, DNS_TYPE_SRV) != 0 ||
        append_u16(buf, off, cap, DNS_CLASS_IN_UNIQUE) != 0 ||
        append_u32(buf, off, cap, ttl) != 0) {
        return -1;
    }
    rdlength_pos = *off;
    if (append_u16(buf, off, cap, 0) != 0) {
        return -1;
    }
    rdata_start = *off;
    if (append_u16(buf, off, cap, 0) != 0 ||
        append_u16(buf, off, cap, 0) != 0 ||
        append_u16(buf, off, cap, port) != 0 ||
        encode_name(buf, off, cap, target) != 0) {
        return -1;
    }
    {
        uint16_t rdlength = htons((uint16_t)(*off - rdata_start));
        memcpy(buf + rdlength_pos, &rdlength, sizeof(rdlength));
    }
    return 0;
}

TC_LOCAL int add_rr_a(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ipv4_addr, uint32_t ttl) {
    if (encode_name(buf, off, cap, owner) != 0 ||
        append_u16(buf, off, cap, DNS_TYPE_A) != 0 ||
        append_u16(buf, off, cap, DNS_CLASS_IN_UNIQUE) != 0 ||
        append_u32(buf, off, cap, ttl) != 0 ||
        append_u16(buf, off, cap, 4) != 0 ||
        append_bytes(buf, off, cap, &ipv4_addr, 4) != 0) {
        return -1;
    }
    return 0;
}

TC_LOCAL int add_rr_aaaa(uint8_t *buf, size_t *off, size_t cap, const char *owner, const struct in6_addr *ipv6_addr, uint32_t ttl) {
    if (encode_name(buf, off, cap, owner) != 0 ||
        append_u16(buf, off, cap, DNS_TYPE_AAAA) != 0 ||
        append_u16(buf, off, cap, DNS_CLASS_IN_UNIQUE) != 0 ||
        append_u32(buf, off, cap, ttl) != 0 ||
        append_u16(buf, off, cap, 16) != 0 ||
        append_bytes(buf, off, cap, ipv6_addr->s6_addr, 16) != 0) {
        return -1;
    }
    return 0;
}
