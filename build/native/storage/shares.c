#include "shares.h"

static size_t prefix_bytes(const char *text, size_t budget) {
    size_t length = strlen(text);
    if (length <= budget)
        return length;
    length = budget;
    /* Keep the existing ADisk TXT byte budget without cutting a UTF-8 code
     * point, which produces invalid DNS-SD names in the old byte-only cut. */
    while (length && ((unsigned char)text[length] & 0xc0) == 0x80)
        length--;
    return length;
}
static int name_exists(const struct tc_share_set *shares, const char *name) {
    size_t i;
    for (i = 0; i < shares->count; i++)
        if (!strcasecmp(shares->values[i].name, name))
            return 1;
    return 0;
}
static void share_name(const struct tc_share_set *shares, const struct tc_volume *volume,
                       char out[TC_SHARE_NAME_MAX], int advertise_afp) {
    char base[TC_VOLUME_NAME_MAX], suffix[40];
    size_t used = 0, i, length, budget;
    unsigned collision = 0;
    const char *name = volume->name;
    while (*name && isspace((unsigned char)*name))
        name++;
    for (i = 0; name[i] && used + 1 < sizeof(base); i++) {
        unsigned char ch = name[i];
        if (ch < 32 || ch == 127 || strchr("/\\:*?\"<>|,=[]", ch))
            ch = '_';
        base[used++] = ch;
    }
    while (used && isspace((unsigned char)base[used - 1]))
        used--;
    base[used] = 0;
    if (!used)
        snprintf(base, sizeof(base), "Disk %s", volume->device);
    /* Apple's ADisk entry is one <=255-byte TXT string. The exact legacy
     * accounting includes device key, flags, name/UUID separators and UUID. */
    budget = 255 - strlen(volume->device) - 6 - strlen(advertise_afp ? "0x83" : "0x82") - 6 - 6 -
             strlen(volume->uuid);
    length = prefix_bytes(base, budget);
    memcpy(out, base, length);
    out[length] = 0;
    while (name_exists(shares, out)) {
        if (!collision)
            snprintf(suffix, sizeof(suffix), " (%s)", volume->device);
        else
            snprintf(suffix, sizeof(suffix), " (%s-%u)", volume->device, collision);
        collision++;
        length = prefix_bytes(base, budget - strlen(suffix));
        memcpy(out, base, length);
        strcpy(out + length, suffix);
    }
}

int tc_shares_build(struct tc_share_set *out, const struct tc_inventory *inventory, uint32_t available,
                    int internal_root, int advertise_afp) {
    size_t i;
    memset(out, 0, sizeof(*out));
    if (inventory->count > TC_MAX_VOLUMES)
        return -1;
    for (i = 0; i < inventory->count; i++) {
        const struct tc_volume *volume = &inventory->volumes[i];
        struct tc_share *share;
        if (!(available & (1u << i)))
            continue;
        share = &out->values[out->count];
        share_name(out, volume, share->name, advertise_afp);
        if (snprintf(share->path, sizeof(share->path), "%s%s", volume->root,
                     volume->builtin && !internal_root ? "/ShareRoot" : "") >= (int)sizeof(share->path))
            return -1;
        strcpy(share->device, volume->device);
        strcpy(share->uuid, volume->uuid);
        share->builtin = volume->builtin;
        out->count++;
    }
    return 0;
}

int tc_shares_equal(const struct tc_share_set *a, const struct tc_share_set *b) {
    size_t i;
    if (a->count != b->count)
        return 0;
    for (i = 0; i < a->count; i++) {
        const struct tc_share *x = &a->values[i], *y = &b->values[i];
        if (strcmp(x->name, y->name) || strcmp(x->path, y->path) || strcmp(x->device, y->device) ||
            strcmp(x->uuid, y->uuid) || x->builtin != y->builtin)
            return 0;
    }
    return 1;
}
