#include "shares.h"
#include "../common/utf8.h"
#include "compose_table.h"
#include "case_table.h"

/* A UTF-8 character as the Mac kernel's decoder (utf8_decodestr, also in
 * Apple's firmware) accepts it: any Unicode scalar value but U+FFFE and
 * U+FFFF. Returns its length, or 0. A string's NUL fails the continuation
 * check before anything past it is read, so the 4-byte bound is safe. */
static size_t utf8_decode(const char *s, unsigned *cp) {
    size_t length = tc_utf8_decode((const unsigned char *)s, (const unsigned char *)s + 4, cp);
    return length && *cp - 0xfffe >= 2 ? length : 0;
}
static size_t utf8_encode(char *out, unsigned cp) {
    if (cp < 0x80) {
        out[0] = (char)cp;
        return 1;
    }
    if (cp < 0x800) {
        out[0] = (char)(0xc0 | cp >> 6);
        out[1] = (char)(0x80 | (cp & 0x3f));
        return 2;
    }
    if (cp < 0x10000) {
        out[0] = (char)(0xe0 | cp >> 12);
        out[1] = (char)(0x80 | (cp >> 6 & 0x3f));
        out[2] = (char)(0x80 | (cp & 0x3f));
        return 3;
    }
    out[0] = (char)(0xf0 | cp >> 18);
    out[1] = (char)(0x80 | (cp >> 12 & 0x3f));
    out[2] = (char)(0x80 | (cp >> 6 & 0x3f));
    out[3] = (char)(0x80 | (cp & 0x3f));
    return 4;
}
static unsigned compose_pair(unsigned base, unsigned mark) {
    size_t i, j;
    if (base - 0x1100 < 19 && mark - 0x1161 < 21)
        return 0xac00 + ((base - 0x1100) * 21 + mark - 0x1161) * 28;
    if (base - 0xac00 < 11172 && !((base - 0xac00) % 28) && mark - 0x11a8 < 27)
        return base + mark - 0x11a7;
    for (i = 0; compose_marks[i][0]; i++)
        if (compose_marks[i][0] == mark)
            for (j = compose_marks[i][1]; j < compose_marks[i + 1][1]; j++)
                if (compose_pairs[j][0] == base)
                    return compose_pairs[j][1];
    return 0;
}
/* HFS hands volume names back decomposed, and the share and its ADisk name
 * keep that spelling, as Apple's diskd does. macOS always sends a tree-connect
 * name precomposed, and Samba matches section names without normalizing, so
 * the section takes the name Apple's SMB server stores: decoded with each
 * character combined into the one before it when the pair has a composite
 * (utf8_decodestr with UTF_PRECOMPOSED). A composite never takes more bytes
 * than its parts (gen_compose_table.py checks), and share_name has replaced
 * any byte that is not UTF-8, which is copied unchanged here. */
void tc_share_section(char out[TC_SHARE_NAME_MAX], const char *name) {
    size_t used = 0, last = 0, length;
    unsigned prev = 0, cp, composed;
    while (*name) {
        if (!(length = utf8_decode(name, &cp))) {
            out[used++] = *name++;
            prev = 0;
            continue;
        }
        name += length;
        if (prev && (composed = compose_pair(prev, cp)) != 0) {
            used = last;
            cp = composed;
        }
        last = used;
        prev = cp;
        used += utf8_encode(out + used, cp);
    }
    out[used] = 0;
}

static size_t prefix_bytes(const char *text, size_t budget) {
    size_t length = strlen(text);
    if (length <= budget)
        return length;
    length = budget;
    /* Keep the existing ADisk TXT byte budget without cutting a UTF-8 code
     * point, which produces invalid DNS-SD names in the old byte-only cut. */
    while (length && ((unsigned char)text[length] & 0xc0) == 0x80)
        length--;
    /* Samba drops a section name's trailing spaces, and a collision suffix
     * after one would make a run Samba collapses; either way the ADisk name
     * would not be the name Samba serves. */
    while (length && text[length - 1] == ' ')
        length--;
    return length;
}
static unsigned samba_lower(unsigned cp) {
    size_t i;
    for (i = 0; i < sizeof(case_runs) / sizeof(*case_runs); i++)
        if (cp - case_runs[i][0] <= (unsigned)(case_runs[i][1] - case_runs[i][0]) &&
            !((cp - case_runs[i][0]) % case_runs[i][3]))
            return (cp + case_runs[i][2]) & 0xffff;
    return cp;
}
/* Samba looks a share up by its section name lowercased one code point at a
 * time with its own table (canonicalize_servicename), so sections equal that
 * way are one share. A byte that is not UTF-8 only matches itself. */
static int sections_match(const char *a, const char *b) {
    unsigned x, y;
    size_t m, n;
    while (*a && *b) {
        if (!(m = utf8_decode(a, &x))) {
            x = 0x110000 | (unsigned char)*a;
            m = 1;
        }
        if (!(n = utf8_decode(b, &y))) {
            y = 0x110000 | (unsigned char)*b;
            n = 1;
        }
        if (samba_lower(x) != samba_lower(y))
            return 0;
        a += m;
        b += n;
    }
    return !*a && !*b;
}
/* Section names Samba keeps for itself. [global] and [globals] open the
 * global section, and [printers] becomes a print share; Samba compares these
 * ignoring ASCII case and every space (strwicmp). [homes] stands for each
 * user's home directory, and Samba adds its IPC$ service over a share of that
 * name; it compares those by its lowercase table, which maps no other
 * character to or from ASCII, so strcasecmp gives the same answer. Apple's
 * server shares such a disk under its own name; here it takes a suffix like
 * any other collision, in its ADisk name too, so that the name a Mac asks for
 * is one Samba serves. */
static int samba_reserved(const char *section) {
    static const char *const names[] = {"global", "globals", "printers", "homes", "IPC$"};
    char squeezed[TC_SHARE_NAME_MAX];
    size_t i, used = 0;
    for (i = 0; section[i]; i++)
        if (!isspace((unsigned char)section[i]))
            squeezed[used++] = section[i];
    squeezed[used] = 0;
    for (i = 0; i < sizeof(names) / sizeof(*names); i++)
        if (!strcasecmp(i < 3 ? squeezed : section, names[i]))
            return 1;
    return 0;
}
/* Samba sees the composed section, so two names that compose alike would be
 * one share: HFS keeps characters it does not decompose (U+2000-2FFF) in
 * either spelling. */
static int name_exists(const struct tc_share_set *shares, const char *name) {
    char section[TC_SHARE_NAME_MAX], other[TC_SHARE_NAME_MAX];
    size_t i;
    tc_share_section(section, name);
    if (samba_reserved(section))
        return 1;
    for (i = 0; i < shares->count; i++) {
        tc_share_section(other, shares->values[i].name);
        if (sections_match(other, section))
            return 1;
    }
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
    for (i = 0; name[i] && used + 1 < sizeof(base);) {
        unsigned char ch = name[i];
        unsigned cp;
        length = utf8_decode(name + i, &cp);
        if (length > 1) {
            if (used + length >= sizeof(base))
                break;
            memcpy(base + used, name + i, length);
            used += length;
            i += length;
            continue;
        }
        i++;
        /* A byte that starts no valid UTF-8 sequence becomes '_' as well:
         * composing the section would otherwise decode an overlong form
         * into a character this filter never saw, such as a newline, and
         * Samba asserts on a section name it cannot lowercase. A Mac cannot
         * send such a name at all; '_' keeps the disk shared. */
        if (!length || ch < 32 || ch == 127 || strchr("/\\:*?\"<>|,=[]", ch))
            ch = '_';
        /* Samba expands these %-sequences in a section name before it
         * compares it with the name a Mac asks for (lp_servicenumber), and
         * has no escape for them. "%$(" expands only with a ')' after it;
         * any "%$" is replaced to keep this short. */
        else if (ch == '%' && name[i] && strchr("UGDIJijLNMRTtadhmvwV$", name[i]))
            ch = '_';
        /* Samba compresses each run of spaces in an smb.conf section name to
         * one and looks shares up by exact name, so a Mac asking for the
         * ADisk name "A  B" would not find the share Samba serves as "A B". */
        if (ch == ' ' && used && base[used - 1] == ' ')
            continue;
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
