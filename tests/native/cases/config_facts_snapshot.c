#include <stdio.h>
#include "common/plan.h"

int main(int argc, char **argv) {
    struct device_config config;
    int rc;
    if (argc != 2) return 2;
    rc = device_facts_read_config(&config, argv[1]);
    printf("rc=%d afp=%d nbns=%d debug=%d\n", rc, config.advertise_afp,
           config.nbns_enabled, config.debug_logging);
    return 0;
}
