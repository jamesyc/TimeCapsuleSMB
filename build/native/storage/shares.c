#include "shares.h"

static size_t utf8_prefix(const char *value, size_t limit) {
    size_t length = strlen(value);
    if (length <= limit) return length;
    length = limit;
    while (length && (((unsigned char)value[length] & 0xc0) == 0x80)) length--;
    return length;
}

static void base_name(char out[TC_SHARE_NAME_MAX], const char *name, const char *device) {
    char cleaned[TC_STORAGE_NAME_MAX];
    size_t i, used = 0, length;
    const char *source = name && *name ? name : device;
    for (i = 0; source[i] && used + 1 < sizeof(cleaned); i++) {
        unsigned char ch = (unsigned char)source[i];
        if (ch < 0x20 || ch == 0x7f || ch == '/' || ch == '\\' || ch == '[' || ch == ']') ch = '_';
        cleaned[used++] = (char)ch;
    }
    while (used && (cleaned[used - 1] == ' ' || cleaned[used - 1] == '.')) used--;
    cleaned[used] = '\0';
    if (!used) { strncpy(cleaned, device, sizeof(cleaned) - 1); cleaned[sizeof(cleaned) - 1] = '\0'; }
    length = utf8_prefix(cleaned, TC_SHARE_NAME_MAX - 1);
    memcpy(out, cleaned, length); out[length] = '\0';
}

static int name_exists(const struct tc_share_set *shares, const char *name) {
    size_t i;
    for (i = 0; i < shares->count; i++) if (!strcmp(shares->values[i].name, name)) return 1;
    return 0;
}

static void unique_name(struct tc_share_set *shares, char out[TC_SHARE_NAME_MAX],
                        const char *name, const char *device) {
    char base[TC_SHARE_NAME_MAX];
    unsigned suffix = 0;
    base_name(base, name, device);
    strcpy(out, base);
    while (name_exists(shares, out)) {
        char ending[24];
        size_t budget, length;
        if (suffix++ == 0) snprintf(ending, sizeof(ending), " (%s)", device);
        else snprintf(ending, sizeof(ending), " (%s-%u)", device, suffix);
        budget = (TC_SHARE_NAME_MAX - 1) - strlen(ending);
        length = utf8_prefix(base, budget);
        memcpy(out, base, length); strcpy(out + length, ending);
    }
}

int tc_shares_build(struct tc_share_set *shares, const struct tc_inventory *inventory,
                    int internal_uses_root) {
    size_t i;
    memset(shares, 0, sizeof(*shares));
    if (!inventory->valid) return -1;
    for (i = 0; i < inventory->count; i++) {
        const struct tc_volume *volume = &inventory->volumes[i];
        struct tc_share *share;
        if (!volume->available || !volume->writable) continue;
        if (shares->count >= TC_MAX_SHARES) return -1;
        share = &shares->values[shares->count];
        unique_name(shares, share->name, volume->name, volume->device);
        if (volume->builtin && !internal_uses_root)
            snprintf(share->path, sizeof(share->path), "%s/ShareRoot", volume->root);
        else strncpy(share->path, volume->root, sizeof(share->path) - 1);
        if (mkdir(share->path, 0777) != 0 && errno != EEXIST) return -1;
        strncpy(share->device, volume->device, sizeof(share->device) - 1);
        strncpy(share->uuid, volume->uuid, sizeof(share->uuid) - 1);
        share->builtin = volume->builtin;
        shares->count++;
    }
    return 0;
}
