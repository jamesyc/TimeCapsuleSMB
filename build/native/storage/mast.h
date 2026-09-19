#ifndef TC_STORAGE_MAST_H
#define TC_STORAGE_MAST_H

#include "../common/platform.h"

#define TC_MAX_VOLUMES 16
#define TC_STORAGE_NAME_MAX 128

struct tc_volume {
    char disk[16];
    char device[16];
    char root[64];
    char name[TC_STORAGE_NAME_MAX];
    char uuid[37];
    int builtin;
    int hfs;
    int available;
    int writable;
    uint64_t mount_identity;
};

struct tc_inventory {
    struct tc_volume volumes[TC_MAX_VOLUMES];
    size_t count;
    int valid;
    int empty;
};

int tc_mast_parse(struct tc_inventory *inventory, const char *text);
int tc_mast_collect(struct tc_inventory *inventory);
int tc_mast_print(FILE *stream);
int tc_storage_activate(struct tc_volume *volume);
int tc_storage_verify_identity(const struct tc_volume *volume);

#endif
