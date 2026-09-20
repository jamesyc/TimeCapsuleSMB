#include "storage/mast.h"
#include <assert.h>

int main(int argc, char **argv) {
    struct tc_inventory a, b;
    char text[TC_MAST_MAX + 1];
    size_t n = fread(text, 1, sizeof(text), stdin), i, j;
    int rc = tc_mast_parse(&a, text, n);
    if (rc) {
        assert(a.count == 0);
        return 2;
    }
    if (argc == 2 && !strcmp(argv[1], "topology")) {
        b = a;
        assert(tc_inventory_same_topology(&a, &b));
        for (i = 0; i < b.count; i++)
            b.volumes[i].users = 73;
        assert(tc_inventory_same_topology(&a, &b));
        if (b.count > 1) {
            struct tc_volume swap = b.volumes[0];
            b.volumes[0] = b.volumes[1];
            b.volumes[1] = swap;
        }
        assert(tc_inventory_same_topology(&a, &b));
        if (b.count) {
            b.volumes[0].uuid[0] = b.volumes[0].uuid[0] == 'a' ? 'b' : 'a';
            assert(!tc_inventory_same_topology(&a, &b));
        }
    }
    printf("%zu\n", a.count);
    for (i = 0; i < a.count; i++) {
        struct tc_volume *v = &a.volumes[i];
        printf("%s %s %s %d %d ", v->disk, v->device, v->uuid, v->builtin, v->users);
        for (j = 0; v->name[j]; j++)
            printf("%02x", (unsigned char)v->name[j]);
        putchar('\n');
    }
    return 0;
}
