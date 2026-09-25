#include <stdio.h>
#include <string.h>
#include "discovery/discovery.h"

/* Drives discovery's production path for one --adisk-share row and the ACP
 * waMA fact: add_adisk_disk_config() exactly as discovery/main.c parses the
 * argument, identity_derive() on the raw fact as the plan does, then
 * registrant_compute_desired() twice (two plan passes). Each pass prints the
 * desired services on the one LAN link and the _adisk TXT items, if any.
 * Usage: case diskful|diskless <uuid|-> <waMA|empty for unavailable> */
static void print_pass(const struct device_plan *plan, const struct config *cfg) {
    struct reg_desired desired[REG_MAX_ENTRIES];
    unsigned char txt[REG_TXT_MAX];
    size_t txt_len, count, i;

    count = registrant_compute_desired(desired, REG_MAX_ENTRIES, plan, cfg, txt, &txt_len);
    for (i = 0; i < count; i++) {
        printf("%s%s", i ? "," : "", desired[i].service == REG_SMB ? "smb" :
                                     desired[i].service == REG_ADISK ? "adisk" : "afp");
    }
    printf(" txt=");
    for (i = 0; i < txt_len; i += 1 + txt[i]) {
        printf("%s%.*s", i ? "|" : "", (int)txt[i], (const char *)txt + i + 1);
    }
    printf("\n");
}

int main(int argc, char **argv) {
    struct config cfg;
    struct device_facts facts;
    struct device_plan plan;

    if (argc != 4) {
        return 99;
    }
    memset(&cfg, 0, sizeof(cfg));
    cfg.diskless = strcmp(argv[1], "diskless") == 0;
    if (strcmp(argv[2], "-") != 0 && add_adisk_disk_config(&cfg, "Data", "dk2", argv[2], "0x82") != 0) {
        return EXIT_INVALID_ADISK_DISK;
    }

    memset(&facts, 0, sizeof(facts));
    facts.acp[ACP_KEY_syNm].status = ACP_UNAVAILABLE;
    facts.acp[ACP_KEY_waMA].status = argv[3][0] ? ACP_OK : ACP_UNAVAILABLE;
    snprintf(facts.acp[ACP_KEY_waMA].text, sizeof(facts.acp[ACP_KEY_waMA].text), "%s", argv[3]);

    memset(&plan, 0, sizeof(plan));
    identity_derive(&plan.id, &facts);
    plan.link_count = 1;
    snprintf(plan.links[0].link.name, sizeof(plan.links[0].link.name), "bridge0");
    plan.links[0].link.index = 9;
    /* Policy grants _adisk here; the registrant alone decides to skip it. */
    plan.links[0].mask = SVC_SMB | SVC_ADISK;

    print_pass(&plan, &cfg);
    print_pass(&plan, &cfg);
    return EXIT_OK;
}
