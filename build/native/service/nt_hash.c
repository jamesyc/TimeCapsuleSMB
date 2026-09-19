#include "service.h"
struct tc_md4_ctx {
    uint32_t a;
    uint32_t b;
    uint32_t c;
    uint32_t d;
    uint64_t bytes;
    uint8_t block[64];
    size_t block_len;
};

static uint32_t tc_load_le32(const uint8_t *p) {
    return ((uint32_t)p[0]) |
           ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static void tc_store_le32(uint8_t *p, uint32_t value) {
    p[0] = (uint8_t)(value & 0xff);
    p[1] = (uint8_t)((value >> 8) & 0xff);
    p[2] = (uint8_t)((value >> 16) & 0xff);
    p[3] = (uint8_t)((value >> 24) & 0xff);
}

static uint32_t tc_rotl32(uint32_t value, unsigned int bits) {
    return (value << bits) | (value >> (32U - bits));
}

#define TC_MD4_F(x, y, z) (((x) & (y)) | (~(x) & (z)))
#define TC_MD4_G(x, y, z) (((x) & (y)) | ((x) & (z)) | ((y) & (z)))
#define TC_MD4_H(x, y, z) ((x) ^ (y) ^ (z))
#define TC_MD4_ROUND1(a, b, c, d, x, s) do { (a) = tc_rotl32((a) + TC_MD4_F((b), (c), (d)) + (x), (s)); } while (0)
#define TC_MD4_ROUND2(a, b, c, d, x, s) do { (a) = tc_rotl32((a) + TC_MD4_G((b), (c), (d)) + (x) + 0x5a827999U, (s)); } while (0)
#define TC_MD4_ROUND3(a, b, c, d, x, s) do { (a) = tc_rotl32((a) + TC_MD4_H((b), (c), (d)) + (x) + 0x6ed9eba1U, (s)); } while (0)

static void tc_md4_init(struct tc_md4_ctx *ctx) {
    ctx->a = 0x67452301U;
    ctx->b = 0xefcdab89U;
    ctx->c = 0x98badcfeU;
    ctx->d = 0x10325476U;
    ctx->bytes = 0;
    ctx->block_len = 0;
}

static void tc_md4_process_block(struct tc_md4_ctx *ctx, const uint8_t block[64]) {
    uint32_t x[16];
    uint32_t a;
    uint32_t b;
    uint32_t c;
    uint32_t d;
    size_t i;

    for (i = 0; i < 16; i++) {
        x[i] = tc_load_le32(block + i * 4);
    }

    a = ctx->a;
    b = ctx->b;
    c = ctx->c;
    d = ctx->d;

    TC_MD4_ROUND1(a, b, c, d, x[0], 3);
    TC_MD4_ROUND1(d, a, b, c, x[1], 7);
    TC_MD4_ROUND1(c, d, a, b, x[2], 11);
    TC_MD4_ROUND1(b, c, d, a, x[3], 19);
    TC_MD4_ROUND1(a, b, c, d, x[4], 3);
    TC_MD4_ROUND1(d, a, b, c, x[5], 7);
    TC_MD4_ROUND1(c, d, a, b, x[6], 11);
    TC_MD4_ROUND1(b, c, d, a, x[7], 19);
    TC_MD4_ROUND1(a, b, c, d, x[8], 3);
    TC_MD4_ROUND1(d, a, b, c, x[9], 7);
    TC_MD4_ROUND1(c, d, a, b, x[10], 11);
    TC_MD4_ROUND1(b, c, d, a, x[11], 19);
    TC_MD4_ROUND1(a, b, c, d, x[12], 3);
    TC_MD4_ROUND1(d, a, b, c, x[13], 7);
    TC_MD4_ROUND1(c, d, a, b, x[14], 11);
    TC_MD4_ROUND1(b, c, d, a, x[15], 19);

    TC_MD4_ROUND2(a, b, c, d, x[0], 3);
    TC_MD4_ROUND2(d, a, b, c, x[4], 5);
    TC_MD4_ROUND2(c, d, a, b, x[8], 9);
    TC_MD4_ROUND2(b, c, d, a, x[12], 13);
    TC_MD4_ROUND2(a, b, c, d, x[1], 3);
    TC_MD4_ROUND2(d, a, b, c, x[5], 5);
    TC_MD4_ROUND2(c, d, a, b, x[9], 9);
    TC_MD4_ROUND2(b, c, d, a, x[13], 13);
    TC_MD4_ROUND2(a, b, c, d, x[2], 3);
    TC_MD4_ROUND2(d, a, b, c, x[6], 5);
    TC_MD4_ROUND2(c, d, a, b, x[10], 9);
    TC_MD4_ROUND2(b, c, d, a, x[14], 13);
    TC_MD4_ROUND2(a, b, c, d, x[3], 3);
    TC_MD4_ROUND2(d, a, b, c, x[7], 5);
    TC_MD4_ROUND2(c, d, a, b, x[11], 9);
    TC_MD4_ROUND2(b, c, d, a, x[15], 13);

    TC_MD4_ROUND3(a, b, c, d, x[0], 3);
    TC_MD4_ROUND3(d, a, b, c, x[8], 9);
    TC_MD4_ROUND3(c, d, a, b, x[4], 11);
    TC_MD4_ROUND3(b, c, d, a, x[12], 15);
    TC_MD4_ROUND3(a, b, c, d, x[2], 3);
    TC_MD4_ROUND3(d, a, b, c, x[10], 9);
    TC_MD4_ROUND3(c, d, a, b, x[6], 11);
    TC_MD4_ROUND3(b, c, d, a, x[14], 15);
    TC_MD4_ROUND3(a, b, c, d, x[1], 3);
    TC_MD4_ROUND3(d, a, b, c, x[9], 9);
    TC_MD4_ROUND3(c, d, a, b, x[5], 11);
    TC_MD4_ROUND3(b, c, d, a, x[13], 15);
    TC_MD4_ROUND3(a, b, c, d, x[3], 3);
    TC_MD4_ROUND3(d, a, b, c, x[11], 9);
    TC_MD4_ROUND3(c, d, a, b, x[7], 11);
    TC_MD4_ROUND3(b, c, d, a, x[15], 15);

    ctx->a += a;
    ctx->b += b;
    ctx->c += c;
    ctx->d += d;
}

static void tc_md4_update(struct tc_md4_ctx *ctx, const uint8_t *data, size_t len) {
    size_t take;

    ctx->bytes += len;
    while (len > 0) {
        take = sizeof(ctx->block) - ctx->block_len;
        if (take > len) {
            take = len;
        }
        memcpy(ctx->block + ctx->block_len, data, take);
        ctx->block_len += take;
        data += take;
        len -= take;
        if (ctx->block_len == sizeof(ctx->block)) {
            tc_md4_process_block(ctx, ctx->block);
            ctx->block_len = 0;
        }
    }
}

static void tc_md4_final(struct tc_md4_ctx *ctx, uint8_t digest[16]) {
    uint8_t padding[64];
    uint8_t bit_len_bytes[8];
    uint64_t bit_len;
    size_t pad_len;
    size_t i;

    bit_len = ctx->bytes * 8U;
    memset(padding, 0, sizeof(padding));
    padding[0] = 0x80;
    pad_len = (ctx->block_len < 56) ? (56 - ctx->block_len) : (120 - ctx->block_len);
    tc_md4_update(ctx, padding, pad_len);

    for (i = 0; i < 8; i++) {
        bit_len_bytes[i] = (uint8_t)((bit_len >> (8U * i)) & 0xff);
    }
    tc_md4_update(ctx, bit_len_bytes, sizeof(bit_len_bytes));

    tc_store_le32(digest, ctx->a);
    tc_store_le32(digest + 4, ctx->b);
    tc_store_le32(digest + 8, ctx->c);
    tc_store_le32(digest + 12, ctx->d);
}

static int tc_utf8_next_codepoint(const uint8_t *input, size_t len, size_t *offset, uint32_t *codepoint) {
    uint8_t c0;
    uint8_t c1;
    uint8_t c2;
    uint8_t c3;
    uint32_t cp;

    if (*offset >= len) {
        return -1;
    }

    c0 = input[*offset];
    if (c0 < 0x80) {
        *codepoint = c0;
        (*offset)++;
        return 0;
    }

    if (c0 >= 0xc2 && c0 <= 0xdf) {
        if (*offset + 1 >= len) {
            return -1;
        }
        c1 = input[*offset + 1];
        if ((c1 & 0xc0) != 0x80) {
            return -1;
        }
        *codepoint = ((uint32_t)(c0 & 0x1f) << 6) | (uint32_t)(c1 & 0x3f);
        *offset += 2;
        return 0;
    }

    if (c0 >= 0xe0 && c0 <= 0xef) {
        if (*offset + 2 >= len) {
            return -1;
        }
        c1 = input[*offset + 1];
        c2 = input[*offset + 2];
        if ((c1 & 0xc0) != 0x80 || (c2 & 0xc0) != 0x80) {
            return -1;
        }
        if ((c0 == 0xe0 && c1 < 0xa0) || (c0 == 0xed && c1 >= 0xa0)) {
            return -1;
        }
        cp = ((uint32_t)(c0 & 0x0f) << 12) | ((uint32_t)(c1 & 0x3f) << 6) | (uint32_t)(c2 & 0x3f);
        if (cp >= 0xd800 && cp <= 0xdfff) {
            return -1;
        }
        *codepoint = cp;
        *offset += 3;
        return 0;
    }

    if (c0 >= 0xf0 && c0 <= 0xf4) {
        if (*offset + 3 >= len) {
            return -1;
        }
        c1 = input[*offset + 1];
        c2 = input[*offset + 2];
        c3 = input[*offset + 3];
        if ((c1 & 0xc0) != 0x80 || (c2 & 0xc0) != 0x80 || (c3 & 0xc0) != 0x80) {
            return -1;
        }
        if ((c0 == 0xf0 && c1 < 0x90) || (c0 == 0xf4 && c1 >= 0x90)) {
            return -1;
        }
        cp = ((uint32_t)(c0 & 0x07) << 18) |
             ((uint32_t)(c1 & 0x3f) << 12) |
             ((uint32_t)(c2 & 0x3f) << 6) |
             (uint32_t)(c3 & 0x3f);
        if (cp > 0x10ffff) {
            return -1;
        }
        *codepoint = cp;
        *offset += 4;
        return 0;
    }

    return -1;
}

static void tc_md4_update_utf16le_unit(struct tc_md4_ctx *ctx, uint16_t unit) {
    uint8_t encoded[2];

    encoded[0] = (uint8_t)(unit & 0xff);
    encoded[1] = (uint8_t)((unit >> 8) & 0xff);
    tc_md4_update(ctx, encoded, sizeof(encoded));
}

static int tc_nt_hash_utf8(const uint8_t *input, size_t len, uint8_t digest[16]) {
    struct tc_md4_ctx ctx;
    size_t offset;
    uint32_t cp;
    uint32_t shifted;
    uint16_t high;
    uint16_t low;

    tc_md4_init(&ctx);
    offset = 0;
    while (offset < len) {
        if (tc_utf8_next_codepoint(input, len, &offset, &cp) != 0) {
            fprintf(stderr, "invalid UTF-8 password input\n");
            return -1;
        }
        if (cp <= 0xffff) {
            tc_md4_update_utf16le_unit(&ctx, (uint16_t)cp);
        } else {
            shifted = cp - 0x10000;
            high = (uint16_t)(0xd800U + (shifted >> 10));
            low = (uint16_t)(0xdc00U + (shifted & 0x3ffU));
            tc_md4_update_utf16le_unit(&ctx, high);
            tc_md4_update_utf16le_unit(&ctx, low);
        }
    }
    tc_md4_final(&ctx, digest);
    return 0;
}

int print_nt_hash_from_stdin(void) {
    uint8_t input[NT_HASH_MAX_PASSWORD_BYTES + 1];
    uint8_t digest[16];
    size_t len;
    ssize_t read_len;
    size_t i;

    len = 0;
    while (len < sizeof(input)) {
        read_len = read(STDIN_FILENO, input + len, sizeof(input) - len);
        if (read_len < 0) {
            fprintf(stderr, "failed to read password input: %s\n", strerror(errno));
            return 1;
        }
        if (read_len == 0) {
            break;
        }
        len += (size_t)read_len;
    }
    if (len > NT_HASH_MAX_PASSWORD_BYTES) {
        fprintf(stderr, "password input exceeds %d bytes\n", NT_HASH_MAX_PASSWORD_BYTES);
        return 1;
    }
    if (len > 0 && input[len - 1] == '\n') {
        len--;
        if (len > 0 && input[len - 1] == '\r') {
            len--;
        }
    }
    if (len == 0) {
        fprintf(stderr, "password input is empty\n");
        return 1;
    }
    if (tc_nt_hash_utf8(input, len, digest) != 0) {
        return 1;
    }
    for (i = 0; i < sizeof(digest); i++) {
        printf("%02X", digest[i]);
    }
    printf("\n");
    if (ferror(stdout) || fflush(stdout) != 0) {
        fprintf(stderr, "failed to write NT hash\n");
        return 1;
    }
    return 0;
}

/* Keep passwords inside this process. Raw capture preserves whitespace; the
 * final-LF/CR treatment matches the old shell substitution + stdin interface. */
int device_nt_hash(char output[33]) {
    char input[8193];
    struct acp_request request;
    uint8_t digest[16];
    size_t len, i;
    int rc = 1;
    volatile char *wipe = input;
    memset(&request, 0, sizeof(request));
    request.key = "syPW";
    request.multiline = 1;
    request.output = input;
    request.capacity = sizeof(input);
    (void)acp_collect_run(&request, 1, (long long)TC_ACP_TIMEOUT_SECONDS * 1000,
                        (long long)TC_ACP_COLLECTION_BUDGET_SECONDS * 1000);
    if (request.status != ACP_OK) {
        if (request.status == ACP_UNAVAILABLE && request.exit_status > 0) rc = request.exit_status;
        goto done;
    }
    len = request.length;
    while (len && input[len - 1] == '\n') len--;
    if (!len || len + 1 > NT_HASH_MAX_PASSWORD_BYTES) goto done;
    if (input[len - 1] == '\r') len--;
    if (!len || tc_nt_hash_utf8((const uint8_t *)input, len, digest) != 0) goto done;
    for (i = 0; i < sizeof(digest); i++) snprintf(output + i * 2, 3, "%02X", digest[i]);
    output[32] = '\0';
    rc = 0;
done:
    for (i = 0; i < sizeof(input); i++) wipe[i] = 0;
    return rc;
}

int print_device_nt_hash(void) {
    char output[33];
    if (device_nt_hash(output) != 0) return 1;
    return printf("%s\n", output) < 0 || fflush(stdout) != 0;
}
