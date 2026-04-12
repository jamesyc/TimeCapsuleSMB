#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/types.h>
#include <unistd.h>

#define NBNS_PORT 137
#define BUF_SIZE 576
#define MAX_NAME 16
#define MAX_PACKET_NAME 34
#define DNS_CLASS_IN 1
#define NB_TYPE_NB 0x0020
#define NBNS_FLAG_RESPONSE 0x8000
#define NBNS_FLAG_AUTHORITATIVE 0x0400
#define NBNS_RCODE_POSITIVE 0x0000
#define NBNS_SUFFIX_WORKSTATION 0x00
#define NBNS_SUFFIX_SERVER 0x20

static volatile sig_atomic_t g_stop = 0;

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
            "Usage: %s --name <netbios-name> --ipv4 <address> [options]\n"
            "Options:\n"
            "  --ttl <seconds>    Record TTL (default: 300)\n",
            prog);
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

static int build_positive_response(uint8_t *out,
                                   size_t out_len,
                                   const uint8_t *request,
                                   size_t request_len,
                                   size_t question_name_off,
                                   size_t question_end_off,
                                   uint16_t response_flags,
                                   uint32_t ttl,
                                   uint32_t ipv4_addr) {
    struct nbns_header header;
    size_t off = 0;
    uint16_t flags = 0;
    uint16_t rr_class = htons(DNS_CLASS_IN);
    uint16_t rr_type = htons(NB_TYPE_NB);
    uint16_t rdlength = htons(6);
    uint16_t nb_flags = htons(0x0000);
    uint32_t ttl_net = htonl(ttl);
    uint16_t name_ptr = htons((uint16_t)(0xC000 | question_name_off));

    if (request_len < sizeof(header) || question_end_off > request_len || question_name_off >= question_end_off) {
        return -1;
    }

    memcpy(&header, request, sizeof(header));
    flags = (uint16_t)((ntohs(header.flags) & 0x0110) | response_flags | NBNS_RCODE_POSITIVE);
    header.flags = htons(flags);
    header.qdcount = htons(1);
    header.ancount = htons(1);
    header.nscount = 0;
    header.arcount = 0;

    if (off + sizeof(header) > out_len) {
        return -1;
    }
    memcpy(out + off, &header, sizeof(header));
    off += sizeof(header);

    if (off + (question_end_off - question_name_off) > out_len) {
        return -1;
    }
    memcpy(out + off, request + question_name_off, question_end_off - question_name_off);
    off += question_end_off - question_name_off;

    if (off + sizeof(name_ptr) + sizeof(rr_type) + sizeof(rr_class) + sizeof(ttl_net) + sizeof(rdlength) + sizeof(nb_flags) + sizeof(ipv4_addr) > out_len) {
        return -1;
    }

    memcpy(out + off, &name_ptr, sizeof(name_ptr));
    off += sizeof(name_ptr);
    memcpy(out + off, &rr_type, sizeof(rr_type));
    off += sizeof(rr_type);
    memcpy(out + off, &rr_class, sizeof(rr_class));
    off += sizeof(rr_class);
    memcpy(out + off, &ttl_net, sizeof(ttl_net));
    off += sizeof(ttl_net);
    memcpy(out + off, &rdlength, sizeof(rdlength));
    off += sizeof(rdlength);
    memcpy(out + off, &nb_flags, sizeof(nb_flags));
    off += sizeof(nb_flags);
    memcpy(out + off, &ipv4_addr, sizeof(ipv4_addr));
    off += sizeof(ipv4_addr);

    return (int)off;
}

static int maybe_respond_to_query(int sock,
                                  const struct config *cfg,
                                  const uint8_t *buf,
                                  size_t len,
                                  const struct sockaddr_in *peer,
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
    if (buf[off] != 32) {
        return 0;
    }
    off++;

    if (off + 32 + 1 + 2 + 2 > len) {
        return 0;
    }

    if (decode_netbios_question_name(buf + off, 32, queried_name, &suffix) != 0) {
        return 0;
    }
    off += 32;

    if (buf[off] != 0) {
        return 0;
    }
    off++;

    memcpy(&qtype, buf + off, sizeof(qtype));
    off += sizeof(qtype);
    memcpy(&qclass, buf + off, sizeof(qclass));
    off += sizeof(qclass);

    if (ntohs(qtype) != NB_TYPE_NB || ntohs(qclass) != DNS_CLASS_IN) {
        return 0;
    }

    if (suffix != NBNS_SUFFIX_WORKSTATION && suffix != NBNS_SUFFIX_SERVER) {
        return 0;
    }

    normalize_netbios_name(normalized_name, cfg->netbios_name);
    if (!names_match(normalized_name, queried_name)) {
        return 0;
    }

    response_len = build_positive_response(
        response,
        sizeof(response),
        buf,
        len,
        question_name_off,
        off,
        (uint16_t)(NBNS_FLAG_RESPONSE | NBNS_FLAG_AUTHORITATIVE),
        cfg->ttl,
        cfg->ipv4_addr);
    if (response_len < 0) {
        return 0;
    }

    if (sendto(sock, response, (size_t)response_len, 0, (const struct sockaddr *)peer, peer_len) < 0) {
        perror("sendto");
    }

    return 1;
}

int main(int argc, char **argv) {
    struct config cfg;
    struct sockaddr_in addr;
    int sock = -1;
    int yes = 1;
    int i;

    memset(&cfg, 0, sizeof(cfg));
    cfg.ttl = 300;

    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--name") == 0 && i + 1 < argc) {
            strncpy(cfg.netbios_name, argv[++i], sizeof(cfg.netbios_name) - 1);
        } else if (strcmp(argv[i], "--ipv4") == 0 && i + 1 < argc) {
            if (inet_aton(argv[++i], (struct in_addr *)&cfg.ipv4_addr) == 0) {
                fprintf(stderr, "invalid IPv4 address\n");
                return 2;
            }
        } else if (strcmp(argv[i], "--ttl") == 0 && i + 1 < argc) {
            long ttl = strtol(argv[++i], NULL, 10);
            if (ttl <= 0 || ttl > 86400) {
                fprintf(stderr, "ttl must be between 1 and 86400\n");
                return 2;
            }
            cfg.ttl = (uint32_t)ttl;
        } else if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            usage(argv[0]);
            return 0;
        } else {
            usage(argv[0]);
            return 2;
        }
    }

    if (validate_netbios_name(cfg.netbios_name) != 0) {
        return 2;
    }
    if (cfg.ipv4_addr == 0) {
        fprintf(stderr, "missing required option: --ipv4\n");
        return 2;
    }

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        perror("socket");
        return 1;
    }

    if (setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes)) < 0) {
        perror("setsockopt(SO_REUSEADDR)");
        close(sock);
        return 1;
    }

#ifdef SO_REUSEPORT
    setsockopt(sock, SOL_SOCKET, SO_REUSEPORT, &yes, sizeof(yes));
#endif

    if (setsockopt(sock, SOL_SOCKET, SO_BROADCAST, &yes, sizeof(yes)) < 0) {
        perror("setsockopt(SO_BROADCAST)");
        close(sock);
        return 1;
    }

    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(NBNS_PORT);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);

    if (bind(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind");
        close(sock);
        return 1;
    }

    while (!g_stop) {
        fd_set readfds;
        struct timeval timeout;
        uint8_t buf[BUF_SIZE];
        struct sockaddr_in peer;
        socklen_t peer_len = sizeof(peer);
        ssize_t nread;

        FD_ZERO(&readfds);
        FD_SET(sock, &readfds);
        timeout.tv_sec = 1;
        timeout.tv_usec = 0;

        if (select(sock + 1, &readfds, NULL, NULL, &timeout) < 0) {
            if (errno == EINTR) {
                continue;
            }
            perror("select");
            close(sock);
            return 1;
        }

        if (!FD_ISSET(sock, &readfds)) {
            continue;
        }

        nread = recvfrom(sock, buf, sizeof(buf), 0, (struct sockaddr *)&peer, &peer_len);
        if (nread < 0) {
            if (errno == EINTR) {
                continue;
            }
            perror("recvfrom");
            close(sock);
            return 1;
        }

        (void)maybe_respond_to_query(sock, &cfg, buf, (size_t)nread, &peer, peer_len);
    }

    close(sock);
    return 0;
}
