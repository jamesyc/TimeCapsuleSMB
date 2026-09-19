#include "../../../build/native/storage/shares.h"

int main(void) {
    struct tc_inventory inventory;
    struct tc_share_set shares;
    memset(&inventory, 0, sizeof(inventory));
    inventory.valid = 1; inventory.count = 3;
    strcpy(inventory.volumes[0].device, "dk2"); strcpy(inventory.volumes[0].root, "/tmp");
    strcpy(inventory.volumes[0].name, "Caf\303\251 [Data]"); strcpy(inventory.volumes[0].uuid, "11111111-1111-1111-1111-111111111111");
    inventory.volumes[0].available = inventory.volumes[0].writable = inventory.volumes[0].builtin = 1;
    inventory.volumes[1] = inventory.volumes[0]; strcpy(inventory.volumes[1].device, "dk3");
    strcpy(inventory.volumes[1].uuid, "22222222-2222-2222-2222-222222222222"); inventory.volumes[1].builtin = 0;
    inventory.volumes[2] = inventory.volumes[0]; strcpy(inventory.volumes[2].device, "dk4");
    inventory.volumes[2].available = 0;
    if (tc_shares_build(&shares, &inventory, 1) != 0 || shares.count != 2) return 1;
    if (strcmp(shares.values[0].name, "Caf\303\251 _Data_") != 0) return 2;
    if (strcmp(shares.values[1].name, "Caf\303\251 _Data_ (dk3)") != 0) return 3;
    if (strcmp(shares.values[0].path, "/tmp") != 0 || strcmp(shares.values[1].path, "/tmp") != 0) return 4;
    return 0;
}
