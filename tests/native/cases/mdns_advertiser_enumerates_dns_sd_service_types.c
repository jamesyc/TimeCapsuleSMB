#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);

#include "mdns/mdns.h"

static unsigned char captured[BUF_SIZE];
static size_t captured_len = 0;
static struct sockaddr_in captured_dest;
static size_t captured_count = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {
    (void)sockfd;
    (void)flags;
    if (dest_len != sizeof(struct sockaddr_in) || len > sizeof(captured)) {
        return -1;
    }
    memcpy(captured, buf, len);
    captured_len = len;
    memcpy(&captured_dest, dest, sizeof(captured_dest));
    captured_count++;
    return (ssize_t)len;
}

static void reset_capture(void) {
    memset(captured, 0, sizeof(captured));
    captured_len = 0;
    memset(&captured_dest, 0, sizeof(captured_dest));
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
    snprintf(cfg->device_model, sizeof(cfg->device_model), "%s", "TimeCapsule8,119");
    snprintf(cfg->airport_wama, sizeof(cfg->airport_wama), "%s", "80:EA:96:E6:58:68");
    snprintf(cfg->afp_service_type, sizeof(cfg->afp_service_type), "%s", AFP_SERVICE_TYPE);
    snprintf(cfg->riousbprint_instance_name, sizeof(cfg->riousbprint_instance_name), "%s", "USB Printer");
    cfg->adisk_disks.count = 1;
    cfg->advertise_afp = 1;
    cfg->port = 445;
    cfg->adisk_port = 9;
    cfg->airport_port = 5009;
    cfg->afp_port = AFP_DEFAULT_PORT;
    cfg->riousbprint_port = RIOUSBPRINT_DEFAULT_PORT;
    cfg->pdl_datastream_port = PDL_DATASTREAM_DEFAULT_PORT;
    cfg->ttl = 120;
}

static void configure_link(struct link_context *link, uint32_t ipv4_addr) {
    memset(link, 0, sizeof(*link));
    snprintf(link->name, sizeof(link->name), "%s", "bridge0");
    link->flags = IFF_UP | IFF_RUNNING;
    link->ipv4[0].addr = ipv4_addr;
    link->ipv4[0].netmask = inet_addr("255.255.255.0");
    link->ipv4_count = 1;
    link->mdns_ipv4_transport = 1;
    link->mdns_ipv4_transport_addr = ipv4_addr;
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

static size_t make_query(unsigned char *packet, const char *qname, unsigned short qtype) {
    struct dns_header hdr;
    size_t off = sizeof(hdr);

    memset(&hdr, 0, sizeof(hdr));
    hdr.id = htons(0x4444);
    hdr.qdcount = htons(1);
    memcpy(packet, &hdr, sizeof(hdr));
    if (encode_name(packet, &off, BUF_SIZE, qname) != 0 ||
        append_u16(packet, &off, BUF_SIZE, qtype) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_CLASS_IN) != 0) {
        return 0;
    }
    return off;
}

static int skip_questions(size_t *cursor) {
    struct dns_header hdr;
    unsigned short i;

    if (captured_len < sizeof(hdr)) {
        return -1;
    }
    memcpy(&hdr, captured, sizeof(hdr));
    *cursor = sizeof(hdr);
    for (i = 0; i < ntohs(hdr.qdcount); i++) {
        char qname[MAX_NAME];
        if (decode_name(captured, captured_len, cursor, qname, sizeof(qname)) != 0 ||
            *cursor + 4 > captured_len) {
            return -1;
        }
        *cursor += 4;
    }
    return 0;
}

static int count_ptr_target(const char *target) {
    struct dns_header hdr;
    size_t cursor;
    unsigned short i;
    int matches = 0;

    memcpy(&hdr, captured, sizeof(hdr));
    if (skip_questions(&cursor) != 0) {
        return -1;
    }
    for (i = 0; i < ntohs(hdr.ancount); i++) {
        char owner[MAX_NAME];
        char ptr_target[MAX_NAME];
        unsigned short rrtype;
        unsigned short rdlength;
        size_t rdata_cursor;
        size_t rdata_end;

        if (decode_name(captured, captured_len, &cursor, owner, sizeof(owner)) != 0 ||
            cursor + 10 > captured_len) {
            return -1;
        }
        memcpy(&rrtype, captured + cursor, 2);
        memcpy(&rdlength, captured + cursor + 8, 2);
        cursor += 10;
        rrtype = ntohs(rrtype);
        rdlength = ntohs(rdlength);
        if (cursor + rdlength > captured_len) {
            return -1;
        }
        rdata_cursor = cursor;
        rdata_end = cursor + rdlength;
        if (rrtype == DNS_TYPE_PTR &&
            decode_name(captured, captured_len, &rdata_cursor, ptr_target, sizeof(ptr_target)) == 0 &&
            rdata_cursor == rdata_end &&
            name_equals(ptr_target, target)) {
            matches++;
        }
        cursor += rdlength;
    }
    return matches;
}

static int count_ptr_owner(const char *wanted_owner) {
    struct dns_header hdr;
    size_t cursor;
    unsigned short i;
    int matches = 0;

    memcpy(&hdr, captured, sizeof(hdr));
    if (skip_questions(&cursor) != 0) {
        return -1;
    }
    for (i = 0; i < ntohs(hdr.ancount); i++) {
        char owner[MAX_NAME];
        unsigned short rrtype;
        unsigned short rdlength;

        if (decode_name(captured, captured_len, &cursor, owner, sizeof(owner)) != 0 ||
            cursor + 10 > captured_len) {
            return -1;
        }
        memcpy(&rrtype, captured + cursor, 2);
        memcpy(&rdlength, captured + cursor + 8, 2);
        cursor += 10;
        rrtype = ntohs(rrtype);
        rdlength = ntohs(rdlength);
        if (cursor + rdlength > captured_len) {
            return -1;
        }
        if (rrtype == DNS_TYPE_PTR && name_equals(owner, wanted_owner)) {
            matches++;
        }
        cursor += rdlength;
    }
    return matches;
}

static int expect_generated_types(void) {
    if (captured_count != 1 ||
        captured_dest.sin_addr.s_addr != inet_addr("10.0.1.42") ||
        count_ptr_target("_smb._tcp.local.") != 1 ||
        count_ptr_target("_adisk._tcp.local.") != 1 ||
        count_ptr_target("_device-info._tcp.local.") != 1 ||
        count_ptr_target("_airport._tcp.local.") != 1 ||
        count_ptr_target("_riousbprint._tcp.local.") != 1 ||
        count_ptr_target("_pdl-datastream._tcp.local.") != 1 ||
        count_ptr_target("_ipp._tcp.local.") != 0 ||
        count_ptr_target("_afpovertcp._tcp.local.") != 1) {
        return 1;
    }
    return 0;
}

static int expect_wan_types(void) {
    if (captured_count != 1 ||
        count_ptr_target("_airport._tcp.local.") != 1 ||
        count_ptr_target("_smb._tcp.local.") != 0 ||
        count_ptr_target("_adisk._tcp.local.") != 0 ||
        count_ptr_target("_device-info._tcp.local.") != 0 ||
        count_ptr_target("_riousbprint._tcp.local.") != 0 ||
        count_ptr_target("_pdl-datastream._tcp.local.") != 0 ||
        count_ptr_target("_ipp._tcp.local.") != 0 ||
        count_ptr_target("_afpovertcp._tcp.local.") != 0) {
        return 1;
    }
    return 0;
}

int main(void) {
    struct config cfg;
    struct link_context link;
    struct link_context_set links;
    struct sockaddr_in mdns_dest;
    struct sockaddr_in source;
    unsigned char query[BUF_SIZE];
    char airport_instance_fqdn[MAX_NAME];
    size_t query_len;

    configure_base(&cfg);
    configure_link(&link, inet_addr("10.0.1.77"));
    memset(&links, 0, sizeof(links));
    append_link_ipv4(&links, "bridge0", inet_addr("10.0.1.77"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    append_link_ipv4(&links, "bcmeth1", inet_addr("192.168.1.218"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    if (mdns_service_scope_for_link(&links, &links.links[0]) != MDNS_SERVICE_SCOPE_LAN ||
        mdns_service_scope_for_link(&links, &links.links[1]) != MDNS_SERVICE_SCOPE_WAN) {
        return 10;
    }
    if (build_instance_fqdn(airport_instance_fqdn,
                            sizeof(airport_instance_fqdn),
                            cfg.instance_name,
                            cfg.airport_service_type) != 0) {
        return 11;
    }
    configure_addrs(&mdns_dest, &source);

    query_len = make_query(query, DNS_SD_SERVICE_ENUMERATION_NAME, DNS_TYPE_PTR);
    reset_capture();
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &link) != 0 ||
        expect_generated_types() != 0) {
        return 1;
    }

    query_len = make_query(query, DNS_SD_SERVICE_ENUMERATION_NAME, DNS_TYPE_ANY);
    reset_capture();
    if (query_len == 0 ||
        handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &link) != 0 ||
        expect_generated_types() != 0) {
        return 2;
    }

    snprintf(link.name, sizeof(link.name), "%s", "bcmeth1");
    link.ipv4[0].addr = inet_addr("192.168.1.218");
    link.mdns_ipv4_transport_addr = link.ipv4[0].addr;
    source.sin_addr.s_addr = inet_addr("192.168.1.42");
    reset_capture();
    if (handle_query_scoped(1,
                            query,
                            query_len,
                            &mdns_dest,
                            &source,
                            &cfg,
                            &link,
                            MDNS_SERVICE_SCOPE_WAN) != 0 ||
        expect_wan_types() != 0) {
        return 3;
    }

    query_len = make_query(query, cfg.service_type, DNS_TYPE_PTR);
    reset_capture();
    if (query_len == 0 ||
        handle_query_scoped(1,
                            query,
                            query_len,
                            &mdns_dest,
                            &source,
                            &cfg,
                            &link,
                            MDNS_SERVICE_SCOPE_WAN) != 0 ||
        captured_count != 0) {
        return 4;
    }

    query_len = make_query(query, cfg.airport_service_type, DNS_TYPE_PTR);
    reset_capture();
    if (query_len == 0 ||
        handle_query_scoped(1,
                            query,
                            query_len,
                            &mdns_dest,
                            &source,
                            &cfg,
                            &link,
                            MDNS_SERVICE_SCOPE_WAN) != 0 ||
        captured_count != 1 ||
        count_ptr_target(airport_instance_fqdn) != 1) {
        return 5;
    }

    reset_capture();
    if (send_announcement_any_scoped(1,
                                     (const struct sockaddr *)&mdns_dest,
                                     sizeof(mdns_dest),
                                     &cfg,
                                     &link,
                                     cfg.ttl,
                                     MDNS_SERVICE_SCOPE_WAN) != 0) return 60;
    if (captured_count == 0) return 61;
    if (count_ptr_owner(cfg.airport_service_type) != 1) return 62;
    if (count_ptr_owner(cfg.service_type) != 0) return 63;
    if (count_ptr_owner(cfg.afp_service_type) != 0) return 64;
    if (count_ptr_owner(cfg.adisk_service_type) != 0) return 65;
    if (count_ptr_owner(cfg.device_info_service_type) != 0) return 66;
    if (count_ptr_owner(RIOUSBPRINT_SERVICE_TYPE) != 0) return 67;
    if (count_ptr_owner(PDL_DATASTREAM_SERVICE_TYPE) != 0) return 68;
    if (count_ptr_owner("_ipp._tcp.local.") != 0) return 69;

    printf("ok\n");
    return 0;
}
