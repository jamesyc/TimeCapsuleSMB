#ifndef TC_STORAGE_MAST_H
#define TC_STORAGE_MAST_H
#include "../common/platform.h"

#define TC_MAX_VOLUMES 16
#define TC_VOLUME_NAME_MAX 512
#define TC_MAST_MAX 65536
#ifndef TC_VOLUMES_ROOT
#define TC_VOLUMES_ROOT "/Volumes"
#endif

struct tc_volume {
    char disk[16], device[16], root[256], name[TC_VOLUME_NAME_MAX], uuid[37];
    int builtin;
    int users; /* -1 means unavailable, not zero users */
};
struct tc_inventory {
    struct tc_volume volumes[TC_MAX_VOLUMES];
    size_t count;
};

/* A complete empty array is success. Malformed/truncated observations never
 * become a diskless inventory. Both Apple's text plist and XML are accepted. */
int tc_mast_parse(struct tc_inventory *out, const char *text, size_t length);
int tc_inventory_same_topology(const struct tc_inventory *a, const struct tc_inventory *b);
#endif
