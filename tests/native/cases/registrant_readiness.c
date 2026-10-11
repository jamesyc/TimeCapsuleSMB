/* registrant_readiness over hand-built registrant states: prints one
 * "label state registered/desired" line per state. */
#include <stdio.h>
#include <string.h>
#include "discovery/registrant.h"

static void show(const char *label, const struct registrant *reg) {
    unsigned registered, desired;
    const char *state = registrant_readiness(reg, &registered, &desired);
    printf("%s %s %u/%u\n", label, state, registered, desired);
}

static void add(struct registrant *reg, size_t slot, enum reg_status status) {
    reg->entries[slot].in_use = 1;
    reg->entries[slot].status = status;
}

int main(void) {
    struct registrant reg;

    memset(&reg, 0, sizeof(reg));
    show("empty", &reg);
    add(&reg, 0, REG_REGISTERED);
    add(&reg, 1, REG_PENDING);
    show("pending", &reg);
    reg.entries[1].status = REG_REGISTERED;
    show("all", &reg);
    /* A slot freed by a plan change does not count. */
    reg.entries[3].status = REG_DEGRADED;
    show("unused", &reg);
    add(&reg, 2, REG_CONFLICT);
    show("conflict", &reg);
    reg.entries[2].status = REG_DEGRADED;
    show("degraded", &reg);
    reg.daemon_unreachable = 1;
    show("unreachable", &reg);
    memset(&reg, 0, sizeof(reg));
    reg.daemon_unreachable = 1;
    show("unreachable-empty", &reg);
    return 0;
}
