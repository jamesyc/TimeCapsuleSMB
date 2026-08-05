#include <stdio.h>
#include <string.h>
#include "mdns/mdns.h"

int main(void) {
    unsigned char packet[BUF_SIZE];
    size_t off = 0;
    size_t cursor = 0;
    char decoded[MAX_NAME];
    char host_fqdn[MAX_NAME];
    char instance_fqdn[MAX_NAME];
    const char *raw_name = "A.B.'s AirPort Time Capsule";
    const char *expected_host = "A\\.B\\.'s AirPort Time Capsule.local.";
    const char *expected_instance = "A\\.B\\.'s AirPort Time Capsule._smb._tcp.local.";
    size_t raw_len = strlen(raw_name);

    if (validate_generated_dns_label(raw_name, "host label") != 0) {
        return 1;
    }
    if (build_host_fqdn(host_fqdn, sizeof(host_fqdn), raw_name) != 0) {
        return 2;
    }
    if (strcmp(host_fqdn, expected_host) != 0) {
        fprintf(stderr, "host_fqdn=%s\n", host_fqdn);
        return 3;
    }
    if (encode_name(packet, &off, sizeof(packet), host_fqdn) != 0) {
        return 4;
    }
    if (packet[0] != (unsigned char)raw_len || memcmp(packet + 1, raw_name, raw_len) != 0) {
        return 5;
    }
    if (packet[raw_len + 1] != 5 ||
        memcmp(packet + raw_len + 2, "local", 5) != 0 ||
        packet[raw_len + 7] != 0 ||
        off != raw_len + 8) {
        return 6;
    }
    if (decode_name(packet, off, &cursor, decoded, sizeof(decoded)) != 0 ||
        cursor != off ||
        !name_equals(decoded, expected_host)) {
        fprintf(stderr, "decoded=%s cursor=%lu off=%lu\n", decoded, (unsigned long)cursor, (unsigned long)off);
        return 7;
    }
    if (build_instance_fqdn(instance_fqdn, sizeof(instance_fqdn), raw_name, "_smb._tcp.local.") != 0) {
        return 9;
    }
    if (strcmp(instance_fqdn, expected_instance) != 0) {
        fprintf(stderr, "instance_fqdn=%s\n", instance_fqdn);
        return 10;
    }
    if (build_host_fqdn(host_fqdn, sizeof(host_fqdn), "Time Capsule") != 0 ||
        strcmp(host_fqdn, "Time Capsule.local.") != 0) {
        fprintf(stderr, "plain_host=%s\n", host_fqdn);
        return 12;
    }
    return 0;
}
