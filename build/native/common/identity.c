#include "plan.h"

/* Mirrors probe.py normalize_runtime_mdns_instance_name(): control
 * characters -> '-', trim ASCII whitespace, truncate to 63 UTF-8 bytes on a
 * character boundary. */
static int normalize_display_name(char *out, size_t out_len, const char *value, size_t max_bytes) {
    char work[512];
    size_t len, start, end, i, cut;

    out[0] = '\0';
    if (value == NULL) {
        return -1;
    }
    len = strlen(value);
    if (len >= sizeof(work)) {
        len = sizeof(work) - 1;
    }
    for (i = 0; i < len; i++) {
        unsigned char ch = (unsigned char)value[i];
        work[i] = (ch < 0x20 || ch == 0x7f) ? '-' : (char)ch;
    }
    work[len] = '\0';
    start = 0;
    while (start < len && (work[start] == ' ' || work[start] == '\t')) {
        start++;
    }
    end = len;
    while (end > start && (work[end - 1] == ' ' || work[end - 1] == '\t')) {
        end--;
    }
    cut = end - start;
    if (cut > max_bytes) {
        cut = max_bytes;
        /* Do not split a UTF-8 sequence. */
        while (cut > 0 && ((unsigned char)work[start + cut] & 0xc0) == 0x80) {
            cut--;
        }
    }
    while (cut > 0 && (work[start + cut - 1] == ' ' || work[start + cut - 1] == '\t')) {
        cut--;
    }
    if (cut == 0 || cut >= out_len) {
        return -1;
    }
    memcpy(out, work + start, cut);
    out[cut] = '\0';
    return 0;
}

int normalize_instance_name(char *out, size_t out_len, const char *value) {
    return normalize_display_name(out, out_len, value, 63);
}

int normalize_server_string(char *out, size_t out_len, const char *value) {
    return normalize_display_name(out, out_len, value, 255);
}

/* Mirrors probe.py normalize_runtime_netbios_name(): first hostname label,
 * keep [A-Za-z0-9_-], require one alphanumeric, truncate to 15. */
int normalize_netbios_name(char *out, size_t out_len, const char *value) {
    size_t used = 0;
    int has_alnum = 0;
    const char *p;

    out[0] = '\0';
    if (value == NULL || out_len < 16) {
        return -1;
    }
    p = value;
    while (*p == ' ' || *p == '\t') {
        p++;
    }
    for (; *p != '\0' && *p != '.' && used < 15; p++) {
        unsigned char ch = (unsigned char)*p;
        if (isalnum(ch)) {
            has_alnum = 1;
        } else if (ch != '_' && ch != '-') {
            continue;
        }
        out[used++] = (char)ch;
    }
    out[used] = '\0';
    if (!has_alnum) {
        out[0] = '\0';
        return -1;
    }
    return 0;
}

/* Colon or dash separated hex -> XX:XX:XX:XX:XX:XX uppercase (the form used
 * in the adisk sys= TXT item today). */
int normalize_mac_text(char *out, size_t out_len, const char *value) {
    char digits[13];
    size_t count = 0;
    const char *p;

    out[0] = '\0';
    if (value == NULL || out_len < 18) {
        return -1;
    }
    for (p = value; *p != '\0'; p++) {
        unsigned char ch = (unsigned char)*p;
        if (ch == ':' || ch == '-') {
            continue;
        }
        if (!isxdigit(ch) || count >= 12) {
            return -1;
        }
        digits[count++] = (char)toupper(ch);
    }
    if (count != 12) {
        return -1;
    }
    digits[12] = '\0';
    (void)snprintf(out, out_len, "%c%c:%c%c:%c%c:%c%c:%c%c:%c%c",
                   digits[0], digits[1], digits[2], digits[3], digits[4], digits[5],
                   digits[6], digits[7], digits[8], digits[9], digits[10], digits[11]);
    return 0;
}

/* Mirrors probe.py normalize_runtime_mdns_host_label(): first label,
 * lowercase, [a-z0-9-] with everything else '-', trimmed of dashes. */
int normalize_host_label(char *out, size_t out_len, const char *value) {
    size_t used = 0;
    const char *p;
    const char *end;

    out[0] = '\0';
    if (value == NULL || out_len < 64) {
        return -1;
    }
    p = value;
    while (isspace((unsigned char)*p)) {
        p++;
    }
    end = p;
    while (*end != '\0' && *end != '.') {
        end++;
    }
    while (end > p && isspace((unsigned char)end[-1])) {
        end--;
    }
    for (; p < end && used < 63; p++) {
        unsigned char ch = (unsigned char)tolower((unsigned char)*p);
        if (!((ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9'))) {
            ch = '-';
        }
        if (ch == '-' && used == 0) {
            continue;
        }
        out[used++] = (char)ch;
    }
    while (used > 0 && out[used - 1] == '-') {
        used--;
    }
    out[used] = '\0';
    return used ? 0 : -1;
}

void identity_derive(struct identity *out, const struct device_facts *facts) {
    const char *synm = acp_str(&facts->acp[ACP_KEY_syNm]);
    const char *wama = acp_str(&facts->acp[ACP_KEY_waMA]);

    memset(out, 0, sizeof(*out));

    if (out->instance[0] == '\0' && synm != NULL) {
        (void)normalize_instance_name(out->instance, sizeof(out->instance), synm);
    }
    if (out->instance[0] == '\0') {
        /* Same fallback chain as the doctor's derive_runtime_naming_identity. */
        (void)normalize_host_label(out->instance, sizeof(out->instance), facts->hostname);
    }
    if (out->instance[0] == '\0' && synm != NULL) {
        (void)normalize_host_label(out->instance, sizeof(out->instance), synm);
    }
    if (out->instance[0] == '\0') {
        strcpy(out->instance, "timecapsule");
    }

    if (out->netbios[0] == '\0') {
        (void)normalize_netbios_name(out->netbios, sizeof(out->netbios), facts->hostname);
    }
    if (out->netbios[0] == '\0' && synm != NULL) {
        (void)normalize_netbios_name(out->netbios, sizeof(out->netbios), synm);
    }
    if (out->netbios[0] == '\0') {
        strcpy(out->netbios, "TimeCapsule");
    }
    if (wama != NULL) {
        (void)normalize_mac_text(out->wama, sizeof(out->wama), wama);
    }
}
