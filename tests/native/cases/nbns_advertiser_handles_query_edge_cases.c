#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);

#include "nbns/nbns.h"

static uint8_t captured[BUF_SIZE];
static size_t captured_len = 0;
static int sendto_call_count = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {
    (void)sockfd;
    (void)flags;
    (void)dest;
    (void)dest_len;

    if (len > sizeof(captured)) {
        errno = EMSGSIZE;
        return -1;
    }
    memcpy(captured, buf, len);
    captured_len = len;
    sendto_call_count++;
    return (ssize_t)len;
}

static void reset_capture(void) {
    memset(captured, 0, sizeof(captured));
    captured_len = 0;
    sendto_call_count = 0;
}

static void put_u16(uint8_t *out, uint16_t value) {
    uint16_t net = htons(value);
    memcpy(out, &net, sizeof(net));
}

static uint16_t get_u16(const uint8_t *buf, size_t off) {
    uint16_t value;
    memcpy(&value, buf + off, sizeof(value));
    return ntohs(value);
}

static size_t append_query_name(uint8_t *out, const char *name, uint8_t suffix) {
    char raw[16];
    size_t i;
    size_t len;
    size_t off = 0;

    memset(raw, ' ', sizeof(raw));
    len = strlen(name);
    if (len > 15) {
        len = 15;
    }
    for (i = 0; i < len; i++) {
        raw[i] = (char)toupper((unsigned char)name[i]);
    }
    raw[15] = (char)suffix;

    out[off++] = 32;
    for (i = 0; i < 16; i++) {
        unsigned char value = (unsigned char)raw[i];
        out[off++] = (uint8_t)('A' + ((value >> 4) & 0x0f));
        out[off++] = (uint8_t)('A' + (value & 0x0f));
    }
    out[off++] = 0;
    return off;
}

static size_t build_query(uint8_t *out,
                          const char *name,
                          uint8_t suffix,
                          uint16_t qtype,
                          uint16_t flags) {
    size_t off;

    memset(out, 0, 256);
    put_u16(out, 0x1337);
    put_u16(out + 2, flags);
    put_u16(out + 4, 1);
    off = 12;
    off += append_query_name(out + off, name, suffix);
    put_u16(out + off, qtype);
    off += 2;
    put_u16(out + off, DNS_CLASS_IN);
    off += 2;
    return off;
}

static int invoke_query(const uint8_t *query, size_t query_len) {
    struct config cfg;
    struct sockaddr_in peer;

    memset(&cfg, 0, sizeof(cfg));
    memcpy(cfg.netbios_name, "TimeCapsule", sizeof("TimeCapsule"));
    cfg.ipv4_addr = inet_addr("192.168.1.217");
    cfg.ttl = 300;

    memset(&peer, 0, sizeof(peer));
    peer.sin_family = AF_INET;
    peer.sin_port = htons(40000);
    peer.sin_addr.s_addr = inet_addr("192.168.1.50");

    return maybe_respond_to_query_addr(
        1,
        &cfg,
        query,
        query_len,
        (const struct sockaddr *)(const void *)&peer,
        sizeof(peer));
}

static int expect_no_response(const uint8_t *query, size_t query_len) {
    reset_capture();
    if (invoke_query(query, query_len) != 0) return 1;
    if (sendto_call_count != 0 || captured_len != 0) return 2;
    return 0;
}

static int expect_negative_response(const uint8_t *query, size_t query_len) {
    size_t qname_len = query_len - 12 - 4;
    size_t off = 12 + qname_len;

    reset_capture();
    if (invoke_query(query, query_len) != 1) return 10;
    if (sendto_call_count != 1) return 11;
    if (captured_len != 12 + qname_len + 10) return 12;
    if (get_u16(captured, 2) != 0x8483) return 13;
    if (get_u16(captured, 4) != 0 || get_u16(captured, 6) != 0) return 14;
    if (memcmp(captured + 12, query + 12, qname_len) != 0) return 15;
    if (get_u16(captured, off) != NB_TYPE_NULL) return 16;
    if (get_u16(captured, off + 2) != DNS_CLASS_IN) return 17;
    if (get_u16(captured, off + 8) != 0) return 18;
    return 0;
}

int main(void) {
    uint8_t query[256];
    size_t query_len;
    int rc;

    query_len = build_query(query, "OtherName", NBNS_SUFFIX_SERVER, NB_TYPE_NB, NBNS_FLAG_BROADCAST);
    rc = expect_no_response(query, query_len);
    if (rc != 0) return rc;

    query_len = build_query(query, "OtherName", NBNS_SUFFIX_SERVER, NB_TYPE_NB, 0);
    rc = expect_negative_response(query, query_len);
    if (rc != 0) return 20 + rc;

    query_len = build_query(query, "TimeCapsule", 0x03, NB_TYPE_NB, 0);
    rc = expect_negative_response(query, query_len);
    if (rc != 0) return 50 + rc;

    query_len = build_query(query, "TimeCapsule", NBNS_SUFFIX_SERVER, 0x0001, 0);
    rc = expect_no_response(query, query_len);
    if (rc != 0) return 80 + rc;

    query_len = build_query(query, "OtherName", NBNS_SUFFIX_SERVER, NB_TYPE_NBSTAT, 0);
    rc = expect_no_response(query, query_len);
    if (rc != 0) return 90 + rc;

    query_len = build_query(query, "TimeCapsule", NBNS_SUFFIX_SERVER, NB_TYPE_NB, 0);
    query[45] = 0xc0;
    query[46] = 0x0c;
    rc = expect_no_response(query, query_len);
    if (rc != 0) return 100 + rc;

    return 0;
}
