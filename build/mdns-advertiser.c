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
#define TAKEOVER_RETRY_COUNT 6
#define STARTUP_BURST_COUNT 7

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

enum exit_code {
    EXIT_OK = 0,
    EXIT_SOCKET_ACQUIRE_FAILED = 1,
    EXIT_INVALID_IPV4 = 2,
    EXIT_USAGE = 3,
    EXIT_MISSING_REQUIRED_ARGS = 4,
    EXIT_INVALID_DNS_LABEL = 5,
    EXIT_INVALID_SERVICE_TYPE = 6,
    EXIT_INVALID_ADISK_SYSTEM = 7,
    EXIT_INVALID_ADISK_DISK = 8,
    EXIT_INVALID_DEVICE_MODEL = 9,
    EXIT_INVALID_AIRPORT_TXT = 10
};

struct config {
    char save_all_snapshot_path[MAX_NAME];
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
    char load_snapshot_path[MAX_NAME];
    char save_snapshot_path[MAX_NAME];
};

struct service_record {
    char service_type[MAX_NAME];
    char instance_name[MAX_NAME];
    char instance_fqdn[MAX_NAME];
    char host_label[MAX_LABEL + 1];
    char host_fqdn[MAX_NAME];
    uint16_t port;
    char txt[SNAPSHOT_MAX_TXT_ITEMS][MAX_TXT_STRING + 1];
    uint8_t txt_len[SNAPSHOT_MAX_TXT_ITEMS];
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
static int open_mdns_socket(int shared_bind, int log_bind_errors, uint32_t ipv4_addr, const char *socket_role);
static int is_airport_enabled(const struct config *cfg);
static int add_rr_ptr(uint8_t *buf, size_t *off, size_t cap, const char *owner, const char *target, uint32_t ttl);
static int add_rr_txt_empty(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ttl);
static int add_rr_txt_items(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ttl,
                            const char **strings, const uint8_t *lengths, size_t string_count);
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

static const char *ipv4_to_string(uint32_t ipv4_addr, char *out, size_t out_len) {
    struct in_addr addr;

    addr.s_addr = ipv4_addr;
    if (inet_ntop(AF_INET, &addr, out, out_len) == NULL) {
        strncpy(out, "invalid", out_len - 1);
        out[out_len - 1] = '\0';
    }
    return out;
}

static void log_startup_config(const struct config *cfg, int shared_bind) {
    char ipv4_buf[INET_ADDRSTRLEN];

    fprintf(stderr,
            "mdns startup: mode=%s instance=%s host=%s ipv4=%s service=%s adisk=%s device_model=%s airport=%s\n",
            shared_bind ? "shared" : "exclusive",
            cfg->instance_name[0] != '\0' ? cfg->instance_name : "(empty)",
            cfg->host_label[0] != '\0' ? cfg->host_label : "(empty)",
            ipv4_to_string(cfg->ipv4_addr, ipv4_buf, sizeof(ipv4_buf)),
            cfg->service_type[0] != '\0' ? cfg->service_type : "(empty)",
            cfg->adisk_share_name[0] != '\0' ? "enabled" : "disabled",
            cfg->device_model[0] != '\0' ? cfg->device_model : "(empty)",
            is_airport_enabled(cfg) ? "enabled" : "disabled");
}

static void log_send_failure(const char *stage, const struct sockaddr_in *dest, int use_snapshot_records,
                             const char *detail) {
    char dest_ip[INET_ADDRSTRLEN];

    fprintf(stderr,
            "mdns send failure: stage=%s dest=%s:%u records=%s detail=%s\n",
            stage,
            ipv4_to_string(dest->sin_addr.s_addr, dest_ip, sizeof(dest_ip)),
            (unsigned int)ntohs(dest->sin_port),
            use_snapshot_records ? "snapshot" : "generated",
            detail);
    fprintf(stderr,
            "mdns send failure: listener remains active; discovery may still work via received queries even though unsolicited announcements failed\n");
}

static void log_served_records(const struct config *cfg, const struct service_record_set *snapshot_records,
                               int use_snapshot_records) {
    fprintf(stderr, "serving summary: source=%s\n", use_snapshot_records ? "snapshot" : "generated");
    fprintf(stderr, "serving service: type=%s instance=%s port=%u host=%s\n",
            cfg->service_type, cfg->instance_name, (unsigned int)cfg->port, cfg->host_fqdn);
    if (cfg->device_model[0] != '\0') {
        fprintf(stderr, "serving service: type=%s instance=%s model=%s\n",
                cfg->device_info_service_type, cfg->instance_name, cfg->device_model);
    }
    if (cfg->adisk_share_name[0] != '\0') {
        fprintf(stderr, "serving service: type=%s instance=%s share=%s disk_key=%s uuid=%s\n",
                cfg->adisk_service_type, cfg->instance_name, cfg->adisk_share_name,
                cfg->adisk_disk_key, cfg->adisk_uuid);
    }
    if (is_airport_enabled(cfg)) {
        fprintf(stderr, "serving service: type=%s instance=%s syAP=%s syVs=%s srcv=%s\n",
                cfg->airport_service_type, cfg->instance_name,
                cfg->airport_syap[0] != '\0' ? cfg->airport_syap : "(none)",
                cfg->airport_syvs[0] != '\0' ? cfg->airport_syvs : "(none)",
                cfg->airport_srcv[0] != '\0' ? cfg->airport_srcv : "(none)");
    }
    if (use_snapshot_records) {
        size_t i;
        for (i = 0; i < snapshot_records->count; i++) {
            const struct service_record *record = &snapshot_records->records[i];
            fprintf(stderr, "serving snapshot record[%lu]: type=%s instance=%s host=%s port=%u txt=%lu\n",
                    (unsigned long)i,
                    record->service_type,
                    record->instance_fqdn,
                    record->host_fqdn,
                    (unsigned int)record->port,
                    (unsigned long)record->txt_count);
        }
    }
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
            "  --save-all-snapshot <path> Capture raw LAN-wide mDNS records into a snapshot file\n"
            "  --save-snapshot <path> Capture Apple mDNS records into a snapshot file\n"
            "  --load-snapshot <path> Kill Apple mDNSResponder and replay snapshot records\n"
            "  --shared-bind     Allow shared UDP 5353 binding instead of exclusive takeover\n"
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

static int normalize_mac_for_airport_txt(char *out, size_t out_len, const char *value, const char *field_name) {
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
    uint8_t txt_lengths[SNAPSHOT_MAX_TXT_ITEMS];
    size_t i;

    for (i = 0; i < record->txt_count; i++) {
        txts[i] = record->txt[i];
        txt_lengths[i] = record->txt_len[i];
    }

    if (add_rr_ptr(buf, off, cap, record->service_type, record->instance_fqdn, ttl) != 0) {
        fprintf(stderr,
                "mdns snapshot rr failure: rr=PTR type=%s instance=%s host=%s port=%u txt_count=%lu packet_len=%lu\n",
                record->service_type, record->instance_fqdn, record->host_fqdn,
                (unsigned int)record->port, (unsigned long)record->txt_count, (unsigned long)*off);
        return -1;
    }
    if (add_rr_srv(buf, off, cap, record->instance_fqdn, record->host_fqdn, record->port, ttl) != 0) {
        fprintf(stderr,
                "mdns snapshot rr failure: rr=SRV type=%s instance=%s host=%s port=%u txt_count=%lu packet_len=%lu\n",
                record->service_type, record->instance_fqdn, record->host_fqdn,
                (unsigned int)record->port, (unsigned long)record->txt_count, (unsigned long)*off);
        return -1;
    }
    *answers += 2;

    if (record->txt_count > 0) {
        if (add_rr_txt_items(buf, off, cap, record->instance_fqdn, ttl, txts, txt_lengths, record->txt_count) != 0) {
            fprintf(stderr,
                    "mdns snapshot rr failure: rr=TXT type=%s instance=%s host=%s port=%u txt_count=%lu packet_len=%lu\n",
                    record->service_type, record->instance_fqdn, record->host_fqdn,
                    (unsigned int)record->port, (unsigned long)record->txt_count, (unsigned long)*off);
            return -1;
        }
        *answers += 1;
    } else {
        if (add_rr_txt_empty(buf, off, cap, record->instance_fqdn, ttl) != 0) {
            fprintf(stderr,
                    "mdns snapshot rr failure: rr=TXT_EMPTY type=%s instance=%s host=%s port=%u txt_count=%lu packet_len=%lu\n",
                    record->service_type, record->instance_fqdn, record->host_fqdn,
                    (unsigned int)record->port, (unsigned long)record->txt_count, (unsigned long)*off);
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
        fprintf(stderr,
                "mdns snapshot rr failure: rr=A type=%s instance=%s host=%s port=%u txt_count=%lu packet_len=%lu\n",
                record->service_type, record->instance_fqdn, record->host_fqdn,
                (unsigned int)record->port, (unsigned long)record->txt_count, (unsigned long)*off);
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

static int add_rr_txt_items(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ttl,
                            const char **strings, const uint8_t *lengths, size_t string_count) {
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
        size_t slen = lengths != NULL ? lengths[i] : strlen(strings[i]);
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

static int add_rr_txt_strings(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ttl,
                              const char **strings, size_t string_count) {
    return add_rr_txt_items(buf, off, cap, owner, ttl, strings, NULL, string_count);
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

static int hex_encode_bytes_len(char *out, size_t out_len, const char *bytes, size_t src_len) {
    static const char hex[] = "0123456789abcdef";
    size_t i;

    if (bytes == NULL) {
        return -1;
    }
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

static int hex_encode_bytes(char *out, size_t out_len, const char *bytes) {
    return hex_encode_bytes_len(out, out_len, bytes, strlen(bytes));
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

static int hex_decode_raw_bytes(uint8_t *out, size_t out_len, const char *hex, size_t *decoded_len) {
    size_t hex_len = strlen(hex);
    size_t i;

    if ((hex_len % 2) != 0 || (hex_len / 2) > out_len) {
        return -1;
    }
    for (i = 0; i < hex_len; i += 2) {
        unsigned int value;
        if (sscanf(hex + i, "%2x", &value) != 1) {
            return -1;
        }
        out[i / 2] = (uint8_t)value;
    }
    *decoded_len = hex_len / 2;
    return 0;
}

static int snapshot_txt_is_safe_text(const char *bytes, size_t len) {
    size_t i;

    for (i = 0; i < len; i++) {
        unsigned char ch = (unsigned char)bytes[i];
        if (ch < 0x20 || ch > 0x7e) {
            return 0;
        }
    }
    return 1;
}

static int write_snapshot_file_atomic(const char *path, const struct service_record_set *set) {
    char tmp_path[MAX_NAME * 2];
    char host_hex[(MAX_NAME * 2) + 1];
    char txt_hex[((MAX_TXT_STRING + 1) * 2) + 1];
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
            if (snapshot_txt_is_safe_text(record->txt[j], record->txt_len[j])) {
                if (fprintf(fp, "TXT=%.*s\n", (int)record->txt_len[j], record->txt[j]) < 0) {
                    fclose(fp);
                    unlink(tmp_path);
                    return -1;
                }
            } else {
                if (hex_encode_bytes_len(txt_hex, sizeof(txt_hex), record->txt[j], record->txt_len[j]) != 0) {
                    fclose(fp);
                    unlink(tmp_path);
                    return -1;
                }
                if (fprintf(fp, "TXT_HEX=%s\n", txt_hex) < 0) {
                    fclose(fp);
                    unlink(tmp_path);
                    return -1;
                }
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
        } else if (strncmp(line, "TXT_HEX=", 8) == 0) {
            size_t decoded_len;
            if (current.txt_count >= SNAPSHOT_MAX_TXT_ITEMS) {
                fclose(fp);
                return -1;
            }
            if (hex_decode_raw_bytes((uint8_t *)current.txt[current.txt_count], MAX_TXT_STRING, line + 8, &decoded_len) != 0) {
                fclose(fp);
                return -1;
            }
            current.txt[current.txt_count][decoded_len] = '\0';
            current.txt_len[current.txt_count] = (uint8_t)decoded_len;
            current.txt_count++;
        } else if (strncmp(line, "TXT=", 4) == 0) {
            size_t txt_len;
            if (current.txt_count >= SNAPSHOT_MAX_TXT_ITEMS) {
                fclose(fp);
                return -1;
            }
            strncpy(current.txt[current.txt_count++], line + 4, MAX_TXT_STRING);
            txt_len = strlen(line + 4);
            if (txt_len > MAX_TXT_STRING) {
                txt_len = MAX_TXT_STRING;
            }
            current.txt[current.txt_count - 1][MAX_TXT_STRING] = '\0';
            current.txt_len[current.txt_count - 1] = (uint8_t)txt_len;
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
        record->txt_len[record->txt_count] = len;
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
    return open_mdns_socket(1, 1, 0, "capture");
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

static int mac_equals(const char *a, const char *b) {
    size_t ai = 0;
    size_t bi = 0;

    if (a == NULL || b == NULL || a[0] == '\0' || b[0] == '\0') {
        return 0;
    }

    while (a[ai] != '\0' || b[bi] != '\0') {
        while (a[ai] == ':' || a[ai] == '-' || a[ai] == '.') {
            ai++;
        }
        while (b[bi] == ':' || b[bi] == '-' || b[bi] == '.') {
            bi++;
        }
        if (a[ai] == '\0' || b[bi] == '\0') {
            break;
        }
        if (tolower((unsigned char)a[ai]) != tolower((unsigned char)b[bi])) {
            return 0;
        }
        ai++;
        bi++;
    }

    while (a[ai] == ':' || a[ai] == '-' || a[ai] == '.') {
        ai++;
    }
    while (b[bi] == ':' || b[bi] == '-' || b[bi] == '.') {
        bi++;
    }
    return a[ai] == '\0' && b[bi] == '\0';
}

static int cfg_has_airport_identity_macs(const struct config *cfg) {
    return cfg->airport_wama[0] != '\0' ||
           cfg->airport_rama[0] != '\0' ||
           cfg->airport_ram2[0] != '\0';
}

static int local_airport_mac_matches(const struct config *cfg, const char *value) {
    return mac_equals(value, cfg->airport_wama) ||
           mac_equals(value, cfg->airport_rama) ||
           mac_equals(value, cfg->airport_ram2);
}

static int airport_txt_key_matches_local_mac(const char *txt, const struct config *cfg) {
    const char *segment = txt;

    while (segment != NULL && *segment != '\0') {
        const char *next = strchr(segment, ',');
        size_t len = next != NULL ? (size_t)(next - segment) : strlen(segment);

        if (len > 5 &&
            (strncasecmp(segment, "waMA=", 5) == 0 ||
             strncasecmp(segment, "raMA=", 5) == 0 ||
             strncasecmp(segment, "raM2=", 5) == 0)) {
            char value[32];

            if (len - 5 >= sizeof(value)) {
                return 0;
            }
            memcpy(value, segment + 5, len - 5);
            value[len - 5] = '\0';
            if (local_airport_mac_matches(cfg, value)) {
                return 1;
            }
        }
        segment = next != NULL ? next + 1 : NULL;
    }

    return 0;
}

static int airport_record_matches_local_identity(const struct service_record *record, const struct config *cfg) {
    size_t i;

    if (!name_equals(record->service_type, AIRPORT_SERVICE_TYPE)) {
        return 0;
    }
    for (i = 0; i < record->txt_count; i++) {
        if (airport_txt_key_matches_local_mac(record->txt[i], cfg)) {
            return 1;
        }
    }
    return 0;
}

static int find_matching_airport_host(char *out, size_t out_len, const struct service_record_set *set,
                                      const struct config *cfg) {
    size_t i;
    int found = 0;

    for (i = 0; i < set->count; i++) {
        const struct service_record *record = &set->records[i];
        if (!airport_record_matches_local_identity(record, cfg) || record->host_label[0] == '\0') {
            continue;
        }
        if (!found) {
            if (strlen(record->host_label) >= out_len) {
                return -1;
            }
            strcpy(out, record->host_label);
            found = 1;
            continue;
        }
        if (strcmp(out, record->host_label) != 0) {
            return -1;
        }
    }

    return found ? 0 : -1;
}

static int filter_records_by_host(struct service_record_set *out, const struct service_record_set *in,
                                  const char *host_label) {
    size_t i;

    memset(out, 0, sizeof(*out));
    for (i = 0; i < in->count; i++) {
        const struct service_record *record = &in->records[i];
        if (record->host_label[0] == '\0' || strcmp(record->host_label, host_label) != 0) {
            continue;
        }
        if (out->count >= SNAPSHOT_MAX_RECORDS) {
            break;
        }
        out->records[out->count++] = *record;
    }

    return out->count > 0 ? 0 : -1;
}

static int capture_mdns_snapshot_raw(struct service_record_set *out) {
    int sockfd;
    struct sockaddr_in mdns_dest;
    size_t i;
    struct service_type_set service_types;

    memset(out, 0, sizeof(*out));
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
    (void)collect_mdns_responses(sockfd, 5, out, &service_types);

    for (i = 0; i < service_types.count; i++) {
        (void)send_query_question(sockfd, &mdns_dest, service_types.types[i], DNS_TYPE_PTR);
    }
    (void)collect_mdns_responses(sockfd, 5, out, &service_types);

    for (i = 0; i < out->count; i++) {
        (void)send_query_question(sockfd, &mdns_dest, out->records[i].instance_fqdn, DNS_TYPE_SRV);
        (void)send_query_question(sockfd, &mdns_dest, out->records[i].instance_fqdn, DNS_TYPE_TXT);
    }
    (void)collect_mdns_responses(sockfd, 5, out, &service_types);
    close(sockfd);

    return out->count > 0 ? 0 : -1;
}

static int prepare_loaded_snapshot_for_advertising(const struct config *cfg, const struct service_record_set *loaded,
                                                   struct service_record_set *out) {
    char matched_host[MAX_LABEL + 1];

    if (!cfg_has_airport_identity_macs(cfg)) {
        return -1;
    }
    if (find_matching_airport_host(matched_host, sizeof(matched_host), loaded, cfg) != 0) {
        return -1;
    }

    return filter_records_by_host(out, loaded, matched_host);
}

static int capture_mdns_snapshot_raw_with_retry(struct service_record_set *out) {
    time_t deadline = time(NULL) + SNAPSHOT_CAPTURE_TIMEOUT_SECONDS;

    do {
        if (capture_mdns_snapshot_raw(out) == 0) {
            return 0;
        }
        if (time(NULL) >= deadline) {
            break;
        }
        sleep(SNAPSHOT_CAPTURE_RETRY_INTERVAL_SECONDS);
    } while (time(NULL) < deadline);

    return -1;
}

static int mdnsresponder_is_alive(void) {
    FILE *ps = popen("/bin/ps ax -o stat= -o ucomm= 2>/dev/null", "r");
    char line[256];
    int alive = 0;

    if (ps == NULL) {
        return 0;
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
    return alive;
}

static void sleep_millis(unsigned int delay_ms) {
    if (delay_ms == 0) {
        return;
    }
    (void)usleep((useconds_t)delay_ms * 1000U);
}

static long long monotonic_millis(void) {
    struct timeval tv;

    gettimeofday(&tv, NULL);
    return ((long long)tv.tv_sec * 1000LL) + ((long long)tv.tv_usec / 1000LL);
}

static void kill_mdnsresponder(int sig) {
    if (sig == SIGKILL) {
        (void)system("/usr/bin/pkill -9 mDNSResponder >/dev/null 2>&1 || true");
    } else {
        (void)system("/usr/bin/pkill mDNSResponder >/dev/null 2>&1 || true");
    }
}

static int configure_outbound_multicast_socket(int sockfd, uint32_t ipv4_addr, const char *socket_role) {
    int yes;
    struct in_addr multicast_if;
    char ipv4_buf[INET_ADDRSTRLEN];

    multicast_if.s_addr = ipv4_addr;
    if (setsockopt(sockfd, IPPROTO_IP, IP_MULTICAST_IF, &multicast_if, sizeof(multicast_if)) < 0) {
        perror("setsockopt(IP_MULTICAST_IF)");
        return -1;
    }

    fprintf(stderr, "mdns %s socket: outbound multicast interface %s\n",
            socket_role,
            ipv4_to_string(ipv4_addr, ipv4_buf, sizeof(ipv4_buf)));

    yes = 255;
    (void)setsockopt(sockfd, IPPROTO_IP, IP_MULTICAST_TTL, &yes, sizeof(yes));
    yes = 1;
    (void)setsockopt(sockfd, IPPROTO_IP, IP_MULTICAST_LOOP, &yes, sizeof(yes));
    return 0;
}

static int acquire_mdns_socket(int shared_bind, uint32_t ipv4_addr) {
    static const unsigned int retry_delays_ms[TAKEOVER_RETRY_COUNT] = {0, 100, 200, 300, 400, 500};
    size_t i;
    int sockfd;

    for (i = 0; i < TAKEOVER_RETRY_COUNT; i++) {
        kill_mdnsresponder(SIGTERM);
        sleep_millis(retry_delays_ms[i]);
        sockfd = open_mdns_socket(shared_bind, 0, ipv4_addr, "runtime");
        if (sockfd >= 0) {
            fprintf(stderr, "mDNS takeover established after SIGTERM + %ums using %s bind\n",
                    retry_delays_ms[i], shared_bind ? "shared" : "exclusive");
            return sockfd;
        }
    }

    for (i = 0; i < TAKEOVER_RETRY_COUNT; i++) {
        kill_mdnsresponder(SIGKILL);
        sleep_millis(retry_delays_ms[i]);
        sockfd = open_mdns_socket(shared_bind, 0, ipv4_addr, "runtime");
        if (sockfd >= 0) {
            fprintf(stderr, "mDNS takeover established after SIGKILL + %ums using %s bind\n",
                    retry_delays_ms[i], shared_bind ? "shared" : "exclusive");
            return sockfd;
        }
    }

    if (!shared_bind && mdnsresponder_is_alive()) {
        fprintf(stderr, "mDNS takeover failed: Apple mDNSResponder is still alive after retry ladder\n");
    } else {
        fprintf(stderr, "mDNS takeover failed: could not bind UDP 5353 using %s mode\n",
                shared_bind ? "shared" : "exclusive");
    }
    errno = EADDRINUSE;
    return -1;
}

static void format_dest_addr(const struct sockaddr_in *dest, char *buf, size_t buf_size) {
    char ipbuf[INET_ADDRSTRLEN];

    snprintf(buf, buf_size, "%s:%u",
             ipv4_to_string(dest->sin_addr.s_addr, ipbuf, sizeof(ipbuf)),
             (unsigned int)ntohs(dest->sin_port));
}

static void log_packet_build_failure(const char *stage, const char *step, size_t packet_len, int answers,
                                     int use_snapshot_records) {
    fprintf(stderr,
            "mdns packet build failure: stage=%s step=%s packet_len=%lu answers=%d records=%s\n",
            stage,
            step,
            (unsigned long)packet_len,
            answers,
            use_snapshot_records ? "snapshot" : "generated");
}

static void log_snapshot_record_build_failure(const char *stage, const char *step, size_t record_index,
                                              const struct service_record *record, size_t packet_len, int answers) {
    fprintf(stderr,
            "mdns snapshot build failure: stage=%s step=%s record_index=%lu type=%s instance=%s host=%s port=%u txt_count=%lu packet_len=%lu answers=%d\n",
            stage,
            step,
            (unsigned long)record_index,
            record->service_type,
            record->instance_fqdn,
            record->host_fqdn,
            (unsigned int)record->port,
            (unsigned long)record->txt_count,
            (unsigned long)packet_len,
            answers);
}

static void log_packet_send_failure_detail(const char *stage, const struct sockaddr_in *dest, size_t packet_len,
                                           int answers, int use_snapshot_records, int saved_errno) {
    char destbuf[64];

    format_dest_addr(dest, destbuf, sizeof(destbuf));
    fprintf(stderr,
            "mdns packet send failure: stage=%s dest=%s packet_len=%lu answers=%d records=%s errno=%d (%s)\n",
            stage,
            destbuf,
            (unsigned long)packet_len,
            answers,
            use_snapshot_records ? "snapshot" : "generated",
            saved_errno,
            strerror(saved_errno));
}

static int send_dns_packet(const char *stage, int sockfd, const uint8_t *buf, size_t packet_len,
                           const struct sockaddr_in *dest, int answers, int use_snapshot_records) {
    static int logged_success_announcement = 0;
    static int logged_success_reply = 0;

    ssize_t sent;
    int saved_errno;

    errno = 0;
    sent = sendto(sockfd, buf, packet_len, 0, (const struct sockaddr *)dest, sizeof(*dest));
    saved_errno = errno;
    if (sent < 0) {
        errno = saved_errno;
        log_packet_send_failure_detail(stage, dest, packet_len, answers, use_snapshot_records, saved_errno);
        return -1;
    }

    if (strcmp(stage, "query_response") == 0) {
        if (!logged_success_reply) {
            char destbuf[64];
            format_dest_addr(dest, destbuf, sizeof(destbuf));
            fprintf(stderr,
                    "mdns packet send success: stage=%s dest=%s packet_len=%lu answers=%d\n",
                    stage, destbuf, (unsigned long)packet_len, answers);
            logged_success_reply = 1;
        }
    } else if (!logged_success_announcement) {
        char destbuf[64];
        format_dest_addr(dest, destbuf, sizeof(destbuf));
        fprintf(stderr,
                "mdns packet send success: stage=%s dest=%s packet_len=%lu answers=%d records=%s\n",
                stage, destbuf, (unsigned long)packet_len, answers,
                use_snapshot_records ? "snapshot" : "generated");
        logged_success_announcement = 1;
    }

    return 0;
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

static void init_announcement_packet(size_t *off, int *answers) {
    *off = sizeof(struct dns_header);
    *answers = 0;
}

static int finalize_and_send_announcement_packet(int sockfd, uint8_t *buf, size_t off, int answers,
                                                 const struct sockaddr_in *dest, int use_snapshot_records) {
    struct dns_header hdr;

    if (answers <= 0) {
        return 0;
    }

    memset(&hdr, 0, sizeof(hdr));
    hdr.flags = htons(DNS_FLAG_QR | DNS_FLAG_AA);
    hdr.ancount = htons((uint16_t)answers);
    memcpy(buf, &hdr, sizeof(hdr));
    return send_dns_packet("announcement", sockfd, buf, off, dest, answers, use_snapshot_records);
}

static int append_generated_base_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg,
                                         int *answers) {
    char instance_fqdn[MAX_NAME];

    if (build_instance_fqdn(instance_fqdn, sizeof(instance_fqdn), cfg->instance_name, cfg->service_type) != 0) {
        return -1;
    }
    if (add_rr_ptr(buf, off, cap, cfg->service_type, instance_fqdn, cfg->ttl) != 0 ||
        add_rr_srv(buf, off, cap, instance_fqdn, cfg->host_fqdn, cfg->port, cfg->ttl) != 0 ||
        add_rr_txt_empty(buf, off, cap, instance_fqdn, cfg->ttl) != 0 ||
        add_rr_a(buf, off, cap, cfg->host_fqdn, cfg->ipv4_addr, cfg->ttl) != 0) {
        return -1;
    }
    *answers += 4;
    if (add_adisk_records(buf, off, cap, cfg, answers) != 0) {
        return -1;
    }
    if (add_device_info_records(buf, off, cap, cfg, answers) != 0) {
        return -1;
    }
    return 0;
}

static int host_already_announced(char announced_hosts[][MAX_NAME], size_t announced_count, const char *host_fqdn) {
    size_t i;

    for (i = 0; i < announced_count; i++) {
        if (name_equals(announced_hosts[i], host_fqdn)) {
            return 1;
        }
    }
    return 0;
}

static int remember_announced_host(char announced_hosts[][MAX_NAME], size_t *announced_count, const char *host_fqdn) {
    if (*announced_count >= SNAPSHOT_MAX_RECORDS) {
        return -1;
    }
    strncpy(announced_hosts[*announced_count], host_fqdn, MAX_NAME - 1);
    announced_hosts[*announced_count][MAX_NAME - 1] = '\0';
    (*announced_count)++;
    return 0;
}

static int send_announcement(int sockfd, const struct sockaddr_in *dest, const struct config *cfg,
                             const struct service_record_set *snapshot_records, int use_snapshot_records) {
    uint8_t buf[BUF_SIZE];
    size_t off;
    int answers;
    size_t i;
    char announced_hosts[SNAPSHOT_MAX_RECORDS][MAX_NAME];
    size_t announced_host_count = 0;
    static int logged_duplicate_host_suppression = 0;

    init_announcement_packet(&off, &answers);
    if (append_generated_base_records(buf, &off, sizeof(buf), cfg, &answers) != 0) {
        log_packet_build_failure("announcement", "add_core_records", off, answers, use_snapshot_records);
        return -1;
    }
    if (use_snapshot_records) {
        if (finalize_and_send_announcement_packet(sockfd, buf, off, answers, dest, use_snapshot_records) != 0) {
            return -1;
        }
        for (i = 0; i < snapshot_records->count; i++) {
            int include_host_a;
            size_t before_host_a_off;
            int before_host_a_answers;

            if (is_suppressed_snapshot_service_type(snapshot_records->records[i].service_type)) {
                continue;
            }
            init_announcement_packet(&off, &answers);
            if (add_service_record_answers(buf, &off, sizeof(buf), &snapshot_records->records[i], cfg->ttl, &answers) != 0) {
                log_snapshot_record_build_failure("announcement", "add_service_record_answers", i,
                                                  &snapshot_records->records[i], off, answers);
                log_packet_build_failure("announcement", "add_service_record_answers", off, answers, use_snapshot_records);
                return -1;
            }
            include_host_a = snapshot_records->records[i].host_fqdn[0] != '\0' &&
                             !host_already_announced(announced_hosts, announced_host_count,
                                                     snapshot_records->records[i].host_fqdn);
            if (include_host_a) {
                before_host_a_off = off;
                before_host_a_answers = answers;
                if (add_snapshot_host_a_record(buf, &off, sizeof(buf), &snapshot_records->records[i], cfg, &answers) != 0) {
                    off = before_host_a_off;
                    answers = before_host_a_answers;
                    if (finalize_and_send_announcement_packet(sockfd, buf, off, answers, dest, use_snapshot_records) != 0) {
                        return -1;
                    }
                    init_announcement_packet(&off, &answers);
                    if (add_snapshot_host_a_record(buf, &off, sizeof(buf), &snapshot_records->records[i], cfg, &answers) != 0) {
                        log_snapshot_record_build_failure("announcement", "add_snapshot_host_a_record", i,
                                                          &snapshot_records->records[i], off, answers);
                        log_packet_build_failure("announcement", "add_snapshot_host_a_record", off, answers, use_snapshot_records);
                        return -1;
                    }
                }
                if (remember_announced_host(announced_hosts, &announced_host_count,
                                            snapshot_records->records[i].host_fqdn) != 0) {
                    log_packet_build_failure("announcement", "remember_announced_host", off, answers, use_snapshot_records);
                    return -1;
                }
            } else if (snapshot_records->records[i].host_fqdn[0] != '\0' && !logged_duplicate_host_suppression) {
                fprintf(stderr,
                        "mdns snapshot host A suppression: host=%s service=%s instance=%s already announced earlier in this cycle\n",
                        snapshot_records->records[i].host_fqdn,
                        snapshot_records->records[i].service_type,
                        snapshot_records->records[i].instance_fqdn);
                logged_duplicate_host_suppression = 1;
            }
            if (finalize_and_send_announcement_packet(sockfd, buf, off, answers, dest, use_snapshot_records) != 0) {
                return -1;
            }
        }
    } else {
        size_t before_airport_off = off;
        int before_airport_answers = answers;
        if (add_airport_records(buf, &off, sizeof(buf), cfg, &answers) != 0) {
            off = before_airport_off;
            answers = before_airport_answers;
            if (finalize_and_send_announcement_packet(sockfd, buf, off, answers, dest, use_snapshot_records) != 0) {
                return -1;
            }
            init_announcement_packet(&off, &answers);
            if (add_airport_records(buf, &off, sizeof(buf), cfg, &answers) != 0) {
                log_packet_build_failure("announcement", "add_airport_records", off, answers, use_snapshot_records);
                return -1;
            }
        }
        if (finalize_and_send_announcement_packet(sockfd, buf, off, answers, dest, use_snapshot_records) != 0) {
            return -1;
        }
    }
    return 0;
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
        log_packet_build_failure("query_response", "build_instance_fqdn", off, answers, use_snapshot_records);
        return 0;
    }
    if (cfg->adisk_share_name[0] != '\0' && cfg->adisk_uuid[0] != '\0' &&
        build_instance_fqdn(adisk_instance_fqdn, sizeof(adisk_instance_fqdn), cfg->instance_name, cfg->adisk_service_type) != 0) {
        log_packet_build_failure("query_response", "build_adisk_instance_fqdn", off, answers, use_snapshot_records);
        return 0;
    }
    if (cfg->device_model[0] != '\0' &&
        build_instance_fqdn(device_info_instance_fqdn, sizeof(device_info_instance_fqdn), cfg->instance_name, cfg->device_info_service_type) != 0) {
        log_packet_build_failure("query_response", "build_device_info_instance_fqdn", off, answers, use_snapshot_records);
        return 0;
    }
    if (!use_snapshot_records && is_airport_enabled(cfg) &&
        build_instance_fqdn(airport_instance_fqdn, sizeof(airport_instance_fqdn), cfg->instance_name, cfg->airport_service_type) != 0) {
        log_packet_build_failure("query_response", "build_airport_instance_fqdn", off, answers, use_snapshot_records);
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
            log_packet_build_failure("query_response", "add_ptr", off, answers, use_snapshot_records);
            return -1;
        }
        answers++;
    }
    if (want_srv) {
        if (add_rr_srv(reply, &off, sizeof(reply), instance_fqdn, cfg->host_fqdn, cfg->port, cfg->ttl) != 0) {
            log_packet_build_failure("query_response", "add_srv", off, answers, use_snapshot_records);
            return -1;
        }
        answers++;
    }
    if (want_txt) {
        if (add_rr_txt_empty(reply, &off, sizeof(reply), instance_fqdn, cfg->ttl) != 0) {
            log_packet_build_failure("query_response", "add_txt", off, answers, use_snapshot_records);
            return -1;
        }
        answers++;
    }
    if (want_adisk_ptr || want_adisk_srv || want_adisk_txt) {
        char txt1[128];
        char txt2[256];
        const char *txts[2];

        if (build_adisk_system_txt(txt1, sizeof(txt1), cfg->adisk_sys_wama) != 0) {
            log_packet_build_failure("query_response", "build_adisk_system_txt", off, answers, use_snapshot_records);
            return -1;
        }
        if (build_adisk_disk_txt(txt2, sizeof(txt2), cfg->adisk_disk_key, cfg->adisk_share_name, cfg->adisk_uuid) != 0) {
            log_packet_build_failure("query_response", "build_adisk_disk_txt", off, answers, use_snapshot_records);
            return -1;
        }
        txts[0] = txt1;
        txts[1] = txt2;

        if (want_adisk_ptr) {
            if (add_rr_ptr(reply, &off, sizeof(reply), cfg->adisk_service_type, adisk_instance_fqdn, cfg->ttl) != 0) {
                log_packet_build_failure("query_response", "add_adisk_ptr", off, answers, use_snapshot_records);
                return -1;
            }
            answers++;
        }
        if (want_adisk_srv) {
            if (add_rr_srv(reply, &off, sizeof(reply), adisk_instance_fqdn, cfg->host_fqdn, cfg->adisk_port, cfg->ttl) != 0) {
                log_packet_build_failure("query_response", "add_adisk_srv", off, answers, use_snapshot_records);
                return -1;
            }
            answers++;
        }
        if (want_adisk_txt) {
            if (add_rr_txt_strings(reply, &off, sizeof(reply), adisk_instance_fqdn, cfg->ttl, txts, 2) != 0) {
                log_packet_build_failure("query_response", "add_adisk_txt", off, answers, use_snapshot_records);
                return -1;
            }
            answers++;
        }
    }
    if (want_device_info_ptr || want_device_info_srv || want_device_info_txt) {
        char model_txt[MAX_NAME + 16];
        const char *txts[1];

        if (build_model_txt(model_txt, sizeof(model_txt), cfg->device_model) != 0) {
            log_packet_build_failure("query_response", "build_model_txt", off, answers, use_snapshot_records);
            return -1;
        }
        txts[0] = model_txt;

        if (want_device_info_ptr) {
            if (add_rr_ptr(reply, &off, sizeof(reply), cfg->device_info_service_type, device_info_instance_fqdn, cfg->ttl) != 0) {
                log_packet_build_failure("query_response", "add_device_info_ptr", off, answers, use_snapshot_records);
                return -1;
            }
            answers++;
        }
        if (want_device_info_srv) {
            if (add_rr_srv(reply, &off, sizeof(reply), device_info_instance_fqdn, cfg->host_fqdn, 0, cfg->ttl) != 0) {
                log_packet_build_failure("query_response", "add_device_info_srv", off, answers, use_snapshot_records);
                return -1;
            }
            answers++;
        }
        if (want_device_info_txt) {
            if (add_rr_txt_strings(reply, &off, sizeof(reply), device_info_instance_fqdn, cfg->ttl, txts, 1) != 0) {
                log_packet_build_failure("query_response", "add_device_info_txt", off, answers, use_snapshot_records);
                return -1;
            }
            answers++;
        }
    }
    if (!use_snapshot_records && (want_airport_ptr || want_airport_srv || want_airport_txt)) {
        char airport_txt[256];
        const char *txts[1];

        if (build_airport_txt(airport_txt, sizeof(airport_txt), cfg) != 0) {
            log_packet_build_failure("query_response", "build_airport_txt", off, answers, use_snapshot_records);
            return -1;
        }
        txts[0] = airport_txt;

        if (want_airport_ptr) {
            if (add_rr_ptr(reply, &off, sizeof(reply), cfg->airport_service_type, airport_instance_fqdn, cfg->ttl) != 0) {
                log_packet_build_failure("query_response", "add_airport_ptr", off, answers, use_snapshot_records);
                return -1;
            }
            answers++;
        }
        if (want_airport_srv) {
            if (add_rr_srv(reply, &off, sizeof(reply), airport_instance_fqdn, cfg->host_fqdn, cfg->airport_port, cfg->ttl) != 0) {
                log_packet_build_failure("query_response", "add_airport_srv", off, answers, use_snapshot_records);
                return -1;
            }
            answers++;
        }
        if (want_airport_txt) {
            if (add_rr_txt_strings(reply, &off, sizeof(reply), airport_instance_fqdn, cfg->ttl, txts, 1) != 0) {
                log_packet_build_failure("query_response", "add_airport_txt", off, answers, use_snapshot_records);
                return -1;
            }
            answers++;
        }
    }
    if (want_a) {
        if (add_rr_a(reply, &off, sizeof(reply), cfg->host_fqdn, cfg->ipv4_addr, cfg->ttl) != 0) {
            log_packet_build_failure("query_response", "add_a", off, answers, use_snapshot_records);
            return -1;
        }
        answers++;
    }

    if (use_snapshot_records) {
        size_t j;
        for (j = 0; j < snapshot_records->count; j++) {
            const struct service_record *record = &snapshot_records->records[j];
            const char *txts[SNAPSHOT_MAX_TXT_ITEMS];
            uint8_t txt_lengths[SNAPSHOT_MAX_TXT_ITEMS];
            size_t k;

            if (is_suppressed_snapshot_service_type(record->service_type)) {
                continue;
            }
            for (k = 0; k < record->txt_count; k++) {
                txts[k] = record->txt[k];
                txt_lengths[k] = record->txt_len[k];
            }
            if (want_snapshot_ptr[j]) {
                if (add_rr_ptr(reply, &off, sizeof(reply), record->service_type, record->instance_fqdn, cfg->ttl) != 0) {
                    log_snapshot_record_build_failure("query_response", "add_snapshot_ptr", j, record, off, answers);
                    log_packet_build_failure("query_response", "add_snapshot_ptr", off, answers, use_snapshot_records);
                    return -1;
                }
                answers++;
            }
            if (want_snapshot_srv[j]) {
                if (add_rr_srv(reply, &off, sizeof(reply), record->instance_fqdn, record->host_fqdn, record->port, cfg->ttl) != 0) {
                    log_snapshot_record_build_failure("query_response", "add_snapshot_srv", j, record, off, answers);
                    log_packet_build_failure("query_response", "add_snapshot_srv", off, answers, use_snapshot_records);
                    return -1;
                }
                answers++;
            }
            if (want_snapshot_txt[j]) {
                if (record->txt_count > 0) {
                    if (add_rr_txt_items(reply, &off, sizeof(reply), record->instance_fqdn, cfg->ttl, txts, txt_lengths, record->txt_count) != 0) {
                        log_snapshot_record_build_failure("query_response", "add_snapshot_txt", j, record, off, answers);
                        log_packet_build_failure("query_response", "add_snapshot_txt", off, answers, use_snapshot_records);
                        return -1;
                    }
                } else {
                    if (add_rr_txt_empty(reply, &off, sizeof(reply), record->instance_fqdn, cfg->ttl) != 0) {
                        log_snapshot_record_build_failure("query_response", "add_snapshot_txt_empty", j, record, off, answers);
                        log_packet_build_failure("query_response", "add_snapshot_txt_empty", off, answers, use_snapshot_records);
                        return -1;
                    }
                }
                answers++;
            }
            if (want_snapshot_a[j]) {
                if (add_rr_a(reply, &off, sizeof(reply), record->host_fqdn, cfg->ipv4_addr, cfg->ttl) != 0) {
                    log_snapshot_record_build_failure("query_response", "add_snapshot_a", j, record, off, answers);
                    log_packet_build_failure("query_response", "add_snapshot_a", off, answers, use_snapshot_records);
                    return -1;
                }
                answers++;
            }
        }
    }

    hdr.ancount = htons((uint16_t)answers);
    memcpy(reply, &hdr, sizeof(hdr));

    return send_dns_packet("query_response", sockfd, reply, off, dest, answers, use_snapshot_records);
}

static int open_mdns_socket(int shared_bind, int log_bind_errors, uint32_t ipv4_addr, const char *socket_role) {
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

    if (shared_bind) {
#ifdef SO_REUSEPORT
        (void)setsockopt(sockfd, SOL_SOCKET, SO_REUSEPORT, &yes, sizeof(yes));
#endif
    }

    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(MDNS_PORT);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(sockfd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        if (log_bind_errors) {
            perror("bind");
        }
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

    if (configure_outbound_multicast_socket(sockfd, ipv4_addr, socket_role) != 0) {
        close(sockfd);
        return -1;
    }

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
    int shared_bind = 0;
    static const unsigned int startup_burst_offsets_ms[STARTUP_BURST_COUNT] = {0, 250, 1000, 2000, 3000, 4000, 5000};
    size_t startup_burst_index = 0;
    long long startup_burst_start_ms = 0;

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
        if (strcmp(argv[i], "--save-all-snapshot") == 0 && i + 1 < argc) {
            strncpy(cfg.save_all_snapshot_path, argv[++i], sizeof(cfg.save_all_snapshot_path) - 1);
        } else if (strcmp(argv[i], "--save-snapshot") == 0 && i + 1 < argc) {
            strncpy(cfg.save_snapshot_path, argv[++i], sizeof(cfg.save_snapshot_path) - 1);
        } else if (strcmp(argv[i], "--load-snapshot") == 0 && i + 1 < argc) {
            strncpy(cfg.load_snapshot_path, argv[++i], sizeof(cfg.load_snapshot_path) - 1);
        } else if (strcmp(argv[i], "--shared-bind") == 0) {
            shared_bind = 1;
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
                return EXIT_INVALID_IPV4;
            }
        } else if (strcmp(argv[i], "--port") == 0 && i + 1 < argc) {
            cfg.port = (uint16_t)atoi(argv[++i]);
        } else if (strcmp(argv[i], "--ttl") == 0 && i + 1 < argc) {
            cfg.ttl = (uint32_t)atoi(argv[++i]);
        } else {
            usage(argv[0]);
            return EXIT_USAGE;
        }
    }

    if (cfg.instance_name[0] == '\0' || cfg.host_label[0] == '\0' || cfg.ipv4_addr == 0) {
        usage(argv[0]);
        return EXIT_MISSING_REQUIRED_ARGS;
    }

    if (validate_single_dns_label(cfg.instance_name, "instance name") != 0 ||
        validate_single_dns_label(cfg.host_label, "host label") != 0) {
        return EXIT_INVALID_DNS_LABEL;
    }
    if (validate_dns_name(cfg.service_type, "service type") != 0) {
        return EXIT_INVALID_SERVICE_TYPE;
    }
    if (cfg.adisk_share_name[0] != '\0') {
        char adisk_sys_txt[128];
        char adisk_disk_txt[256];
        if (build_adisk_system_txt(adisk_sys_txt, sizeof(adisk_sys_txt), cfg.adisk_sys_wama) != 0) {
            return EXIT_INVALID_ADISK_SYSTEM;
        }
        if (build_adisk_disk_txt(adisk_disk_txt, sizeof(adisk_disk_txt), cfg.adisk_disk_key, cfg.adisk_share_name, cfg.adisk_uuid) != 0) {
            return EXIT_INVALID_ADISK_DISK;
        }
    }
    if (cfg.device_model[0] != '\0') {
        char model_txt[MAX_NAME + 16];
        if (build_model_txt(model_txt, sizeof(model_txt), cfg.device_model) != 0) {
            return EXIT_INVALID_DEVICE_MODEL;
        }
    }
    if (cfg.airport_wama[0] != '\0' || cfg.airport_rama[0] != '\0' || cfg.airport_ram2[0] != '\0' ||
        cfg.airport_rast[0] != '\0' || cfg.airport_rana[0] != '\0' || cfg.airport_syfl[0] != '\0' ||
        cfg.airport_syap[0] != '\0' || cfg.airport_syvs[0] != '\0' || cfg.airport_srcv[0] != '\0' ||
        cfg.airport_bjsd[0] != '\0') {
        char airport_txt[256];
        if (build_airport_txt(airport_txt, sizeof(airport_txt), &cfg) != 0) {
            return EXIT_INVALID_AIRPORT_TXT;
        }
    }

    snprintf(cfg.host_fqdn, sizeof(cfg.host_fqdn), "%s.local.", cfg.host_label);
    log_startup_config(&cfg, shared_bind);

    if (cfg.save_all_snapshot_path[0] != '\0' || cfg.save_snapshot_path[0] != '\0') {
        struct service_record_set captured_records;
        struct service_record_set filtered_records;
        memset(&captured_records, 0, sizeof(captured_records));
        memset(&filtered_records, 0, sizeof(filtered_records));
        if (capture_mdns_snapshot_raw_with_retry(&captured_records) == 0) {
            fprintf(stderr, "snapshot capture: captured %lu records\n", (unsigned long)captured_records.count);
            if (cfg.save_all_snapshot_path[0] != '\0' &&
                write_snapshot_file_atomic(cfg.save_all_snapshot_path, &captured_records) != 0) {
                fprintf(stderr, "failed to write all snapshot file: %s\n", cfg.save_all_snapshot_path);
            }
            /*
             * Keep a raw LAN-wide dump in allmdns.txt for diagnostics, but
             * only refresh applemdns.txt when the capture can be tied back to
             * this unit's _airport identity.
             */
            if (cfg.save_snapshot_path[0] != '\0') {
                if (prepare_loaded_snapshot_for_advertising(&cfg, &captured_records, &filtered_records) == 0) {
                    fprintf(stderr, "snapshot capture: filtered %lu records for trusted snapshot\n",
                            (unsigned long)filtered_records.count);
                    if (write_snapshot_file_atomic(cfg.save_snapshot_path, &filtered_records) != 0) {
                        fprintf(stderr, "failed to write snapshot file: %s\n", cfg.save_snapshot_path);
                    }
                } else {
                    fprintf(stderr, "warning: could not identify local Apple mDNS records for snapshot file: %s\n",
                            cfg.save_snapshot_path);
                }
            }
        } else {
            fprintf(stderr, "warning: could not capture Apple mDNS snapshot\n");
        }
    }

    if (cfg.load_snapshot_path[0] != '\0') {
        struct service_record_set loaded_records;
        memset(&loaded_records, 0, sizeof(loaded_records));
        if (load_snapshot_file(cfg.load_snapshot_path, &loaded_records) == 0 &&
            prepare_loaded_snapshot_for_advertising(&cfg, &loaded_records, &snapshot_records) == 0) {
            fprintf(stderr, "snapshot load: loaded %lu records, advertising %lu snapshot records\n",
                    (unsigned long)loaded_records.count, (unsigned long)snapshot_records.count);
            use_snapshot_records = 1;
        } else {
            fprintf(stderr, "warning: could not load trusted snapshot file: %s; falling back to generated records\n",
                    cfg.load_snapshot_path);
        }
    }

    log_served_records(&cfg, &snapshot_records, use_snapshot_records);

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    sockfd = acquire_mdns_socket(shared_bind, cfg.ipv4_addr);
    if (sockfd < 0) {
        return EXIT_SOCKET_ACQUIRE_FAILED;
    }

    memset(&mdns_dest, 0, sizeof(mdns_dest));
    mdns_dest.sin_family = AF_INET;
    mdns_dest.sin_port = htons(MDNS_PORT);
    mdns_dest.sin_addr.s_addr = inet_addr(MDNS_GROUP);

    startup_burst_start_ms = monotonic_millis();
    last_announce = time(NULL);

    while (!g_stop) {
        fd_set rfds;
        struct timeval tv;
        uint8_t packet[BUF_SIZE];
        ssize_t nread;
        long long now_ms;
        long long next_burst_ms = -1;
        long long wait_ms = 1000;

        now_ms = monotonic_millis();
        while (startup_burst_index < STARTUP_BURST_COUNT &&
               now_ms - startup_burst_start_ms >= (long long)startup_burst_offsets_ms[startup_burst_index]) {
            if (send_announcement(sockfd, &mdns_dest, &cfg, &snapshot_records, use_snapshot_records) != 0) {
                char detail[96];
                snprintf(detail, sizeof(detail), "burst_index=%lu offset_ms=%u",
                         (unsigned long)startup_burst_index, startup_burst_offsets_ms[startup_burst_index]);
                log_send_failure("startup_announce", &mdns_dest, use_snapshot_records, detail);
            }
            startup_burst_index++;
            now_ms = monotonic_millis();
        }

        FD_ZERO(&rfds);
        FD_SET(sockfd, &rfds);
        if (startup_burst_index < STARTUP_BURST_COUNT) {
            next_burst_ms = startup_burst_start_ms + (long long)startup_burst_offsets_ms[startup_burst_index];
            wait_ms = next_burst_ms - now_ms;
            if (wait_ms < 0) {
                wait_ms = 0;
            } else if (wait_ms > 1000) {
                wait_ms = 1000;
            }
        }
        tv.tv_sec = (time_t)(wait_ms / 1000);
        tv.tv_usec = (suseconds_t)((wait_ms % 1000) * 1000);

        if (select(sockfd + 1, &rfds, NULL, NULL, &tv) > 0 && FD_ISSET(sockfd, &rfds)) {
            struct sockaddr_in src;
            socklen_t src_len = sizeof(src);
            nread = recvfrom(sockfd, packet, sizeof(packet), 0, (struct sockaddr *)&src, &src_len);
            if (nread > 0) {
                if (handle_query(sockfd, packet, (size_t)nread, &mdns_dest, &cfg, &snapshot_records, use_snapshot_records) != 0) {
                    char detail[128];
                    snprintf(detail, sizeof(detail), "packet_len=%ld from=%s:%u",
                             (long)nread, inet_ntoa(src.sin_addr), (unsigned int)ntohs(src.sin_port));
                    log_send_failure("query_response", &mdns_dest, use_snapshot_records, detail);
                }
            }
        }

        if (time(NULL) - last_announce >= ANNOUNCE_INTERVAL) {
            if (send_announcement(sockfd, &mdns_dest, &cfg, &snapshot_records, use_snapshot_records) != 0) {
                char detail[96];
                snprintf(detail, sizeof(detail), "interval=%d last_announce_age=%ld",
                         ANNOUNCE_INTERVAL, (long)(time(NULL) - last_announce));
                log_send_failure("periodic_announce", &mdns_dest, use_snapshot_records, detail);
            }
            last_announce = time(NULL);
        }
    }

    close(sockfd);
    return 0;
}
