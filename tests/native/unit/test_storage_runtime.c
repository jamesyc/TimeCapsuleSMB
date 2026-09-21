#include "storage/runtime.h"
#include <assert.h>
#include <sys/stat.h>

int main(int argc, char **argv) {
    struct tc_inventory inventory;
    struct tc_storage_snapshot storage, previous;
    struct tc_runtime_config config;
    char text[TC_MAST_MAX + 1];
    size_t length, i;
    if (argc == 4 && !strcmp(argv[1], "mounted")) {
        struct tc_volume volume = {0};
        int writable, guard;
        snprintf(volume.root, sizeof(volume.root), "%s", argv[2]);
        snprintf(volume.device, sizeof(volume.device), "%s", argv[3]);
        assert(tc_volume_mounted(&volume, &writable) == 1 && writable);
        guard = tc_storage_guard(&volume);
        assert(guard >= 0 && tc_storage_guard_valid(&volume, guard));
        close(guard);
        strcpy(volume.device, "dk999");
        assert(tc_volume_mounted(&volume, &writable) == 0);
        puts("live mount and root guard passed");
        return 0;
    }
    length = fread(text, 1, sizeof(text), stdin);
    if (tc_mast_parse(&inventory, text, length) || tc_runtime_config_load(&config))
        return 2;
    if (argc > 1 && !strcmp(argv[1], "guard")) {
        const struct tc_volume *volume = &inventory.volumes[0];
        char old[288];
        int fd = tc_storage_guard(volume);
        assert(fd >= 0 && tc_storage_guard_valid(volume, fd));
        snprintf(old, sizeof(old), "%s.old", volume->root);
        assert(!rename(volume->root, old) && !mkdir(volume->root, 0755));
        /* Apple's reattached dkN can reuse its old path. A changed root must
         * invalidate the active operation even before topology debounce ends. */
        assert(!tc_storage_guard_valid(volume, fd));
        close(fd);
        assert(!rmdir(volume->root) && !rename(old, volume->root));
        return 0;
    }
    memset(&previous, 0, sizeof(previous));
    previous.payload_index = -1;
    if (argc > 1 && !strcmp(argv[1], "already-active")) {
        previous.inventory = inventory;
        previous.available = 0xffff;
    }
    if (tc_storage_prepare(&storage, &inventory, &previous, &config, 0, UINT32_MAX))
        return 3;
    if (argc > 1 && !strcmp(argv[1], "retry-cache")) {
        char path[320];
        struct tc_volume swap;
        assert(storage.retry_payload == 1 && storage.payload_index == 1);
        previous = storage;
        snprintf(path, sizeof(path), "%s/.samba4/private", inventory.volumes[0].root);
        assert(!mkdir(path, 0700));
        snprintf(path, sizeof(path), "%s/.samba4/smbd", inventory.volumes[1].root);
        assert(!unlink(path)); /* Healthy candidates must not be re-statted. */
        swap = inventory.volumes[0]; inventory.volumes[0] = inventory.volumes[1]; inventory.volumes[1] = swap;
        assert(!tc_storage_prepare(&storage, &inventory, &previous, &config, 0, previous.retry_payload));
        assert(storage.payload_valid == 3 && !storage.retry_payload && !storage.retry_prepare);
        assert(storage.payload_index == 1); /* Internal UUID moved, priority did not. */
    }
    printf("available=%u payload=%s shares=%zu\n", storage.available, storage.payload, storage.shares.count);
    for (i = 0; i < storage.shares.count; i++)
        printf("share=%s path=%s\n", storage.shares.values[i].name, storage.shares.values[i].path);
    return 0;
}
