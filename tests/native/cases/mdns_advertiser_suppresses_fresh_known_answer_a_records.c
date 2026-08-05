#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);

#include "mdns/mdns.h"

static unsigned char captured_packet[BUF_SIZE];
static size_t captured_len = 0;
static size_t captured_count = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {
    (void)sockfd;
    (void)flags;
    (void)dest;
    (void)dest_len;
    memcpy(captured_packet, buf, len);
    captured_len = len;
    captured_count++;
    return (ssize_t)len;
}

static void reset_captures(void) {
    memset(captured_packet, 0, sizeof(captured_packet));
    captured_len = 0;
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
    cfg->ttl = 120;
}

static size_t make_query_with_known_a_pair(unsigned char *packet, const struct config *cfg,
                                           uint32_t ttl, uint32_t first_addr, int include_second,
                                           uint32_t second_addr) {
    struct dns_header hdr;
    size_t off = sizeof(hdr);
    memset(&hdr, 0, sizeof(hdr));
    hdr.qdcount = htons(1);
    hdr.ancount = htons(include_second ? 2 : 1);
    memcpy(packet, &hdr, sizeof(hdr));
    if (encode_name(packet, &off, BUF_SIZE, cfg->host_fqdn) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_TYPE_A) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_CLASS_IN) != 0 ||
        encode_name(packet, &off, BUF_SIZE, cfg->host_fqdn) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_TYPE_A) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_CLASS_IN_UNIQUE) != 0 ||
        append_u32(packet, &off, BUF_SIZE, ttl) != 0 ||
        append_u16(packet, &off, BUF_SIZE, 4) != 0 ||
        append_bytes(packet, &off, BUF_SIZE, &first_addr, 4) != 0) {
        return 0;
    }
    if (include_second &&
        (encode_name(packet, &off, BUF_SIZE, cfg->host_fqdn) != 0 ||
         append_u16(packet, &off, BUF_SIZE, DNS_TYPE_A) != 0 ||
         append_u16(packet, &off, BUF_SIZE, DNS_CLASS_IN_UNIQUE) != 0 ||
         append_u32(packet, &off, BUF_SIZE, ttl) != 0 ||
         append_u16(packet, &off, BUF_SIZE, 4) != 0 ||
         append_bytes(packet, &off, BUF_SIZE, &second_addr, 4) != 0)) {
        return 0;
    }
    return off;
}

static size_t make_query_with_known_a(unsigned char *packet, const struct config *cfg,
                                      uint32_t ttl, uint32_t known_addr) {
    return make_query_with_known_a_pair(packet, cfg, ttl, known_addr, 0, 0);
}

static int count_rr_type(const unsigned char *packet, size_t packet_len, unsigned short want_type) {
    struct dns_header hdr;
    size_t cursor = sizeof(hdr);
    unsigned short total_answers;
    int matches = 0;
    unsigned short i;

    memcpy(&hdr, packet, sizeof(hdr));
    total_answers = ntohs(hdr.ancount);
    for (i = 0; i < ntohs(hdr.qdcount); i++) {
        char qname[MAX_NAME];
        if (decode_name(packet, packet_len, &cursor, qname, sizeof(qname)) != 0 || cursor + 4 > packet_len) {
            return -1;
        }
        cursor += 4;
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

int main(void) {
    struct config cfg;
    struct link_context response_link;
    struct sockaddr_in mdns_dest;
    struct sockaddr_in source;
    unsigned char query[BUF_SIZE];
    size_t query_len;
    uint32_t primary_addr;
    uint32_t link_local_addr;

    configure_base(&cfg);
    primary_addr = inet_addr("10.0.1.77");
    memset(&response_link, 0, sizeof(response_link));
    snprintf(response_link.name, sizeof(response_link.name), "%s", "bridge0");
    response_link.flags = IFF_UP | IFF_RUNNING;
    response_link.ipv4[0].addr = primary_addr;
    response_link.ipv4[0].netmask = inet_addr("255.255.255.0");
    response_link.ipv4_count = 1;
    response_link.mdns_ipv4_transport = 1;
    response_link.mdns_ipv4_transport_addr = primary_addr;
    memset(&mdns_dest, 0, sizeof(mdns_dest));
    mdns_dest.sin_family = AF_INET;
    mdns_dest.sin_port = htons(MDNS_PORT);
    mdns_dest.sin_addr.s_addr = inet_addr(MDNS_GROUP);
    memset(&source, 0, sizeof(source));
    source.sin_family = AF_INET;
    source.sin_port = htons(62001);
    source.sin_addr.s_addr = inet_addr("10.0.1.42");

    reset_captures();
    query_len = make_query_with_known_a(query, &cfg, 100, primary_addr);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link) != 0 ||
        captured_count != 0) {
        return 1;
    }

    link_local_addr = inet_addr("169.254.44.55");
    if (response_link.ipv4_count >= MAX_LINK_IPV4_ADDRS) {
        return 2;
    }
    response_link.ipv4[response_link.ipv4_count].addr = link_local_addr;
    response_link.ipv4[response_link.ipv4_count].netmask = ipv4_link_local_netmask();
    response_link.ipv4_count++;

    reset_captures();
    query_len = make_query_with_known_a(query, &cfg, 100, primary_addr);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link) != 0 ||
        captured_count != 1 ||
        count_rr_type(captured_packet, captured_len, DNS_TYPE_A) != 1) {
        return 3;
    }

    reset_captures();
    query_len = make_query_with_known_a_pair(query, &cfg, 100, primary_addr, 1, link_local_addr);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link) != 0 ||
        captured_count != 0) {
        return 4;
    }

    reset_captures();
    query_len = make_query_with_known_a(query, &cfg, 10, primary_addr);
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link) != 0 ||
        captured_count != 1 ||
        count_rr_type(captured_packet, captured_len, DNS_TYPE_A) != 2) {
        return 5;
    }

    reset_captures();
    query_len = make_query_with_known_a(query, &cfg, 100, inet_addr("10.0.1.88"));
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &response_link) != 0 ||
        captured_count != 1 ||
        count_rr_type(captured_packet, captured_len, DNS_TYPE_A) != 2) {
        return 6;
    }

    printf("ok\n");
    return 0;
}
