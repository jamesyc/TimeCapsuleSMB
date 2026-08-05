#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);
int fake_setsockopt(int sockfd, int level, int optname, const void *optval, socklen_t optlen);
int fake_usleep(useconds_t usec);
int fake_rand(void);
void fake_srand(unsigned int seed);

#include "mdns/mdns.h"

struct opt_call {
    int level;
    int optname;
    unsigned int value;
};

static unsigned char captured_packets[4][BUF_SIZE];
static size_t captured_lengths[4];
static size_t captured_count = 0;
static useconds_t captured_usleeps[8];
static size_t captured_usleep_count = 0;
static struct opt_call captured_opts[32];
static size_t captured_opt_count = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {
    (void)sockfd;
    (void)flags;
    (void)dest;
    (void)dest_len;
    if (captured_count < 4 && len <= sizeof(captured_packets[0])) {
        memcpy(captured_packets[captured_count], buf, len);
        captured_lengths[captured_count] = len;
        captured_count++;
    }
    return (ssize_t)len;
}

int fake_setsockopt(int sockfd, int level, int optname, const void *optval, socklen_t optlen) {
    (void)sockfd;
    if (captured_opt_count < 32) {
        captured_opts[captured_opt_count].level = level;
        captured_opts[captured_opt_count].optname = optname;
        captured_opts[captured_opt_count].value = 0;
        if (optval != NULL) {
            if (optlen == sizeof(int)) {
                int value;
                memcpy(&value, optval, sizeof(value));
                captured_opts[captured_opt_count].value = (unsigned int)value;
            } else if (optlen == sizeof(unsigned int)) {
                unsigned int value;
                memcpy(&value, optval, sizeof(value));
                captured_opts[captured_opt_count].value = value;
            }
        }
        captured_opt_count++;
    }
    return 0;
}

int fake_usleep(useconds_t usec) {
    if (captured_usleep_count < 8) {
        captured_usleeps[captured_usleep_count++] = usec;
    }
    return 0;
}

int fake_rand(void) {
    return 0;
}

void fake_srand(unsigned int seed) {
    (void)seed;
}

static void reset_packet_capture(void) {
    memset(captured_packets, 0, sizeof(captured_packets));
    memset(captured_lengths, 0, sizeof(captured_lengths));
    captured_count = 0;
    captured_usleep_count = 0;
}

static void reset_option_capture(void) {
    memset(captured_opts, 0, sizeof(captured_opts));
    captured_opt_count = 0;
}

static int saw_opt(int level, int optname, unsigned int value) {
    size_t i;

    for (i = 0; i < captured_opt_count; i++) {
        if (captured_opts[i].level == level &&
            captured_opts[i].optname == optname &&
            captured_opts[i].value == value) {
            return 1;
        }
    }
    return 0;
}

static void configure_base(struct config *cfg) {
    memset(cfg, 0, sizeof(*cfg));
    snprintf(cfg->instance_name, sizeof(cfg->instance_name), "%s", "Alton Time Capsule");
    snprintf(cfg->host_label, sizeof(cfg->host_label), "%s", "alton-time-capsule");
    snprintf(cfg->host_fqdn, sizeof(cfg->host_fqdn), "%s", "alton-time-capsule.local.");
    snprintf(cfg->service_type, sizeof(cfg->service_type), "%s", "_smb._tcp.local.");
    cfg->port = 445;
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
    link->ifindex = 5;
}

static size_t make_query(unsigned char *packet, const char *qname) {
    struct dns_header hdr;
    size_t off = sizeof(hdr);

    memset(&hdr, 0, sizeof(hdr));
    hdr.qdcount = htons(1);
    memcpy(packet, &hdr, sizeof(hdr));
    if (encode_name(packet, &off, BUF_SIZE, qname) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_TYPE_PTR) != 0 ||
        append_u16(packet, &off, BUF_SIZE, DNS_CLASS_IN) != 0) {
        return 0;
    }
    return off;
}

static int handle_query_from_port(uint16_t port) {
    struct config cfg;
    struct link_context link;
    struct sockaddr_in mdns_dest;
    struct sockaddr_in source;
    unsigned char query[BUF_SIZE];
    size_t query_len;

    configure_base(&cfg);
    configure_link(&link, inet_addr("10.0.1.77"));
    memset(&mdns_dest, 0, sizeof(mdns_dest));
    mdns_dest.sin_family = AF_INET;
    mdns_dest.sin_port = htons(MDNS_PORT);
    mdns_dest.sin_addr.s_addr = inet_addr(MDNS_GROUP);
    memset(&source, 0, sizeof(source));
    source.sin_family = AF_INET;
    source.sin_port = htons(port);
    source.sin_addr.s_addr = inet_addr("10.0.1.42");

    query_len = make_query(query, cfg.service_type);
    return query_len == 0 ? -1 : handle_query(1, query, query_len, &mdns_dest, &source, &cfg, &link);
}

int main(void) {
    reset_packet_capture();
    if (handle_query_from_port(MDNS_PORT) != 0 ||
        captured_count != 1 ||
        captured_usleep_count != 1 ||
        captured_usleeps[0] != 20000) {
        return 1;
    }

    reset_packet_capture();
    if (handle_query_from_port(62001) != 0 ||
        captured_count != 1 ||
        captured_usleep_count != 0) {
        return 2;
    }

    reset_option_capture();
    if (configure_multicast_socket_options(77) != 0 ||
        !saw_opt(IPPROTO_IP, IP_MULTICAST_TTL, 255) ||
        !saw_opt(IPPROTO_IP, IP_MULTICAST_LOOP, 1)) {
        return 3;
    }
#ifdef IP_TTL
    if (!saw_opt(IPPROTO_IP, IP_TTL, 255)) {
        return 4;
    }
#endif

#ifdef IPV6_MULTICAST_IF
    reset_option_capture();
    if (set_outbound_multicast_interface6(77, 5, "test", 0, 0) != 0 ||
        !saw_opt(IPPROTO_IPV6, IPV6_MULTICAST_IF, 5)) {
        return 5;
    }
#ifdef IPV6_MULTICAST_HOPS
    if (!saw_opt(IPPROTO_IPV6, IPV6_MULTICAST_HOPS, 255)) {
        return 6;
    }
#endif
#ifdef IPV6_MULTICAST_LOOP
    if (!saw_opt(IPPROTO_IPV6, IPV6_MULTICAST_LOOP, 1)) {
        return 7;
    }
#endif
#ifdef IPV6_UNICAST_HOPS
    if (!saw_opt(IPPROTO_IPV6, IPV6_UNICAST_HOPS, 255)) {
        return 8;
    }
#endif
#endif

    printf("ok\n");
    return 0;
}
