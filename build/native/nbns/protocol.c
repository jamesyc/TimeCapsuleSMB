#include "nbns.h"
TC_LOCAL void normalize_netbios_name(char out[16], const char *name);
TC_LOCAL int decode_netbios_question_name(const uint8_t *encoded, size_t encoded_len, char out[16], uint8_t *suffix);
TC_LOCAL int names_match(const char configured[16], const char queried[16]);
TC_LOCAL int name_is_wildcard(const char queried[16]);
TC_LOCAL int parse_question_name(const uint8_t *buf,
                               size_t len,
                               size_t question_name_off,
                               char out[16],
                               uint8_t *suffix,
                               size_t *question_name_end_off);
TC_LOCAL int build_resource_response(uint8_t *out,
                                   size_t out_len,
                                   const uint8_t *request,
                                   size_t request_len,
                                   size_t question_name_off,
                                   size_t question_name_end_off,
                                   uint16_t response_flags,
                                   uint16_t answer_count,
                                   uint16_t rr_type_value,
                                   uint32_t ttl,
                                   const uint8_t *rdata,
                                   uint16_t rdata_len);
TC_LOCAL int build_positive_response(uint8_t *out,
                                   size_t out_len,
                                   const uint8_t *request,
                                   size_t request_len,
                                   size_t question_name_off,
                                   size_t question_name_end_off,
                                   uint32_t ttl,
                                   uint32_t ipv4_addr);
TC_LOCAL int build_negative_query_response(uint8_t *out,
                                         size_t out_len,
                                         const uint8_t *request,
                                         size_t request_len,
                                         size_t question_name_off,
                                         size_t question_name_end_off);
TC_LOCAL void append_node_status_name(uint8_t *out, const char normalized_name[16], uint8_t suffix);
TC_LOCAL int build_node_status_response(uint8_t *out,
                                      size_t out_len,
                                      const uint8_t *request,
                                      size_t request_len,
                                      size_t question_name_off,
                                      size_t question_name_end_off,
                                      const char *netbios_name);
TC_LOCAL void normalize_netbios_name(char out[16], const char *name) {
    size_t i;
    size_t len = strlen(name);

    for (i = 0; i < 15; i++) {
        if (i < len && name[i] != '\0') {
            out[i] = (char)toupper((unsigned char)name[i]);
        } else {
            out[i] = ' ';
        }
    }
    out[15] = '\0';
}

int validate_netbios_name(const char *name) {
    size_t i;
    size_t len;

    if (name == NULL || name[0] == '\0') {
        fprintf(stderr, "netbios name must not be empty\n");
        return -1;
    }

    len = strlen(name);
    if (len > 15) {
        fprintf(stderr, "netbios name must be 15 bytes or fewer\n");
        return -1;
    }

    for (i = 0; i < len; i++) {
        unsigned char ch = (unsigned char)name[i];
        if (ch < 0x20 || ch == 0x7f) {
            fprintf(stderr, "netbios name contains an invalid control character\n");
            return -1;
        }
    }

    return 0;
}

TC_LOCAL int decode_netbios_question_name(const uint8_t *encoded, size_t encoded_len, char out[16], uint8_t *suffix) {
    size_t i;

    if (encoded_len != 32) {
        return -1;
    }

    for (i = 0; i < 16; i++) {
        uint8_t hi;
        uint8_t lo;
        uint8_t value;

        if (encoded[i * 2] < 'A' || encoded[i * 2] > 'P' || encoded[i * 2 + 1] < 'A' || encoded[i * 2 + 1] > 'P') {
            return -1;
        }

        hi = (uint8_t)(encoded[i * 2] - 'A');
        lo = (uint8_t)(encoded[i * 2 + 1] - 'A');
        value = (uint8_t)((hi << 4) | lo);

        if (i < 15) {
            out[i] = (char)value;
        } else {
            *suffix = value;
        }
    }

    out[15] = '\0';
    return 0;
}

TC_LOCAL int names_match(const char configured[16], const char queried[16]) {
    size_t i;

    for (i = 0; i < 15; i++) {
        if ((unsigned char)configured[i] != (unsigned char)toupper((unsigned char)queried[i])) {
            return 0;
        }
    }

    return 1;
}

TC_LOCAL int name_is_wildcard(const char queried[16]) {
    size_t i;

    if (queried[0] != '*') {
        return 0;
    }
    for (i = 1; i < 15; i++) {
        if (queried[i] != ' ') {
            return 0;
        }
    }
    return 1;
}

TC_LOCAL int parse_question_name(const uint8_t *buf,
                               size_t len,
                               size_t question_name_off,
                               char out[16],
                               uint8_t *suffix,
                               size_t *question_name_end_off) {
    size_t off = question_name_off;

    if (off >= len || buf[off] != 32) {
        return -1;
    }
    off++;

    if (off + 32 > len) {
        return -1;
    }
    if (decode_netbios_question_name(buf + off, 32, out, suffix) != 0) {
        return -1;
    }
    off += 32;

    while (off < len) {
        uint8_t label_len = buf[off++];
        if (label_len == 0) {
            *question_name_end_off = off;
            return 0;
        }
        if ((label_len & 0xC0) != 0 || label_len > 63 || off + label_len > len) {
            return -1;
        }
        off += label_len;
    }

    return -1;
}

TC_LOCAL int build_resource_response(uint8_t *out,
                                   size_t out_len,
                                   const uint8_t *request,
                                   size_t request_len,
                                   size_t question_name_off,
                                   size_t question_name_end_off,
                                   uint16_t response_flags,
                                   uint16_t answer_count,
                                   uint16_t rr_type_value,
                                   uint32_t ttl,
                                   const uint8_t *rdata,
                                   uint16_t rdata_len) {
    struct nbns_header header;
    size_t off = 0;
    uint16_t rr_class = htons(DNS_CLASS_IN);
    uint16_t rr_type = htons(rr_type_value);
    uint32_t ttl_net = htonl(ttl);
    uint16_t rdlength = htons(rdata_len);
    size_t rr_name_len;

    if (request_len < sizeof(header) ||
        question_name_end_off > request_len ||
        question_name_off >= question_name_end_off) {
        return -1;
    }

    rr_name_len = question_name_end_off - question_name_off;
    memcpy(&header, request, sizeof(header));
    header.flags = htons(response_flags);
    header.qdcount = 0;
    header.ancount = htons(answer_count);
    header.nscount = 0;
    header.arcount = 0;

    if (off + sizeof(header) > out_len) {
        return -1;
    }
    memcpy(out + off, &header, sizeof(header));
    off += sizeof(header);

    if (off + rr_name_len > out_len) {
        return -1;
    }
    memcpy(out + off, request + question_name_off, rr_name_len);
    off += rr_name_len;

    if (off + sizeof(rr_type) + sizeof(rr_class) + sizeof(ttl_net) + sizeof(rdlength) + rdata_len > out_len) {
        return -1;
    }

    memcpy(out + off, &rr_type, sizeof(rr_type));
    off += sizeof(rr_type);
    memcpy(out + off, &rr_class, sizeof(rr_class));
    off += sizeof(rr_class);
    memcpy(out + off, &ttl_net, sizeof(ttl_net));
    off += sizeof(ttl_net);
    memcpy(out + off, &rdlength, sizeof(rdlength));
    off += sizeof(rdlength);
    if (rdata_len > 0) {
        memcpy(out + off, rdata, rdata_len);
        off += rdata_len;
    }

    return (int)off;
}

TC_LOCAL int build_positive_response(uint8_t *out,
                                   size_t out_len,
                                   const uint8_t *request,
                                   size_t request_len,
                                   size_t question_name_off,
                                   size_t question_name_end_off,
                                   uint32_t ttl,
                                   uint32_t ipv4_addr) {
    uint8_t rdata[6];
    uint16_t nb_flags = htons(0x0000);

    memcpy(rdata, &nb_flags, sizeof(nb_flags));
    memcpy(rdata + sizeof(nb_flags), &ipv4_addr, sizeof(ipv4_addr));

    return build_resource_response(
        out,
        out_len,
        request,
        request_len,
        question_name_off,
        question_name_end_off,
        (uint16_t)(NBNS_FLAG_RESPONSE | NBNS_FLAG_AUTHORITATIVE | NBNS_FLAG_RECURSION_AVAILABLE | NBNS_RCODE_POSITIVE),
        1,
        NB_TYPE_NB,
        ttl,
        rdata,
        sizeof(rdata));
}

TC_LOCAL int build_negative_query_response(uint8_t *out,
                                         size_t out_len,
                                         const uint8_t *request,
                                         size_t request_len,
                                         size_t question_name_off,
                                         size_t question_name_end_off) {
    return build_resource_response(
        out,
        out_len,
        request,
        request_len,
        question_name_off,
        question_name_end_off,
        (uint16_t)(NBNS_FLAG_RESPONSE | NBNS_FLAG_AUTHORITATIVE | NBNS_FLAG_RECURSION_AVAILABLE | NBNS_RCODE_NAME_ERROR),
        0,
        NB_TYPE_NULL,
        0,
        NULL,
        0);
}

TC_LOCAL void append_node_status_name(uint8_t *out, const char normalized_name[16], uint8_t suffix) {
    uint16_t name_flags = htons(NBNS_NAME_FLAGS_ACTIVE);

    memcpy(out, normalized_name, 15);
    out[15] = suffix;
    memcpy(out + 16, &name_flags, sizeof(name_flags));
}

TC_LOCAL int build_node_status_response(uint8_t *out,
                                      size_t out_len,
                                      const uint8_t *request,
                                      size_t request_len,
                                      size_t question_name_off,
                                      size_t question_name_end_off,
                                      const char *netbios_name) {
    uint8_t rdata[1 + (NBNS_NODE_STATUS_NAME_COUNT * 18) + NBNS_NODE_STATUS_STATS_LEN];
    char normalized_name[16];

    memset(rdata, 0, sizeof(rdata));
    normalize_netbios_name(normalized_name, netbios_name);
    rdata[0] = NBNS_NODE_STATUS_NAME_COUNT;
    append_node_status_name(rdata + 1, normalized_name, NBNS_SUFFIX_WORKSTATION);
    append_node_status_name(rdata + 1 + 18, normalized_name, NBNS_SUFFIX_SERVER);

    return build_resource_response(
        out,
        out_len,
        request,
        request_len,
        question_name_off,
        question_name_end_off,
        (uint16_t)(NBNS_FLAG_RESPONSE | NBNS_FLAG_AUTHORITATIVE | NBNS_RCODE_POSITIVE),
        1,
        NB_TYPE_NBSTAT,
        0,
        rdata,
        sizeof(rdata));
}

int maybe_respond_to_query_addr(int sock,
                                       const struct config *cfg,
                                       const uint8_t *buf,
                                       size_t len,
                                       const struct sockaddr *peer,
                                       socklen_t peer_len) {
    struct nbns_header header;
    uint16_t flags;
    uint16_t qtype;
    uint16_t qclass;
    uint8_t response[BUF_SIZE];
    char normalized_name[16];
    char queried_name[16];
    uint8_t suffix = 0;
    size_t off;
    size_t question_name_off;
    size_t question_name_end_off;
    int response_len;

    if (len < sizeof(header)) {
        return 0;
    }

    memcpy(&header, buf, sizeof(header));
    flags = ntohs(header.flags);

    if ((flags & NBNS_FLAG_RESPONSE) != 0) {
        return 0;
    }

    if ((flags & 0x7800) != 0) {
        return 0;
    }

    if (ntohs(header.qdcount) != 1) {
        return 0;
    }

    off = sizeof(header);
    if (off >= len) {
        return 0;
    }

    question_name_off = off;
    if (parse_question_name(buf, len, question_name_off, queried_name, &suffix, &question_name_end_off) != 0) {
        return 0;
    }
    off = question_name_end_off;

    if (off + 2 + 2 > len) {
        return 0;
    }
    memcpy(&qtype, buf + off, sizeof(qtype));
    off += sizeof(qtype);
    memcpy(&qclass, buf + off, sizeof(qclass));

    if (ntohs(qclass) != DNS_CLASS_IN) {
        return 0;
    }

    normalize_netbios_name(normalized_name, cfg->netbios_name);

    if (ntohs(qtype) == NB_TYPE_NBSTAT) {
        if (!names_match(normalized_name, queried_name) && !name_is_wildcard(queried_name)) {
            return 0;
        }
        response_len = build_node_status_response(
            response,
            sizeof(response),
            buf,
            len,
            question_name_off,
            question_name_end_off,
            cfg->netbios_name);
        if (response_len < 0) {
            return 0;
        }
        if (sendto_retry(sock, response, (size_t)response_len, 0, (const struct sockaddr *)peer, peer_len) < 0) {
            perror("sendto");
        }
        return 1;
    }

    if (ntohs(qtype) != NB_TYPE_NB) {
        return 0;
    }

    if (suffix != NBNS_SUFFIX_WORKSTATION && suffix != NBNS_SUFFIX_SERVER) {
        if ((flags & NBNS_FLAG_BROADCAST) == 0) {
            response_len = build_negative_query_response(response, sizeof(response), buf, len, question_name_off, question_name_end_off);
            if (response_len >= 0 &&
                sendto_retry(sock, response, (size_t)response_len, 0, (const struct sockaddr *)peer, peer_len) < 0) {
                perror("sendto");
            }
            return response_len >= 0 ? 1 : 0;
        }
        return 0;
    }

    if (!names_match(normalized_name, queried_name)) {
        if ((flags & NBNS_FLAG_BROADCAST) == 0) {
            response_len = build_negative_query_response(response, sizeof(response), buf, len, question_name_off, question_name_end_off);
            if (response_len >= 0 &&
                sendto_retry(sock, response, (size_t)response_len, 0, (const struct sockaddr *)peer, peer_len) < 0) {
                perror("sendto");
            }
            return response_len >= 0 ? 1 : 0;
        }
        return 0;
    }

    response_len = build_positive_response(
        response,
        sizeof(response),
        buf,
        len,
        question_name_off,
        question_name_end_off,
        cfg->ttl,
        cfg->ipv4_addr);
    if (response_len < 0) {
        return 0;
    }

    if (sendto_retry(sock, response, (size_t)response_len, 0, (const struct sockaddr *)peer, peer_len) < 0) {
        perror("sendto");
    }

    return 1;
}
