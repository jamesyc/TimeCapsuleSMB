#include "storage/settle.h"
#include <assert.h>

int main(void) {
    struct tc_storage_settle state = {0};
    struct tc_storage_retry retry = {0};
    struct tc_inventory a = {0}, b, c;
    a.count = 1;
    strcpy(a.volumes[0].device, "dk2");
    strcpy(a.volumes[0].uuid, "first");
    a.volumes[0].users = 1;
    b = a;
    strcpy(b.volumes[0].uuid, "second");
    c = b;
    strcpy(c.volumes[0].uuid, "third");
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
    tc_storage_retry_finish(&retry, 100, 1);
    assert(retry.at == 5100 && retry.failures == 1);
    /* Observation traffic does not call finish or move the absolute deadline. */
    assert(5099 < retry.at && 5100 == retry.at);
    tc_storage_retry_finish(&retry, 5200, 1);
    assert(retry.at == 20200 && retry.failures == 2);
    tc_storage_retry_finish(&retry, 20300, 1);
    assert(retry.at == 80300 && retry.failures == 2);
    tc_storage_retry_finish(&retry, 80400, 1);
    assert(retry.at == 140400 && retry.failures == 2);
    tc_storage_retry_finish(&retry, 90000, 0);
    assert(!retry.at && !retry.failures);
    tc_storage_retry_finish(&retry, 91000, 1);
    assert(retry.at == 96000);
    return 0;
}
