#include "mast.h"
#include <limits.h>

/* A small streaming plist reader, not a general property-list object model.
 * Apple puts builtin/deviceName after partitions in some firmware versions.
 * Keep a disk's partitions until its dictionary has ended. */
enum token_kind { END, DICT, ARRAY, END_DICT, END_ARRAY, STRING, DATA, EQUAL, BAD };
struct parser {
    const char *p, *end;
    int xml, depth;
    enum token_kind token;
    enum token_kind pending;
    char value[1024];
};
struct part {
    char device[16], name[TC_VOLUME_NAME_MAX], format[16], uuid[37];
    int users;
};
struct disk {
    char device[16];
    int builtin;
    struct part parts[TC_MAX_VOLUMES];
    size_t count;
};
enum context { IGNORE, DISKS, DISK, PARTS, PART };

static int at(struct parser *p, const char *s) {
    size_t n = strlen(s);
    return (size_t)(p->end - p->p) >= n && !memcmp(p->p, s, n);
}
static void whitespace(struct parser *p) {
    while (p->p < p->end && isspace((unsigned char)*p->p))
        p->p++;
}
static int put(char *out, size_t *n, unsigned char c) {
    if (!c || *n >= 1023)
        return -1;
    out[(*n)++] = (char)c;
    out[*n] = 0;
    return 0;
}
static int codepoint(char *out, size_t *n, unsigned long cp) {
    if (!cp || cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff))
        return -1;
    if (cp < 0x80)
        return put(out, n, cp);
    if (cp < 0x800)
        return put(out, n, 0xc0 | (cp >> 6)) || put(out, n, 0x80 | (cp & 63));
    if (cp < 0x10000)
        return put(out, n, 0xe0 | (cp >> 12)) || put(out, n, 0x80 | ((cp >> 6) & 63)) ||
               put(out, n, 0x80 | (cp & 63));
    return put(out, n, 0xf0 | (cp >> 18)) || put(out, n, 0x80 | ((cp >> 12) & 63)) ||
           put(out, n, 0x80 | ((cp >> 6) & 63)) || put(out, n, 0x80 | (cp & 63));
}
static int xml_text(struct parser *p, const char *closing) {
    size_t n = 0;
    while (p->p < p->end && !at(p, closing)) {
        unsigned char ch = *p->p++;
        if (ch == '<')
            return -1;
        if (ch == '&') {
            char entity[24];
            size_t k = 0;
            unsigned long cp = 0;
            char *tail;
            while (p->p < p->end && *p->p != ';' && k + 1 < sizeof(entity))
                entity[k++] = *p->p++;
            if (p->p == p->end || *p->p++ != ';')
                return -1;
            entity[k] = 0;
            if (!strcmp(entity, "amp"))
                cp = '&';
            else if (!strcmp(entity, "lt"))
                cp = '<';
            else if (!strcmp(entity, "gt"))
                cp = '>';
            else if (!strcmp(entity, "quot"))
                cp = '"';
            else if (!strcmp(entity, "apos"))
                cp = '\'';
            else if (entity[0] == '#') {
                const char *digits = entity + 1;
                int base = 10;
                if (*digits == 'x') {
                    digits++;
                    base = 16;
                }
                errno = 0;
                cp = strtoul(digits, &tail, base);
                if (errno || !*digits || *tail)
                    return -1;
            } else
                return -1;
            if (codepoint(p->value, &n, cp))
                return -1;
        } else if (put(p->value, &n, ch))
            return -1;
    }
    if (!at(p, closing))
        return -1;
    p->p += strlen(closing);
    return 0;
}
static enum token_kind xml_token(struct parser *p) {
    static const struct {
        const char *open, *close;
        enum token_kind kind;
    } tags[] = {{"<key>", "</key>", STRING},         {"<string>", "</string>", STRING},
                {"<integer>", "</integer>", STRING}, {"<real>", "</real>", STRING},
                {"<data>", "</data>", DATA},         {"<date>", "</date>", STRING}};
    size_t i;
    whitespace(p);
    if (p->p == p->end)
        return END;
    if (at(p, "<array/>") || at(p, "<array />")) {
        p->p += at(p, "<array/>") ? 8 : 9;
        p->pending = END_ARRAY;
        return ARRAY;
    }
    if (at(p, "<dict/>") || at(p, "<dict />")) {
        p->p += at(p, "<dict/>") ? 7 : 8;
        p->pending = END_DICT;
        return DICT;
    }
    if (at(p, "<string/>")) {
        p->p += 9;
        return STRING;
    }
    if (at(p, "<data/>")) {
        p->p += 7;
        return DATA;
    }
    if (at(p, "<dict>")) {
        p->p += 6;
        return DICT;
    }
    if (at(p, "</dict>")) {
        p->p += 7;
        return END_DICT;
    }
    if (at(p, "<array>")) {
        p->p += 7;
        return ARRAY;
    }
    if (at(p, "</array>")) {
        p->p += 8;
        return END_ARRAY;
    }
    if (at(p, "<true/>")) {
        p->p += 7;
        strcpy(p->value, "true");
        return STRING;
    }
    if (at(p, "<false/>")) {
        p->p += 8;
        strcpy(p->value, "false");
        return STRING;
    }
    for (i = 0; i < sizeof(tags) / sizeof(tags[0]); i++)
        if (at(p, tags[i].open)) {
            p->p += strlen(tags[i].open);
            return xml_text(p, tags[i].close) ? BAD : tags[i].kind;
        }
    return BAD;
}
static void next(struct parser *p) {
    size_t n = 0;
    char ch;
    p->value[0] = 0;
    if (p->pending) {
        p->token = p->pending;
        p->pending = END;
        return;
    }
    if (p->xml) {
        p->token = xml_token(p);
        return;
    }
    while (p->p < p->end && (isspace((unsigned char)*p->p) || *p->p == ',' || *p->p == ';'))
        p->p++;
    if (p->p == p->end) {
        p->token = END;
        return;
    }
    ch = *p->p++;
    switch (ch) {
    case '{':
        p->token = DICT;
        return;
    case '}':
        p->token = END_DICT;
        return;
    case '[':
    case '(':
        p->token = ARRAY;
        return;
    case ']':
    case ')':
        p->token = END_ARRAY;
        return;
    case '=':
        p->token = EQUAL;
        return;
    case '"':
        while (p->p < p->end && *p->p != '"') {
            ch = *p->p++;
            if (ch == '\\') {
                if (p->p == p->end) {
                    p->token = BAD;
                    return;
                }
                ch = *p->p++;
                if (ch == 'n')
                    ch = '\n';
                else if (ch == 'r')
                    ch = '\r';
                else if (ch == 't')
                    ch = '\t';
            }
            if (put(p->value, &n, ch)) {
                p->token = BAD;
                return;
            }
        }
        if (p->p == p->end) {
            p->token = BAD;
            return;
        }
        p->p++;
        p->token = STRING;
        return;
    case '<':
        while (p->p < p->end && *p->p != '>')
            if (put(p->value, &n, *p->p++)) {
                p->token = BAD;
                return;
            }
        if (p->p == p->end) {
            p->token = BAD;
            return;
        }
        p->p++;
        p->token = STRING;
        return;
    default:
        p->p--;
    }
    while (p->p < p->end && !strchr("=;,\r\n{}[]()", *p->p)) {
        /* Native ACP's binary annotation contains braces/parentheses and
         * printable bytes. None of that annotation is part of a UUID. */
        if (*p->p == '|') {
            while (p->p < p->end && *p->p != '\n')
                p->p++;
            break;
        }
        if (put(p->value, &n, *p->p++)) {
            p->token = BAD;
            return;
        }
    }
    while (n && isspace((unsigned char)p->value[n - 1]))
        p->value[--n] = 0;
    p->token = n ? STRING : BAD;
}
static int copy_value(char *out, size_t capacity, const char *value) {
    if (strlen(value) >= capacity)
        return -1;
    strcpy(out, value);
    return 0;
}
static int device_ok(const char *s) {
    return *s && strspn(s, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") == strlen(s);
}
static int uuid_value(char out[37], const char *s, int base64) {
    static const char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    char hex[33];
    size_t n = 0;
    unsigned acc = 0, bits = 0;
    int padding = 0;
    while (*s) {
        unsigned char c = *s++;
        const char *found;
        if (isspace(c))
            continue;
        if (base64) {
            if (c == '=') {
                padding = 1;
                continue;
            }
            if (padding || !(found = strchr(alphabet, c)))
                return -1;
            acc = (acc << 6) | (unsigned)(found - alphabet);
            bits += 6;
            if (bits >= 8) {
                unsigned byte;
                bits -= 8;
                byte = (acc >> bits) & 255;
                if (n + 2 > 32)
                    return -1;
                hex[n++] = "0123456789abcdef"[byte >> 4];
                hex[n++] = "0123456789abcdef"[byte & 15];
            }
        } else {
            if (c == '-')
                continue;
            if (!isxdigit(c) || n == 32)
                return -1;
            hex[n++] = (char)tolower(c);
        }
    }
    if (n != 32 || !memcmp(hex, "00000000000000000000000000000000", 32))
        return -1;
    hex[n] = 0;
    snprintf(out, 37, "%.8s-%.4s-%.4s-%.4s-%.12s", hex, hex + 8, hex + 12, hex + 16, hex + 20);
    return 0;
}
static int append_disk(struct tc_inventory *out, const struct disk *disk) {
    size_t i, j;
    for (i = 0; i < disk->count; i++) {
        const struct part *part = &disk->parts[i];
        struct tc_volume *v;
        if (out->count == TC_MAX_VOLUMES || !device_ok(disk->device))
            return -1;
        for (j = 0; j < out->count; j++)
            if (!strcmp(out->volumes[j].device, part->device))
                return -1;
        v = &out->volumes[out->count++];
        strcpy(v->disk, disk->device);
        strcpy(v->device, part->device);
        strcpy(v->name, part->name);
        strcpy(v->uuid, part->uuid);
        if (snprintf(v->root, sizeof(v->root), "%s/%s", TC_VOLUMES_ROOT, part->device) >=
            (int)sizeof(v->root))
            return -1;
        v->builtin = disk->builtin;
        v->users = part->users;
    }
    return 0;
}
static int value(struct parser *, enum context, struct tc_inventory *, struct disk *, struct part *);
static int dictionary(struct parser *p, enum context ctx, struct tc_inventory *out, struct disk *d,
                      struct part *part) {
    next(p);
    while (p->token != END_DICT) {
        char key[80];
        enum context child = IGNORE;
        if (p->token != STRING || copy_value(key, sizeof(key), p->value))
            return -1;
        next(p);
        if (!p->xml) {
            if (p->token != EQUAL)
                return -1;
            next(p);
        }
        if (ctx == DISK && !strcmp(key, "partitions"))
            child = PARTS;
        if (child == PARTS) {
            if (p->token != ARRAY || value(p, child, out, d, NULL))
                return -1;
            continue;
        }
        if (p->token == STRING || p->token == DATA) {
            const char *s = p->value;
            int rc = 0;
            if (ctx == DISK) {
                if (!strcmp(key, "deviceName"))
                    rc = copy_value(d->device, sizeof(d->device), s);
                else if (!strcmp(key, "builtin")) {
                    if (!strcmp(s, "true") || !strcmp(s, "1"))
                        d->builtin = 1;
                    else if (!strcmp(s, "false") || !strcmp(s, "0"))
                        d->builtin = 0;
                    else
                        return -1;
                }
            } else if (ctx == PART) {
                if (!strcmp(key, "deviceName"))
                    rc = copy_value(part->device, sizeof(part->device), s);
                else if (!strcmp(key, "name"))
                    rc = copy_value(part->name, sizeof(part->name), s);
                else if (!strcmp(key, "format"))
                    rc = copy_value(part->format, sizeof(part->format), s);
                else if (!strcmp(key, "uuid")) {
                    if (uuid_value(part->uuid, s, p->token == DATA))
                        part->uuid[0] = 0;
                } else if (!strcmp(key, "users")) {
                    char *end;
                    long users;
                    errno = 0;
                    users = strtol(s, &end, 10);
                    part->users = !errno && *s && !*end && users >= 0 && users <= INT_MAX ? (int)users : -1;
                }
            }
            if (rc)
                return -1;
            next(p);
        } else if (value(p, IGNORE, out, NULL, NULL))
            return -1;
    }
    next(p);
    return 0;
}
static int value(struct parser *p, enum context ctx, struct tc_inventory *out, struct disk *disk,
                 struct part *part) {
    int result = 0;
    if (++p->depth > 32)
        return -1;
    if (p->token == ARRAY) {
        next(p);
        while (p->token != END_ARRAY) {
            if (ctx == DISKS) {
                struct disk d;
                memset(&d, 0, sizeof(d));
                if (p->token != DICT || value(p, DISK, out, &d, NULL) || append_disk(out, &d)) {
                    result = -1;
                    break;
                }
            } else if (ctx == PARTS) {
                struct part item;
                memset(&item, 0, sizeof(item));
                item.users = -1;
                if (p->token != DICT || value(p, PART, out, disk, &item)) {
                    result = -1;
                    break;
                }
                if (!strcasecmp(item.format, "hfs") && !strncmp(item.device, "dk", 2) &&
                    isdigit((unsigned char)item.device[2]) && device_ok(item.device) && item.name[0] &&
                    item.uuid[0]) {
                    if (disk->count == TC_MAX_VOLUMES) {
                        result = -1;
                        break;
                    }
                    disk->parts[disk->count++] = item;
                }
            } else if (value(p, IGNORE, out, NULL, NULL)) {
                result = -1;
                break;
            }
        }
        if (!result)
            next(p);
    } else if (p->token == DICT)
        result = dictionary(p, ctx, out, disk, part);
    else if (p->token == STRING || p->token == DATA)
        next(p);
    else
        result = -1;
    p->depth--;
    return result;
}

int tc_mast_parse(struct tc_inventory *out, const char *text, size_t length) {
    struct parser p;
    int result = -1;
    memset(out, 0, sizeof(*out));
    memset(&p, 0, sizeof(p));
    if (!text || !length || length > TC_MAST_MAX || memchr(text, 0, length))
        return -1;
    p.p = text;
    p.end = text + length;
    whitespace(&p);
    if (at(&p, "MaSt")) {
        p.p += 4;
        whitespace(&p);
        if (p.p == p.end || *p.p++ != '=')
            goto done;
        whitespace(&p);
    }
    if (at(&p, "<?xml") || at(&p, "<plist")) {
        p.xml = 1;
        if (at(&p, "<?xml")) {
            while (p.p < p.end && !at(&p, "?>"))
                p.p++;
            if (!at(&p, "?>"))
                goto done;
            p.p += 2;
        }
        whitespace(&p);
        if (at(&p, "<!DOCTYPE")) {
            while (p.p < p.end && *p.p != '>') {
                if (*p.p == '[')
                    goto done;
                p.p++;
            }
            if (p.p == p.end)
                goto done;
            p.p++;
        }
        whitespace(&p);
        if (!at(&p, "<plist"))
            goto done;
        while (p.p < p.end && *p.p != '>')
            p.p++;
        if (p.p == p.end)
            goto done;
        p.p++;
    }
    next(&p);
    if (p.token != ARRAY || value(&p, DISKS, out, NULL, NULL))
        goto done;
    /* XML's closing plist envelope is not a value token. Native acp instead
     * prints a trailing empty MaSt= label after its complete array. */
    if (p.xml) {
        if (!at(&p, "</plist>"))
            goto done;
        p.p += 8;
        whitespace(&p);
    } else if (p.token == STRING && !strcmp(p.value, "MaSt")) {
        next(&p);
        if (p.token != EQUAL)
            goto done;
        next(&p);
    }
    if (p.p != p.end || (!p.xml && p.token != END))
        goto done;
    result = 0;
done:
    if (result)
        memset(out, 0, sizeof(*out));
    return result;
}

int tc_inventory_same_topology(const struct tc_inventory *a, const struct tc_inventory *b) {
    size_t i, j;
    if (a->count != b->count)
        return 0;
    for (i = 0; i < a->count; i++) {
        const struct tc_volume *v = &a->volumes[i];
        for (j = 0; j < b->count; j++) {
            const struct tc_volume *w = &b->volumes[j];
            if (!strcmp(v->device, w->device) && !strcmp(v->disk, w->disk) && !strcmp(v->uuid, w->uuid) &&
                !strcmp(v->name, w->name) && v->builtin == w->builtin)
                break;
        }
        if (j == b->count)
            return 0;
    }
    return 1;
}
