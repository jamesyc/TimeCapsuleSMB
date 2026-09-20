#include "samba/runtime.h"
#include <assert.h>

int main(int argc, char **argv) {
    struct tc_inventory inventory;
    struct tc_share_set shares;
    struct tc_runtime_config config;
    struct tc_samba_identity identity = {"TESTCAPSULE", "Test Capsule", "TimeCapsule6,116", 1};
    char text[TC_MAST_MAX + 1];
    size_t length = fread(text, 1, sizeof(text), stdin), i;
    uint32_t available = 0xffff;
    if (tc_mast_parse(&inventory, text, length) || tc_runtime_config_load(&config))
        return 2;
    for (i = 1; i < (size_t)argc; i++) {
        if (!strcmp(argv[i], "netbsd4"))
            config.netbsd4 = 1;
        else if (!strcmp(argv[i], "skip-first"))
            available &= ~1u;
    }
    if (tc_shares_build(&shares, &inventory, available, config.internal_root, config.advertise_afp))
        return 3;
    assert(tc_shares_equal(&shares, &shares));
    return tc_samba_render(stdout, &config, &identity, "127.0.0.1/8 ::1/128 192.0.2.3/24",
                           "/Volumes/dk2/.samba4", &shares);
}
