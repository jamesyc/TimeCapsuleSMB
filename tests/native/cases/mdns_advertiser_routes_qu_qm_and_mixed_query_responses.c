#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);

#include "mdns/mdns.h"

static unsigned char captured_packets[8][BUF_SIZE];
static size_t captured_lengths[8];
static struct sockaddr_in captured_dests[8];
static size_t captured_count = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {
    (void)sockfd;
    (void)flags;
    if (dest_len != sizeof(struct sockaddr_in)) {
        return -1;
    }
    if (captured_count < 8) {
        memcpy(captured_packets[captured_count], buf, len);
        captured_lengths[captured_count] = len;
        memcpy(&captured_dests[captured_count], dest, sizeof(struct sockaddr_in));
        captured_count++;
    }
    return (ssize_t)len;
}

static void reset_captures(void) {
    memset(captured_packets, 0, sizeof(captured_packets));
    memset(captured_lengths, 0, sizeof(captured_lengths));
    memset(captured_dests, 0, sizeof(captured_dests));
    captured_count = 0;
}

static void configure_base(struct config *cfg) {
    memset(cfg, 0, sizeof(*cfg));
    snprintf(cfg->instance_name, sizeof(cfg->instance_name), "%s", "Alton Time Capsule");
    snprintf(cfg->host_label, sizeof(cfg->host_label), "%s", "alton-time-capsule");
    snprintf(cfg->host_fqdn, sizeof(cfg->host_fqdn), "%s", "alton-time-capsule.local.");
    snprintf(cfg->service_type, sizeof(cfg->service_type), "%s", "_smb._tcp.local.");
    snprintf(cfg->adisk_service_type, sizeof(cfg->adisk_service_type), "%s", "_adisk._tcp.local.");
    snprintf(cfg->device_info_service_type, sizeof(cfg->device_info_service_type), "%s", "_device-info._tcp.local.");
    snprintf(cfg->airport_service_type, sizeof(cfg->airport_service_type), "%s", "_airport._tcp.local.");
    cfg->port = 445;
    cfg->adisk_port = 9;
    cfg->airport_port = 5009;
    cfg->ttl = 120;
}

static void configure_addrs(struct sockaddr_in *mdns_dest, struct sockaddr_in *source) {
    memset(mdns_dest, 0, sizeof(*mdns_dest));
    mdns_dest->sin_family = AF_INET;
    mdns_dest->sin_port = htons(MDNS_PORT);
    mdns_dest->sin_addr.s_addr = inet_addr(MDNS_GROUP);

    memset(source, 0, sizeof(*source));
    source->sin_family = AF_INET;
    source->sin_port = htons(62001);
    source->sin_addr.s_addr = inet_addr("10.0.1.42");
}

static int append_question(unsigned char *packet, size_t *off, const char *qname,
                           unsigned short qtype, unsigned short qclass) {
    return encode_name(packet, off, BUF_SIZE, qname) != 0 ||
           append_u16(packet, off, BUF_SIZE, qtype) != 0 ||
           append_u16(packet, off, BUF_SIZE, qclass) != 0;
}

static size_t make_query(unsigned char *packet, const char *qname, unsigned short qtype, unsigned short qclass) {
    struct dns_header hdr;
    size_t off = sizeof(hdr);
    memset(&hdr, 0, sizeof(hdr));
    hdr.qdcount = htons(1);
    memcpy(packet, &hdr, sizeof(hdr));
    if (append_question(packet, &off, qname, qtype, qclass) != 0) {
        return 0;
    }
    return off;
}

static size_t make_mixed_query(unsigned char *packet, const char *qu_name, const char *qm_name) {
    struct dns_header hdr;
    size_t off = sizeof(hdr);
    memset(&hdr, 0, sizeof(hdr));
    hdr.id = htons(0x1234);
    hdr.qdcount = htons(2);
    memcpy(packet, &hdr, sizeof(hdr));
    if (append_question(packet, &off, qu_name, DNS_TYPE_PTR, DNS_CLASS_IN | DNS_CLASS_CACHE_FLUSH) != 0 ||
        append_question(packet, &off, qm_name, DNS_TYPE_A, DNS_CLASS_IN) != 0) {
        return 0;
    }
    return off;
}

static int skip_response_questions(const unsigned char *packet, size_t packet_len, size_t *cursor) {
    struct dns_header hdr;
    unsigned short i;
    unsigned short qdcount;

    if (packet_len < sizeof(hdr)) {
        return -1;
    }
    memcpy(&hdr, packet, sizeof(hdr));
    qdcount = ntohs(hdr.qdcount);
    *cursor = sizeof(hdr);
    for (i = 0; i < qdcount; i++) {
        char name[MAX_NAME];
        if (decode_name(packet, packet_len, cursor, name, sizeof(name)) != 0 || *cursor + 4 > packet_len) {
            return -1;
        }
        *cursor += 4;
    }
    return 0;
}

static int count_rr_type(const unsigned char *packet, size_t packet_len, unsigned short want_type) {
    struct dns_header hdr;
    size_t cursor;
    unsigned short total_answers;
    int matches = 0;
    unsigned short i;

    memcpy(&hdr, packet, sizeof(hdr));
    total_answers = ntohs(hdr.ancount);
    if (skip_response_questions(packet, packet_len, &cursor) != 0) {
        return -1;
    }
    for (i = 0; i < total_answers; i++) {
        char name[MAX_NAME];
        unsigned short rrtype;
        unsigned short rdlength;

        if (decode_name(packet, packet_len, &cursor, name, sizeof(name)) != 0 || cursor + 10 > packet_len) {
            return -1;
        }
        memcpy(&rrtype, packet + cursor, 2);
        memcpy(&rdlength, packet + cursor + 8, 2);
        cursor += 10;
        rrtype = ntohs(rrtype);
        rdlength = ntohs(rdlength);
        if (cursor + rdlength > packet_len) {
            return -1;
        }
        if (rrtype == want_type) {
            matches++;
        }
        cursor += rdlength;
    }
    return matches;
}

static int packet_has_smb_browse_additionals(const unsigned char *packet, size_t packet_len) {
    return count_rr_type(packet, packet_len, DNS_TYPE_PTR) == 1 &&
           count_rr_type(packet, packet_len, DNS_TYPE_SRV) == 1 &&
           count_rr_type(packet, packet_len, DNS_TYPE_TXT) == 1 &&
           count_rr_type(packet, packet_len, DNS_TYPE_A) == 1;
}

static int legacy_unicast_ttls_and_classes_are_capped(const unsigned char *packet, size_t packet_len) {
    struct dns_header hdr;
    size_t cursor;
    unsigned short total_answers;
    unsigned short i;

    memcpy(&hdr, packet, sizeof(hdr));
    total_answers = ntohs(hdr.ancount);
    if (skip_response_questions(packet, packet_len, &cursor) != 0) {
        return 0;
    }
    for (i = 0; i < total_answers; i++) {
        char name[MAX_NAME];
        unsigned short rrclass;
        unsigned int ttl;
        unsigned short rdlength;

        if (decode_name(packet, packet_len, &cursor, name, sizeof(name)) != 0 || cursor + 10 > packet_len) {
            return 0;
        }
        memcpy(&rrclass, packet + cursor + 2, 2);
        memcpy(&ttl, packet + cursor + 4, 4);
        memcpy(&rdlength, packet + cursor + 8, 2);
        cursor += 10;
        rrclass = ntohs(rrclass);
        ttl = ntohl(ttl);
        rdlength = ntohs(rdlength);
        if ((rrclass & DNS_CLASS_CACHE_FLUSH) != 0 || ttl > LEGACY_UNICAST_TTL_MAX || cursor + rdlength > packet_len) {
            return 0;
        }
        cursor += rdlength;
    }
    return 1;
}

static int legacy_response_repeats_question(const unsigned char *response, size_t response_len,
                                            const unsigned char *query, size_t query_len) {
    struct dns_header hdr;
    size_t question_len = query_len - sizeof(hdr);

    if (response_len < query_len || query_len < sizeof(hdr)) {
        return 0;
    }
    memcpy(&hdr, response, sizeof(hdr));
    if (ntohs(hdr.qdcount) != 1) {
        return 0;
    }
    return memcmp(response + sizeof(hdr), query + sizeof(hdr), question_len) == 0;
}

static int run_route_cases(void) {
    struct config cfg;
    struct link_context response_link;
    struct sockaddr_in mdns_dest;
    struct sockaddr_in source;
    unsigned char query[BUF_SIZE];
    size_t query_len;

    configure_base(&cfg);
    memset(&response_link, 0, sizeof(response_link));
    snprintf(response_link.name, sizeof(response_link.name), "%s", "bridge0");
    response_link.flags = IFF_UP | IFF_RUNNING;
    response_link.ipv4[0].addr = inet_addr("10.0.1.77");
    response_link.ipv4[0].netmask = inet_addr("255.255.255.0");
    response_link.ipv4_count = 1;
    response_link.mdns_ipv4_transport = 1;
    response_link.mdns_ipv4_transport_addr = response_link.ipv4[0].addr;
    configure_addrs(&mdns_dest, &source);

    reset_captures();
    query_len = make_query(query, cfg.service_type, DNS_TYPE_PTR, DNS_CLASS_IN | DNS_CLASS_CACHE_FLUSH);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link) != 0) {
        return 1;
    }
    if (captured_count != 1 ||
        captured_dests[0].sin_addr.s_addr != source.sin_addr.s_addr ||
        captured_dests[0].sin_port != source.sin_port ||
        !packet_has_smb_browse_additionals(captured_packets[0], captured_lengths[0]) ||
        !legacy_response_repeats_question(captured_packets[0], captured_lengths[0], query, query_len) ||
        !legacy_unicast_ttls_and_classes_are_capped(captured_packets[0], captured_lengths[0])) {
        return 2;
    }

    source.sin_port = htons(MDNS_PORT);
    reset_captures();
    query_len = make_query(query, cfg.service_type, DNS_TYPE_PTR, DNS_CLASS_IN);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link) != 0) {
        return 3;
    }
    if (captured_count != 1 ||
        captured_dests[0].sin_addr.s_addr != mdns_dest.sin_addr.s_addr ||
        captured_dests[0].sin_port != mdns_dest.sin_port ||
        !packet_has_smb_browse_additionals(captured_packets[0], captured_lengths[0])) {
        return 4;
    }

    reset_captures();
    query_len = make_query(query, cfg.service_type, DNS_TYPE_PTR, DNS_CLASS_IN | DNS_CLASS_CACHE_FLUSH);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link) != 0) {
        return 13;
    }
    if (captured_count != 2 ||
        captured_dests[0].sin_addr.s_addr != source.sin_addr.s_addr ||
        captured_dests[0].sin_port != source.sin_port ||
        captured_dests[1].sin_addr.s_addr != mdns_dest.sin_addr.s_addr ||
        captured_dests[1].sin_port != mdns_dest.sin_port ||
        !packet_has_smb_browse_additionals(captured_packets[0], captured_lengths[0]) ||
        !packet_has_smb_browse_additionals(captured_packets[1], captured_lengths[1])) {
        return 14;
    }

    source.sin_port = htons(MDNS_PORT);
    reset_captures();
    query_len = make_mixed_query(query, cfg.service_type, cfg.host_fqdn);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link) != 0) {
        return 5;
    }
    if (captured_count != 2 ||
        captured_dests[0].sin_addr.s_addr != source.sin_addr.s_addr ||
        captured_dests[0].sin_port != source.sin_port ||
        captured_dests[1].sin_addr.s_addr != mdns_dest.sin_addr.s_addr ||
        captured_dests[1].sin_port != mdns_dest.sin_port ||
        !packet_has_smb_browse_additionals(captured_packets[0], captured_lengths[0]) ||
        count_rr_type(captured_packets[1], captured_lengths[1], DNS_TYPE_A) != 1) {
        return 6;
    }

    reset_captures();
    query_len = make_query(query, cfg.service_type, DNS_TYPE_PTR, DNS_CLASS_ANY);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link) != 0) {
        return 7;
    }
    if (captured_count != 1 ||
        captured_dests[0].sin_addr.s_addr != mdns_dest.sin_addr.s_addr ||
        !packet_has_smb_browse_additionals(captured_packets[0], captured_lengths[0])) {
        return 8;
    }

    reset_captures();
    query_len = make_query(query, cfg.service_type, DNS_TYPE_ANY, DNS_CLASS_IN);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link) != 0) {
        return 9;
    }
    if (captured_count != 1 ||
        captured_dests[0].sin_addr.s_addr != mdns_dest.sin_addr.s_addr ||
        !packet_has_smb_browse_additionals(captured_packets[0], captured_lengths[0])) {
        return 10;
    }

    reset_captures();
    query_len = make_query(query, cfg.service_type, DNS_TYPE_PTR, DNS_CLASS_CACHE_FLUSH | 2);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link) != 0) {
        return 11;
    }
    if (captured_count != 0) {
        return 12;
    }

    return 0;
}

int main(void) {
    int result = run_route_cases();
    if (result != 0) {
        return result;
    }
    printf("ok\n");
    return 0;
}
