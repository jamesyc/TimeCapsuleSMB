/* identity.c normalizers vs probe.py: <case> instance|netbios|host|mac <value> */
#include "common/plan.h"
int main(int argc, char **argv) {
    char out[64];
    int rc;
    if (argc != 3) return 2;
    if (!strcmp(argv[1], "instance")) rc = normalize_instance_name(out, sizeof(out), argv[2]);
    else if (!strcmp(argv[1], "netbios")) rc = normalize_netbios_name(out, sizeof(out), argv[2]);
    else if (!strcmp(argv[1], "host")) rc = normalize_host_label(out, sizeof(out), argv[2]);
    else if (!strcmp(argv[1], "mac")) rc = normalize_mac_text(out, sizeof(out), argv[2]);
    else return 2;
    printf("%d:%s\n", rc, out);
    return 0;
}
