#include <stdio.h>
#include <string.h>
#include "mdns/mdns.h"

static int has_txt(const char *txts[], size_t count, const char *want) {
    size_t i;
    for (i = 0; i < count; i++) {
        if (strcmp(txts[i], want) == 0) {
            return 1;
        }
    }
    return 0;
}

int main(void) {
    struct config cfg;
    char storage[PDL_DATASTREAM_MAX_TXT_ITEMS][MAX_TXT_STRING + 1];
    const char *txts[PDL_DATASTREAM_MAX_TXT_ITEMS];
    size_t txt_count = 0;

    memset(&cfg, 0, sizeof(cfg));
    snprintf(cfg.instance_name, sizeof(cfg.instance_name), "%s", "James's AirPort Time Capsule");
    snprintf(cfg.riousbprint_instance_name, sizeof(cfg.riousbprint_instance_name), "%s", "Canon MP490 series");
    snprintf(cfg.riousbprint_note, sizeof(cfg.riousbprint_note), "%s", "James's AirPort Time Capsule");
    snprintf(cfg.riousbprint_mfg, sizeof(cfg.riousbprint_mfg), "%s", "Canon");
    snprintf(cfg.riousbprint_mdl, sizeof(cfg.riousbprint_mdl), "%s", "MP490 series");
    snprintf(cfg.riousbprint_serial, sizeof(cfg.riousbprint_serial), "%s", "C0958C");
    snprintf(cfg.riousbprint_cmd, sizeof(cfg.riousbprint_cmd), "%s", "BJL,BJRaster3,BSCCe,IVEC,IVECPLI");

    if (build_pdl_datastream_txt_items(&cfg, storage, txts, &txt_count) != 0) {
        return 1;
    }
    if (txt_count != 12) {
        return 2;
    }
    if (!has_txt(txts, txt_count, "txtvers=1") ||
        !has_txt(txts, txt_count, "qtotal=1") ||
        !has_txt(txts, txt_count, "note=James's AirPort Time Capsule") ||
        !has_txt(txts, txt_count, "product=(Canon MP490 series)") ||
        !has_txt(txts, txt_count, "pdl=U") ||
        !has_txt(txts, txt_count, "priority=5") ||
        !has_txt(txts, txt_count, "usb_MFG=Canon") ||
        !has_txt(txts, txt_count, "usb_CMD=BJL,BJRaster3,BSCCe,IVEC,IVECPLI") ||
        !has_txt(txts, txt_count, "usb_MDL=MP490 series") ||
        !has_txt(txts, txt_count, "usb_CLS=PRINTER") ||
        !has_txt(txts, txt_count, "usb_DES=Canon MP490 series") ||
        !has_txt(txts, txt_count, "ty=Canon MP490 series")) {
        return 3;
    }
    if (has_txt(txts, txt_count, "rp=Canon MP490 series C0958C")) {
        return 4;
    }
    printf("ok\n");
    return 0;
}
