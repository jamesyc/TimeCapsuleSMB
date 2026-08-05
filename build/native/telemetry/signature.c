#include "telemetry.h"
#ifndef TC_HEARTBEAT_PUBLIC_KEY_BYTES
#define TC_HEARTBEAT_PUBLIC_KEY_BYTES \
    0x86, 0x07, 0x80, 0x6e, 0x04, 0xbf, 0x0b, 0x35, \
    0x02, 0x90, 0x48, 0x22, 0x06, 0x13, 0xdd, 0x05, \
    0xd9, 0x2c, 0x84, 0xe0, 0x7c, 0xea, 0x37, 0xfe, \
    0xe5, 0x1b, 0xdf, 0xc2, 0x65, 0x3c, 0xbc, 0xbe
#endif

static int hex_value(int ch) {
    if (ch >= '0' && ch <= '9') {
        return ch - '0';
    }
    if (ch >= 'a' && ch <= 'f') {
        return ch - 'a' + 10;
    }
    if (ch >= 'A' && ch <= 'F') {
        return ch - 'A' + 10;
    }
    return -1;
}

static int parse_heartbeat_signature(const unsigned char *input, size_t input_len, unsigned char out[64]) {
    size_t i;
    size_t hex_count;
    int high;
    int low;

    if (input_len == 64) {
        memcpy(out, input, 64);
        return 0;
    }

    hex_count = 0;
    high = -1;
    memset(out, 0, 64);
    for (i = 0; i < input_len; i++) {
        int value = hex_value(input[i]);
        if (value < 0) {
            if (isspace(input[i])) {
                continue;
            }
            return -1;
        }
        if (hex_count >= 128) {
            return -1;
        }
        if ((hex_count % 2) == 0) {
            high = value;
        } else {
            low = value;
            out[hex_count / 2] = (unsigned char)((high << 4) | low);
            high = -1;
        }
        hex_count++;
    }
    return hex_count == 128 && high == -1 ? 0 : -1;
}

static int verify_heartbeat_signature_bytes(const unsigned char *data,
                                            size_t data_len,
                                            const unsigned char signature[64],
                                            const unsigned char public_key[32]) {
    unsigned char *signed_message;
    unsigned char *opened_message;
    unsigned long long signed_len;
    unsigned long long opened_len;
    int ok;

    if (data_len > TC_DEBUG_MAX) {
        return 0;
    }
    signed_len = (unsigned long long)data_len + 64ULL;
    signed_message = (unsigned char *)malloc((size_t)signed_len);
    opened_message = (unsigned char *)malloc((size_t)signed_len);
    if (signed_message == NULL || opened_message == NULL) {
        free(signed_message);
        free(opened_message);
        return 0;
    }

    memcpy(signed_message, signature, 64);
    memcpy(signed_message + 64, data, data_len);
    opened_len = 0;
    ok = crypto_sign_open(opened_message, &opened_len, signed_message, signed_len, public_key) == 0 &&
         opened_len == (unsigned long long)data_len &&
         memcmp(opened_message, data, data_len) == 0;
    free(signed_message);
    free(opened_message);
    return ok ? 1 : 0;
}


int telemetry_verify(const unsigned char *data, size_t len, const unsigned char *signature, size_t sig_len) {
    static const unsigned char key[32] = { TC_HEARTBEAT_PUBLIC_KEY_BYTES };
    unsigned char parsed[64];
    return parse_heartbeat_signature(signature, sig_len, parsed) == 0 &&
           verify_heartbeat_signature_bytes(data, len, parsed, key);
}
int telemetry_authorized(const struct telemetry_response *response, const char *payload) {
    unsigned char digest[64];
    char hex[129], message[160];
    size_t i;
    int n;
    /* Bind authorization to the exact POST, including device identity, lane,
     * and random nonce. Binding only the nonce would allow an intermediary to
     * substitute the ID of another router selected for debugging. */
    crypto_hash(digest, (const unsigned char *)payload, (unsigned long long)strlen(payload));
    for (i = 0; i < sizeof(digest); i++) snprintf(hex + 2 * i, 3, "%02x", digest[i]);
    n = snprintf(message, sizeof(message), "tc-debug-v1\n%s\n", hex);
    return n > 0 && (size_t)n < sizeof(message) && response->debug &&
        telemetry_verify((const unsigned char *)message, (size_t)n,
                         (const unsigned char *)response->signature, strlen(response->signature));
}
