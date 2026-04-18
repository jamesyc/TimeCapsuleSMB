#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/wait.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define MDNS_PORT 5353
#define MDNS_GROUP "224.0.0.251"
#define BUF_SIZE 1500
#define MAX_NAME 256
#define MAX_LABEL 63
#define MAX_TXT_STRING 255
#define ANNOUNCE_INTERVAL 30
#define MODEL_TXT_PREFIX "model="
#define ADISK_DEFAULT_DISK_KEY "dk0"
#define ADISK_SYS_ADVF "0x1010"
#define ADISK_DISK_ADVF "0x1093"
#define ADISK_DISK_UUID_LEN 36
#define AIRPORT_SERVICE_TYPE "_airport._tcp.local."
#define AIRPORT_DEFAULT_PORT 5009
#define ADISK_SYS_TXT_PREFIX "sys=waMA="
#define ADISK_SYS_TXT_SUFFIX ",adVF=" ADISK_SYS_ADVF
#define ADISK_DISK_TXT_MID "=adVF=" ADISK_DISK_ADVF ",adVN="
#define ADISK_DISK_TXT_SUFFIX ",adVU="
#define SNAPSHOT_MAX_RECORDS 64
#define SNAPSHOT_MAX_TXT_ITEMS 16
#define SNAPSHOT_LINE_MAX 1024
#define SNAPSHOT_MAX_SERVICE_TYPES 64
#define SNAPSHOT_CAPTURE_TIMEOUT_SECONDS 60
#define SNAPSHOT_CAPTURE_RETRY_INTERVAL_SECONDS 5

#define DNS_TYPE_A 1
#define DNS_TYPE_PTR 12
#define DNS_TYPE_TXT 16
#define DNS_TYPE_AAAA 28
#define DNS_TYPE_SRV 33
#define DNS_TYPE_ANY 255
#define DNS_CLASS_IN 1
#define DNS_FLAG_QR 0x8000
#define DNS_FLAG_AA 0x0400

static volatile sig_atomic_t g_stop = 0;

struct config {
    char service_type[MAX_NAME];
    char instance_name[MAX_NAME];
    char host_label[MAX_LABEL + 1];
    char host_fqdn[MAX_NAME];
    char adisk_service_type[MAX_NAME];
    char adisk_share_name[MAX_NAME];
    char adisk_disk_key[MAX_LABEL + 1];
    char adisk_uuid[ADISK_DISK_UUID_LEN + 1];
    char adisk_sys_wama[18];
    char device_info_service_type[MAX_NAME];
    char device_model[MAX_NAME];
    char airport_service_type[MAX_NAME];
    char airport_wama[18];
    char airport_rama[18];
    char airport_ram2[18];
    char airport_rast[16];
    char airport_rana[16];
    char airport_syfl[32];
    char airport_syap[16];
    char airport_syvs[32];
    char airport_srcv[32];
    char airport_bjsd[16];
    uint32_t ipv4_addr;
    uint16_t port;
    uint16_t adisk_port;
    uint16_t airport_port;
    uint32_t ttl;
    char save_snapshot_path[MAX_NAME];
    char load_snapshot_path[MAX_NAME];
};

struct service_record {
    char service_type[MAX_NAME];
    char instance_name[MAX_NAME];
    char instance_fqdn[MAX_NAME];
    char host_label[MAX_LABEL + 1];
    char host_fqdn[MAX_NAME];
    uint16_t port;
    char txt[SNAPSHOT_MAX_TXT_ITEMS][MAX_TXT_STRING + 1];
    size_t txt_count;
};

struct service_record_set {
    struct service_record records[SNAPSHOT_MAX_RECORDS];
    size_t count;
};

struct service_type_set {
    char types[SNAPSHOT_MAX_SERVICE_TYPES][MAX_NAME];
    size_t count;
};

static int name_equals(const char *a, const char *b);
static int build_instance_fqdn(char *out, size_t out_len, const char *instance_name, const char *service_type);
static int open_mdns_socket(void);
static int add_rr_ptr(uint8_t *buf, size_t *off, size_t cap, const char *owner, const char *target, uint32_t ttl);
static int add_rr_txt_empty(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ttl);
static int add_rr_txt_strings(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ttl,
                              const char **strings, size_t string_count);
static int add_rr_srv(uint8_t *buf, size_t *off, size_t cap, const char *owner, const char *target, uint16_t port, uint32_t ttl);
static int add_rr_a(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ipv4_addr, uint32_t ttl);

struct dns_header {
    uint16_t id;
    uint16_t flags;
    uint16_t qdcount;
    uint16_t ancount;
    uint16_t nscount;
    uint16_t arcount;
};

static void on_signal(int signo) {
    (void)signo;
    g_stop = 1;
}

static int is_suppressed_snapshot_service_type(const char *service_type) {
    return name_equals(service_type, "_smb._tcp.local.") ||
           name_equals(service_type, "_adisk._tcp.local.") ||
           name_equals(service_type, "_device-info._tcp.local.") ||
           name_equals(service_type, "_afpovertcp._tcp.local.");
}

static void trim_trailing_dot(char *value) {
    size_t len = strlen(value);
    while (len > 0 && value[len - 1] == '.') {
        value[--len] = '\0';
    }
}

static int extract_instance_name(char *out, size_t out_len, const char *instance_fqdn, const char *service_type) {
    char fqdn_copy[MAX_NAME];
    char service_copy[MAX_NAME];
    size_t fqdn_len;
    size_t service_len;

    if (strlen(instance_fqdn) >= sizeof(fqdn_copy) || strlen(service_type) >= sizeof(service_copy)) {
        return -1;
    }
    strcpy(fqdn_copy, instance_fqdn);
    strcpy(service_copy, service_type);
    trim_trailing_dot(fqdn_copy);
    trim_trailing_dot(service_copy);

    fqdn_len = strlen(fqdn_copy);
    service_len = strlen(service_copy);
    if (fqdn_len <= service_len + 1) {
        return -1;
    }
    if (strncasecmp(fqdn_copy + fqdn_len - service_len, service_copy, service_len) != 0) {
        return -1;
    }
    if (fqdn_copy[fqdn_len - service_len - 1] != '.') {
        return -1;
    }
    if (fqdn_len - service_len - 1 >= out_len) {
        return -1;
    }
    memcpy(out, fqdn_copy, fqdn_len - service_len - 1);
    out[fqdn_len - service_len - 1] = '\0';
    trim_trailing_dot(out);
    return 0;
}

static int build_host_label_from_fqdn(char *out, size_t out_len, const char *host_fqdn) {
    size_t i;

    if (host_fqdn == NULL || host_fqdn[0] == '\0') {
        return -1;
    }
    for (i = 0; host_fqdn[i] != '\0'; i++) {
        if (host_fqdn[i] == '.') {
            break;
        }
        if (i + 1 >= out_len) {
            return -1;
        }
        out[i] = host_fqdn[i];
    }
    if (i == 0 || i >= out_len) {
        return -1;
    }
    out[i] = '\0';
    return 0;
}

static struct service_record *find_record(struct service_record_set *set, const char *service_type, const char *instance_name) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (name_equals(set->records[i].service_type, service_type) &&
            strcmp(set->records[i].instance_name, instance_name) == 0) {
            return &set->records[i];
        }
    }
    return NULL;
}

static struct service_record *find_or_add_record(struct service_record_set *set, const char *service_type, const char *instance_name) {
    struct service_record *record;

    record = find_record(set, service_type, instance_name);
    if (record != NULL) {
        return record;
    }
    if (set->count >= SNAPSHOT_MAX_RECORDS) {
        return NULL;
    }
    record = &set->records[set->count++];
    memset(record, 0, sizeof(*record));
    strncpy(record->service_type, service_type, sizeof(record->service_type) - 1);
    strncpy(record->instance_name, instance_name, sizeof(record->instance_name) - 1);
    if (build_instance_fqdn(record->instance_fqdn, sizeof(record->instance_fqdn), record->instance_name, record->service_type) != 0) {
        set->count--;
        return NULL;
    }
    return record;
}

static int has_transport_suffix(const char *service_type) {
    return strstr(service_type, "._tcp.local.") != NULL ||
           strstr(service_type, "._udp.local.") != NULL ||
           strstr(service_type, "._tcp.local") != NULL ||
           strstr(service_type, "._udp.local") != NULL;
}

static int find_service_type_for_instance_fqdn(char *out, size_t out_len, const char *instance_fqdn) {
    const char *service_start = strstr(instance_fqdn, "._");

    if (service_start == NULL) {
        return -1;
    }
    if (!has_transport_suffix(service_start + 1)) {
        return -1;
    }
    if (strlen(service_start + 1) >= out_len) {
        return -1;
    }
    strncpy(out, service_start + 1, out_len - 1);
    out[out_len - 1] = '\0';
    return 0;
}

static int service_type_set_contains(const struct service_type_set *set, const char *service_type) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (name_equals(set->types[i], service_type)) {
            return 1;
        }
    }
    return 0;
}

static int service_type_set_add(struct service_type_set *set, const char *service_type) {
    if (!has_transport_suffix(service_type)) {
        return 0;
    }
    if (service_type_set_contains(set, service_type)) {
        return 0;
    }
    if (set->count >= SNAPSHOT_MAX_SERVICE_TYPES) {
        return -1;
    }
    strncpy(set->types[set->count], service_type, sizeof(set->types[set->count]) - 1);
    set->types[set->count][sizeof(set->types[set->count]) - 1] = '\0';
    set->count++;
    return 0;
}

static void usage(const char *prog) {
    fprintf(stderr,
            "Usage: %s --instance <name> --host <label> --ipv4 <address> [options]\n"
            "Options:\n"
            "  --save-snapshot <path> Capture Apple mDNS records into a snapshot file\n"
            "  --load-snapshot <path> Kill Apple mDNSResponder and replay snapshot records\n"
            "  --service <type>   Service type (default: _smb._tcp.local.)\n"
            "  --adisk-share <n>  Also advertise _adisk._tcp for Time Machine\n"
            "  --adisk-disk-key <k> Disk key for _adisk TXT (default: dk0)\n"
            "  --adisk-uuid <u>   Stable UUID for _adisk TXT\n"
            "  --adisk-sys-wama <m> MAC address for _adisk sys TXT\n"
            "  --device-model <m> Also advertise _device-info._tcp with model=<m>\n"
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
            "  --airport-port <p> _airport._tcp service port (default: 5009)\n"
            "  --port <port>      Service port (default: 445)\n"
            "  --ttl <seconds>    Record TTL (default: 120)\n",
            prog);
}

static int append_bytes(uint8_t *buf, size_t *off, size_t cap, const void *src, size_t len) {
    if (*off + len > cap) {
        return -1;
    }
    memcpy(buf + *off, src, len);
    *off += len;
    return 0;
}

static int append_u16(uint8_t *buf, size_t *off, size_t cap, uint16_t value) {
    uint16_t net = htons(value);
    return append_bytes(buf, off, cap, &net, sizeof(net));
}

static int append_u32(uint8_t *buf, size_t *off, size_t cap, uint32_t value) {
    uint32_t net = htonl(value);
    return append_bytes(buf, off, cap, &net, sizeof(net));
}

static int validate_single_dns_label(const char *value, const char *field_name) {
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

    if (strchr(value, '.') != NULL) {
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

static int build_instance_fqdn(char *out, size_t out_len, const char *instance_name, const char *service_type) {
    int written;

    written = snprintf(out, out_len, "%s.%s", instance_name, service_type);
    if (written < 0 || (size_t)written >= out_len) {
        return -1;
    }

    return 0;
}

static int build_model_txt(char *out, size_t out_len, const char *device_model) {
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

static int build_adisk_system_txt(char *out, size_t out_len, const char *wama) {
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

static int validate_mac_addr(const char *value, const char *field_name) {
    char scratch[128];

    if (build_adisk_system_txt(scratch, sizeof(scratch), value) != 0) {
        fprintf(stderr, "%s must be a MAC address\n", field_name);
        return -1;
    }
    return 0;
}

static int build_adisk_disk_txt(char *out, size_t out_len, const char *disk_key, const char *share_name, const char *adisk_uuid) {
    int written;
    const unsigned char *p;

    if (disk_key == NULL || disk_key[0] == '\0' || share_name == NULL || share_name[0] == '\0' ||
        adisk_uuid == NULL || adisk_uuid[0] == '\0') {
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

    if (strlen(disk_key) + strlen(ADISK_DISK_TXT_MID) + strlen(share_name) + strlen(ADISK_DISK_TXT_SUFFIX) + strlen(adisk_uuid) > MAX_TXT_STRING) {
        fprintf(stderr, "adisk share name must be %d bytes or fewer\n",
                MAX_TXT_STRING - (int)strlen(disk_key) - (int)strlen(ADISK_DISK_TXT_MID) - (int)strlen(ADISK_DISK_TXT_SUFFIX) - (int)strlen(adisk_uuid));
        return -1;
    }

    written = snprintf(out, out_len, "%s" ADISK_DISK_TXT_MID "%s" ADISK_DISK_TXT_SUFFIX "%s", disk_key, share_name, adisk_uuid);
    if (written < 0 || (size_t)written >= out_len) {
        return -1;
    }

    return 0;
}

static int is_airport_enabled(const struct config *cfg) {
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

static int validate_airport_ascii_field(const char *value, const char *field_name) {
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

static int build_airport_txt(char *out, size_t out_len, const struct config *cfg) {
    int written;
    size_t off = 0;
    int appended = 0;

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
    if ((cfg->airport_wama[0] != '\0' && validate_mac_addr(cfg->airport_wama, "airport waMA") != 0) ||
        (cfg->airport_rama[0] != '\0' && validate_mac_addr(cfg->airport_rama, "airport raMA") != 0) ||
        (cfg->airport_ram2[0] != '\0' && validate_mac_addr(cfg->airport_ram2, "airport raM2") != 0) ||
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

    APPEND_AIRPORT_FIELD("waMA=%s", cfg->airport_wama);
    APPEND_AIRPORT_FIELD("raMA=%s", cfg->airport_rama);
    APPEND_AIRPORT_FIELD("raM2=%s", cfg->airport_ram2);
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

static int validate_dns_name(const char *value, const char *field_name) {
    const unsigned char *p;
    size_t label_len = 0;
    size_t total_len;

    if (value == NULL || value[0] == '\0') {
        fprintf(stderr, "%s must not be empty\n", field_name);
        return -1;
    }

    total_len = strlen(value);
    if (total_len >= MAX_NAME) {
        fprintf(stderr, "%s must be %d bytes or fewer\n", field_name, MAX_NAME - 1);
        return -1;
    }

    for (p = (const unsigned char *)value; *p != '\0'; p++) {
        if (*p < 0x20 || *p == 0x7f) {
            fprintf(stderr, "%s contains an invalid control character\n", field_name);
            return -1;
        }
        if (*p == '.') {
            if (label_len == 0) {
                if (*(p + 1) == '\0' && p != (const unsigned char *)value) {
                    return 0;
                }
                fprintf(stderr, "%s contains an empty label\n", field_name);
                return -1;
            }
            if (label_len > MAX_LABEL) {
                fprintf(stderr, "%s contains a label longer than %d bytes\n", field_name, MAX_LABEL);
                return -1;
            }
            label_len = 0;
            continue;
        }
        label_len++;
        if (label_len > MAX_LABEL) {
            fprintf(stderr, "%s contains a label longer than %d bytes\n", field_name, MAX_LABEL);
            return -1;
        }
    }

    if (label_len == 0) {
        if (total_len > 1 && value[total_len - 1] == '.') {
            return 0;
        }
        fprintf(stderr, "%s contains an empty label\n", field_name);
        return -1;
    }

    return 0;
}

static int encode_name(uint8_t *buf, size_t *off, size_t cap, const char *name) {
    char temp[MAX_NAME];
    char *token;
    char *saveptr = NULL;

    if (strlen(name) >= sizeof(temp)) {
        return -1;
    }
    strcpy(temp, name);

    token = strtok_r(temp, ".", &saveptr);
    while (token != NULL) {
        size_t len = strlen(token);
        uint8_t label_len;
        if (len == 0 || len > 63) {
            return -1;
        }
        label_len = (uint8_t)len;
        if (append_bytes(buf, off, cap, &label_len, 1) != 0) {
            return -1;
        }
        if (append_bytes(buf, off, cap, token, len) != 0) {
            return -1;
        }
        token = strtok_r(NULL, ".", &saveptr);
    }

    return append_bytes(buf, off, cap, "\0", 1);
}

static int decode_name(const uint8_t *packet, size_t packet_len, size_t *cursor, char *out, size_t out_len) {
    size_t pos = *cursor;
    size_t out_pos = 0;
    int jumped = 0;
    size_t jump_count = 0;
    size_t next_cursor = pos;

    while (pos < packet_len) {
        uint8_t len = packet[pos];

        if (len == 0) {
            if (!jumped) {
                next_cursor = pos + 1;
            }
            if (out_pos == 0) {
                if (out_len < 2) {
                    return -1;
                }
                out[out_pos++] = '.';
            }
            out[out_pos] = '\0';
            *cursor = next_cursor;
            return 0;
        }

        if ((len & 0xC0) == 0xC0) {
            uint16_t ptr;
            if (pos + 1 >= packet_len) {
                return -1;
            }
            ptr = (uint16_t)(((len & 0x3F) << 8) | packet[pos + 1]);
            if (ptr >= packet_len || jump_count++ > 16) {
                return -1;
            }
            if (!jumped) {
                next_cursor = pos + 2;
            }
            pos = ptr;
            jumped = 1;
            continue;
        }

        if (len > 63 || pos + 1 + len > packet_len) {
            return -1;
        }

        if (out_pos != 0) {
            if (out_pos + 1 >= out_len) {
                return -1;
            }
            out[out_pos++] = '.';
        }
        if (out_pos + len >= out_len) {
            return -1;
        }
        memcpy(out + out_pos, packet + pos + 1, len);
        out_pos += len;
        pos += 1 + len;
        if (!jumped) {
            next_cursor = pos;
        }
    }

    return -1;
}

static int name_equals(const char *a, const char *b) {
    size_t a_len = strlen(a);
    size_t b_len = strlen(b);
    while (a_len > 0 && a[a_len - 1] == '.') {
        a_len--;
    }
    while (b_len > 0 && b[b_len - 1] == '.') {
        b_len--;
    }
    return a_len == b_len && strncasecmp(a, b, a_len) == 0;
}

static int add_service_record_answers(uint8_t *buf, size_t *off, size_t cap, const struct service_record *record, uint32_t ttl, int *answers) {
    const char *txts[SNAPSHOT_MAX_TXT_ITEMS];
    size_t i;

    for (i = 0; i < record->txt_count; i++) {
        txts[i] = record->txt[i];
    }

    if (add_rr_ptr(buf, off, cap, record->service_type, record->instance_fqdn, ttl) != 0 ||
        add_rr_srv(buf, off, cap, record->instance_fqdn, record->host_fqdn, record->port, ttl) != 0) {
        return -1;
    }
    *answers += 2;

    if (record->txt_count > 0) {
        if (add_rr_txt_strings(buf, off, cap, record->instance_fqdn, ttl, txts, record->txt_count) != 0) {
            return -1;
        }
        *answers += 1;
    } else {
        if (add_rr_txt_empty(buf, off, cap, record->instance_fqdn, ttl) != 0) {
            return -1;
        }
        *answers += 1;
    }

    return 0;
}

static int add_snapshot_host_a_record(uint8_t *buf, size_t *off, size_t cap, const struct service_record *record,
                                      const struct config *cfg, int *answers) {
    if (record->host_fqdn[0] == '\0') {
        return 0;
    }
    if (add_rr_a(buf, off, cap, record->host_fqdn, cfg->ipv4_addr, cfg->ttl) != 0) {
        return -1;
    }
    *answers += 1;
    return 0;
}

static int add_rr_ptr(uint8_t *buf, size_t *off, size_t cap, const char *owner, const char *target, uint32_t ttl) {
    size_t rdlength_pos;
    size_t rdata_start;
    if (encode_name(buf, off, cap, owner) != 0 ||
        append_u16(buf, off, cap, DNS_TYPE_PTR) != 0 ||
        append_u16(buf, off, cap, DNS_CLASS_IN) != 0 ||
        append_u32(buf, off, cap, ttl) != 0) {
        return -1;
    }
    rdlength_pos = *off;
    if (append_u16(buf, off, cap, 0) != 0) {
        return -1;
    }
    rdata_start = *off;
    if (encode_name(buf, off, cap, target) != 0) {
        return -1;
    }
    {
        uint16_t rdlength = htons((uint16_t)(*off - rdata_start));
        memcpy(buf + rdlength_pos, &rdlength, sizeof(rdlength));
    }
    return 0;
}

static int add_rr_txt_empty(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ttl) {
    static const uint8_t empty_txt[] = {0x00};
    if (encode_name(buf, off, cap, owner) != 0 ||
        append_u16(buf, off, cap, DNS_TYPE_TXT) != 0 ||
        append_u16(buf, off, cap, DNS_CLASS_IN) != 0 ||
        append_u32(buf, off, cap, ttl) != 0 ||
        append_u16(buf, off, cap, (uint16_t)sizeof(empty_txt)) != 0 ||
        append_bytes(buf, off, cap, empty_txt, sizeof(empty_txt)) != 0) {
        return -1;
    }
    return 0;
}

static int add_rr_txt_strings(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ttl,
                              const char **strings, size_t string_count) {
    size_t rdlength_pos;
    size_t rdata_start;
    size_t i;

    if (encode_name(buf, off, cap, owner) != 0 ||
        append_u16(buf, off, cap, DNS_TYPE_TXT) != 0 ||
        append_u16(buf, off, cap, DNS_CLASS_IN) != 0 ||
        append_u32(buf, off, cap, ttl) != 0) {
        return -1;
    }

    rdlength_pos = *off;
    if (append_u16(buf, off, cap, 0) != 0) {
        return -1;
    }
    rdata_start = *off;

    for (i = 0; i < string_count; i++) {
        uint8_t len;
        size_t slen = strlen(strings[i]);
        if (slen > 255) {
            return -1;
        }
        len = (uint8_t)slen;
        if (append_bytes(buf, off, cap, &len, 1) != 0 ||
            append_bytes(buf, off, cap, strings[i], slen) != 0) {
            return -1;
        }
    }

    {
        uint16_t rdlength = htons((uint16_t)(*off - rdata_start));
        memcpy(buf + rdlength_pos, &rdlength, sizeof(rdlength));
    }
    return 0;
}

static int add_rr_srv(uint8_t *buf, size_t *off, size_t cap, const char *owner, const char *target, uint16_t port, uint32_t ttl) {
    size_t rdlength_pos;
    size_t rdata_start;
    if (encode_name(buf, off, cap, owner) != 0 ||
        append_u16(buf, off, cap, DNS_TYPE_SRV) != 0 ||
        append_u16(buf, off, cap, DNS_CLASS_IN) != 0 ||
        append_u32(buf, off, cap, ttl) != 0) {
        return -1;
    }
    rdlength_pos = *off;
    if (append_u16(buf, off, cap, 0) != 0) {
        return -1;
    }
    rdata_start = *off;
    if (append_u16(buf, off, cap, 0) != 0 ||
        append_u16(buf, off, cap, 0) != 0 ||
        append_u16(buf, off, cap, port) != 0 ||
        encode_name(buf, off, cap, target) != 0) {
        return -1;
    }
    {
        uint16_t rdlength = htons((uint16_t)(*off - rdata_start));
        memcpy(buf + rdlength_pos, &rdlength, sizeof(rdlength));
    }
    return 0;
}

static int add_rr_a(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ipv4_addr, uint32_t ttl) {
    if (encode_name(buf, off, cap, owner) != 0 ||
        append_u16(buf, off, cap, DNS_TYPE_A) != 0 ||
        append_u16(buf, off, cap, DNS_CLASS_IN) != 0 ||
        append_u32(buf, off, cap, ttl) != 0 ||
        append_u16(buf, off, cap, 4) != 0 ||
        append_bytes(buf, off, cap, &ipv4_addr, 4) != 0) {
        return -1;
    }
    return 0;
}

static int hex_encode_bytes(char *out, size_t out_len, const char *bytes) {
    static const char hex[] = "0123456789abcdef";
    size_t i;
    size_t src_len;

    if (bytes == NULL) {
        return -1;
    }
    src_len = strlen(bytes);
    if ((src_len * 2) + 1 > out_len) {
        return -1;
    }
    for (i = 0; i < src_len; i++) {
        unsigned char ch = (unsigned char)bytes[i];
        out[i * 2] = hex[ch >> 4];
        out[i * 2 + 1] = hex[ch & 0x0f];
    }
    out[src_len * 2] = '\0';
    return 0;
}

static int hex_decode_bytes(char *out, size_t out_len, const char *hex) {
    size_t i;
    size_t hex_len;

    if (hex == NULL) {
        return -1;
    }
    hex_len = strlen(hex);
    if ((hex_len % 2) != 0 || (hex_len / 2) + 1 > out_len) {
        return -1;
    }
    for (i = 0; i < hex_len; i += 2) {
        char byte_str[3];
        char *endptr = NULL;
        long value;

        byte_str[0] = hex[i];
        byte_str[1] = hex[i + 1];
        byte_str[2] = '\0';
        value = strtol(byte_str, &endptr, 16);
        if (endptr == NULL || *endptr != '\0' || value < 0 || value > 255) {
            return -1;
        }
        out[i / 2] = (char)value;
    }
    out[hex_len / 2] = '\0';
    return 0;
}

static int write_snapshot_file_atomic(const char *path, const struct service_record_set *set) {
    char tmp_path[MAX_NAME * 2];
    char host_hex[(MAX_NAME * 2) + 1];
    FILE *fp;
    size_t i;
    size_t j;

    if (snprintf(tmp_path, sizeof(tmp_path), "%s.tmp", path) >= (int)sizeof(tmp_path)) {
        return -1;
    }

    fp = fopen(tmp_path, "w");
    if (fp == NULL) {
        return -1;
    }

    for (i = 0; i < set->count; i++) {
        const struct service_record *record = &set->records[i];
        if (hex_encode_bytes(host_hex, sizeof(host_hex), record->host_fqdn) != 0) {
            fclose(fp);
            unlink(tmp_path);
            return -1;
        }
        if (fprintf(fp, "BEGIN\nTYPE=%s\nINSTANCE=%s\nHOST_HEX=%s\nPORT=%u\n",
                    record->service_type,
                    record->instance_name,
                    host_hex,
                    (unsigned)record->port) < 0) {
            fclose(fp);
            unlink(tmp_path);
            return -1;
        }
        for (j = 0; j < record->txt_count; j++) {
            if (fprintf(fp, "TXT=%s\n", record->txt[j]) < 0) {
                fclose(fp);
                unlink(tmp_path);
                return -1;
            }
        }
        if (fprintf(fp, "END\n") < 0) {
            fclose(fp);
            unlink(tmp_path);
            return -1;
        }
    }

    if (fclose(fp) != 0) {
        unlink(tmp_path);
        return -1;
    }
    if (rename(tmp_path, path) != 0) {
        unlink(tmp_path);
        return -1;
    }
    return 0;
}

static int load_snapshot_file(const char *path, struct service_record_set *out) {
    FILE *fp;
    char line[SNAPSHOT_LINE_MAX];
    struct service_record current;
    int in_record = 0;

    memset(out, 0, sizeof(*out));
    fp = fopen(path, "r");
    if (fp == NULL) {
        return -1;
    }

    memset(&current, 0, sizeof(current));
    while (fgets(line, sizeof(line), fp) != NULL) {
        size_t len = strlen(line);
        if (len > 0 && line[len - 1] == '\n') {
            line[len - 1] = '\0';
        }

        if (strcmp(line, "BEGIN") == 0) {
            memset(&current, 0, sizeof(current));
            in_record = 1;
            continue;
        }
        if (strcmp(line, "END") == 0) {
            if (!in_record || current.service_type[0] == '\0' || current.instance_name[0] == '\0' ||
                current.host_fqdn[0] == '\0') {
                fclose(fp);
                return -1;
            }
            if (out->count >= SNAPSHOT_MAX_RECORDS ||
                build_instance_fqdn(current.instance_fqdn, sizeof(current.instance_fqdn), current.instance_name, current.service_type) != 0 ||
                build_host_label_from_fqdn(current.host_label, sizeof(current.host_label), current.host_fqdn) != 0) {
                fclose(fp);
                return -1;
            }
            out->records[out->count++] = current;
            memset(&current, 0, sizeof(current));
            in_record = 0;
            continue;
        }
        if (!in_record) {
            continue;
        }
        if (strncmp(line, "TYPE=", 5) == 0) {
            strncpy(current.service_type, line + 5, sizeof(current.service_type) - 1);
        } else if (strncmp(line, "INSTANCE=", 9) == 0) {
            strncpy(current.instance_name, line + 9, sizeof(current.instance_name) - 1);
        } else if (strncmp(line, "HOST_HEX=", 9) == 0) {
            if (hex_decode_bytes(current.host_fqdn, sizeof(current.host_fqdn), line + 9) != 0) {
                fclose(fp);
                return -1;
            }
        } else if (strncmp(line, "HOST=", 5) == 0) {
            strncpy(current.host_label, line + 5, sizeof(current.host_label) - 1);
            if (snprintf(current.host_fqdn, sizeof(current.host_fqdn), "%s.local.", current.host_label) >= (int)sizeof(current.host_fqdn)) {
                fclose(fp);
                return -1;
            }
        } else if (strncmp(line, "PORT=", 5) == 0) {
            current.port = (uint16_t)atoi(line + 5);
        } else if (strncmp(line, "TXT=", 4) == 0) {
            if (current.txt_count >= SNAPSHOT_MAX_TXT_ITEMS) {
                fclose(fp);
                return -1;
            }
            strncpy(current.txt[current.txt_count++], line + 4, MAX_TXT_STRING);
        }
    }

    fclose(fp);
    return out->count > 0 ? 0 : -1;
}

static int send_query_question(int sockfd, const struct sockaddr_in *dest, const char *qname, uint16_t qtype) {
    uint8_t packet[BUF_SIZE];
    struct dns_header hdr;
    size_t off = sizeof(hdr);

    memset(&hdr, 0, sizeof(hdr));
    hdr.qdcount = htons(1);
    memcpy(packet, &hdr, sizeof(hdr));
    if (encode_name(packet, &off, sizeof(packet), qname) != 0 ||
        append_u16(packet, &off, sizeof(packet), qtype) != 0 ||
        append_u16(packet, &off, sizeof(packet), DNS_CLASS_IN) != 0) {
        return -1;
    }
    return sendto(sockfd, packet, off, 0, (const struct sockaddr *)dest, sizeof(*dest)) >= 0 ? 0 : -1;
}

static void parse_txt_rdata(struct service_record *record, const uint8_t *rdata, size_t rdlength) {
    size_t pos = 0;
    record->txt_count = 0;
    while (pos < rdlength && record->txt_count < SNAPSHOT_MAX_TXT_ITEMS) {
        uint8_t len = rdata[pos++];
        if (pos + len > rdlength) {
            return;
        }
        memcpy(record->txt[record->txt_count], rdata + pos, len);
        record->txt[record->txt_count][len] = '\0';
        record->txt_count++;
        pos += len;
    }
}

static int parse_snapshot_rrs(const uint8_t *packet, size_t packet_len, struct service_record_set *set,
                              struct service_type_set *service_types) {
    struct dns_header hdr;
    size_t cursor = sizeof(hdr);
    uint16_t sections[3];
    size_t s;
    uint16_t i;

    if (packet_len < sizeof(hdr)) {
        return -1;
    }
    memcpy(&hdr, packet, sizeof(hdr));
    sections[0] = ntohs(hdr.qdcount);
    sections[1] = ntohs(hdr.ancount);
    sections[2] = (uint16_t)(ntohs(hdr.nscount) + ntohs(hdr.arcount));

    for (i = 0; i < sections[0]; i++) {
        char qname[MAX_NAME];
        if (decode_name(packet, packet_len, &cursor, qname, sizeof(qname)) != 0 || cursor + 4 > packet_len) {
            return -1;
        }
        cursor += 4;
    }

    for (s = 1; s < 3; s++) {
        for (i = 0; i < sections[s]; i++) {
            char owner[MAX_NAME];
            uint16_t type;
            uint16_t rdlength;
            size_t rdata_cursor;

            if (decode_name(packet, packet_len, &cursor, owner, sizeof(owner)) != 0 || cursor + 10 > packet_len) {
                return -1;
            }
            memcpy(&type, packet + cursor, 2);
            memcpy(&rdlength, packet + cursor + 8, 2);
            cursor += 10;
            rdlength = ntohs(rdlength);
            if (cursor + rdlength > packet_len) {
                return -1;
            }
            rdata_cursor = cursor;

            type = ntohs(type);

            if (type == DNS_TYPE_PTR && name_equals(owner, "_services._dns-sd._udp.local.")) {
                char target[MAX_NAME];
                if (decode_name(packet, packet_len, &rdata_cursor, target, sizeof(target)) == 0) {
                    (void)service_type_set_add(service_types, target);
                }
            } else if (type == DNS_TYPE_PTR && has_transport_suffix(owner)) {
                char target[MAX_NAME];
                char service_type[MAX_NAME];
                char instance_name[MAX_NAME];
                struct service_record *record;

                if (decode_name(packet, packet_len, &rdata_cursor, target, sizeof(target)) == 0 &&
                    find_service_type_for_instance_fqdn(service_type, sizeof(service_type), target) == 0 &&
                    extract_instance_name(instance_name, sizeof(instance_name), target, service_type) == 0) {
                    (void)service_type_set_add(service_types, service_type);
                    record = find_or_add_record(set, service_type, instance_name);
                    if (record != NULL) {
                        strncpy(record->instance_fqdn, target, sizeof(record->instance_fqdn) - 1);
                    }
                }
            } else if (type == DNS_TYPE_SRV) {
                char service_type[MAX_NAME];
                if (find_service_type_for_instance_fqdn(service_type, sizeof(service_type), owner) == 0 && rdlength >= 6) {
                    char instance_name[MAX_NAME];
                    char host_fqdn[MAX_NAME];
                    struct service_record *record;
                    uint16_t port;
                    size_t tmp_cursor = rdata_cursor + 6;
                    memcpy(&port, packet + rdata_cursor + 4, 2);
                    port = ntohs(port);
                    if (extract_instance_name(instance_name, sizeof(instance_name), owner, service_type) == 0 &&
                        decode_name(packet, packet_len, &tmp_cursor, host_fqdn, sizeof(host_fqdn)) == 0) {
                        record = find_or_add_record(set, service_type, instance_name);
                        if (record != NULL) {
                            record->port = port;
                            strncpy(record->host_fqdn, host_fqdn, sizeof(record->host_fqdn) - 1);
                            trim_trailing_dot(record->host_fqdn);
                            strncat(record->host_fqdn, ".", sizeof(record->host_fqdn) - strlen(record->host_fqdn) - 1);
                            (void)build_host_label_from_fqdn(record->host_label, sizeof(record->host_label), host_fqdn);
                        }
                    }
                }
            } else if (type == DNS_TYPE_TXT) {
                char service_type[MAX_NAME];
                if (find_service_type_for_instance_fqdn(service_type, sizeof(service_type), owner) == 0) {
                    char instance_name[MAX_NAME];
                    struct service_record *record;
                    if (extract_instance_name(instance_name, sizeof(instance_name), owner, service_type) == 0) {
                        record = find_or_add_record(set, service_type, instance_name);
                        if (record != NULL) {
                            parse_txt_rdata(record, packet + rdata_cursor, rdlength);
                        }
                    }
                }
            }

            cursor += rdlength;
        }
    }

    return 0;
}

static int open_capture_socket(void) {
    return open_mdns_socket();
}

static int collect_mdns_responses(int sockfd, int seconds, struct service_record_set *set,
                                  struct service_type_set *service_types) {
    time_t deadline = time(NULL) + seconds;

    while (time(NULL) < deadline) {
        fd_set rfds;
        struct timeval tv;
        uint8_t packet[BUF_SIZE];
        ssize_t nread;

        FD_ZERO(&rfds);
        FD_SET(sockfd, &rfds);
        tv.tv_sec = 1;
        tv.tv_usec = 0;
        if (select(sockfd + 1, &rfds, NULL, NULL, &tv) <= 0) {
            continue;
        }
        nread = recvfrom(sockfd, packet, sizeof(packet), 0, NULL, NULL);
        if (nread > 0) {
            (void)parse_snapshot_rrs(packet, (size_t)nread, set, service_types);
        }
    }

    return 0;
}

static int capture_apple_snapshot(const struct config *cfg, struct service_record_set *out) {
    int sockfd;
    struct sockaddr_in mdns_dest;
    size_t i;
    struct service_record_set captured;
    struct service_record_set filtered;
    struct service_type_set service_types;
    const char *preferred_host = NULL;

    memset(&captured, 0, sizeof(captured));
    memset(&filtered, 0, sizeof(filtered));
    memset(&service_types, 0, sizeof(service_types));
    sockfd = open_capture_socket();
    if (sockfd < 0) {
        return -1;
    }

    memset(&mdns_dest, 0, sizeof(mdns_dest));
    mdns_dest.sin_family = AF_INET;
    mdns_dest.sin_port = htons(MDNS_PORT);
    mdns_dest.sin_addr.s_addr = inet_addr(MDNS_GROUP);

    (void)send_query_question(sockfd, &mdns_dest, "_services._dns-sd._udp.local.", DNS_TYPE_PTR);
    (void)collect_mdns_responses(sockfd, 5, &captured, &service_types);

    for (i = 0; i < service_types.count; i++) {
        (void)send_query_question(sockfd, &mdns_dest, service_types.types[i], DNS_TYPE_PTR);
    }
    (void)collect_mdns_responses(sockfd, 5, &captured, &service_types);

    for (i = 0; i < captured.count; i++) {
        (void)send_query_question(sockfd, &mdns_dest, captured.records[i].instance_fqdn, DNS_TYPE_SRV);
        (void)send_query_question(sockfd, &mdns_dest, captured.records[i].instance_fqdn, DNS_TYPE_TXT);
    }
    (void)collect_mdns_responses(sockfd, 5, &captured, &service_types);
    close(sockfd);

    for (i = 0; i < captured.count; i++) {
        const struct service_record *record = &captured.records[i];
        size_t j;
        if (name_equals(record->service_type, AIRPORT_SERVICE_TYPE)) {
            if (cfg->airport_wama[0] != '\0') {
                for (j = 0; j < record->txt_count; j++) {
                    if (strncasecmp(record->txt[j], "waMA=", 5) == 0 &&
                        strcasecmp(record->txt[j] + 5, cfg->airport_wama) == 0) {
                        preferred_host = record->host_label;
                        break;
                    }
                }
            }
            if (preferred_host == NULL) {
                preferred_host = record->host_label;
            }
        }
    }

    if (preferred_host != NULL) {
        for (i = 0; i < captured.count; i++) {
            const struct service_record *record = &captured.records[i];
            if (record->host_label[0] == '\0' || strcmp(record->host_label, preferred_host) != 0) {
                continue;
            }
            if (filtered.count >= SNAPSHOT_MAX_RECORDS) {
                break;
            }
            filtered.records[filtered.count++] = *record;
        }
        if (filtered.count > 0) {
            *out = filtered;
            return 0;
        }
    }

    *out = captured;
    return out->count > 0 ? 0 : -1;
}

static int capture_apple_snapshot_with_retry(const struct config *cfg, struct service_record_set *out) {
    time_t deadline = time(NULL) + SNAPSHOT_CAPTURE_TIMEOUT_SECONDS;

    do {
        if (capture_apple_snapshot(cfg, out) == 0) {
            return 0;
        }
        if (time(NULL) >= deadline) {
            break;
        }
        sleep(SNAPSHOT_CAPTURE_RETRY_INTERVAL_SECONDS);
    } while (time(NULL) < deadline);

    return -1;
}

static void gracefully_kill_mdnsresponder(void) {
    int attempt;
    (void)system("/usr/bin/pkill mDNSResponder >/dev/null 2>&1 || true");
    for (attempt = 0; attempt < 5; attempt++) {
        FILE *ps = popen("/bin/ps ax -o stat= -o ucomm= 2>/dev/null", "r");
        char line[256];
        int alive = 0;

        if (ps == NULL) {
            break;
        }
        while (fgets(line, sizeof(line), ps) != NULL) {
            char stat[32];
            char ucomm[128];
            if (sscanf(line, "%31s %127s", stat, ucomm) == 2 && strcmp(ucomm, "mDNSResponder") == 0) {
                if (stat[0] != 'Z') {
                    alive = 1;
                    break;
                }
            }
        }
        pclose(ps);
        if (!alive) {
            break;
        }
        sleep(1);
    }
}

static int add_adisk_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg, int *answers) {
    char instance_fqdn[MAX_NAME];
    char txt1[128];
    char txt2[256];
    const char *txts[2];

    if (cfg->adisk_share_name[0] == '\0' || cfg->adisk_uuid[0] == '\0' || cfg->adisk_sys_wama[0] == '\0') {
        return 0;
    }

    if (build_instance_fqdn(instance_fqdn, sizeof(instance_fqdn), cfg->instance_name, cfg->adisk_service_type) != 0) {
        return -1;
    }
    if (build_adisk_system_txt(txt1, sizeof(txt1), cfg->adisk_sys_wama) != 0) {
        return -1;
    }
    if (build_adisk_disk_txt(txt2, sizeof(txt2), cfg->adisk_disk_key, cfg->adisk_share_name, cfg->adisk_uuid) != 0) {
        return -1;
    }
    txts[0] = txt1;
    txts[1] = txt2;

    if (add_rr_ptr(buf, off, cap, cfg->adisk_service_type, instance_fqdn, cfg->ttl) != 0 ||
        add_rr_srv(buf, off, cap, instance_fqdn, cfg->host_fqdn, cfg->adisk_port, cfg->ttl) != 0 ||
        add_rr_txt_strings(buf, off, cap, instance_fqdn, cfg->ttl, txts, 2) != 0) {
        return -1;
    }

    *answers += 3;
    return 0;
}

static int add_device_info_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg, int *answers) {
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

    if (add_rr_ptr(buf, off, cap, cfg->device_info_service_type, instance_fqdn, cfg->ttl) != 0 ||
        add_rr_srv(buf, off, cap, instance_fqdn, cfg->host_fqdn, 0, cfg->ttl) != 0 ||
        add_rr_txt_strings(buf, off, cap, instance_fqdn, cfg->ttl, txts, 1) != 0) {
        return -1;
    }

    *answers += 3;
    return 0;
}

static int add_airport_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg, int *answers) {
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

    if (add_rr_ptr(buf, off, cap, cfg->airport_service_type, instance_fqdn, cfg->ttl) != 0 ||
        add_rr_srv(buf, off, cap, instance_fqdn, cfg->host_fqdn, cfg->airport_port, cfg->ttl) != 0 ||
        add_rr_txt_strings(buf, off, cap, instance_fqdn, cfg->ttl, txts, 1) != 0) {
        return -1;
    }

    *answers += 3;
    return 0;
}

static int send_announcement(int sockfd, const struct sockaddr_in *dest, const struct config *cfg,
                             const struct service_record_set *snapshot_records, int use_snapshot_records) {
    uint8_t buf[BUF_SIZE];
    size_t off = sizeof(struct dns_header);
    struct dns_header hdr;
    char instance_fqdn[MAX_NAME];
    int answers = 0;
    size_t i;

    if (build_instance_fqdn(instance_fqdn, sizeof(instance_fqdn), cfg->instance_name, cfg->service_type) != 0) {
        return -1;
    }

    memset(&hdr, 0, sizeof(hdr));
    hdr.flags = htons(DNS_FLAG_QR | DNS_FLAG_AA);

    if (add_rr_ptr(buf, &off, sizeof(buf), cfg->service_type, instance_fqdn, cfg->ttl) != 0 ||
        add_rr_srv(buf, &off, sizeof(buf), instance_fqdn, cfg->host_fqdn, cfg->port, cfg->ttl) != 0 ||
        add_rr_txt_empty(buf, &off, sizeof(buf), instance_fqdn, cfg->ttl) != 0 ||
        add_rr_a(buf, &off, sizeof(buf), cfg->host_fqdn, cfg->ipv4_addr, cfg->ttl) != 0) {
        return -1;
    }
    answers = 4;

    if (add_adisk_records(buf, &off, sizeof(buf), cfg, &answers) != 0) {
        return -1;
    }
    if (add_device_info_records(buf, &off, sizeof(buf), cfg, &answers) != 0) {
        return -1;
    }
    if (use_snapshot_records) {
        for (i = 0; i < snapshot_records->count; i++) {
            if (is_suppressed_snapshot_service_type(snapshot_records->records[i].service_type)) {
                continue;
            }
            if (add_service_record_answers(buf, &off, sizeof(buf), &snapshot_records->records[i], cfg->ttl, &answers) != 0) {
                return -1;
            }
            if (add_snapshot_host_a_record(buf, &off, sizeof(buf), &snapshot_records->records[i], cfg, &answers) != 0) {
                return -1;
            }
        }
    } else {
        if (add_airport_records(buf, &off, sizeof(buf), cfg, &answers) != 0) {
            return -1;
        }
    }

    hdr.ancount = htons((uint16_t)answers);
    memcpy(buf, &hdr, sizeof(hdr));

    return (sendto(sockfd, buf, off, 0, (const struct sockaddr *)dest, sizeof(*dest)) >= 0) ? 0 : -1;
}

static int handle_query(int sockfd, const uint8_t *packet, size_t packet_len, const struct sockaddr_in *dest, const struct config *cfg,
                        const struct service_record_set *snapshot_records, int use_snapshot_records) {
    struct dns_header hdr;
    size_t cursor = sizeof(struct dns_header);
    uint16_t qdcount;
    uint8_t reply[BUF_SIZE];
    size_t off = sizeof(struct dns_header);
    char instance_fqdn[MAX_NAME];
    char adisk_instance_fqdn[MAX_NAME];
    char device_info_instance_fqdn[MAX_NAME];
    char airport_instance_fqdn[MAX_NAME];
    int want_ptr = 0;
    int want_srv = 0;
    int want_txt = 0;
    int want_a = 0;
    int want_adisk_ptr = 0;
    int want_adisk_srv = 0;
    int want_adisk_txt = 0;
    int want_device_info_ptr = 0;
    int want_device_info_srv = 0;
    int want_device_info_txt = 0;
    int want_airport_ptr = 0;
    int want_airport_srv = 0;
    int want_airport_txt = 0;
    int answers = 0;
    uint16_t i;
    int want_snapshot_ptr[SNAPSHOT_MAX_RECORDS];
    int want_snapshot_srv[SNAPSHOT_MAX_RECORDS];
    int want_snapshot_txt[SNAPSHOT_MAX_RECORDS];
    int want_snapshot_a[SNAPSHOT_MAX_RECORDS];

    memset(want_snapshot_ptr, 0, sizeof(want_snapshot_ptr));
    memset(want_snapshot_srv, 0, sizeof(want_snapshot_srv));
    memset(want_snapshot_txt, 0, sizeof(want_snapshot_txt));
    memset(want_snapshot_a, 0, sizeof(want_snapshot_a));

    if (packet_len < sizeof(struct dns_header)) {
        return 0;
    }
    memcpy(&hdr, packet, sizeof(hdr));
    if (ntohs(hdr.flags) & DNS_FLAG_QR) {
        return 0;
    }

    qdcount = ntohs(hdr.qdcount);
    if (build_instance_fqdn(instance_fqdn, sizeof(instance_fqdn), cfg->instance_name, cfg->service_type) != 0) {
        return 0;
    }
    if (cfg->adisk_share_name[0] != '\0' && cfg->adisk_uuid[0] != '\0' &&
        build_instance_fqdn(adisk_instance_fqdn, sizeof(adisk_instance_fqdn), cfg->instance_name, cfg->adisk_service_type) != 0) {
        return 0;
    }
    if (cfg->device_model[0] != '\0' &&
        build_instance_fqdn(device_info_instance_fqdn, sizeof(device_info_instance_fqdn), cfg->instance_name, cfg->device_info_service_type) != 0) {
        return 0;
    }
    if (!use_snapshot_records && is_airport_enabled(cfg) &&
        build_instance_fqdn(airport_instance_fqdn, sizeof(airport_instance_fqdn), cfg->instance_name, cfg->airport_service_type) != 0) {
        return 0;
    }

    for (i = 0; i < qdcount; i++) {
        char qname[MAX_NAME];
        uint16_t qtype;
        uint16_t qclass;

        if (decode_name(packet, packet_len, &cursor, qname, sizeof(qname)) != 0 ||
            cursor + 4 > packet_len) {
            return 0;
        }
        memcpy(&qtype, packet + cursor, 2);
        memcpy(&qclass, packet + cursor + 2, 2);
        cursor += 4;
        qtype = ntohs(qtype);
        qclass = ntohs(qclass) & 0x7FFF;
        if (qclass != DNS_CLASS_IN) {
            continue;
        }

        if (name_equals(qname, cfg->service_type) && (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
            want_ptr = 1;
        } else if (cfg->adisk_share_name[0] != '\0' &&
                   name_equals(qname, cfg->adisk_service_type) &&
                   (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
            want_adisk_ptr = 1;
        } else if (cfg->device_model[0] != '\0' &&
                   name_equals(qname, cfg->device_info_service_type) &&
                   (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
            want_device_info_ptr = 1;
        } else if (!use_snapshot_records && is_airport_enabled(cfg) &&
                   name_equals(qname, cfg->airport_service_type) &&
                   (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
            want_airport_ptr = 1;
        } else if (name_equals(qname, instance_fqdn) && (qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY)) {
            want_srv = 1;
        } else if (name_equals(qname, instance_fqdn) && (qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY)) {
            want_txt = 1;
        } else if (cfg->adisk_share_name[0] != '\0' &&
                   name_equals(qname, adisk_instance_fqdn)) {
            if (qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY) {
                want_adisk_srv = 1;
            }
            if (qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY) {
                want_adisk_txt = 1;
            }
        } else if (cfg->device_model[0] != '\0' &&
                   name_equals(qname, device_info_instance_fqdn)) {
            if (qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY) {
                want_device_info_srv = 1;
            }
            if (qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY) {
                want_device_info_txt = 1;
            }
        } else if (!use_snapshot_records && is_airport_enabled(cfg) &&
                   name_equals(qname, airport_instance_fqdn)) {
            if (qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY) {
                want_airport_srv = 1;
            }
            if (qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY) {
                want_airport_txt = 1;
            }
        } else if (name_equals(qname, cfg->host_fqdn) && (qtype == DNS_TYPE_A || qtype == DNS_TYPE_ANY)) {
            want_a = 1;
        } else if (use_snapshot_records) {
            size_t j;
            for (j = 0; j < snapshot_records->count; j++) {
                const struct service_record *record = &snapshot_records->records[j];
                if (is_suppressed_snapshot_service_type(record->service_type)) {
                    continue;
                }
                if (name_equals(qname, record->service_type) && (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
                    want_snapshot_ptr[j] = 1;
                } else if (name_equals(qname, record->instance_fqdn)) {
                    if (qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY) {
                        want_snapshot_srv[j] = 1;
                    }
                    if (qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY) {
                        want_snapshot_txt[j] = 1;
                    }
                } else if (name_equals(qname, record->host_fqdn) && (qtype == DNS_TYPE_A || qtype == DNS_TYPE_ANY)) {
                    want_snapshot_a[j] = 1;
                }
            }
        }
    }

    if (!want_ptr && !want_srv && !want_txt && !want_a &&
        !want_adisk_ptr && !want_adisk_srv && !want_adisk_txt &&
        !want_device_info_ptr && !want_device_info_srv && !want_device_info_txt &&
        !want_airport_ptr && !want_airport_srv && !want_airport_txt) {
        int any_snapshot = 0;
        if (use_snapshot_records) {
            size_t j;
            for (j = 0; j < snapshot_records->count; j++) {
                if (want_snapshot_ptr[j] || want_snapshot_srv[j] || want_snapshot_txt[j]) {
                    any_snapshot = 1;
                    break;
                }
                if (want_snapshot_a[j]) {
                    any_snapshot = 1;
                    break;
                }
            }
        }
        if (!any_snapshot) {
            return 0;
        }
    }

    memset(&hdr, 0, sizeof(hdr));
    hdr.flags = htons(DNS_FLAG_QR | DNS_FLAG_AA);

    if (want_ptr) {
        if (add_rr_ptr(reply, &off, sizeof(reply), cfg->service_type, instance_fqdn, cfg->ttl) != 0) {
            return -1;
        }
        answers++;
    }
    if (want_srv) {
        if (add_rr_srv(reply, &off, sizeof(reply), instance_fqdn, cfg->host_fqdn, cfg->port, cfg->ttl) != 0) {
            return -1;
        }
        answers++;
    }
    if (want_txt) {
        if (add_rr_txt_empty(reply, &off, sizeof(reply), instance_fqdn, cfg->ttl) != 0) {
            return -1;
        }
        answers++;
    }
    if (want_adisk_ptr || want_adisk_srv || want_adisk_txt) {
        char txt1[128];
        char txt2[256];
        const char *txts[2];

        if (build_adisk_system_txt(txt1, sizeof(txt1), cfg->adisk_sys_wama) != 0) {
            return -1;
        }
        if (build_adisk_disk_txt(txt2, sizeof(txt2), cfg->adisk_disk_key, cfg->adisk_share_name, cfg->adisk_uuid) != 0) {
            return -1;
        }
        txts[0] = txt1;
        txts[1] = txt2;

        if (want_adisk_ptr) {
            if (add_rr_ptr(reply, &off, sizeof(reply), cfg->adisk_service_type, adisk_instance_fqdn, cfg->ttl) != 0) {
                return -1;
            }
            answers++;
        }
        if (want_adisk_srv) {
            if (add_rr_srv(reply, &off, sizeof(reply), adisk_instance_fqdn, cfg->host_fqdn, cfg->adisk_port, cfg->ttl) != 0) {
                return -1;
            }
            answers++;
        }
        if (want_adisk_txt) {
            if (add_rr_txt_strings(reply, &off, sizeof(reply), adisk_instance_fqdn, cfg->ttl, txts, 2) != 0) {
                return -1;
            }
            answers++;
        }
    }
    if (want_device_info_ptr || want_device_info_srv || want_device_info_txt) {
        char model_txt[MAX_NAME + 16];
        const char *txts[1];

        if (build_model_txt(model_txt, sizeof(model_txt), cfg->device_model) != 0) {
            return -1;
        }
        txts[0] = model_txt;

        if (want_device_info_ptr) {
            if (add_rr_ptr(reply, &off, sizeof(reply), cfg->device_info_service_type, device_info_instance_fqdn, cfg->ttl) != 0) {
                return -1;
            }
            answers++;
        }
        if (want_device_info_srv) {
            if (add_rr_srv(reply, &off, sizeof(reply), device_info_instance_fqdn, cfg->host_fqdn, 0, cfg->ttl) != 0) {
                return -1;
            }
            answers++;
        }
        if (want_device_info_txt) {
            if (add_rr_txt_strings(reply, &off, sizeof(reply), device_info_instance_fqdn, cfg->ttl, txts, 1) != 0) {
                return -1;
            }
            answers++;
        }
    }
    if (!use_snapshot_records && (want_airport_ptr || want_airport_srv || want_airport_txt)) {
        char airport_txt[256];
        const char *txts[1];

        if (build_airport_txt(airport_txt, sizeof(airport_txt), cfg) != 0) {
            return -1;
        }
        txts[0] = airport_txt;

        if (want_airport_ptr) {
            if (add_rr_ptr(reply, &off, sizeof(reply), cfg->airport_service_type, airport_instance_fqdn, cfg->ttl) != 0) {
                return -1;
            }
            answers++;
        }
        if (want_airport_srv) {
            if (add_rr_srv(reply, &off, sizeof(reply), airport_instance_fqdn, cfg->host_fqdn, cfg->airport_port, cfg->ttl) != 0) {
                return -1;
            }
            answers++;
        }
        if (want_airport_txt) {
            if (add_rr_txt_strings(reply, &off, sizeof(reply), airport_instance_fqdn, cfg->ttl, txts, 1) != 0) {
                return -1;
            }
            answers++;
        }
    }
    if (want_a) {
        if (add_rr_a(reply, &off, sizeof(reply), cfg->host_fqdn, cfg->ipv4_addr, cfg->ttl) != 0) {
            return -1;
        }
        answers++;
    }

    if (use_snapshot_records) {
        size_t j;
        for (j = 0; j < snapshot_records->count; j++) {
            const struct service_record *record = &snapshot_records->records[j];
            const char *txts[SNAPSHOT_MAX_TXT_ITEMS];
            size_t k;

            if (is_suppressed_snapshot_service_type(record->service_type)) {
                continue;
            }
            for (k = 0; k < record->txt_count; k++) {
                txts[k] = record->txt[k];
            }
            if (want_snapshot_ptr[j]) {
                if (add_rr_ptr(reply, &off, sizeof(reply), record->service_type, record->instance_fqdn, cfg->ttl) != 0) {
                    return -1;
                }
                answers++;
            }
            if (want_snapshot_srv[j]) {
                if (add_rr_srv(reply, &off, sizeof(reply), record->instance_fqdn, record->host_fqdn, record->port, cfg->ttl) != 0) {
                    return -1;
                }
                answers++;
            }
            if (want_snapshot_txt[j]) {
                if (record->txt_count > 0) {
                    if (add_rr_txt_strings(reply, &off, sizeof(reply), record->instance_fqdn, cfg->ttl, txts, record->txt_count) != 0) {
                        return -1;
                    }
                } else {
                    if (add_rr_txt_empty(reply, &off, sizeof(reply), record->instance_fqdn, cfg->ttl) != 0) {
                        return -1;
                    }
                }
                answers++;
            }
            if (want_snapshot_a[j]) {
                if (add_rr_a(reply, &off, sizeof(reply), record->host_fqdn, cfg->ipv4_addr, cfg->ttl) != 0) {
                    return -1;
                }
                answers++;
            }
        }
    }

    hdr.ancount = htons((uint16_t)answers);
    memcpy(reply, &hdr, sizeof(hdr));

    return (sendto(sockfd, reply, off, 0, (const struct sockaddr *)dest, sizeof(*dest)) >= 0) ? 0 : -1;
}

static int open_mdns_socket(void) {
    int sockfd;
    int yes = 1;
    struct sockaddr_in addr;
    struct ip_mreq mreq;

    sockfd = socket(AF_INET, SOCK_DGRAM, 0);
    if (sockfd < 0) {
        perror("socket");
        return -1;
    }

    if (setsockopt(sockfd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes)) < 0) {
        perror("setsockopt(SO_REUSEADDR)");
        close(sockfd);
        return -1;
    }

#ifdef SO_REUSEPORT
    (void)setsockopt(sockfd, SOL_SOCKET, SO_REUSEPORT, &yes, sizeof(yes));
#endif

    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(MDNS_PORT);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(sockfd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind");
        close(sockfd);
        return -1;
    }

    memset(&mreq, 0, sizeof(mreq));
    mreq.imr_multiaddr.s_addr = inet_addr(MDNS_GROUP);
    mreq.imr_interface.s_addr = htonl(INADDR_ANY);
    if (setsockopt(sockfd, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mreq, sizeof(mreq)) < 0) {
        perror("setsockopt(IP_ADD_MEMBERSHIP)");
        close(sockfd);
        return -1;
    }

    yes = 255;
    (void)setsockopt(sockfd, IPPROTO_IP, IP_MULTICAST_TTL, &yes, sizeof(yes));
    yes = 1;
    (void)setsockopt(sockfd, IPPROTO_IP, IP_MULTICAST_LOOP, &yes, sizeof(yes));

    return sockfd;
}

int main(int argc, char **argv) {
    struct config cfg;
    struct service_record_set snapshot_records;
    int sockfd;
    struct sockaddr_in mdns_dest;
    int i;
    time_t last_announce = 0;
    int use_snapshot_records = 0;

    memset(&cfg, 0, sizeof(cfg));
    memset(&snapshot_records, 0, sizeof(snapshot_records));
    strcpy(cfg.service_type, "_smb._tcp.local.");
    strcpy(cfg.adisk_service_type, "_adisk._tcp.local.");
    strcpy(cfg.adisk_disk_key, ADISK_DEFAULT_DISK_KEY);
    strcpy(cfg.device_info_service_type, "_device-info._tcp.local.");
    strcpy(cfg.airport_service_type, AIRPORT_SERVICE_TYPE);
    cfg.port = 445;
    cfg.adisk_port = 9;
    cfg.airport_port = AIRPORT_DEFAULT_PORT;
    cfg.ttl = 120;

    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--save-snapshot") == 0 && i + 1 < argc) {
            strncpy(cfg.save_snapshot_path, argv[++i], sizeof(cfg.save_snapshot_path) - 1);
        } else if (strcmp(argv[i], "--load-snapshot") == 0 && i + 1 < argc) {
            strncpy(cfg.load_snapshot_path, argv[++i], sizeof(cfg.load_snapshot_path) - 1);
        } else if (strcmp(argv[i], "--service") == 0 && i + 1 < argc) {
            strncpy(cfg.service_type, argv[++i], sizeof(cfg.service_type) - 1);
        } else if (strcmp(argv[i], "--adisk-share") == 0 && i + 1 < argc) {
            strncpy(cfg.adisk_share_name, argv[++i], sizeof(cfg.adisk_share_name) - 1);
        } else if (strcmp(argv[i], "--adisk-disk-key") == 0 && i + 1 < argc) {
            strncpy(cfg.adisk_disk_key, argv[++i], sizeof(cfg.adisk_disk_key) - 1);
        } else if (strcmp(argv[i], "--adisk-uuid") == 0 && i + 1 < argc) {
            strncpy(cfg.adisk_uuid, argv[++i], sizeof(cfg.adisk_uuid) - 1);
        } else if (strcmp(argv[i], "--adisk-sys-wama") == 0 && i + 1 < argc) {
            strncpy(cfg.adisk_sys_wama, argv[++i], sizeof(cfg.adisk_sys_wama) - 1);
        } else if (strcmp(argv[i], "--device-model") == 0 && i + 1 < argc) {
            strncpy(cfg.device_model, argv[++i], sizeof(cfg.device_model) - 1);
        } else if (strcmp(argv[i], "--airport-wama") == 0 && i + 1 < argc) {
            strncpy(cfg.airport_wama, argv[++i], sizeof(cfg.airport_wama) - 1);
        } else if (strcmp(argv[i], "--airport-rama") == 0 && i + 1 < argc) {
            strncpy(cfg.airport_rama, argv[++i], sizeof(cfg.airport_rama) - 1);
        } else if (strcmp(argv[i], "--airport-ram2") == 0 && i + 1 < argc) {
            strncpy(cfg.airport_ram2, argv[++i], sizeof(cfg.airport_ram2) - 1);
        } else if (strcmp(argv[i], "--airport-rast") == 0 && i + 1 < argc) {
            strncpy(cfg.airport_rast, argv[++i], sizeof(cfg.airport_rast) - 1);
        } else if (strcmp(argv[i], "--airport-rana") == 0 && i + 1 < argc) {
            strncpy(cfg.airport_rana, argv[++i], sizeof(cfg.airport_rana) - 1);
        } else if (strcmp(argv[i], "--airport-syfl") == 0 && i + 1 < argc) {
            strncpy(cfg.airport_syfl, argv[++i], sizeof(cfg.airport_syfl) - 1);
        } else if (strcmp(argv[i], "--airport-syap") == 0 && i + 1 < argc) {
            strncpy(cfg.airport_syap, argv[++i], sizeof(cfg.airport_syap) - 1);
        } else if (strcmp(argv[i], "--airport-syvs") == 0 && i + 1 < argc) {
            strncpy(cfg.airport_syvs, argv[++i], sizeof(cfg.airport_syvs) - 1);
        } else if (strcmp(argv[i], "--airport-srcv") == 0 && i + 1 < argc) {
            strncpy(cfg.airport_srcv, argv[++i], sizeof(cfg.airport_srcv) - 1);
        } else if (strcmp(argv[i], "--airport-bjsd") == 0 && i + 1 < argc) {
            strncpy(cfg.airport_bjsd, argv[++i], sizeof(cfg.airport_bjsd) - 1);
        } else if (strcmp(argv[i], "--airport-port") == 0 && i + 1 < argc) {
            cfg.airport_port = (uint16_t)atoi(argv[++i]);
        } else if (strcmp(argv[i], "--instance") == 0 && i + 1 < argc) {
            strncpy(cfg.instance_name, argv[++i], sizeof(cfg.instance_name) - 1);
        } else if (strcmp(argv[i], "--host") == 0 && i + 1 < argc) {
            strncpy(cfg.host_label, argv[++i], sizeof(cfg.host_label) - 1);
        } else if (strcmp(argv[i], "--ipv4") == 0 && i + 1 < argc) {
            if (inet_pton(AF_INET, argv[++i], &cfg.ipv4_addr) != 1) {
                fprintf(stderr, "Invalid IPv4 address\n");
                return 2;
            }
        } else if (strcmp(argv[i], "--port") == 0 && i + 1 < argc) {
            cfg.port = (uint16_t)atoi(argv[++i]);
        } else if (strcmp(argv[i], "--ttl") == 0 && i + 1 < argc) {
            cfg.ttl = (uint32_t)atoi(argv[++i]);
        } else {
            usage(argv[0]);
            return 2;
        }
    }

    if (cfg.instance_name[0] == '\0' || cfg.host_label[0] == '\0' || cfg.ipv4_addr == 0) {
        usage(argv[0]);
        return 2;
    }

    if (validate_single_dns_label(cfg.instance_name, "instance name") != 0 ||
        validate_single_dns_label(cfg.host_label, "host label") != 0) {
        return 2;
    }
    if (validate_dns_name(cfg.service_type, "service type") != 0) {
        return 2;
    }
    if (cfg.adisk_share_name[0] != '\0') {
        char adisk_sys_txt[128];
        char adisk_disk_txt[256];
        if (build_adisk_system_txt(adisk_sys_txt, sizeof(adisk_sys_txt), cfg.adisk_sys_wama) != 0) {
            return 2;
        }
        if (build_adisk_disk_txt(adisk_disk_txt, sizeof(adisk_disk_txt), cfg.adisk_disk_key, cfg.adisk_share_name, cfg.adisk_uuid) != 0) {
            return 2;
        }
    }
    if (cfg.device_model[0] != '\0') {
        char model_txt[MAX_NAME + 16];
        if (build_model_txt(model_txt, sizeof(model_txt), cfg.device_model) != 0) {
            return 2;
        }
    }
    if (cfg.airport_wama[0] != '\0' || cfg.airport_rama[0] != '\0' || cfg.airport_ram2[0] != '\0' ||
        cfg.airport_rast[0] != '\0' || cfg.airport_rana[0] != '\0' || cfg.airport_syfl[0] != '\0' ||
        cfg.airport_syap[0] != '\0' || cfg.airport_syvs[0] != '\0' || cfg.airport_srcv[0] != '\0' ||
        cfg.airport_bjsd[0] != '\0') {
        char airport_txt[256];
        if (build_airport_txt(airport_txt, sizeof(airport_txt), &cfg) != 0) {
            return 2;
        }
    }

    snprintf(cfg.host_fqdn, sizeof(cfg.host_fqdn), "%s.local.", cfg.host_label);

    if (cfg.save_snapshot_path[0] != '\0') {
        struct service_record_set captured_records;
        memset(&captured_records, 0, sizeof(captured_records));
        if (capture_apple_snapshot_with_retry(&cfg, &captured_records) == 0) {
            if (write_snapshot_file_atomic(cfg.save_snapshot_path, &captured_records) != 0) {
                fprintf(stderr, "failed to write snapshot file: %s\n", cfg.save_snapshot_path);
            }
        } else {
            fprintf(stderr, "warning: could not capture Apple mDNS snapshot\n");
        }
    }

    if (cfg.load_snapshot_path[0] != '\0') {
        gracefully_kill_mdnsresponder();
        if (load_snapshot_file(cfg.load_snapshot_path, &snapshot_records) == 0) {
            use_snapshot_records = 1;
        } else {
            fprintf(stderr, "warning: could not load snapshot file: %s; falling back to generated records\n",
                    cfg.load_snapshot_path);
        }
    }

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    sockfd = open_mdns_socket();
    if (sockfd < 0) {
        return 1;
    }

    memset(&mdns_dest, 0, sizeof(mdns_dest));
    mdns_dest.sin_family = AF_INET;
    mdns_dest.sin_port = htons(MDNS_PORT);
    mdns_dest.sin_addr.s_addr = inet_addr(MDNS_GROUP);

    if (send_announcement(sockfd, &mdns_dest, &cfg, &snapshot_records, use_snapshot_records) != 0) {
        perror("send_announcement");
    }
    last_announce = time(NULL);

    while (!g_stop) {
        fd_set rfds;
        struct timeval tv;
        uint8_t packet[BUF_SIZE];
        ssize_t nread;

        FD_ZERO(&rfds);
        FD_SET(sockfd, &rfds);
        tv.tv_sec = 1;
        tv.tv_usec = 0;

        if (select(sockfd + 1, &rfds, NULL, NULL, &tv) > 0 && FD_ISSET(sockfd, &rfds)) {
            struct sockaddr_in src;
            socklen_t src_len = sizeof(src);
            nread = recvfrom(sockfd, packet, sizeof(packet), 0, (struct sockaddr *)&src, &src_len);
            if (nread > 0) {
                (void)handle_query(sockfd, packet, (size_t)nread, &mdns_dest, &cfg, &snapshot_records, use_snapshot_records);
            }
        }

        if (time(NULL) - last_announce >= ANNOUNCE_INTERVAL) {
            (void)send_announcement(sockfd, &mdns_dest, &cfg, &snapshot_records, use_snapshot_records);
            last_announce = time(NULL);
        }
    }

    close(sockfd);
    return 0;
}
