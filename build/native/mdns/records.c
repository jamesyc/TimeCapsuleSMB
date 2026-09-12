#include "mdns.h"
TC_LOCAL int normalize_mac_for_airport_txt(char *out, size_t out_len, const char *value, const char *field_name);
TC_LOCAL int validate_adisk_disk_advf(const char *value);
TC_LOCAL int validate_airport_ascii_field(const char *value, const char *field_name);
TC_LOCAL int validate_txt_ascii_field(const char *value, const char *field_name);
TC_LOCAL int append_txt_itemf(char storage[][MAX_TXT_STRING + 1],
                            const char *txts[],
                            size_t *count,
                            size_t max_count,
                            const char *format,
                            ...);
TC_LOCAL int build_riousbprint_pdl(char *out, size_t out_len, const char *cmd);
TC_LOCAL int validate_airport_usb_printer_txt_fields(const struct config *cfg);
TC_LOCAL int append_airport_usb_printer_intro_txt_items(const struct config *cfg,
                                                      char storage[][MAX_TXT_STRING + 1],
                                                      const char *txts[],
                                                      size_t *txt_count,
                                                      size_t max_count);
TC_LOCAL int append_airport_usb_printer_device_txt_items(const struct config *cfg,
                                                       char storage[][MAX_TXT_STRING + 1],
                                                       const char *txts[],
                                                       size_t *txt_count,
                                                       size_t max_count);
TC_LOCAL int plan_empty_txt_service_records(struct planned_rr_set *set,
                                          int routes,
                                          const struct config *cfg,
                                          const char *service_type,
                                          uint16_t port,
                                          const char *instance_fqdn,
                                          const struct link_context *link,
                                          int include_ptr,
                                          int include_srv,
                                          int include_txt,
                                          int include_a,
                                          int include_aaaa);
TC_LOCAL int plan_service_type_enumeration_type(struct planned_rr_set *set,
                                              int routes,
                                              const char *service_type,
                                              uint32_t ttl);
int build_model_txt(char *out, size_t out_len, const char *device_model) {
    int written;

    if (device_model == NULL || device_model[0] == '\0') {
        return -1;
    }

    if (strlen(MODEL_TXT_PREFIX) + strlen(device_model) > MAX_TXT_STRING) {
        fprintf(stderr, "device model must be %d bytes or fewer\n", MAX_TXT_STRING - (int)strlen(MODEL_TXT_PREFIX));
        return -1;
    }

    written = snprintf(out, out_len, MODEL_TXT_PREFIX "%s", device_model);
    if (written < 0 || (size_t)written >= out_len) {
        return -1;
    }

    return 0;
}

int build_adisk_system_txt(char *out, size_t out_len, const char *wama) {
    int written;
    const unsigned char *p;
    char normalized[18];
    size_t i;

    if (wama == NULL || wama[0] == '\0') {
        return -1;
    }

    if (strlen(wama) >= sizeof(normalized)) {
        fprintf(stderr, "adisk sys waMA must be a MAC address\n");
        return -1;
    }

    for (p = (const unsigned char *)wama; *p != '\0'; p++) {
        if (!((*p >= '0' && *p <= '9') || (*p >= 'A' && *p <= 'F') || (*p >= 'a' && *p <= 'f') || *p == ':')) {
            fprintf(stderr, "adisk sys waMA must be a MAC address\n");
            return -1;
        }
    }

    for (i = 0; wama[i] != '\0'; i++) {
        normalized[i] = (char)toupper((unsigned char)wama[i]);
    }
    normalized[i] = '\0';

    if (strlen(ADISK_SYS_TXT_PREFIX) + strlen(normalized) + strlen(ADISK_SYS_TXT_SUFFIX) > MAX_TXT_STRING) {
        return -1;
    }

    written = snprintf(out, out_len, ADISK_SYS_TXT_PREFIX "%s" ADISK_SYS_TXT_SUFFIX, normalized);
    if (written < 0 || (size_t)written >= out_len) {
        return -1;
    }

    return 0;
}

TC_LOCAL int normalize_mac_for_airport_txt(char *out, size_t out_len, const char *value, const char *field_name) {
    size_t in_i = 0;
    size_t hex_count = 0;
    char hex_digits[13];

    if (out_len < 18) {
        return -1;
    }
    if (value == NULL || value[0] == '\0') {
        fprintf(stderr, "%s must be a MAC address\n", field_name);
        return -1;
    }

    while (value[in_i] != '\0') {
        unsigned char ch = (unsigned char)value[in_i];
        if (ch == ':' || ch == '-') {
            in_i++;
            continue;
        }
        if (!isxdigit(ch)) {
            fprintf(stderr, "%s must be a MAC address\n", field_name);
            return -1;
        }
        if (hex_count >= 12) {
            fprintf(stderr, "%s must be a MAC address\n", field_name);
            return -1;
        }
        hex_digits[hex_count++] = (char)toupper(ch);
        in_i++;
    }

    if (hex_count != 12) {
        fprintf(stderr, "%s must be a MAC address\n", field_name);
        return -1;
    }
    hex_digits[12] = '\0';

    snprintf(out, out_len, "%c%c-%c%c-%c%c-%c%c-%c%c-%c%c",
             hex_digits[0], hex_digits[1], hex_digits[2], hex_digits[3],
             hex_digits[4], hex_digits[5], hex_digits[6], hex_digits[7],
             hex_digits[8], hex_digits[9], hex_digits[10], hex_digits[11]);
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

TC_LOCAL int validate_airport_ascii_field(const char *value, const char *field_name) {
    const unsigned char *p;

    if (value == NULL || value[0] == '\0') {
        fprintf(stderr, "%s must not be empty\n", field_name);
        return -1;
    }

    for (p = (const unsigned char *)value; *p != '\0'; p++) {
        if (*p < 0x20 || *p == 0x7f || *p == ',') {
            fprintf(stderr, "%s contains an invalid character\n", field_name);
            return -1;
        }
    }

    return 0;
}

int build_airport_txt(char *out, size_t out_len, const struct config *cfg) {
    int written;
    size_t off = 0;
    int appended = 0;
    char normalized_wama[18];
    char normalized_rama[18];
    char normalized_ram2[18];

    normalized_wama[0] = '\0';
    normalized_rama[0] = '\0';
    normalized_ram2[0] = '\0';

    #define APPEND_AIRPORT_CHUNK(...) \
        do { \
            written = snprintf(out + off, out_len - off, __VA_ARGS__); \
            if (written < 0 || (size_t)written >= out_len - off) { \
                return -1; \
            } \
            off += (size_t)written; \
            appended = 1; \
        } while (0)

    #define APPEND_AIRPORT_FIELD(fmt, value) \
        do { \
            if ((value)[0] != '\0') { \
                APPEND_AIRPORT_CHUNK("%s" fmt, appended ? "," : "", value); \
            } \
        } while (0)

    if (!is_airport_enabled(cfg)) {
        return -1;
    }
    if ((cfg->airport_wama[0] != '\0' &&
         normalize_mac_for_airport_txt(normalized_wama, sizeof(normalized_wama), cfg->airport_wama, "airport waMA") != 0) ||
        (cfg->airport_rama[0] != '\0' &&
         normalize_mac_for_airport_txt(normalized_rama, sizeof(normalized_rama), cfg->airport_rama, "airport raMA") != 0) ||
        (cfg->airport_ram2[0] != '\0' &&
         normalize_mac_for_airport_txt(normalized_ram2, sizeof(normalized_ram2), cfg->airport_ram2, "airport raM2") != 0) ||
        (cfg->airport_rast[0] != '\0' && validate_airport_ascii_field(cfg->airport_rast, "airport raSt") != 0) ||
        (cfg->airport_rana[0] != '\0' && validate_airport_ascii_field(cfg->airport_rana, "airport raNA") != 0) ||
        (cfg->airport_syfl[0] != '\0' && validate_airport_ascii_field(cfg->airport_syfl, "airport syFl") != 0) ||
        (cfg->airport_syvs[0] != '\0' && validate_airport_ascii_field(cfg->airport_syvs, "airport syVs") != 0) ||
        (cfg->airport_srcv[0] != '\0' && validate_airport_ascii_field(cfg->airport_srcv, "airport srcv") != 0) ||
        (cfg->airport_bjsd[0] != '\0' && validate_airport_ascii_field(cfg->airport_bjsd, "airport bjSd") != 0)) {
        return -1;
    }
    if (cfg->airport_syap[0] != '\0' &&
        validate_airport_ascii_field(cfg->airport_syap, "airport syAP") != 0) {
        return -1;
    }

    APPEND_AIRPORT_FIELD("waMA=%s", normalized_wama);
    APPEND_AIRPORT_FIELD("raMA=%s", normalized_rama);
    APPEND_AIRPORT_FIELD("raM2=%s", normalized_ram2);
    APPEND_AIRPORT_FIELD("raSt=%s", cfg->airport_rast);
    APPEND_AIRPORT_FIELD("raNA=%s", cfg->airport_rana);
    APPEND_AIRPORT_FIELD("syFl=%s", cfg->airport_syfl);
    APPEND_AIRPORT_FIELD("syAP=%s", cfg->airport_syap);
    APPEND_AIRPORT_FIELD("syVs=%s", cfg->airport_syvs);
    APPEND_AIRPORT_FIELD("srcv=%s", cfg->airport_srcv);
    APPEND_AIRPORT_FIELD("bjSd=%s", cfg->airport_bjsd);

    if (!appended) {
        return -1;
    }

    if (off > MAX_TXT_STRING) {
        fprintf(stderr, "_airport._tcp TXT must be %d bytes or fewer\n", MAX_TXT_STRING);
        return -1;
    }
    return 0;

    #undef APPEND_AIRPORT_FIELD
    #undef APPEND_AIRPORT_CHUNK
}

TC_LOCAL int validate_txt_ascii_field(const char *value, const char *field_name) {
    const unsigned char *p;

    if (value == NULL) {
        return 0;
    }
    for (p = (const unsigned char *)value; *p != '\0'; p++) {
        if (*p < 0x20 || *p == 0x7f) {
            fprintf(stderr, "%s contains an invalid control character\n", field_name);
            return -1;
        }
    }
    return 0;
}

TC_LOCAL int append_txt_itemf(char storage[][MAX_TXT_STRING + 1],
                            const char *txts[],
                            size_t *count,
                            size_t max_count,
                            const char *format,
                            ...) {
    va_list ap;
    int written;
    size_t len;

    if (storage == NULL || txts == NULL || count == NULL || format == NULL || *count >= max_count) {
        return -1;
    }

    va_start(ap, format);
    written = vsnprintf(storage[*count], MAX_TXT_STRING + 1, format, ap);
    va_end(ap);
    if (written < 0 || written > MAX_TXT_STRING) {
        return -1;
    }

    len = strlen(storage[*count]);
    if (len > MAX_TXT_STRING) {
        return -1;
    }
    txts[*count] = storage[*count];
    *count += 1;
    return 0;
}

TC_LOCAL int build_riousbprint_pdl(char *out, size_t out_len, const char *cmd) {
    const char *cursor;
    size_t off = 0;
    int appended = 0;

    if (out == NULL || out_len == 0 || cmd == NULL || cmd[0] == '\0') {
        return -1;
    }
    out[0] = '\0';

    cursor = cmd;
    while (*cursor != '\0') {
        const char *start;
        const char *end;
        int written;

        while (*cursor == ',' || isspace((unsigned char)*cursor)) {
            cursor++;
        }
        start = cursor;
        while (*cursor != '\0' && *cursor != ',') {
            cursor++;
        }
        end = cursor;
        while (end > start && isspace((unsigned char)*(end - 1))) {
            end--;
        }
        if (end == start) {
            continue;
        }
        written = snprintf(out + off,
                           out_len - off,
                           "%sapplication/%.*s",
                           appended ? "," : "",
                           (int)(end - start),
                           start);
        if (written < 0 || (size_t)written >= out_len - off) {
            return -1;
        }
        off += (size_t)written;
        appended = 1;
    }

    if (!appended || off > MAX_TXT_STRING) {
        return -1;
    }
    return 0;
}

TC_LOCAL int validate_airport_usb_printer_txt_fields(const struct config *cfg) {
    if (validate_txt_ascii_field(cfg->riousbprint_note, "USB printer note") != 0 ||
        validate_txt_ascii_field(cfg->riousbprint_mfg, "USB printer manufacturer") != 0 ||
        validate_txt_ascii_field(cfg->riousbprint_mdl, "USB printer model") != 0 ||
        validate_txt_ascii_field(cfg->riousbprint_serial, "USB printer serial") != 0 ||
        validate_txt_ascii_field(cfg->riousbprint_cmd, "USB printer command set") != 0) {
        return -1;
    }
    return 0;
}

TC_LOCAL int append_airport_usb_printer_intro_txt_items(const struct config *cfg,
                                                      char storage[][MAX_TXT_STRING + 1],
                                                      const char *txts[],
                                                      size_t *txt_count,
                                                      size_t max_count) {
    const char *note;

    note = cfg->riousbprint_note[0] != '\0' ? cfg->riousbprint_note : cfg->instance_name;
    if (append_txt_itemf(storage, txts, txt_count, max_count, "txtvers=1") != 0 ||
        append_txt_itemf(storage, txts, txt_count, max_count, "qtotal=1") != 0 ||
        append_txt_itemf(storage, txts, txt_count, max_count, "note=%s", note) != 0 ||
        append_txt_itemf(storage, txts, txt_count, max_count, "product=(%s)", cfg->riousbprint_instance_name) != 0) {
        return -1;
    }
    return 0;
}

TC_LOCAL int append_airport_usb_printer_device_txt_items(const struct config *cfg,
                                                       char storage[][MAX_TXT_STRING + 1],
                                                       const char *txts[],
                                                       size_t *txt_count,
                                                       size_t max_count) {
    if (cfg->riousbprint_mfg[0] != '\0' &&
        append_txt_itemf(storage, txts, txt_count, max_count, "usb_MFG=%s", cfg->riousbprint_mfg) != 0) {
        return -1;
    }
    if (cfg->riousbprint_cmd[0] != '\0' &&
        append_txt_itemf(storage, txts, txt_count, max_count, "usb_CMD=%s", cfg->riousbprint_cmd) != 0) {
        return -1;
    }
    if (cfg->riousbprint_mdl[0] != '\0' &&
        append_txt_itemf(storage, txts, txt_count, max_count, "usb_MDL=%s", cfg->riousbprint_mdl) != 0) {
        return -1;
    }
    if (append_txt_itemf(storage, txts, txt_count, max_count, "usb_CLS=PRINTER") != 0 ||
        append_txt_itemf(storage, txts, txt_count, max_count, "usb_DES=%s", cfg->riousbprint_instance_name) != 0) {
        return -1;
    }
    return 0;
}

int build_riousbprint_txt_items(const struct config *cfg,
                                       char storage[][MAX_TXT_STRING + 1],
                                       const char *txts[],
                                       size_t *txt_count) {
    char pdl[MAX_TXT_STRING + 1];

    *txt_count = 0;
    if (!is_riousbprint_enabled(cfg) ||
        validate_airport_usb_printer_txt_fields(cfg) != 0 ||
        append_airport_usb_printer_intro_txt_items(cfg, storage, txts, txt_count, RIOUSBPRINT_MAX_TXT_ITEMS) != 0) {
        return -1;
    }

    if (cfg->riousbprint_serial[0] != '\0') {
        if (append_txt_itemf(storage,
                             txts,
                             txt_count,
                             RIOUSBPRINT_MAX_TXT_ITEMS,
                             "rp=%s %s",
                             cfg->riousbprint_instance_name,
                             cfg->riousbprint_serial) != 0) {
            return -1;
        }
    } else if (append_txt_itemf(storage,
                                txts,
                                txt_count,
                                RIOUSBPRINT_MAX_TXT_ITEMS,
                                "rp=%s",
                                cfg->riousbprint_instance_name) != 0) {
        return -1;
    }

    if (cfg->riousbprint_cmd[0] != '\0') {
        if (build_riousbprint_pdl(pdl, sizeof(pdl), cfg->riousbprint_cmd) != 0 ||
            append_txt_itemf(storage, txts, txt_count, RIOUSBPRINT_MAX_TXT_ITEMS, "pdl=%s", pdl) != 0) {
            return -1;
        }
    }

    if (append_txt_itemf(storage, txts, txt_count, RIOUSBPRINT_MAX_TXT_ITEMS, "priority=1") != 0) {
        return -1;
    }
    if (append_airport_usb_printer_device_txt_items(cfg, storage, txts, txt_count, RIOUSBPRINT_MAX_TXT_ITEMS) != 0) {
        return -1;
    }

    return 0;
}

int build_pdl_datastream_txt_items(const struct config *cfg,
                                          char storage[][MAX_TXT_STRING + 1],
                                          const char *txts[],
                                          size_t *txt_count) {
    *txt_count = 0;
    if (!is_pdl_datastream_enabled(cfg) ||
        validate_airport_usb_printer_txt_fields(cfg) != 0 ||
        append_airport_usb_printer_intro_txt_items(cfg, storage, txts, txt_count, PDL_DATASTREAM_MAX_TXT_ITEMS) != 0) {
        return -1;
    }

    if (append_txt_itemf(storage, txts, txt_count, PDL_DATASTREAM_MAX_TXT_ITEMS, "pdl=U") != 0 ||
        append_txt_itemf(storage, txts, txt_count, PDL_DATASTREAM_MAX_TXT_ITEMS, "priority=5") != 0 ||
        append_airport_usb_printer_device_txt_items(cfg, storage, txts, txt_count, PDL_DATASTREAM_MAX_TXT_ITEMS) != 0 ||
        append_txt_itemf(storage, txts, txt_count, PDL_DATASTREAM_MAX_TXT_ITEMS, "ty=%s", cfg->riousbprint_instance_name) != 0) {
        return -1;
    }

    return 0;
}

int add_adisk_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg, uint32_t ttl, int *answers) {
    char instance_fqdn[MAX_NAME];
    char txt1[128];
    char disk_txts[ADISK_MAX_DISKS][256];
    const char *txts[ADISK_MAX_DISKS + 1];
    size_t i;

    if (!adisk_enabled(cfg)) {
        return 0;
    }

    if (build_instance_fqdn(instance_fqdn, sizeof(instance_fqdn), cfg->instance_name, cfg->adisk_service_type) != 0) {
        return -1;
    }
    if (build_adisk_system_txt(txt1, sizeof(txt1), cfg->adisk_sys_wama) != 0) {
        return -1;
    }
    txts[0] = txt1;
    for (i = 0; i < cfg->adisk_disks.count; i++) {
        const struct adisk_disk *disk = &cfg->adisk_disks.disks[i];
        if (build_adisk_disk_txt(disk_txts[i], sizeof(disk_txts[i]), disk->disk_key, disk->share_name, disk->uuid, disk->disk_advf) != 0) {
            return -1;
        }
        txts[i + 1] = disk_txts[i];
    }

    if (add_rr_ptr(buf, off, cap, cfg->adisk_service_type, instance_fqdn, ttl) != 0 ||
        add_rr_srv(buf, off, cap, instance_fqdn, cfg->host_fqdn, cfg->adisk_port, ttl) != 0 ||
        add_rr_txt_strings(buf, off, cap, instance_fqdn, ttl, txts, cfg->adisk_disks.count + 1) != 0) {
        return -1;
    }

    *answers += 3;
    return 0;
}

int add_device_info_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg, uint32_t ttl, int *answers) {
    char instance_fqdn[MAX_NAME];
    char model_txt[MAX_NAME + 16];
    const char *txts[1];

    if (cfg->device_model[0] == '\0') {
        return 0;
    }

    if (build_instance_fqdn(instance_fqdn, sizeof(instance_fqdn), cfg->instance_name, cfg->device_info_service_type) != 0) {
        return -1;
    }
    if (build_model_txt(model_txt, sizeof(model_txt), cfg->device_model) != 0) {
        return -1;
    }
    txts[0] = model_txt;

    if (add_rr_ptr(buf, off, cap, cfg->device_info_service_type, instance_fqdn, ttl) != 0 ||
        add_rr_srv(buf, off, cap, instance_fqdn, cfg->host_fqdn, 0, ttl) != 0 ||
        add_rr_txt_strings(buf, off, cap, instance_fqdn, ttl, txts, 1) != 0) {
        return -1;
    }

    *answers += 3;
    return 0;
}

int add_airport_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg, uint32_t ttl, int *answers) {
    char instance_fqdn[MAX_NAME];
    char airport_txt[256];
    const char *txts[1];

    if (!is_airport_enabled(cfg)) {
        return 0;
    }

    if (build_instance_fqdn(instance_fqdn, sizeof(instance_fqdn), cfg->instance_name, cfg->airport_service_type) != 0) {
        return -1;
    }
    if (build_airport_txt(airport_txt, sizeof(airport_txt), cfg) != 0) {
        return -1;
    }
    txts[0] = airport_txt;

    if (add_rr_ptr(buf, off, cap, cfg->airport_service_type, instance_fqdn, ttl) != 0 ||
        add_rr_srv(buf, off, cap, instance_fqdn, cfg->host_fqdn, cfg->airport_port, ttl) != 0 ||
        add_rr_txt_strings(buf, off, cap, instance_fqdn, ttl, txts, 1) != 0) {
        return -1;
    }

    *answers += 3;
    return 0;
}

int add_riousbprint_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg, uint32_t ttl, int *answers) {
    char instance_fqdn[MAX_NAME];
    char txt_storage[RIOUSBPRINT_MAX_TXT_ITEMS][MAX_TXT_STRING + 1];
    const char *txts[RIOUSBPRINT_MAX_TXT_ITEMS];
    size_t txt_count;

    if (!is_riousbprint_enabled(cfg)) {
        return 0;
    }

    if (build_instance_fqdn(instance_fqdn, sizeof(instance_fqdn), cfg->riousbprint_instance_name, RIOUSBPRINT_SERVICE_TYPE) != 0 ||
        build_riousbprint_txt_items(cfg, txt_storage, txts, &txt_count) != 0) {
        return -1;
    }

    if (add_rr_ptr(buf, off, cap, RIOUSBPRINT_SERVICE_TYPE, instance_fqdn, ttl) != 0 ||
        add_rr_srv(buf, off, cap, instance_fqdn, cfg->host_fqdn, cfg->riousbprint_port, ttl) != 0 ||
        add_rr_txt_strings(buf, off, cap, instance_fqdn, ttl, txts, txt_count) != 0) {
        return -1;
    }

    *answers += 3;
    return 0;
}

int add_pdl_datastream_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg, uint32_t ttl, int *answers) {
    char instance_fqdn[MAX_NAME];
    char txt_storage[PDL_DATASTREAM_MAX_TXT_ITEMS][MAX_TXT_STRING + 1];
    const char *txts[PDL_DATASTREAM_MAX_TXT_ITEMS];
    size_t txt_count;

    if (!is_pdl_datastream_enabled(cfg)) {
        return 0;
    }

    if (build_instance_fqdn(instance_fqdn, sizeof(instance_fqdn), cfg->riousbprint_instance_name, PDL_DATASTREAM_SERVICE_TYPE) != 0 ||
        build_pdl_datastream_txt_items(cfg, txt_storage, txts, &txt_count) != 0) {
        return -1;
    }

    if (add_rr_ptr(buf, off, cap, PDL_DATASTREAM_SERVICE_TYPE, instance_fqdn, ttl) != 0 ||
        add_rr_srv(buf, off, cap, instance_fqdn, cfg->host_fqdn, cfg->pdl_datastream_port, ttl) != 0 ||
        add_rr_txt_strings(buf, off, cap, instance_fqdn, ttl, txts, txt_count) != 0) {
        return -1;
    }

    *answers += 3;
    return 0;
}

int add_empty_txt_service_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg,
                                         const char *service_type, uint16_t port,
                                         uint32_t ttl, int *answers) {
    char instance_fqdn[MAX_NAME];

    if (build_instance_fqdn(instance_fqdn, sizeof(instance_fqdn), cfg->instance_name, service_type) != 0) {
        return -1;
    }
    if (add_rr_ptr(buf, off, cap, service_type, instance_fqdn, ttl) != 0 ||
        add_rr_srv(buf, off, cap, instance_fqdn, cfg->host_fqdn, port, ttl) != 0 ||
        add_rr_txt_empty(buf, off, cap, instance_fqdn, ttl) != 0) {
        return -1;
    }

    *answers += 3;
    return 0;
}

TC_LOCAL int plan_empty_txt_service_records(struct planned_rr_set *set,
                                          int routes,
                                          const struct config *cfg,
                                          const char *service_type,
                                          uint16_t port,
                                          const char *instance_fqdn,
                                          const struct link_context *link,
                                          int include_ptr,
                                          int include_srv,
                                          int include_txt,
                                          int include_a,
                                          int include_aaaa) {
    if (include_ptr &&
        planned_rr_add_name(set, routes, service_type, DNS_TYPE_PTR, DNS_CLASS_IN, cfg->ttl, instance_fqdn) != 0) {
        return -1;
    }
    if (include_srv && planned_rr_add_srv(set, routes, instance_fqdn, cfg->host_fqdn, port, cfg->ttl) != 0) {
        return -1;
    }
    if (include_txt && planned_rr_add_txt_empty(set, routes, instance_fqdn, cfg->ttl) != 0) {
        return -1;
    }
    return planned_rr_add_link_addresses(set, routes, cfg->host_fqdn, link, include_a, include_aaaa, cfg->ttl);
}

int plan_smb_records(struct planned_rr_set *set,
                            int routes,
                            const struct config *cfg,
                            const char *instance_fqdn,
                            const struct link_context *link,
                            int include_ptr,
                            int include_srv,
                            int include_txt,
                            int include_a,
                            int include_aaaa) {
    if (!smb_enabled(cfg)) {
        return 0;
    }
    return plan_empty_txt_service_records(set, routes, cfg, cfg->service_type, cfg->port, instance_fqdn, link,
                                          include_ptr, include_srv, include_txt, include_a, include_aaaa);
}

int plan_afp_records(struct planned_rr_set *set,
                            int routes,
                            const struct config *cfg,
                            const char *instance_fqdn,
                            const struct link_context *link,
                            int include_ptr,
                            int include_srv,
                            int include_txt,
                            int include_a,
                            int include_aaaa) {
    if (!afp_enabled(cfg)) {
        return 0;
    }
    return plan_empty_txt_service_records(set, routes, cfg, cfg->afp_service_type, cfg->afp_port, instance_fqdn, link,
                                          include_ptr, include_srv, include_txt, include_a, include_aaaa);
}

int plan_adisk_records(struct planned_rr_set *set,
                              int routes,
                              const struct config *cfg,
                              const char *instance_fqdn,
                              const struct link_context *link,
                              int include_ptr,
                              int include_srv,
                              int include_txt,
                              int include_a,
                              int include_aaaa) {
    char txt1[128];
    char disk_txts[ADISK_MAX_DISKS][256];
    const char *txts[ADISK_MAX_DISKS + 1];
    size_t i;

    if (!adisk_enabled(cfg)) {
        return 0;
    }
    if (include_ptr &&
        planned_rr_add_name(set, routes, cfg->adisk_service_type, DNS_TYPE_PTR, DNS_CLASS_IN, cfg->ttl, instance_fqdn) != 0) {
        return -1;
    }
    if (include_srv && planned_rr_add_srv(set, routes, instance_fqdn, cfg->host_fqdn, cfg->adisk_port, cfg->ttl) != 0) {
        return -1;
    }
    if (include_txt) {
        if (build_adisk_system_txt(txt1, sizeof(txt1), cfg->adisk_sys_wama) != 0) {
            return -1;
        }
        txts[0] = txt1;
        for (i = 0; i < cfg->adisk_disks.count; i++) {
            const struct adisk_disk *disk = &cfg->adisk_disks.disks[i];
            if (build_adisk_disk_txt(disk_txts[i], sizeof(disk_txts[i]), disk->disk_key, disk->share_name, disk->uuid, disk->disk_advf) != 0) {
                return -1;
            }
            txts[i + 1] = disk_txts[i];
        }
        if (planned_rr_add_txt_items(set, routes, instance_fqdn, txts, NULL, cfg->adisk_disks.count + 1, cfg->ttl) != 0) {
            return -1;
        }
    }
    return planned_rr_add_link_addresses(set, routes, cfg->host_fqdn, link, include_a, include_aaaa, cfg->ttl);
}

int plan_device_info_records(struct planned_rr_set *set,
                                    int routes,
                                    const struct config *cfg,
                                    const char *instance_fqdn,
                                    const struct link_context *link,
                                    int include_ptr,
                                    int include_srv,
                                    int include_txt,
                                    int include_a,
                                    int include_aaaa) {
    char model_txt[MAX_NAME + 16];
    const char *txts[1];

    if (cfg->device_model[0] == '\0') {
        return 0;
    }
    if (include_ptr &&
        planned_rr_add_name(set, routes, cfg->device_info_service_type, DNS_TYPE_PTR, DNS_CLASS_IN, cfg->ttl, instance_fqdn) != 0) {
        return -1;
    }
    if (include_srv && planned_rr_add_srv(set, routes, instance_fqdn, cfg->host_fqdn, 0, cfg->ttl) != 0) {
        return -1;
    }
    if (include_txt) {
        if (build_model_txt(model_txt, sizeof(model_txt), cfg->device_model) != 0) {
            return -1;
        }
        txts[0] = model_txt;
        if (planned_rr_add_txt_items(set, routes, instance_fqdn, txts, NULL, 1, cfg->ttl) != 0) {
            return -1;
        }
    }
    return planned_rr_add_link_addresses(set, routes, cfg->host_fqdn, link, include_a, include_aaaa, cfg->ttl);
}

int plan_airport_records(struct planned_rr_set *set,
                                int routes,
                                const struct config *cfg,
                                const char *instance_fqdn,
                                const struct link_context *link,
                                int include_ptr,
                                int include_srv,
                                int include_txt,
                                int include_a,
                                int include_aaaa) {
    char airport_txt[256];
    const char *txts[1];

    if (!is_airport_enabled(cfg)) {
        return 0;
    }
    if (include_ptr &&
        planned_rr_add_name(set, routes, cfg->airport_service_type, DNS_TYPE_PTR, DNS_CLASS_IN, cfg->ttl, instance_fqdn) != 0) {
        return -1;
    }
    if (include_srv && planned_rr_add_srv(set, routes, instance_fqdn, cfg->host_fqdn, cfg->airport_port, cfg->ttl) != 0) {
        return -1;
    }
    if (include_txt) {
        if (build_airport_txt(airport_txt, sizeof(airport_txt), cfg) != 0) {
            return -1;
        }
        txts[0] = airport_txt;
        if (planned_rr_add_txt_items(set, routes, instance_fqdn, txts, NULL, 1, cfg->ttl) != 0) {
            return -1;
        }
    }
    return planned_rr_add_link_addresses(set, routes, cfg->host_fqdn, link, include_a, include_aaaa, cfg->ttl);
}

int plan_riousbprint_records(struct planned_rr_set *set,
                                    int routes,
                                    const struct config *cfg,
                                    const char *instance_fqdn,
                                    const struct link_context *link,
                                    int include_ptr,
                                    int include_srv,
                                    int include_txt,
                                    int include_a,
                                    int include_aaaa) {
    char txt_storage[RIOUSBPRINT_MAX_TXT_ITEMS][MAX_TXT_STRING + 1];
    const char *txts[RIOUSBPRINT_MAX_TXT_ITEMS];
    size_t txt_count;

    if (!is_riousbprint_enabled(cfg)) {
        return 0;
    }
    if (include_ptr &&
        planned_rr_add_name(set, routes, RIOUSBPRINT_SERVICE_TYPE, DNS_TYPE_PTR, DNS_CLASS_IN, cfg->ttl, instance_fqdn) != 0) {
        return -1;
    }
    if (include_srv && planned_rr_add_srv(set, routes, instance_fqdn, cfg->host_fqdn, cfg->riousbprint_port, cfg->ttl) != 0) {
        return -1;
    }
    if (include_txt) {
        if (build_riousbprint_txt_items(cfg, txt_storage, txts, &txt_count) != 0) {
            return -1;
        }
        if (planned_rr_add_txt_items(set, routes, instance_fqdn, txts, NULL, txt_count, cfg->ttl) != 0) {
            return -1;
        }
    }
    return planned_rr_add_link_addresses(set, routes, cfg->host_fqdn, link, include_a, include_aaaa, cfg->ttl);
}

int plan_pdl_datastream_records(struct planned_rr_set *set,
                                       int routes,
                                       const struct config *cfg,
                                       const char *instance_fqdn,
                                       const struct link_context *link,
                                       int include_ptr,
                                       int include_srv,
                                       int include_txt,
                                       int include_a,
                                       int include_aaaa) {
    char txt_storage[PDL_DATASTREAM_MAX_TXT_ITEMS][MAX_TXT_STRING + 1];
    const char *txts[PDL_DATASTREAM_MAX_TXT_ITEMS];
    size_t txt_count;

    if (!is_pdl_datastream_enabled(cfg)) {
        return 0;
    }
    if (include_ptr &&
        planned_rr_add_name(set, routes, PDL_DATASTREAM_SERVICE_TYPE, DNS_TYPE_PTR, DNS_CLASS_IN, cfg->ttl, instance_fqdn) != 0) {
        return -1;
    }
    if (include_srv && planned_rr_add_srv(set, routes, instance_fqdn, cfg->host_fqdn, cfg->pdl_datastream_port, cfg->ttl) != 0) {
        return -1;
    }
    if (include_txt) {
        if (build_pdl_datastream_txt_items(cfg, txt_storage, txts, &txt_count) != 0) {
            return -1;
        }
        if (planned_rr_add_txt_items(set, routes, instance_fqdn, txts, NULL, txt_count, cfg->ttl) != 0) {
            return -1;
        }
    }
    return planned_rr_add_link_addresses(set, routes, cfg->host_fqdn, link, include_a, include_aaaa, cfg->ttl);
}

TC_LOCAL int plan_service_type_enumeration_type(struct planned_rr_set *set,
                                              int routes,
                                              const char *service_type,
                                              uint32_t ttl) {
    if (service_type == NULL || service_type[0] == '\0') {
        return 0;
    }
    return planned_rr_add_name(set,
                               routes,
                               DNS_SD_SERVICE_ENUMERATION_NAME,
                               DNS_TYPE_PTR,
                               DNS_CLASS_IN,
                               ttl,
                               service_type);
}

int plan_service_type_enumeration_records(struct planned_rr_set *set,
                                                 int routes,
                                                 const struct config *cfg) {

    if (smb_enabled(cfg) &&
        plan_service_type_enumeration_type(set, routes, cfg->service_type, cfg->ttl) != 0) {
        return -1;
    }
    if (afp_enabled(cfg) &&
        plan_service_type_enumeration_type(set, routes, cfg->afp_service_type, cfg->ttl) != 0) {
        return -1;
    }
    if (adisk_enabled(cfg) &&
        plan_service_type_enumeration_type(set, routes, cfg->adisk_service_type, cfg->ttl) != 0) {
        return -1;
    }
    if (cfg->device_model[0] != '\0' &&
        plan_service_type_enumeration_type(set, routes, cfg->device_info_service_type, cfg->ttl) != 0) {
        return -1;
    }
    if (is_airport_enabled(cfg) &&
        plan_service_type_enumeration_type(set, routes, cfg->airport_service_type, cfg->ttl) != 0) {
        return -1;
    }
    if (is_riousbprint_enabled(cfg) &&
        plan_service_type_enumeration_type(set, routes, RIOUSBPRINT_SERVICE_TYPE, cfg->ttl) != 0) {
        return -1;
    }
    if (is_pdl_datastream_enabled(cfg) &&
        plan_service_type_enumeration_type(set, routes, PDL_DATASTREAM_SERVICE_TYPE, cfg->ttl) != 0) {
        return -1;
    }
    return 0;
}
