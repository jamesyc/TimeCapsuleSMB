#include <string.h>
#include "mdns/mdns.h"

int main(int argc, char **argv) {
    struct config cfg;

    if (argc != 4) {
        return 99;
    }
    memset(&cfg, 0, sizeof(cfg));
    cfg.diskless = strcmp(argv[1], "diskless") == 0;
    if (strcmp(argv[2], "-") != 0 && add_adisk_disk_config(&cfg, "Data", "dk2", argv[2], "0x82") != 0) {
        return EXIT_INVALID_ADISK_DISK;
    }
    if (adisk_enabled(&cfg) && build_adisk_system_txt((char[128]){0}, 128, argv[3]) != 0) {
        return EXIT_INVALID_ADISK_SYSTEM;
    }
    return EXIT_OK;
}
