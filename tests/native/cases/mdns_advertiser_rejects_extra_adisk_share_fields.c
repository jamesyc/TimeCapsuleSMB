#include <stdio.h>
#include <string.h>
#include "mdns/mdns.h"

int main(void) {
    struct config cfg;
    char path[] = "/tmp/tcapsulesmb-adisk-extra-XXXXXX";
    int fd;
    FILE *fp;
    int rc;

    memset(&cfg, 0, sizeof(cfg));
    fd = mkstemp(path);
    if (fd < 0) {
        return 1;
    }
    fp = fdopen(fd, "w");
    if (fp == NULL) {
        close(fd);
        unlink(path);
        return 2;
    }
    fputs("Data\tdk2\t12345678-1234-1234-1234-123456789012\t0x1093\textra\n", fp);
    fclose(fp);

    rc = parse_adisk_shares_file(&cfg, path);
    unlink(path);
    return rc == 0 ? 3 : 0;
}
