#include "common/iflist.h"

/* Heartbeat tests must not depend on the host's VPNs or kernel layout.
 * TC_TEST_IFLIST selects a table:
 *   failed            the sysctl read fails
 *   sockaddr|links|addrs  an incomplete snapshot with that cause
 *   extenders         bridge0 and lo0 plus 30 address-less interfaces
 *   worst             TC_MAX_LINKS links, each with a service address and a
 *                     15-byte name of control characters (fully escaped), in
 *                     a snapshot marked incomplete with ten-digit kernel
 *                     totals, so plan_error and both counts are sent too */
static void add_link(struct if_table *table, const char *name, unsigned index) {
    struct if_link *link = &table->links[table->link_count++];
    strncpy(link->name, name, sizeof(link->name) - 1);
    link->index = index;
    table->kernel_link_count++;
}

static void add_ipv4(struct if_table *table, unsigned index, const char *text, unsigned prefix) {
    struct if_addr *addr = &table->addrs[table->addr_count++];
    addr->owner_index = index;
    addr->scope = index;
    addr->family = AF_INET;
    addr->prefix = prefix;
    inet_pton(AF_INET, text, &addr->v4);
    table->kernel_addr_count++;
}

int iflist_collect(struct if_table *table) {
    const char *mode = getenv("TC_TEST_IFLIST");
    memset(table, 0, sizeof(*table));
    if (mode == NULL) return 0;
    if (!strcmp(mode, "failed")) return -1;
    if (!strcmp(mode, "extenders")) {
        char name[IFNAMSIZ];
        unsigned i;
        add_link(table, "lo0", 1);
        add_ipv4(table, 1, "127.0.0.1", 8);
        add_link(table, "bridge0", 2);
        add_ipv4(table, 2, "192.0.2.10", 24);
        for (i = 0; i < 30; i++) {
            snprintf(name, sizeof(name), "wds%u", i);
            add_link(table, name, 10 + i);
        }
        return 0;
    }
    if (!strcmp(mode, "worst")) {
        char name[IFNAMSIZ], text[16];
        unsigned i;
        memset(name, 1, IFNAMSIZ - 1);
        name[IFNAMSIZ - 1] = '\0';
        for (i = 0; i < TC_MAX_LINKS; i++) {
            add_link(table, name, 10 + i);
            snprintf(text, sizeof(text), "10.%u.0.1", i);
            add_ipv4(table, 10 + i, text, 24);
        }
        table->truncation = IFLIST_TRUNC_LINKS;
        table->kernel_link_count = 4294967295UL;
        table->kernel_addr_count = 4294967295UL;
        return 0;
    }
    if (iflist_truncation_from_name(mode, &table->truncation) != 0) return -1;
    /* What the device reported: 40 interfaces and 12 addresses in the kernel. */
    table->kernel_link_count = 40;
    table->kernel_addr_count = 12;
    return 0;
}
