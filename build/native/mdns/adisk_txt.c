#include "mdns.h"
/* ADisk TXT builders retain the v3.0 wire format. Share metadata arrives
 * as structured arguments from the manager. Their output is golden-tested:
 *   sys=waMA=<XX:XX:XX:XX:XX:XX>,adVF=0x1010
 *   <disk_key>=adVF=<advf>,adVN=<share>,adVU=<uuid>
 */
TC_LOCAL int validate_adisk_disk_advf(const char *value);

int validate_single_dns_label(const char *value, const char *field_name) {
    size_t len;
    const unsigned char *p;

    if (value == NULL || value[0] == '\0') {
        fprintf(stderr, "%s must not be empty\n", field_name);
        return -1;
    }

    len = strlen(value);
    if (len > MAX_LABEL) {
        fprintf(stderr, "%s must be %d bytes or fewer\n", field_name, MAX_LABEL);
        return -1;
    }

    if (strchr(value, '.') != NULL) {
        fprintf(stderr, "%s must not contain dots\n", field_name);
        return -1;
    }

    for (p = (const unsigned char *)value; *p != '\0'; p++) {
        if (*p < 0x20 || *p == 0x7f) {
            fprintf(stderr, "%s contains an invalid control character\n", field_name);
            return -1;
        }
    }

    return 0;
}

int build_adisk_system_txt(char *out, size_t out_len, const char *wama) {
    int written;
    char normalized[18];

    /* The shared identity parser also validates and canonicalizes MACs. */
    if (normalize_mac_text(normalized, sizeof(normalized), wama) != 0) {
        if (wama && *wama) fputs("adisk sys waMA must be a MAC address\n", stderr);
        return -1;
    }
    written = snprintf(out, out_len, ADISK_SYS_TXT_PREFIX "%s" ADISK_SYS_TXT_SUFFIX, normalized);
    if (written < 0 || (size_t)written >= out_len) {
        return -1;
    }

    return 0;
}

TC_LOCAL int validate_adisk_disk_advf(const char *value) {
    const unsigned char *p;

    if (value == NULL || value[0] == '\0') {
        fprintf(stderr, "adisk disk adVF cannot be blank\n");
        return -1;
    }
    if (!(value[0] == '0' && (value[1] == 'x' || value[1] == 'X') && value[2] != '\0')) {
        fprintf(stderr, "adisk disk adVF must be hexadecimal, like 0x82\n");
        return -1;
    }
    for (p = (const unsigned char *)value + 2; *p != '\0'; p++) {
        if (!isxdigit(*p)) {
            fprintf(stderr, "adisk disk adVF must be hexadecimal, like 0x82\n");
            return -1;
        }
    }
    return 0;
}

int build_adisk_disk_txt(char *out, size_t out_len, const char *disk_key, const char *share_name, const char *adisk_uuid, const char *adisk_disk_advf) {
    int written;
    const unsigned char *p;

    if (disk_key == NULL || disk_key[0] == '\0' || share_name == NULL || share_name[0] == '\0' ||
        adisk_uuid == NULL || adisk_uuid[0] == '\0' || adisk_disk_advf == NULL || adisk_disk_advf[0] == '\0') {
        return -1;
    }

    if (validate_single_dns_label(disk_key, "adisk disk key") != 0) {
        return -1;
    }

    for (p = (const unsigned char *)share_name; *p != '\0'; p++) {
        if (*p < 0x20 || *p == 0x7f) {
            fprintf(stderr, "adisk share name contains an invalid control character\n");
            return -1;
        }
    }

    if (strlen(adisk_uuid) != ADISK_DISK_UUID_LEN) {
        fprintf(stderr, "adisk uuid must be %d characters\n", ADISK_DISK_UUID_LEN);
        return -1;
    }

    if (validate_adisk_disk_advf(adisk_disk_advf) != 0) {
        return -1;
    }

    if (strlen(disk_key) + strlen(ADISK_DISK_TXT_ADVF_PREFIX) + strlen(adisk_disk_advf) +
        strlen(ADISK_DISK_TXT_ADVN_MID) + strlen(share_name) +
        strlen(ADISK_DISK_TXT_SUFFIX) + strlen(adisk_uuid) > MAX_TXT_STRING) {
        fprintf(stderr, "adisk share name must be %d bytes or fewer\n",
                MAX_TXT_STRING - (int)strlen(disk_key) - (int)strlen(ADISK_DISK_TXT_ADVF_PREFIX) -
                    (int)strlen(adisk_disk_advf) - (int)strlen(ADISK_DISK_TXT_ADVN_MID) -
                    (int)strlen(ADISK_DISK_TXT_SUFFIX) - (int)strlen(adisk_uuid));
        return -1;
    }

    written = snprintf(out, out_len, "%s" ADISK_DISK_TXT_ADVF_PREFIX "%s" ADISK_DISK_TXT_ADVN_MID "%s" ADISK_DISK_TXT_SUFFIX "%s",
                       disk_key, adisk_disk_advf, share_name, adisk_uuid);
    if (written < 0 || (size_t)written >= out_len) {
        return -1;
    }

    return 0;
}

int add_adisk_disk_config(struct config *cfg, const char *share_name, const char *disk_key,
                                 const char *adisk_uuid, const char *adisk_disk_advf) {
    struct adisk_disk *disk;
    char txt[256];

    if (cfg->adisk_disks.count >= ADISK_MAX_DISKS) {
        fprintf(stderr, "too many adisk disks; maximum is %d\n", ADISK_MAX_DISKS);
        return -1;
    }
    if (build_adisk_disk_txt(txt, sizeof(txt), disk_key, share_name, adisk_uuid, adisk_disk_advf) != 0) {
        return -1;
    }
    if (strlen(adisk_disk_advf) >= sizeof(cfg->adisk_disks.disks[0].disk_advf)) {
        fprintf(stderr, "adisk disk adVF is too long\n");
        return -1;
    }

    disk = &cfg->adisk_disks.disks[cfg->adisk_disks.count++];
    memset(disk, 0, sizeof(*disk));
    strncpy(disk->share_name, share_name, sizeof(disk->share_name) - 1);
    strncpy(disk->disk_key, disk_key, sizeof(disk->disk_key) - 1);
    strncpy(disk->disk_advf, adisk_disk_advf, sizeof(disk->disk_advf) - 1);
    strncpy(disk->uuid, adisk_uuid, sizeof(disk->uuid) - 1);
    return 0;
}

int adisk_enabled(const struct config *cfg) {
    return !cfg->diskless && cfg->adisk_disks.count > 0;
}

int build_adisk_txt_record(unsigned char *out, size_t out_len, const char *wama, const struct adisk_disk_set *disks) {
    char item[MAX_TXT_STRING + 1];
    size_t used = 0;
    size_t i;

    if (build_adisk_system_txt(item, sizeof(item), wama) != 0) {
        return -1;
    }
    for (i = 0; i <= disks->count; i++) {
        size_t len;
        if (i > 0) {
            const struct adisk_disk *disk = &disks->disks[i - 1];
            if (build_adisk_disk_txt(item, sizeof(item), disk->disk_key, disk->share_name, disk->uuid, disk->disk_advf) != 0) {
                return -1;
            }
        }
        len = strlen(item);
        if (len > MAX_TXT_STRING || used + 1 + len > out_len) {
            return -1;
        }
        out[used++] = (unsigned char)len;
        memcpy(out + used, item, len);
        used += len;
    }
    return (int)used;
}
