#include "samba/staging.h"
#include <assert.h>

int tc_samba_identity_read(struct tc_samba_identity *identity) {
    memset(identity, 0, sizeof(*identity));
    strcpy(identity->netbios, "CAPSULE");
    strcpy(identity->server, "Time Capsule");
    strcpy(identity->model, "TimeCapsule6,116");
    return 0;
}
int device_nt_hash(char hash[33]) {
    strcpy(hash, "0123456789ABCDEF0123456789ABCDEF");
    return getenv("FAIL_HASH") ? -1 : 0;
}
int main(int argc, char **argv) {
    struct tc_samba_settings settings;
    struct tc_storage_snapshot storage;
    struct tc_inventory inventory;
    int result;
    char text[TC_MAST_MAX + 1];
    size_t length = fread(text, 1, sizeof(text), stdin);
    assert(argc == 2);
    if (!strcmp(argv[1], "clear-locks")) return tc_samba_clear_locks() ? 5 : 0;
    if (tc_samba_settings_read(&settings) || tc_mast_parse(&inventory, text, length))
        return 2;
    memset(&storage, 0, sizeof(storage));
    storage.inventory = inventory;
    storage.available = 1;
    storage.payload_index = 0;
    snprintf(storage.payload, sizeof(storage.payload), "%s/.samba4", inventory.volumes[0].root);
    snprintf(storage.smbd_source, sizeof(storage.smbd_source), "%s/smbd", storage.payload);
    assert(!tc_shares_build(&storage.shares, &inventory, 1, 0, 0));
    result = tc_samba_stage(&storage, &settings, "127.0.0.1/8 192.0.2.1/24", !strcmp(argv[1], "copy"),
                            !strcmp(argv[1], "copy"));
    if (result)
        return 3;
    if (!strcmp(argv[1], "discard")) {
        tc_samba_discard();
        return 0;
    }
    return tc_samba_publish(settings.config.rsync) ? 4 : 0;
}
