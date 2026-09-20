#ifndef TC_STORAGE_RUNTIME_H
#define TC_STORAGE_RUNTIME_H
#include "../samba/runtime.h"

struct tc_storage_snapshot {
    struct tc_inventory inventory;
    struct tc_share_set shares;
    uint32_t available;
    int payload_index;
    char payload[288], smbd_source[320];
};
/* 1 mounted as the expected HFS device, 0 unavailable, -1 probe failed. */
int tc_volume_mounted(const struct tc_volume *, int *writable);
int tc_storage_prepare(struct tc_storage_snapshot *, const struct tc_inventory *,
                       const struct tc_storage_snapshot *previous, const struct tc_runtime_config *,
                       int tune_ata);
int tc_storage_guard(const struct tc_volume *);
int tc_storage_guard_valid(const struct tc_volume *, int guard);
#endif
