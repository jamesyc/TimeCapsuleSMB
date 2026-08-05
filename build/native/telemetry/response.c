#include "telemetry.h"

/* This parser accepts a bounded JSON object, not a substring resembling a
 * command. Unknown values are validated and skipped with a depth bound. */
struct reader { const unsigned char *p, *end; };
static void space(struct reader *r) {
    while (r->p < r->end && (*r->p == ' ' || *r->p == '\t' || *r->p == '\r' || *r->p == '\n')) r->p++;
}
static int take(struct reader *r, unsigned char c) {
    space(r);
    if (r->p == r->end || *r->p != c) return 0;
    r->p++;
    return 1;
}
static int hex4(struct reader *r, unsigned int *out) {
    int i;
    *out = 0;
    for (i = 0; i < 4; i++) {
        unsigned char c;
        unsigned int n;
        if (r->p == r->end) return -1;
        c = *r->p++;
        if (c >= '0' && c <= '9') n = c - '0';
        else if (c >= 'a' && c <= 'f') n = c - 'a' + 10;
        else if (c >= 'A' && c <= 'F') n = c - 'A' + 10;
        else return -1;
        *out = (*out << 4) | n;
    }
    return 0;
}
static int string(struct reader *r, char *out, size_t cap) {
    size_t n = 0;
    if (!take(r, '"')) return -1;
    while (r->p < r->end) {
        unsigned int c = *r->p++;
        if (c == '"') {
            if (out) out[n] = '\0';
            return 0;
        }
        if (c < 32) return -1;
        if (c == '\\') {
            if (r->p == r->end) return -1;
            c = *r->p++;
            switch (c) {
                case '"': case '\\': case '/': break;
                case 'b': c = 8; break;
                case 'f': c = 12; break;
                case 'n': c = 10; break;
                case 'r': c = 13; break;
                case 't': c = 9; break;
                case 'u': {
                    unsigned int low;
                    if (hex4(r, &c)) return -1;
                    if (c >= 0xd800 && c <= 0xdbff) {
                        if (r->end - r->p < 2 || r->p[0] != '\\' || r->p[1] != 'u') return -1;
                        r->p += 2;
                        if (hex4(r, &low) || low < 0xdc00 || low > 0xdfff) return -1;
                        c = 0x10000;
                    } else if (c >= 0xdc00 && c <= 0xdfff) return -1;
                    break;
                }
                default: return -1;
            }
        } else if (c >= 128) {
            /* Validate UTF-8 even in ignored fields. Captured control fields
             * are ASCII; non-ASCII codepoints cannot match their names. */
            unsigned int cp, min;
            int count, i;
            if (c >= 0xc2 && c <= 0xdf) { count = 1; cp = c & 31; min = 0x80; }
            else if (c >= 0xe0 && c <= 0xef) { count = 2; cp = c & 15; min = 0x800; }
            else if (c >= 0xf0 && c <= 0xf4) { count = 3; cp = c & 7; min = 0x10000; }
            else return -1;
            for (i = 0; i < count; i++) {
                if (r->p == r->end || (*r->p & 0xc0) != 0x80) return -1;
                cp = (cp << 6) | (*r->p++ & 63);
            }
            if (cp < min || cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff)) return -1;
            c = cp;
        }
        if (out) {
            if (n + 1 >= cap) return -1;
            out[n++] = c > 127 || c == 0 ? '?' : (char)c;
        }
    }
    return -1;
}
static int literal(struct reader *r, const char *s) {
    size_t n = strlen(s);
    space(r);
    if ((size_t)(r->end - r->p) < n || memcmp(r->p, s, n)) return 0;
    r->p += n;
    return 1;
}
static int value(struct reader *r, unsigned int depth) {
    unsigned char close;
    space(r);
    if (depth > 16 || r->p == r->end) return -1;
    if (*r->p == '"') return string(r, NULL, 0);
    if (*r->p == '{' || *r->p == '[') {
        close = *r->p++ == '{' ? '}' : ']';
        if (take(r, close)) return 0;
        do {
            if (close == '}' && (string(r, NULL, 0) || !take(r, ':'))) return -1;
            if (value(r, depth + 1)) return -1;
            if (take(r, close)) return 0;
        } while (take(r, ','));
        return -1;
    }
    if (literal(r, "true") || literal(r, "false") || literal(r, "null")) return 0;
    if (*r->p == '-') r->p++;
    if (r->p == r->end) return -1;
    if (*r->p == '0') r->p++;
    else {
        if (*r->p < '1' || *r->p > '9') return -1;
        do { r->p++; } while (r->p < r->end && isdigit(*r->p));
    }
    if (r->p < r->end && *r->p == '.') {
        r->p++;
        if (r->p == r->end || !isdigit(*r->p)) return -1;
        do { r->p++; } while (r->p < r->end && isdigit(*r->p));
    }
    if (r->p < r->end && (*r->p == 'e' || *r->p == 'E')) {
        r->p++;
        if (r->p < r->end && (*r->p == '+' || *r->p == '-')) r->p++;
        if (r->p == r->end || !isdigit(*r->p)) return -1;
        do { r->p++; } while (r->p < r->end && isdigit(*r->p));
    }
    return 0;
}
int telemetry_response_parse(const char *json, size_t len, struct telemetry_response *out) {
    struct reader r;
    struct telemetry_response parsed;
    int seen_debug = 0, seen_signature = 0;
    memset(out, 0, sizeof(*out));
    memset(&parsed, 0, sizeof(parsed));
    if (len > TC_RESPONSE_MAX) return -1;
    r.p = (const unsigned char *)json; r.end = r.p + len;
    space(&r);
    if (r.p == r.end) return 0; /* Old servers may return an empty success. */
    if (!take(&r, '{')) return -1;
    if (!take(&r, '}')) {
        do {
            char key[256];
            if (string(&r, key, sizeof(key)) || !take(&r, ':')) return -1;
            if (!strcmp(key, "DEBUG")) {
                if (seen_debug++) return -1;
                if (literal(&r, "true")) parsed.debug = 1;
                else if (!literal(&r, "false")) return -1;
            } else if (!strcmp(key, "DEBUG_SIGNATURE")) {
                if (seen_signature++ || string(&r, parsed.signature, sizeof(parsed.signature))) return -1;
            } else if (value(&r, 1)) return -1;
            if (take(&r, '}')) break;
            if (!take(&r, ',')) return -1;
        } while (1);
    }
    space(&r);
    if (r.p != r.end) return -1;
    *out = parsed;
    return 0;
}
