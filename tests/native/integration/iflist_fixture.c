#include "common/iflist.h"

/* Heartbeat tests must not depend on the host's VPNs or kernel layout. */
int iflist_collect(struct if_table *table) {
    const char *mode = getenv("TC_TEST_IFLIST");
    memset(table, 0, sizeof(*table));
    if (mode != NULL && !strcmp(mode, "failed")) return -1;
    table->truncated = mode != NULL && !strcmp(mode, "truncated");
    return 0;
}
