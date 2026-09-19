#ifndef TC_STORAGE_SHARES_H
#define TC_STORAGE_SHARES_H

#include "mast.h"

#define TC_MAX_SHARES TC_MAX_VOLUMES
#define TC_SHARE_NAME_MAX 65

struct tc_share {
    char name[TC_SHARE_NAME_MAX];
    char path[96];
    char device[16];
    char uuid[37];
    int builtin;
};

struct tc_share_set {
    struct tc_share values[TC_MAX_SHARES];
    size_t count;
};

int tc_shares_build(struct tc_share_set *shares, const struct tc_inventory *inventory,
                    int internal_uses_root);

#endif
