#include <stdio.h>
#include <string.h>
#include "mdns/mdns.h"

static int read_first_rr_class(const unsigned char *packet, size_t packet_len, unsigned short *out_class) {
    char name[MAX_NAME];
    size_t cursor = 0;
    unsigned short rrtype;
    unsigned short rrclass;

    if (decode_name(packet, packet_len, &cursor, name, sizeof(name)) != 0 || cursor + 10 > packet_len) {
        return -1;
    }
    memcpy(&rrtype, packet + cursor, 2);
    memcpy(&rrclass, packet + cursor + 2, 2);
    (void)rrtype;
    *out_class = ntohs(rrclass);
    return 0;
}

int main(void) {
    uint8_t buf[BUF_SIZE];
    size_t off;
    unsigned short rrclass;
    uint32_t ipv4;
    const char *txts[1] = {"k=v"};

    off = 0;
    if (add_rr_ptr(buf, &off, sizeof(buf), "_smb._tcp.local.", "Home._smb._tcp.local.", 120) != 0 ||
        read_first_rr_class(buf, off, &rrclass) != 0 ||
        rrclass != DNS_CLASS_IN) {
        return 1;
    }

    off = 0;
    if (add_rr_srv(buf, &off, sizeof(buf), "Home._smb._tcp.local.", "home.local.", 445, 120) != 0 ||
        read_first_rr_class(buf, off, &rrclass) != 0 ||
        rrclass != DNS_CLASS_IN_UNIQUE) {
        return 2;
    }

    off = 0;
    if (add_rr_txt_empty(buf, &off, sizeof(buf), "Home._smb._tcp.local.", 120) != 0 ||
        read_first_rr_class(buf, off, &rrclass) != 0 ||
        rrclass != DNS_CLASS_IN_UNIQUE) {
        return 3;
    }

    off = 0;
    if (add_rr_txt_items(buf, &off, sizeof(buf), "Home._adisk._tcp.local.", 120, txts, NULL, 1) != 0 ||
        read_first_rr_class(buf, off, &rrclass) != 0 ||
        rrclass != DNS_CLASS_IN_UNIQUE) {
        return 4;
    }

    if (inet_pton(AF_INET, "10.0.1.1", &ipv4) != 1) {
        return 5;
    }
    off = 0;
    if (add_rr_a(buf, &off, sizeof(buf), "home.local.", ipv4, 120) != 0 ||
        read_first_rr_class(buf, off, &rrclass) != 0 ||
        rrclass != DNS_CLASS_IN_UNIQUE) {
        return 6;
    }

    return 0;
}
