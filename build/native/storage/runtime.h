#ifndef TC_STORAGE_RUNTIME_H
#define TC_STORAGE_RUNTIME_H
#include "../samba/runtime.h"

struct tc_storage_snapshot {
    struct tc_inventory inventory;
    struct tc_share_set shares;
    uint32_t available;
    /* Outcomes are indexed by this snapshot's inventory, never current dkN
     * order. A partial report is usable while its explicit failures retry. */
    uint32_t mounted, readonly, retry_prepare, retry_payload, payload_valid, payload_legacy;
    int payload_index;
    char payload[288], smbd_source[320];
};
/* 1 mounted as the expected HFS device, 0 unavailable, -1 probe failed. */
int tc_volume_mounted(const struct tc_volume *, int *writable);
int tc_storage_prepare(struct tc_storage_snapshot *, const struct tc_inventory *,
                       const struct tc_storage_snapshot *previous, const struct tc_runtime_config *,
                       int tune_ata, uint32_t requested);
int tc_storage_refresh_needed(const struct tc_storage_snapshot *, const struct tc_inventory *);
int tc_storage_guard(const struct tc_volume *);
int tc_storage_guard_valid(const struct tc_volume *, int guard);
#endif
