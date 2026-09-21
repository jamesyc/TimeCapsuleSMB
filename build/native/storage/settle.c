#include "settle.h"

void tc_storage_retry_finish(struct tc_storage_retry *retry, long long now, int pending) {
    static const unsigned delays[] = {5000, 15000, 60000};
    if (!pending) { retry->at = 0; retry->failures = 0; return; }
    retry->at = now + delays[retry->failures];
    if (retry->failures < 2) retry->failures++;
}

int tc_storage_observe(struct tc_storage_settle *state, const struct tc_inventory *inventory, long long now) {
    if (!state->initialized) {
        state->stable = *inventory;
        state->initialized = 1;
        return 1;
    }
    if (tc_inventory_same_topology(&state->stable, inventory)) {
        state->stable = *inventory; /* users=0 still needs an activation check */
        state->pending = 0;
        state->confirm_at = 0;
        return 0;
    }
    if (!state->pending || !tc_inventory_same_topology(&state->candidate, inventory)) {
        state->candidate = *inventory;
        state->confirm_at = now + TC_STORAGE_SETTLE_MS;
        state->pending = 1;
        return 0;
    }
    if (now < state->confirm_at)
        return 0;
    state->stable = *inventory;
    state->pending = 0;
    state->confirm_at = 0;
    return 1;
}
