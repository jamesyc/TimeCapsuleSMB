#include "mdns.h"
TC_LOCAL int validate_dns_label_text(const char *value, const char *field_name, int allow_dots);
TC_LOCAL int escape_dns_label(char *out, size_t out_len, const char *label);
TC_LOCAL void trim_ascii_whitespace(char *value);
TC_LOCAL int add_adisk_disk_config(struct config *cfg, const char *share_name, const char *disk_key,
                                 const char *adisk_uuid, const char *adisk_disk_advf);
TC_LOCAL int adisk_configured(const struct config *cfg);
TC_LOCAL int is_airport_usb_printer_enabled(const struct config *cfg);
void usage(const char *prog) {
    fprintf(stderr,
            "Usage: %s --instance <name> --host <label> --auto-ip [options]\n"
            "       %s --print-mdns-socket-families\n"
            "       %s --version\n"
            "Options:\n"
            "  --auto-ip          Serve every usable live address link and track IP changes\n"
            "  --print-mdns-socket-families Print required mDNS UDP socket families for live advertise links\n"
            "  --version          Print advertiser version code and exit\n"
            "  --debug-logging    Enable verbose mDNS traffic counter diagnostics\n"
            "  --diskless        Suppress generated _smb and _adisk records\n"
            "  --afp             Also advertise generated _afpovertcp._tcp on port 548\n"
            "  --adisk-shares-file <p> Tab-separated share,disk-key,uuid,adVF rows\n"
            "  --adisk-sys-wama <m> MAC address for _adisk sys TXT\n"
            "  --device-model <m> Also advertise _device-info._tcp with model=<m>\n"
            "  --riousbprint-name <n> Also advertise AirPort Remote I/O USB printer service\n"
            "  --riousbprint-note <n> TXT note for _riousbprint, normally AirPort system name\n"
            "  --riousbprint-mfg <m> USB printer manufacturer for usb_MFG\n"
            "  --riousbprint-mdl <m> USB printer model for usb_MDL\n"
            "  --riousbprint-serial <s> USB printer serial used in rp\n"
            "  --riousbprint-cmd <c> Override IEEE-1284 CMD command set for usb_CMD\n"
            "  --riousbprint-vendor-id <n> USB vendor ID used to find IEEE-1284 CMD on NetBSD\n"
            "  --riousbprint-product-id <n> USB product ID used to find IEEE-1284 CMD on NetBSD\n"
            "  --riousbprint-port <p> _riousbprint._tcp service port (default: 10000)\n"
            "  --pdl-datastream-port <p> _pdl-datastream._tcp service port for the same USB printer (default: 9100)\n"
            "  --airport-wama <m> Also advertise _airport._tcp with Apple-style TXT\n"
            "  --airport-rama <m> 5 GHz radio MAC for _airport._tcp\n"
            "  --airport-ram2 <m> 2.4 GHz radio MAC for _airport._tcp\n"
            "  --airport-rast <n> Radio state field for _airport._tcp\n"
            "  --airport-rana <n> Radio network-assist field for _airport._tcp\n"
            "  --airport-syfl <n> System feature flags for _airport._tcp\n"
            "  --airport-syap <n> Apple platform code for _airport._tcp\n"
            "  --airport-syvs <v> Firmware version for _airport._tcp\n"
            "  --airport-srcv <v> Source/build version for _airport._tcp\n"
            "  --airport-bjsd <n> Bonjour seed/build field for _airport._tcp\n"
            "  --airport-port <p> _airport._tcp service port (default: 5009)\n",
            prog, prog, prog);
}

TC_LOCAL int validate_dns_label_text(const char *value, const char *field_name, int allow_dots) {
    size_t len;
    const unsigned char *p;

    if (value == NULL || value[0] == '\0') {
        fprintf(stderr, "%s must not be empty\n", field_name);
        return -1;
    }

    len = strlen(value);
    if (len > MAX_LABEL) {
        fprintf(stderr, "%s must be %d bytes or fewer\n", field_name, MAX_LABEL);
        return -1;
    }

    if (!allow_dots && strchr(value, '.') != NULL) {
        fprintf(stderr, "%s must not contain dots\n", field_name);
        return -1;
    }

    for (p = (const unsigned char *)value; *p != '\0'; p++) {
        if (*p < 0x20 || *p == 0x7f) {
            fprintf(stderr, "%s contains an invalid control character\n", field_name);
            return -1;
        }
    }

    return 0;
}

int validate_single_dns_label(const char *value, const char *field_name) {
    return validate_dns_label_text(value, field_name, 0);
}

int validate_generated_dns_label(const char *value, const char *field_name) {
    return validate_dns_label_text(value, field_name, 1);
}

TC_LOCAL int escape_dns_label(char *out, size_t out_len, const char *label) {
    size_t in_i;
    size_t out_i = 0;

    if (label == NULL || label[0] == '\0') {
        return -1;
    }
    for (in_i = 0; label[in_i] != '\0'; in_i++) {
        unsigned char ch = (unsigned char)label[in_i];
        if (ch == '.' || ch == '\\') {
            if (out_i + 2 >= out_len) {
                return -1;
            }
            out[out_i++] = '\\';
            out[out_i++] = (char)ch;
        } else {
            if (out_i + 1 >= out_len) {
                return -1;
            }
            out[out_i++] = (char)ch;
        }
    }
    out[out_i] = '\0';
    return 0;
}

int build_instance_fqdn(char *out, size_t out_len, const char *instance_name, const char *service_type) {
    char escaped_instance[MAX_NAME];
    int written;

    if (escape_dns_label(escaped_instance, sizeof(escaped_instance), instance_name) != 0) {
        return -1;
    }
    written = snprintf(out, out_len, "%s.%s", escaped_instance, service_type);
    if (written < 0 || (size_t)written >= out_len) {
        return -1;
    }

    return 0;
}

int build_host_fqdn(char *out, size_t out_len, const char *host_label) {
    char escaped_host[MAX_NAME];
    int written;

    if (escape_dns_label(escaped_host, sizeof(escaped_host), host_label) != 0) {
        return -1;
    }
    written = snprintf(out, out_len, "%s.local.", escaped_host);
    if (written < 0 || (size_t)written >= out_len) {
        return -1;
    }
    return 0;
}

TC_LOCAL void trim_ascii_whitespace(char *value) {
    char *start = value;
    char *end;

    while (*start != '\0' && isspace((unsigned char)*start)) {
        start++;
    }
    if (start != value) {
        memmove(value, start, strlen(start) + 1);
    }

    end = value + strlen(value);
    while (end > value && isspace((unsigned char)*(end - 1))) {
        end--;
    }
    *end = '\0';
}

TC_LOCAL int add_adisk_disk_config(struct config *cfg, const char *share_name, const char *disk_key,
                                 const char *adisk_uuid, const char *adisk_disk_advf) {
    struct adisk_disk *disk;
    char txt[256];

    if (cfg->adisk_disks.count >= ADISK_MAX_DISKS) {
        fprintf(stderr, "too many adisk disks; maximum is %d\n", ADISK_MAX_DISKS);
        return -1;
    }
    if (build_adisk_disk_txt(txt, sizeof(txt), disk_key, share_name, adisk_uuid, adisk_disk_advf) != 0) {
        return -1;
    }

    disk = &cfg->adisk_disks.disks[cfg->adisk_disks.count++];
    memset(disk, 0, sizeof(*disk));
    strncpy(disk->share_name, share_name, sizeof(disk->share_name) - 1);
    strncpy(disk->disk_key, disk_key, sizeof(disk->disk_key) - 1);
    strncpy(disk->disk_advf, adisk_disk_advf, sizeof(disk->disk_advf) - 1);
    strncpy(disk->uuid, adisk_uuid, sizeof(disk->uuid) - 1);
    return 0;
}

int parse_adisk_shares_file(struct config *cfg, const char *path) {
    FILE *fp;
    char line[1024];
    unsigned long line_no = 0;

    fp = fopen(path, "r");
    if (fp == NULL) {
        fprintf(stderr, "could not open adisk shares file %s: %s\n", path, strerror(errno));
        return -1;
    }

    while (fgets(line, sizeof(line), fp) != NULL) {
        char *fields[4];
        char *cursor = line;
        size_t i;
        line_no++;

        line[strcspn(line, "\r\n")] = '\0';
        trim_ascii_whitespace(line);
        if (line[0] == '\0' || line[0] == '#') {
            continue;
        }

        for (i = 0; i < 4; i++) {
            char *tab;
            fields[i] = cursor;
            tab = strchr(cursor, '\t');
            if (tab == NULL) {
                if (i != 3) {
                    fprintf(stderr, "adisk shares file %s line %lu must have four tab-separated fields\n", path, line_no);
                    fclose(fp);
                    return -1;
                }
                break;
            }
            if (i == 3) {
                fprintf(stderr, "adisk shares file %s line %lu has extra fields\n", path, line_no);
                fclose(fp);
                return -1;
            }
            *tab = '\0';
            cursor = tab + 1;
        }
        for (i = 0; i < 4; i++) {
            trim_ascii_whitespace(fields[i]);
        }
        if (add_adisk_disk_config(cfg, fields[0], fields[1], fields[2], fields[3]) != 0) {
            fprintf(stderr, "invalid adisk shares file %s line %lu\n", path, line_no);
            fclose(fp);
            return -1;
        }
    }

    fclose(fp);
    return 0;
}

TC_LOCAL int adisk_configured(const struct config *cfg) {
    return cfg->adisk_disks.count > 0;
}

int adisk_enabled(const struct config *cfg) {
    return !cfg->diskless && adisk_configured(cfg);
}

int smb_enabled(const struct config *cfg) {
    return !cfg->diskless;
}

int afp_enabled(const struct config *cfg) {
    return cfg->advertise_afp;
}

int is_airport_enabled(const struct config *cfg) {
    return cfg->airport_wama[0] != '\0' ||
           cfg->airport_rama[0] != '\0' ||
           cfg->airport_ram2[0] != '\0' ||
           cfg->airport_rast[0] != '\0' ||
           cfg->airport_rana[0] != '\0' ||
           cfg->airport_syfl[0] != '\0' ||
           cfg->airport_syap[0] != '\0' ||
           cfg->airport_syvs[0] != '\0' ||
           cfg->airport_srcv[0] != '\0' ||
           cfg->airport_bjsd[0] != '\0';
}

TC_LOCAL int is_airport_usb_printer_enabled(const struct config *cfg) {
    return cfg->riousbprint_instance_name[0] != '\0';
}

int is_riousbprint_enabled(const struct config *cfg) {
    return is_airport_usb_printer_enabled(cfg);
}

int is_pdl_datastream_enabled(const struct config *cfg) {
    return is_airport_usb_printer_enabled(cfg);
}

const struct config *mdns_config_for_scope(struct config *scoped,
                                                  const struct config *cfg,
                                                  enum mdns_service_scope scope) {
    if (scope == MDNS_SERVICE_SCOPE_LAN) {
        return cfg;
    }

    *scoped = *cfg;
    scoped->diskless = 1;
    scoped->advertise_afp = 0;
    scoped->device_model[0] = '\0';
    scoped->riousbprint_instance_name[0] = '\0';
    if (!is_airport_enabled(scoped)) {
        scoped->host_fqdn[0] = '\0';
    }
    return scoped;
}
