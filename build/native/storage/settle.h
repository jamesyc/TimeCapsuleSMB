#ifndef TC_STORAGE_SETTLE_H
#define TC_STORAGE_SETTLE_H
#include "mast.h"

#define TC_STORAGE_SETTLE_MS 5000
struct tc_storage_settle {
    struct tc_inventory stable, candidate;
    int initialized, pending;
    long long confirm_at;
};
/* Feed only successful MaSt parses. An unavailable read is not an empty NAS.
 * Returns 1 on first inventory or a confirmed topology change. */
int tc_storage_observe(struct tc_storage_settle *, const struct tc_inventory *, long long now);
#endif
