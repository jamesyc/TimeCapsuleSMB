#ifndef TC_STORAGE_SHARES_H
#define TC_STORAGE_SHARES_H
#include "mast.h"
#define TC_SHARE_NAME_MAX 256
struct tc_share {
    char name[TC_SHARE_NAME_MAX], path[288], device[16], uuid[37];
    int builtin;
};
struct tc_share_set {
    struct tc_share values[TC_MAX_VOLUMES];
    size_t count;
};

/* Availability is a live mount/writability observation supplied by the
 * manager. Projection itself never touches a disk or creates ShareRoot. */
int tc_shares_build(struct tc_share_set *, const struct tc_inventory *, uint32_t available, int internal_root,
                    int advertise_afp);
int tc_shares_equal(const struct tc_share_set *, const struct tc_share_set *);
#endif
