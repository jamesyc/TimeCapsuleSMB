#ifndef TC_UTF8_H
#define TC_UTF8_H
#include <stddef.h>

/* The Unicode scalar value at s in its shortest UTF-8 form, read no further
 * than end: returns the sequence length, or 0 when the bytes there are not
 * one (overlong, a surrogate, past U+10FFFF, cut off). */
static inline size_t tc_utf8_decode(const unsigned char *s, const unsigned char *end, unsigned *cp) {
    size_t length = *s < 0x80 ? 1 : *s < 0xc2 ? 0 : *s < 0xe0 ? 2 : *s < 0xf0 ? 3 : *s < 0xf5 ? 4 : 0, i;
    unsigned value;
    if (!length || (size_t)(end - s) < length)
        return 0;
    value = length == 1 ? *s : *s & (0x7f >> length);
    for (i = 1; i < length; i++) {
        if ((s[i] & 0xc0) != 0x80)
            return 0;
        value = value << 6 | (s[i] & 0x3f);
    }
    if ((length == 3 && value < 0x800) || (length == 4 && (value < 0x10000 || value > 0x10ffff)) ||
        value - 0xd800 < 0x800)
        return 0;
    *cp = value;
    return length;
}
#endif
