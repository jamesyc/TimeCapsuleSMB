#include <stdio.h>
#include "discovery/discovery.h"

int main(void) {
    char out[256];
    if (build_adisk_disk_txt(out, sizeof(out), "dk2", "Data", "12345678-1234-1234-1234-123456789012", "0x82") != 0) {
        return 1;
    }
    puts(out);
    return 0;
}
