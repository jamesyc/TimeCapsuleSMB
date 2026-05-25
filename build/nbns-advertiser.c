#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <net/if.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define NBNS_PORT 137
#define BUF_SIZE 576
#define MAX_NAME 16
#define MAX_PACKET_NAME 34
#define DNS_CLASS_IN 1
#define NB_TYPE_NULL 0x000A
#define NB_TYPE_NB 0x0020
#define NB_TYPE_NBSTAT 0x0021
#define NBNS_FLAG_RESPONSE 0x8000
#define NBNS_FLAG_AUTHORITATIVE 0x0400
#define NBNS_FLAG_RECURSION_AVAILABLE 0x0080
#define NBNS_FLAG_BROADCAST 0x0010
#define NBNS_RCODE_POSITIVE 0x0000
#define NBNS_RCODE_NAME_ERROR 0x0003
#define NBNS_SUFFIX_WORKSTATION 0x00
#define NBNS_SUFFIX_SERVER 0x20
#define NBNS_NAME_FLAGS_ACTIVE 0x0400
#define NBNS_NODE_STATUS_NAME_COUNT 2
#define NBNS_NODE_STATUS_STATS_LEN 46
#define MAX_IFACE_CONTEXTS 16
#define AUTO_IP_STABILIZE_SECONDS 3
#define AUTO_IP_STARTUP_POLL_SECONDS 2
#define AUTO_IP_STABLE_POLL_SECONDS 30
#define ADVERTISER_VERSION_CODE 2104
#define EXIT_OK 0
#define EXIT_RUNTIME_ERROR 1
#define EXIT_USAGE 2
#define EXIT_AUTO_IP_UNAVAILABLE 11
#define EXIT_AUTO_IP_PROBE_FAILED 13

static volatile sig_atomic_t g_stop = 0;

static void log_timestamp_prefix(FILE *stream) {
    time_t now;
    struct tm *tm_info;
    char stamp[32];

    now = time(NULL);
    tm_info = localtime(&now);
    if (tm_info != NULL && strftime(stamp, sizeof(stamp), "%Y-%m-%d %H:%M:%S", tm_info) > 0) {
        fputs(stamp, stream);
        fputc(' ', stream);
    }
}

static int timestamped_vfprintf(FILE *stream, const char *format, va_list ap) {
    char message[4096];
    const char *cursor;
    int result;

    if (stream != stderr && stream != stdout) {
        return vfprintf(stream, format, ap);
    }

    result = vsnprintf(message, sizeof(message), format, ap);
    if (result < 0) {
        return result;
    }
    message[sizeof(message) - 1] = '\0';

    cursor = message;
    while (*cursor != '\0') {
        log_timestamp_prefix(stream);
        while (*cursor != '\0') {
            int ch = (unsigned char)*cursor++;
            if (fputc(ch, stream) == EOF) {
                return -1;
            }
            if (ch == '\n') {
                break;
            }
        }
    }
    fflush(stream);
    return result;
}

static int timestamped_fprintf(FILE *stream, const char *format, ...) {
    va_list ap;
    int result;

    va_start(ap, format);
    result = timestamped_vfprintf(stream, format, ap);
    va_end(ap);
    return result;
}

static void timestamped_perror(const char *message) {
    int saved_errno = errno;

    if (message != NULL && message[0] != '\0') {
        timestamped_fprintf(stderr, "%s: %s\n", message, strerror(saved_errno));
    } else {
        timestamped_fprintf(stderr, "%s\n", strerror(saved_errno));
    }
}

#define fprintf timestamped_fprintf
#define perror timestamped_perror

static ssize_t sendto_retry(int sockfd, const void *buf, size_t len, int flags,
                            const struct sockaddr *dest, socklen_t dest_len) {
    ssize_t sent;

    do {
        sent = sendto(sockfd, buf, len, flags, dest, dest_len);
    } while (sent < 0 && errno == EINTR);

    return sent;
}

struct config {
    char netbios_name[MAX_NAME];
    uint32_t ipv4_addr;
    uint32_t ttl;
};

struct nbns_header {
    uint16_t id;
    uint16_t flags;
    uint16_t qdcount;
    uint16_t ancount;
    uint16_t nscount;
    uint16_t arcount;
};

static void on_signal(int signo) {
    (void)signo;
    g_stop = 1;
}

static void usage(const char *prog) {
    fprintf(stderr,
            "Usage: %s --name <netbios-name> --auto-ip [options]\n"
            "       %s --print-nbns-socket-families\n"
            "Options:\n"
            "  --auto-ip          Answer with the matching live interface address\n"
            "  --print-nbns-socket-families Print required NBNS UDP socket families for live links\n"
            "  --version          Print advertiser version code and exit\n",
            prog,
            prog);
}

static const char *ipv4_to_string(uint32_t ipv4_addr, char *out, size_t out_len) {
    struct in_addr addr;

    addr.s_addr = ipv4_addr;
    if (inet_ntop(AF_INET, &addr, out, out_len) == NULL) {
        strncpy(out, "invalid", out_len - 1);
        out[out_len - 1] = '\0';
    }
    return out;
}

#include "auto-ip-common.inc"

static void keep_only_nbns_ipv4_link_contexts(struct link_context_set *set) {
    size_t read_pos;
    size_t write_pos = 0;

    for (read_pos = 0; read_pos < set->count; read_pos++) {
        struct link_context link = set->links[read_pos];
        if (!link_context_has_advertisable_ipv4(&link)) {
            continue;
        }
        link.ipv6_count = 0;
        link.mdns_ipv6_transport = 0;
        set->links[write_pos++] = link;
    }
    set->count = write_pos;
}

static int link_contexts_need_nbns_ipv4_socket(const struct link_context_set *set) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (link_context_has_advertisable_ipv4(&set->links[i])) {
            return 1;
        }
    }
    return 0;
}

static int collect_usable_nbns_link_contexts(struct link_context_set *out) {
    struct link_context_set all_links;

    memset(&all_links, 0, sizeof(all_links));
    memset(out, 0, sizeof(*out));
    if (collect_usable_link_contexts(&all_links) != 0) {
        return -1;
    }
    filter_advertise_link_contexts(out, &all_links);
    keep_only_nbns_ipv4_link_contexts(out);
    if (all_links.truncated || out->truncated) {
        fprintf(stderr, "auto-ip: NBNS link list exceeded static capacity\n");
        return -1;
    }
    return 0;
}

static int print_nbns_socket_families(FILE *stream) {
    struct link_context_set links;

    memset(&links, 0, sizeof(links));
    if (collect_usable_nbns_link_contexts(&links) != 0) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    if (!link_contexts_need_nbns_ipv4_socket(&links)) {
        return EXIT_AUTO_IP_UNAVAILABLE;
    }
    if (fputs("ipv4", stream) == EOF) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    if (fputc('\n', stream) == EOF) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    return EXIT_OK;
}

static int wait_for_auto_link_contexts(struct link_context_set *out) {
    struct link_context_set first;

    memset(out, 0, sizeof(*out));
    while (!g_stop) {
        memset(&first, 0, sizeof(first));
        if (collect_usable_nbns_link_contexts(&first) == 0 && link_contexts_need_nbns_ipv4_socket(&first)) {
            fprintf(stderr, "nbns auto-ip: first usable IPv4 address observed; waiting %ds for network stabilization\n",
                    AUTO_IP_STABILIZE_SECONDS);
            sleep(AUTO_IP_STABILIZE_SECONDS);
            if (collect_usable_nbns_link_contexts(out) == 0 && link_contexts_need_nbns_ipv4_socket(out)) {
                return 0;
            }
            fprintf(stderr, "nbns auto-ip: usable IPv4 address disappeared during stabilization; retrying\n");
        }
        sleep(AUTO_IP_STARTUP_POLL_SECONDS);
    }
    return -1;
}

static void log_nbns_ipv4_link_miss(const struct link_context_set *links, uint32_t peer_addr) {
    static time_t last_log = 0;
    time_t now = time(NULL);

    if (last_log != 0 && now - last_log < 60) {
        return;
    }
    last_log = now;
    {
        char peer_buf[INET_ADDRSTRLEN];
        fprintf(stderr,
                "nbns auto-ip: ignoring query from %s; no matching subnet among %lu links\n",
                ipv4_to_string(peer_addr, peer_buf, sizeof(peer_buf)),
                (unsigned long)links->count);
    }
}

static int source_matches_link_ipv4_subnet(uint32_t source_ipv4_addr,
                                           const struct link_ipv4_addr *addr) {
    uint32_t netmask = effective_ipv4_netmask(addr->addr, addr->netmask);

    if (netmask == 0) {
        return source_ipv4_addr == addr->addr;
    }
    return (source_ipv4_addr & netmask) == (addr->addr & netmask);
}

static uint32_t choose_response_ipv4_from_links(const struct link_context_set *links, uint32_t peer_addr) {
    uint32_t only_addr = 0;
    size_t addr_count = 0;
    size_t i;

    for (i = 0; i < links->count; i++) {
        size_t j;
        for (j = 0; j < links->links[i].ipv4_count; j++) {
            if (source_matches_link_ipv4_subnet(peer_addr, &links->links[i].ipv4[j])) {
                return links->links[i].ipv4[j].addr;
            }
            only_addr = links->links[i].ipv4[j].addr;
            addr_count++;
        }
    }
    return addr_count == 1 ? only_addr : 0;
}

static int refresh_auto_link_contexts_if_needed(struct link_context_set *contexts,
                                                time_t *last_link_poll) {
    if (time(NULL) - *last_link_poll >= AUTO_IP_STABLE_POLL_SECONDS) {
        struct link_context_set next_contexts;
        memset(&next_contexts, 0, sizeof(next_contexts));
        if (collect_usable_nbns_link_contexts(&next_contexts) == 0 &&
            !link_context_sets_equal(contexts, &next_contexts)) {
            fprintf(stderr, "nbns auto-ip: interface table changed; rebuilding links after %ds stabilization\n",
                    AUTO_IP_STABILIZE_SECONDS);
            log_link_contexts("nbns auto-ip observed", &next_contexts);
            sleep(AUTO_IP_STABILIZE_SECONDS);
            if (collect_usable_nbns_link_contexts(&next_contexts) == 0 &&
                link_contexts_need_nbns_ipv4_socket(&next_contexts)) {
                *contexts = next_contexts;
            } else if (wait_for_auto_link_contexts(contexts) != 0) {
                return -1;
            }
            log_link_contexts("nbns auto-ip active", contexts);
        }
        *last_link_poll = time(NULL);
    }
    return 0;
}

static void normalize_netbios_name(char out[16], const char *name) {
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

static int validate_netbios_name(const char *name) {
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

static int decode_netbios_question_name(const uint8_t *encoded, size_t encoded_len, char out[16], uint8_t *suffix) {
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

static int names_match(const char configured[16], const char queried[16]) {
    size_t i;

    for (i = 0; i < 15; i++) {
        if ((unsigned char)configured[i] != (unsigned char)toupper((unsigned char)queried[i])) {
            return 0;
        }
    }

    return 1;
}

static int name_is_wildcard(const char queried[16]) {
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

static int parse_question_name(const uint8_t *buf,
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

static int build_resource_response(uint8_t *out,
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

static int build_positive_response(uint8_t *out,
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

static int build_negative_query_response(uint8_t *out,
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

static void append_node_status_name(uint8_t *out, const char normalized_name[16], uint8_t suffix) {
    uint16_t name_flags = htons(NBNS_NAME_FLAGS_ACTIVE);

    memcpy(out, normalized_name, 15);
    out[15] = suffix;
    memcpy(out + 16, &name_flags, sizeof(name_flags));
}

static int build_node_status_response(uint8_t *out,
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

static int maybe_respond_to_query_addr(int sock,
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

static int open_nbns_ipv4_socket(void) {
    struct sockaddr_in addr;
    int sock;
    int yes = 1;

    sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        perror("socket(AF_INET)");
        return -1;
    }
    if (setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes)) < 0) {
        perror("setsockopt(SO_REUSEADDR)");
        close(sock);
        return -1;
    }
#ifdef SO_REUSEPORT
    setsockopt(sock, SOL_SOCKET, SO_REUSEPORT, &yes, sizeof(yes));
#endif
    if (setsockopt(sock, SOL_SOCKET, SO_BROADCAST, &yes, sizeof(yes)) < 0) {
        perror("setsockopt(SO_BROADCAST)");
        close(sock);
        return -1;
    }
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(NBNS_PORT);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind(AF_INET)");
        close(sock);
        return -1;
    }
    return sock;
}

int main(int argc, char **argv) {
    struct config cfg;
    int sock4 = -1;
    int i;
    int auto_ip = 0;
    int print_socket_families = 0;
    time_t last_link_poll = 0;
    struct link_context_set link_contexts;

    memset(&cfg, 0, sizeof(cfg));
    memset(&link_contexts, 0, sizeof(link_contexts));
    cfg.ttl = 120;

    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--name") == 0 && i + 1 < argc) {
            const char *name_arg = argv[++i];
            size_t name_len;
            if (validate_netbios_name(name_arg) != 0) {
                return 2;
            }
            name_len = strlen(name_arg);
            if (name_len >= sizeof(cfg.netbios_name)) {
                fprintf(stderr, "netbios name must be 15 bytes or fewer\n");
                return 2;
            }
            memcpy(cfg.netbios_name, name_arg, name_len + 1);
        } else if (strcmp(argv[i], "--auto-ip") == 0) {
            auto_ip = 1;
        } else if (strcmp(argv[i], "--print-nbns-socket-families") == 0) {
            print_socket_families = 1;
        } else if (strcmp(argv[i], "--version") == 0) {
            printf("%d\n", ADVERTISER_VERSION_CODE);
            return EXIT_OK;
        } else if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            usage(argv[0]);
            return EXIT_OK;
        } else {
            usage(argv[0]);
            return EXIT_USAGE;
        }
    }

    if (print_socket_families) {
        return print_nbns_socket_families(stdout);
    }

    if (cfg.netbios_name[0] == '\0') {
        fprintf(stderr, "missing required option: --name\n");
        return EXIT_USAGE;
    }
    if (!auto_ip) {
        fprintf(stderr, "missing required option: --auto-ip\n");
        usage(argv[0]);
        return EXIT_USAGE;
    }
    if (wait_for_auto_link_contexts(&link_contexts) != 0) {
        return EXIT_RUNTIME_ERROR;
    }
    log_link_contexts("nbns auto-ip active", &link_contexts);
    last_link_poll = time(NULL);

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    if (link_contexts_need_nbns_ipv4_socket(&link_contexts)) {
        sock4 = open_nbns_ipv4_socket();
        if (sock4 < 0) {
            return EXIT_RUNTIME_ERROR;
        }
    }
    while (!g_stop) {
        fd_set readfds;
        struct timeval timeout;
        int maxfd = -1;
        int selected;
        int need_ipv4;

        FD_ZERO(&readfds);
        timeout.tv_sec = 1;
        timeout.tv_usec = 0;

        if (refresh_auto_link_contexts_if_needed(&link_contexts, &last_link_poll) != 0) {
            break;
        }

        need_ipv4 = link_contexts_need_nbns_ipv4_socket(&link_contexts);
        if (need_ipv4 && sock4 < 0) {
            sock4 = open_nbns_ipv4_socket();
            if (sock4 < 0) {
                break;
            }
        } else if (!need_ipv4 && sock4 >= 0) {
            close(sock4);
            sock4 = -1;
        }

        if (sock4 >= 0) {
            FD_SET(sock4, &readfds);
            maxfd = sock4 > maxfd ? sock4 : maxfd;
        }
        if (maxfd < 0) {
            sleep(1);
            continue;
        }

        selected = select(maxfd + 1, &readfds, NULL, NULL, &timeout);
        if (selected < 0) {
            if (errno == EINTR) {
                continue;
            }
            perror("select");
            if (sock4 >= 0) {
                close(sock4);
            }
            return EXIT_RUNTIME_ERROR;
        }

        if (sock4 >= 0 && FD_ISSET(sock4, &readfds)) {
            uint8_t buf[BUF_SIZE];
            struct sockaddr_in peer;
            socklen_t peer_len = sizeof(peer);
            ssize_t nread;

            nread = recvfrom(sock4, buf, sizeof(buf), 0, (struct sockaddr *)&peer, &peer_len);
            if (nread < 0) {
                if (errno != EINTR) {
                    perror("recvfrom(AF_INET)");
                }
            } else {
                uint32_t response_ip;
                struct config context_cfg = cfg;
                response_ip = choose_response_ipv4_from_links(&link_contexts, peer.sin_addr.s_addr);
                if (response_ip == 0) {
                    log_nbns_ipv4_link_miss(&link_contexts, peer.sin_addr.s_addr);
                } else {
                    context_cfg.ipv4_addr = response_ip;
                    (void)maybe_respond_to_query_addr(sock4,
                                                      &context_cfg,
                                                      buf,
                                                      (size_t)nread,
                                                      (const struct sockaddr *)&peer,
                                                      peer_len);
                }
            }
        }
    }

    if (sock4 >= 0) {
        close(sock4);
    }
    return EXIT_OK;
}

#undef fprintf
#undef perror
