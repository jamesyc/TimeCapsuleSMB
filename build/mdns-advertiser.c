#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/types.h>
#include <unistd.h>

#define MDNS_PORT 5353
#define MDNS_GROUP "224.0.0.251"
#define BUF_SIZE 1500
#define MAX_NAME 256
#define MAX_LABEL 63
#define ANNOUNCE_INTERVAL 30

#define DNS_TYPE_A 1
#define DNS_TYPE_PTR 12
#define DNS_TYPE_TXT 16
#define DNS_TYPE_AAAA 28
#define DNS_TYPE_SRV 33
#define DNS_TYPE_ANY 255
#define DNS_CLASS_IN 1
#define DNS_FLAG_QR 0x8000
#define DNS_FLAG_AA 0x0400

static volatile sig_atomic_t g_stop = 0;

struct config {
    char service_type[MAX_NAME];
    char instance_name[MAX_NAME];
    char host_label[MAX_LABEL + 1];
    char host_fqdn[MAX_NAME];
    char adisk_service_type[MAX_NAME];
    char adisk_share_name[MAX_NAME];
    char device_info_service_type[MAX_NAME];
    char device_model[MAX_NAME];
    uint32_t ipv4_addr;
    uint16_t port;
    uint16_t adisk_port;
    uint32_t ttl;
};

struct dns_header {
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
            "Usage: %s --instance <name> --host <label> --ipv4 <address> [options]\n"
            "Options:\n"
            "  --service <type>   Service type (default: _smb._tcp.local.)\n"
            "  --adisk-share <n>  Also advertise _adisk._tcp for Time Machine\n"
            "  --device-model <m> Also advertise _device-info._tcp with model=<m>\n"
            "  --port <port>      Service port (default: 445)\n"
            "  --ttl <seconds>    Record TTL (default: 120)\n",
            prog);
}

static int append_bytes(uint8_t *buf, size_t *off, size_t cap, const void *src, size_t len) {
    if (*off + len > cap) {
        return -1;
    }
    memcpy(buf + *off, src, len);
    *off += len;
    return 0;
}

static int append_u16(uint8_t *buf, size_t *off, size_t cap, uint16_t value) {
    uint16_t net = htons(value);
    return append_bytes(buf, off, cap, &net, sizeof(net));
}

static int append_u32(uint8_t *buf, size_t *off, size_t cap, uint32_t value) {
    uint32_t net = htonl(value);
    return append_bytes(buf, off, cap, &net, sizeof(net));
}

static int validate_single_dns_label(const char *value, const char *field_name) {
    size_t len;
    const unsigned char *p;

    if (value == NULL || value[0] == '\0') {
        fprintf(stderr, "%s must not be empty\n", field_name);
        return -1;
    }

    len = strlen(value);
    if (len > MAX_LABEL) {
        fprintf(stderr, "%s must be %d bytes or fewer\n", field_name, MAX_LABEL);
        return -1;
    }

    if (strchr(value, '.') != NULL) {
        fprintf(stderr, "%s must not contain dots\n", field_name);
        return -1;
    }

    for (p = (const unsigned char *)value; *p != '\0'; p++) {
        if (*p < 0x20 || *p == 0x7f) {
            fprintf(stderr, "%s contains an invalid control character\n", field_name);
            return -1;
        }
    }

    return 0;
}

static int build_instance_fqdn(char *out, size_t out_len, const char *instance_name, const char *service_type) {
    int written;

    written = snprintf(out, out_len, "%s.%s", instance_name, service_type);
    if (written < 0 || (size_t)written >= out_len) {
        return -1;
    }

    return 0;
}

static int encode_name(uint8_t *buf, size_t *off, size_t cap, const char *name) {
    char temp[MAX_NAME];
    char *token;
    char *saveptr = NULL;

    if (strlen(name) >= sizeof(temp)) {
        return -1;
    }
    strcpy(temp, name);

    token = strtok_r(temp, ".", &saveptr);
    while (token != NULL) {
        size_t len = strlen(token);
        uint8_t label_len;
        if (len == 0 || len > 63) {
            return -1;
        }
        label_len = (uint8_t)len;
        if (append_bytes(buf, off, cap, &label_len, 1) != 0) {
            return -1;
        }
        if (append_bytes(buf, off, cap, token, len) != 0) {
            return -1;
        }
        token = strtok_r(NULL, ".", &saveptr);
    }

    return append_bytes(buf, off, cap, "\0", 1);
}

static int decode_name(const uint8_t *packet, size_t packet_len, size_t *cursor, char *out, size_t out_len) {
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
        if (out_pos + len >= out_len) {
            return -1;
        }
        memcpy(out + out_pos, packet + pos + 1, len);
        out_pos += len;
        pos += 1 + len;
        if (!jumped) {
            next_cursor = pos;
        }
    }

    return -1;
}

static int name_equals(const char *a, const char *b) {
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

static int add_rr_ptr(uint8_t *buf, size_t *off, size_t cap, const char *owner, const char *target, uint32_t ttl) {
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

static int add_rr_txt_empty(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ttl) {
    static const uint8_t empty_txt[] = {0x00};
    if (encode_name(buf, off, cap, owner) != 0 ||
        append_u16(buf, off, cap, DNS_TYPE_TXT) != 0 ||
        append_u16(buf, off, cap, DNS_CLASS_IN) != 0 ||
        append_u32(buf, off, cap, ttl) != 0 ||
        append_u16(buf, off, cap, (uint16_t)sizeof(empty_txt)) != 0 ||
        append_bytes(buf, off, cap, empty_txt, sizeof(empty_txt)) != 0) {
        return -1;
    }
    return 0;
}

static int add_rr_txt_strings(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ttl,
                              const char **strings, size_t string_count) {
    size_t rdlength_pos;
    size_t rdata_start;
    size_t i;

    if (encode_name(buf, off, cap, owner) != 0 ||
        append_u16(buf, off, cap, DNS_TYPE_TXT) != 0 ||
        append_u16(buf, off, cap, DNS_CLASS_IN) != 0 ||
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
        size_t slen = strlen(strings[i]);
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

static int add_rr_srv(uint8_t *buf, size_t *off, size_t cap, const char *owner, const char *target, uint16_t port, uint32_t ttl) {
    size_t rdlength_pos;
    size_t rdata_start;
    if (encode_name(buf, off, cap, owner) != 0 ||
        append_u16(buf, off, cap, DNS_TYPE_SRV) != 0 ||
        append_u16(buf, off, cap, DNS_CLASS_IN) != 0 ||
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

static int add_rr_a(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ipv4_addr, uint32_t ttl) {
    if (encode_name(buf, off, cap, owner) != 0 ||
        append_u16(buf, off, cap, DNS_TYPE_A) != 0 ||
        append_u16(buf, off, cap, DNS_CLASS_IN) != 0 ||
        append_u32(buf, off, cap, ttl) != 0 ||
        append_u16(buf, off, cap, 4) != 0 ||
        append_bytes(buf, off, cap, &ipv4_addr, 4) != 0) {
        return -1;
    }
    return 0;
}

static int add_adisk_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg, int *answers) {
    char instance_fqdn[MAX_NAME];
    char txt1[128];
    char txt2[256];
    const char *txts[2];

    if (cfg->adisk_share_name[0] == '\0') {
        return 0;
    }

    if (build_instance_fqdn(instance_fqdn, sizeof(instance_fqdn), cfg->instance_name, cfg->adisk_service_type) != 0) {
        return -1;
    }
    snprintf(txt1, sizeof(txt1), "sys=waMa=0,adVF=0x100");
    snprintf(txt2, sizeof(txt2), "dk0=adVN=%s,adVF=0x82", cfg->adisk_share_name);
    txts[0] = txt1;
    txts[1] = txt2;

    if (add_rr_ptr(buf, off, cap, cfg->adisk_service_type, instance_fqdn, cfg->ttl) != 0 ||
        add_rr_srv(buf, off, cap, instance_fqdn, cfg->host_fqdn, cfg->adisk_port, cfg->ttl) != 0 ||
        add_rr_txt_strings(buf, off, cap, instance_fqdn, cfg->ttl, txts, 2) != 0) {
        return -1;
    }

    *answers += 3;
    return 0;
}

static int add_device_info_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg, int *answers) {
    char instance_fqdn[MAX_NAME];
    char model_txt[MAX_NAME + 16];
    const char *txts[1];

    if (cfg->device_model[0] == '\0') {
        return 0;
    }

    if (build_instance_fqdn(instance_fqdn, sizeof(instance_fqdn), cfg->instance_name, cfg->device_info_service_type) != 0) {
        return -1;
    }
    snprintf(model_txt, sizeof(model_txt), "model=%s", cfg->device_model);
    txts[0] = model_txt;

    if (add_rr_ptr(buf, off, cap, cfg->device_info_service_type, instance_fqdn, cfg->ttl) != 0 ||
        add_rr_srv(buf, off, cap, instance_fqdn, cfg->host_fqdn, cfg->port, cfg->ttl) != 0 ||
        add_rr_txt_strings(buf, off, cap, instance_fqdn, cfg->ttl, txts, 1) != 0) {
        return -1;
    }

    *answers += 3;
    return 0;
}

static int send_announcement(int sockfd, const struct sockaddr_in *dest, const struct config *cfg) {
    uint8_t buf[BUF_SIZE];
    size_t off = sizeof(struct dns_header);
    struct dns_header hdr;
    char instance_fqdn[MAX_NAME];
    int answers = 0;

    if (build_instance_fqdn(instance_fqdn, sizeof(instance_fqdn), cfg->instance_name, cfg->service_type) != 0) {
        return -1;
    }

    memset(&hdr, 0, sizeof(hdr));
    hdr.flags = htons(DNS_FLAG_QR | DNS_FLAG_AA);

    if (add_rr_ptr(buf, &off, sizeof(buf), cfg->service_type, instance_fqdn, cfg->ttl) != 0 ||
        add_rr_srv(buf, &off, sizeof(buf), instance_fqdn, cfg->host_fqdn, cfg->port, cfg->ttl) != 0 ||
        add_rr_txt_empty(buf, &off, sizeof(buf), instance_fqdn, cfg->ttl) != 0 ||
        add_rr_a(buf, &off, sizeof(buf), cfg->host_fqdn, cfg->ipv4_addr, cfg->ttl) != 0) {
        return -1;
    }
    answers = 4;

    if (add_adisk_records(buf, &off, sizeof(buf), cfg, &answers) != 0) {
        return -1;
    }
    if (add_device_info_records(buf, &off, sizeof(buf), cfg, &answers) != 0) {
        return -1;
    }

    hdr.ancount = htons((uint16_t)answers);
    memcpy(buf, &hdr, sizeof(hdr));

    return (sendto(sockfd, buf, off, 0, (const struct sockaddr *)dest, sizeof(*dest)) >= 0) ? 0 : -1;
}

static int handle_query(int sockfd, const uint8_t *packet, size_t packet_len, const struct sockaddr_in *dest, const struct config *cfg) {
    struct dns_header hdr;
    size_t cursor = sizeof(struct dns_header);
    uint16_t qdcount;
    uint8_t reply[BUF_SIZE];
    size_t off = sizeof(struct dns_header);
    char instance_fqdn[MAX_NAME];
    char adisk_instance_fqdn[MAX_NAME];
    char device_info_instance_fqdn[MAX_NAME];
    int want_ptr = 0;
    int want_srv = 0;
    int want_txt = 0;
    int want_a = 0;
    int want_adisk_ptr = 0;
    int want_adisk_srv = 0;
    int want_adisk_txt = 0;
    int want_device_info_ptr = 0;
    int want_device_info_srv = 0;
    int want_device_info_txt = 0;
    int answers = 0;
    uint16_t i;

    if (packet_len < sizeof(struct dns_header)) {
        return 0;
    }
    memcpy(&hdr, packet, sizeof(hdr));
    if (ntohs(hdr.flags) & DNS_FLAG_QR) {
        return 0;
    }

    qdcount = ntohs(hdr.qdcount);
    if (build_instance_fqdn(instance_fqdn, sizeof(instance_fqdn), cfg->instance_name, cfg->service_type) != 0) {
        return 0;
    }
    if (cfg->adisk_share_name[0] != '\0' &&
        build_instance_fqdn(adisk_instance_fqdn, sizeof(adisk_instance_fqdn), cfg->instance_name, cfg->adisk_service_type) != 0) {
        return 0;
    }
    if (cfg->device_model[0] != '\0' &&
        build_instance_fqdn(device_info_instance_fqdn, sizeof(device_info_instance_fqdn), cfg->instance_name, cfg->device_info_service_type) != 0) {
        return 0;
    }

    for (i = 0; i < qdcount; i++) {
        char qname[MAX_NAME];
        uint16_t qtype;
        uint16_t qclass;

        if (decode_name(packet, packet_len, &cursor, qname, sizeof(qname)) != 0 ||
            cursor + 4 > packet_len) {
            return 0;
        }
        memcpy(&qtype, packet + cursor, 2);
        memcpy(&qclass, packet + cursor + 2, 2);
        cursor += 4;
        qtype = ntohs(qtype);
        qclass = ntohs(qclass) & 0x7FFF;
        if (qclass != DNS_CLASS_IN) {
            continue;
        }

        if (name_equals(qname, cfg->service_type) && (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
            want_ptr = 1;
        } else if (cfg->adisk_share_name[0] != '\0' &&
                   name_equals(qname, cfg->adisk_service_type) &&
                   (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
            want_adisk_ptr = 1;
        } else if (cfg->device_model[0] != '\0' &&
                   name_equals(qname, cfg->device_info_service_type) &&
                   (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
            want_device_info_ptr = 1;
        } else if (name_equals(qname, instance_fqdn) && (qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY)) {
            want_srv = 1;
        } else if (name_equals(qname, instance_fqdn) && (qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY)) {
            want_txt = 1;
        } else if (cfg->adisk_share_name[0] != '\0' &&
                   name_equals(qname, adisk_instance_fqdn) &&
                   (qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY)) {
            want_adisk_srv = 1;
        } else if (cfg->adisk_share_name[0] != '\0' &&
                   name_equals(qname, adisk_instance_fqdn) &&
                   (qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY)) {
            want_adisk_txt = 1;
        } else if (cfg->device_model[0] != '\0' &&
                   name_equals(qname, device_info_instance_fqdn) &&
                   (qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY)) {
            want_device_info_srv = 1;
        } else if (cfg->device_model[0] != '\0' &&
                   name_equals(qname, device_info_instance_fqdn) &&
                   (qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY)) {
            want_device_info_txt = 1;
        } else if (name_equals(qname, cfg->host_fqdn) && (qtype == DNS_TYPE_A || qtype == DNS_TYPE_ANY)) {
            want_a = 1;
        }
    }

    if (!want_ptr && !want_srv && !want_txt && !want_a &&
        !want_adisk_ptr && !want_adisk_srv && !want_adisk_txt &&
        !want_device_info_ptr && !want_device_info_srv && !want_device_info_txt) {
        return 0;
    }

    memset(&hdr, 0, sizeof(hdr));
    hdr.flags = htons(DNS_FLAG_QR | DNS_FLAG_AA);

    if (want_ptr) {
        if (add_rr_ptr(reply, &off, sizeof(reply), cfg->service_type, instance_fqdn, cfg->ttl) != 0) {
            return -1;
        }
        answers++;
    }
    if (want_srv) {
        if (add_rr_srv(reply, &off, sizeof(reply), instance_fqdn, cfg->host_fqdn, cfg->port, cfg->ttl) != 0) {
            return -1;
        }
        answers++;
    }
    if (want_txt) {
        if (add_rr_txt_empty(reply, &off, sizeof(reply), instance_fqdn, cfg->ttl) != 0) {
            return -1;
        }
        answers++;
    }
    if (want_adisk_ptr || want_adisk_srv || want_adisk_txt) {
        char txt1[128];
        char txt2[256];
        const char *txts[2];

        snprintf(txt1, sizeof(txt1), "sys=waMa=0,adVF=0x100");
        snprintf(txt2, sizeof(txt2), "dk0=adVN=%s,adVF=0x82", cfg->adisk_share_name);
        txts[0] = txt1;
        txts[1] = txt2;

        if (want_adisk_ptr) {
            if (add_rr_ptr(reply, &off, sizeof(reply), cfg->adisk_service_type, adisk_instance_fqdn, cfg->ttl) != 0) {
                return -1;
            }
            answers++;
        }
        if (want_adisk_srv) {
            if (add_rr_srv(reply, &off, sizeof(reply), adisk_instance_fqdn, cfg->host_fqdn, cfg->adisk_port, cfg->ttl) != 0) {
                return -1;
            }
            answers++;
        }
        if (want_adisk_txt) {
            if (add_rr_txt_strings(reply, &off, sizeof(reply), adisk_instance_fqdn, cfg->ttl, txts, 2) != 0) {
                return -1;
            }
            answers++;
        }
    }
    if (want_device_info_ptr || want_device_info_srv || want_device_info_txt) {
        char model_txt[MAX_NAME + 16];
        const char *txts[1];

        snprintf(model_txt, sizeof(model_txt), "model=%s", cfg->device_model);
        txts[0] = model_txt;

        if (want_device_info_ptr) {
            if (add_rr_ptr(reply, &off, sizeof(reply), cfg->device_info_service_type, device_info_instance_fqdn, cfg->ttl) != 0) {
                return -1;
            }
            answers++;
        }
        if (want_device_info_srv) {
            if (add_rr_srv(reply, &off, sizeof(reply), device_info_instance_fqdn, cfg->host_fqdn, cfg->port, cfg->ttl) != 0) {
                return -1;
            }
            answers++;
        }
        if (want_device_info_txt) {
            if (add_rr_txt_strings(reply, &off, sizeof(reply), device_info_instance_fqdn, cfg->ttl, txts, 1) != 0) {
                return -1;
            }
            answers++;
        }
    }
    if (want_a) {
        if (add_rr_a(reply, &off, sizeof(reply), cfg->host_fqdn, cfg->ipv4_addr, cfg->ttl) != 0) {
            return -1;
        }
        answers++;
    }

    hdr.ancount = htons((uint16_t)answers);
    memcpy(reply, &hdr, sizeof(hdr));

    return (sendto(sockfd, reply, off, 0, (const struct sockaddr *)dest, sizeof(*dest)) >= 0) ? 0 : -1;
}

static int open_mdns_socket(void) {
    int sockfd;
    int yes = 1;
    struct sockaddr_in addr;
    struct ip_mreq mreq;

    sockfd = socket(AF_INET, SOCK_DGRAM, 0);
    if (sockfd < 0) {
        perror("socket");
        return -1;
    }

    if (setsockopt(sockfd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes)) < 0) {
        perror("setsockopt(SO_REUSEADDR)");
        close(sockfd);
        return -1;
    }

#ifdef SO_REUSEPORT
    (void)setsockopt(sockfd, SOL_SOCKET, SO_REUSEPORT, &yes, sizeof(yes));
#endif

    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(MDNS_PORT);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(sockfd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind");
        close(sockfd);
        return -1;
    }

    memset(&mreq, 0, sizeof(mreq));
    mreq.imr_multiaddr.s_addr = inet_addr(MDNS_GROUP);
    mreq.imr_interface.s_addr = htonl(INADDR_ANY);
    if (setsockopt(sockfd, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mreq, sizeof(mreq)) < 0) {
        perror("setsockopt(IP_ADD_MEMBERSHIP)");
        close(sockfd);
        return -1;
    }

    yes = 255;
    (void)setsockopt(sockfd, IPPROTO_IP, IP_MULTICAST_TTL, &yes, sizeof(yes));
    yes = 1;
    (void)setsockopt(sockfd, IPPROTO_IP, IP_MULTICAST_LOOP, &yes, sizeof(yes));

    return sockfd;
}

int main(int argc, char **argv) {
    struct config cfg;
    int sockfd;
    struct sockaddr_in mdns_dest;
    int i;
    time_t last_announce = 0;

    memset(&cfg, 0, sizeof(cfg));
    strcpy(cfg.service_type, "_smb._tcp.local.");
    strcpy(cfg.adisk_service_type, "_adisk._tcp.local.");
    strcpy(cfg.device_info_service_type, "_device-info._tcp.local.");
    cfg.port = 445;
    cfg.adisk_port = 9;
    cfg.ttl = 120;

    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--service") == 0 && i + 1 < argc) {
            strncpy(cfg.service_type, argv[++i], sizeof(cfg.service_type) - 1);
        } else if (strcmp(argv[i], "--adisk-share") == 0 && i + 1 < argc) {
            strncpy(cfg.adisk_share_name, argv[++i], sizeof(cfg.adisk_share_name) - 1);
        } else if (strcmp(argv[i], "--device-model") == 0 && i + 1 < argc) {
            strncpy(cfg.device_model, argv[++i], sizeof(cfg.device_model) - 1);
        } else if (strcmp(argv[i], "--instance") == 0 && i + 1 < argc) {
            strncpy(cfg.instance_name, argv[++i], sizeof(cfg.instance_name) - 1);
        } else if (strcmp(argv[i], "--host") == 0 && i + 1 < argc) {
            strncpy(cfg.host_label, argv[++i], sizeof(cfg.host_label) - 1);
        } else if (strcmp(argv[i], "--ipv4") == 0 && i + 1 < argc) {
            if (inet_pton(AF_INET, argv[++i], &cfg.ipv4_addr) != 1) {
                fprintf(stderr, "Invalid IPv4 address\n");
                return 2;
            }
        } else if (strcmp(argv[i], "--port") == 0 && i + 1 < argc) {
            cfg.port = (uint16_t)atoi(argv[++i]);
        } else if (strcmp(argv[i], "--ttl") == 0 && i + 1 < argc) {
            cfg.ttl = (uint32_t)atoi(argv[++i]);
        } else {
            usage(argv[0]);
            return 2;
        }
    }

    if (cfg.instance_name[0] == '\0' || cfg.host_label[0] == '\0' || cfg.ipv4_addr == 0) {
        usage(argv[0]);
        return 2;
    }

    if (validate_single_dns_label(cfg.instance_name, "instance name") != 0 ||
        validate_single_dns_label(cfg.host_label, "host label") != 0) {
        return 2;
    }

    snprintf(cfg.host_fqdn, sizeof(cfg.host_fqdn), "%s.local.", cfg.host_label);

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    sockfd = open_mdns_socket();
    if (sockfd < 0) {
        return 1;
    }

    memset(&mdns_dest, 0, sizeof(mdns_dest));
    mdns_dest.sin_family = AF_INET;
    mdns_dest.sin_port = htons(MDNS_PORT);
    mdns_dest.sin_addr.s_addr = inet_addr(MDNS_GROUP);

    if (send_announcement(sockfd, &mdns_dest, &cfg) != 0) {
        perror("send_announcement");
    }
    last_announce = time(NULL);

    while (!g_stop) {
        fd_set rfds;
        struct timeval tv;
        uint8_t packet[BUF_SIZE];
        ssize_t nread;

        FD_ZERO(&rfds);
        FD_SET(sockfd, &rfds);
        tv.tv_sec = 1;
        tv.tv_usec = 0;

        if (select(sockfd + 1, &rfds, NULL, NULL, &tv) > 0 && FD_ISSET(sockfd, &rfds)) {
            struct sockaddr_in src;
            socklen_t src_len = sizeof(src);
            nread = recvfrom(sockfd, packet, sizeof(packet), 0, (struct sockaddr *)&src, &src_len);
            if (nread > 0) {
                (void)handle_query(sockfd, packet, (size_t)nread, &mdns_dest, &cfg);
            }
        }

        if (time(NULL) - last_announce >= ANNOUNCE_INTERVAL) {
            (void)send_announcement(sockfd, &mdns_dest, &cfg);
            last_announce = time(NULL);
        }
    }

    close(sockfd);
    return 0;
}
