#include <stdio.h>
#include "mdns/mdns.h"

int main(void) {
    char out[256];
    if (build_adisk_system_txt(out, sizeof(out), "80:ea:96:e6:58:68") != 0) {
        return 1;
    }
    puts(out);
    return 0;
}
