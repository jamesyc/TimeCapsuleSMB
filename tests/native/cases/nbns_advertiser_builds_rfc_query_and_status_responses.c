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

static uint32_t get_u32(const uint8_t *buf, size_t off) {
    uint32_t value;
    memcpy(&value, buf + off, sizeof(value));
    return ntohl(value);
}

static size_t append_query_name(uint8_t *out, const char *name, uint8_t suffix, const char *scope) {
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

    if (scope != NULL && scope[0] != '\0') {
        const char *cursor = scope;
        while (*cursor != '\0') {
            const char *dot = strchr(cursor, '.');
            size_t label_len = dot == NULL ? strlen(cursor) : (size_t)(dot - cursor);
            out[off++] = (uint8_t)label_len;
            memcpy(out + off, cursor, label_len);
            off += label_len;
            if (dot == NULL) {
                break;
            }
            cursor = dot + 1;
        }
    }

    out[off++] = 0;
    return off;
}

static size_t build_query(uint8_t *out,
                          const char *name,
                          uint8_t suffix,
                          uint16_t qtype,
                          uint16_t flags,
                          const char *scope) {
    size_t off;

    memset(out, 0, 256);
    put_u16(out, 0x1337);
    put_u16(out + 2, flags);
    put_u16(out + 4, 1);
    off = 12;
    off += append_query_name(out + off, name, suffix, scope);
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
    cfg.ttl = 123;

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

static int expect_positive_nb_response(const uint8_t *query, size_t qname_len) {
    size_t off = 12;
    uint32_t expected_ip = ntohl(inet_addr("192.168.1.217"));

    if (captured_len != 12 + qname_len + 10 + 6) return 10;
    if (get_u16(captured, 0) != 0x1337) return 11;
    if (get_u16(captured, 2) != 0x8480) return 12;
    if (get_u16(captured, 4) != 0) return 13;
    if (get_u16(captured, 6) != 1) return 14;
    if (get_u16(captured, 8) != 0 || get_u16(captured, 10) != 0) return 15;
    if (memcmp(captured + off, query + 12, qname_len) != 0) return 16;
    off += qname_len;
    if (get_u16(captured, off) != NB_TYPE_NB) return 17;
    if (get_u16(captured, off + 2) != DNS_CLASS_IN) return 18;
    if (get_u32(captured, off + 4) != 123) return 19;
    if (get_u16(captured, off + 8) != 6) return 20;
    if (get_u16(captured, off + 10) != 0) return 21;
    if (get_u32(captured, off + 12) != expected_ip) return 22;
    return 0;
}

static int expect_nbstat_response(const uint8_t *query, size_t qname_len) {
    size_t off = 12;
    size_t rdata_off;
    size_t stats_off;
    size_t i;

    if (captured_len != 12 + qname_len + 10 + 83) return 30;
    if (get_u16(captured, 0) != 0x1337) return 31;
    if (get_u16(captured, 2) != 0x8400) return 32;
    if (get_u16(captured, 4) != 0 || get_u16(captured, 6) != 1) return 33;
    if (get_u16(captured, 8) != 0 || get_u16(captured, 10) != 0) return 34;
    if (memcmp(captured + off, query + 12, qname_len) != 0) return 35;
    off += qname_len;
    if (get_u16(captured, off) != NB_TYPE_NBSTAT) return 36;
    if (get_u16(captured, off + 2) != DNS_CLASS_IN) return 37;
    if (get_u32(captured, off + 4) != 0) return 38;
    if (get_u16(captured, off + 8) != 83) return 39;
    rdata_off = off + 10;
    if (captured[rdata_off] != 2) return 40;
    if (memcmp(captured + rdata_off + 1, "TIMECAPSULE    ", 15) != 0) return 41;
    if (captured[rdata_off + 16] != NBNS_SUFFIX_WORKSTATION) return 42;
    if (get_u16(captured, rdata_off + 17) != NBNS_NAME_FLAGS_ACTIVE) return 43;
    if (memcmp(captured + rdata_off + 19, "TIMECAPSULE    ", 15) != 0) return 44;
    if (captured[rdata_off + 34] != NBNS_SUFFIX_SERVER) return 45;
    if (get_u16(captured, rdata_off + 35) != NBNS_NAME_FLAGS_ACTIVE) return 46;
    stats_off = rdata_off + 1 + (NBNS_NODE_STATUS_NAME_COUNT * 18);
    for (i = 0; i < NBNS_NODE_STATUS_STATS_LEN; i++) {
        if (captured[stats_off + i] != 0) return 47;
    }
    return 0;
}

int main(void) {
    uint8_t query[256];
    size_t query_len;
    size_t qname_len;
    int rc;

    query_len = build_query(query, "TimeCapsule", NBNS_SUFFIX_SERVER, NB_TYPE_NB, 0x0110, NULL);
    qname_len = query_len - 12 - 4;
    reset_capture();
    if (invoke_query(query, query_len) != 1 || sendto_call_count != 1) return 1;
    rc = expect_positive_nb_response(query, qname_len);
    if (rc != 0) return rc;

    query_len = build_query(query, "TimeCapsule", NBNS_SUFFIX_WORKSTATION, NB_TYPE_NB, 0, "office.local");
    qname_len = query_len - 12 - 4;
    reset_capture();
    if (invoke_query(query, query_len) != 1 || sendto_call_count != 1) return 2;
    rc = expect_positive_nb_response(query, qname_len);
    if (rc != 0) return 100 + rc;

    query_len = build_query(query, "*", NBNS_SUFFIX_WORKSTATION, NB_TYPE_NBSTAT, 0, NULL);
    qname_len = query_len - 12 - 4;
    reset_capture();
    if (invoke_query(query, query_len) != 1 || sendto_call_count != 1) return 3;
    rc = expect_nbstat_response(query, qname_len);
    if (rc != 0) return 200 + rc;

    query_len = build_query(query, "*", NBNS_SUFFIX_WORKSTATION, NB_TYPE_NBSTAT, 0, "office.local");
    qname_len = query_len - 12 - 4;
    reset_capture();
    if (invoke_query(query, query_len) != 1 || sendto_call_count != 1) return 4;
    rc = expect_nbstat_response(query, qname_len);
    if (rc != 0) return 300 + rc;

    return 0;
}
