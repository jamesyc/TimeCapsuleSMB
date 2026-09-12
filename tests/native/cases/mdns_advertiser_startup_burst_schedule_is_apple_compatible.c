#include <stdio.h>

#include "mdns/mdns.h"

int main(void) {
    static const unsigned int expected[STARTUP_BURST_COUNT] = {0, 1000, 3000, 7000};
    size_t i;

    if (STARTUP_BURST_COUNT != 4) {
        return 1;
    }
    for (i = 0; i < STARTUP_BURST_COUNT; i++) {
        if (g_startup_burst_offsets_ms[i] != expected[i]) {
            return 2;
        }
    }
    printf("ok\n");
    return 0;
}
