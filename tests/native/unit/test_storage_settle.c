#include "storage/settle.h"
#include <assert.h>

int main(void) {
    struct tc_storage_settle state = {0};
    struct tc_inventory a = {0}, b, c;
    a.count = 1;
    strcpy(a.volumes[0].device, "dk2"); strcpy(a.volumes[0].uuid, "first");
    a.volumes[0].users = 1;
    b = a; strcpy(b.volumes[0].uuid, "second");
    c = b; strcpy(c.volumes[0].uuid, "third");
    assert(tc_storage_observe(&state, &a, 100));
    /* Apple's volatile users count is an activation hint, not topology. */
    a.volumes[0].users = 0;
    assert(!tc_storage_observe(&state, &a, 101) && state.stable.volumes[0].users == 0);
    assert(!tc_storage_observe(&state, &b, 200));
    assert(state.confirm_at == 5200);
    assert(!tc_storage_observe(&state, &b, 4000) && state.confirm_at == 5200);
    assert(!tc_storage_observe(&state, &a, 5000) && !state.pending);
    assert(!tc_storage_observe(&state, &b, 6000));
    assert(!tc_storage_observe(&state, &c, 7000) && state.confirm_at == 12000);
    assert(!tc_storage_observe(&state, &c, 11999));
    /* A failed confirmation has no observation to feed. A later valid one
     * confirms without treating the failed read as physical removal. */
    assert(tc_storage_observe(&state, &c, 14000));
    assert(!strcmp(state.stable.volumes[0].uuid, "third") && !state.pending);
    b.count = 0;
    assert(!tc_storage_observe(&state, &b, 15000));
    assert(tc_storage_observe(&state, &b, 20000) && state.stable.count == 0);
    return 0;
}
