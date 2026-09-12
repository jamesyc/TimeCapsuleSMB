#include <stdio.h>
#include <string.h>
#include "mdns/mdns.h"

int main(void) {
    char storage[RIOUSBPRINT_MAX_TXT_ITEMS][MAX_TXT_STRING + 1];
    const char *txts[RIOUSBPRINT_MAX_TXT_ITEMS];
    size_t txt_count = 0;
    char out[MAX_TXT_STRING + 1];
    unsigned char device_id[] = {0, 10, 'C', 'M', 'D', ':', 'P', 'W', 'G', ';'};

    if (append_txt_itemf(NULL, txts, &txt_count, RIOUSBPRINT_MAX_TXT_ITEMS, "txtvers=1") == 0) {
        return 1;
    }
    if (append_txt_itemf(storage, NULL, &txt_count, RIOUSBPRINT_MAX_TXT_ITEMS, "txtvers=1") == 0) {
        return 2;
    }
    if (append_txt_itemf(storage, txts, NULL, RIOUSBPRINT_MAX_TXT_ITEMS, "txtvers=1") == 0) {
        return 3;
    }
    if (append_txt_itemf(storage, txts, &txt_count, RIOUSBPRINT_MAX_TXT_ITEMS, NULL) == 0) {
        return 4;
    }
    txt_count = RIOUSBPRINT_MAX_TXT_ITEMS;
    if (append_txt_itemf(storage, txts, &txt_count, RIOUSBPRINT_MAX_TXT_ITEMS, "txtvers=1") == 0) {
        return 5;
    }

    if (build_riousbprint_pdl(NULL, sizeof(out), "PWG") == 0) {
        return 6;
    }
    if (build_riousbprint_pdl(out, 0, "PWG") == 0) {
        return 7;
    }
    if (build_riousbprint_pdl(out, sizeof(out), NULL) == 0) {
        return 8;
    }

    if (ieee1284_lookup_field(NULL, sizeof(out), device_id + 2, sizeof(device_id) - 2, "CMD") == 0) {
        return 9;
    }
    if (extract_cmd_from_ieee1284_device_id(NULL, sizeof(out), device_id, sizeof(device_id)) == 0) {
        return 10;
    }
    if (extract_cmd_from_ieee1284_device_id(out, 0, device_id, sizeof(device_id)) == 0) {
        return 11;
    }
    if (extract_cmd_from_ieee1284_device_id(out, sizeof(out), NULL, sizeof(device_id)) == 0) {
        return 12;
    }

    printf("ok\n");
    return 0;
}
