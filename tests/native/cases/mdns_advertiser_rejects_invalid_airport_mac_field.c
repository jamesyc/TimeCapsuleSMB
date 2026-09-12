#include <stdio.h>
#include <string.h>
#include "mdns/mdns.h"

int main(void) {
    struct config cfg;
    char out[256];

    memset(&cfg, 0, sizeof(cfg));
    snprintf(cfg.airport_wama, sizeof(cfg.airport_wama), "%s", "80:ea:96:e6:58");
    snprintf(cfg.airport_syap, sizeof(cfg.airport_syap), "%s", "119");

    if (build_airport_txt(out, sizeof(out), &cfg) == 0) {
        return 1;
    }
    return 0;
}
