#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <net/if.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/types.h>
#if defined(__APPLE__) || defined(__NetBSD__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__DragonFly__)
#include <sys/sysctl.h>
#endif
#if defined(__NetBSD__)
#include <dev/usb/usb.h>
#endif
#include <time.h>
#include <unistd.h>

#ifndef MDNS_PORT
#define MDNS_PORT 5353
#endif
#define MDNS_GROUP "224.0.0.251"
#define MDNS_GROUP_V6 "ff02::fb"
#define BUF_SIZE 1500
#define MAX_NAME 256
#define MAX_LABEL 63
#define MAX_TXT_STRING 255
#define STARTUP_BURST_COUNT 4
#define MODEL_TXT_PREFIX "model="
#define ADISK_DEFAULT_DISK_KEY "dk0"
#define ADISK_SYS_ADVF "0x1010"
#define ADISK_DEFAULT_DISK_ADVF "0x1093"
#define ADISK_MAX_DISKS 16
#define ADISK_DISK_UUID_LEN 36
#define AFP_SERVICE_TYPE "_afpovertcp._tcp.local."
#define AFP_DEFAULT_PORT 548
#define AIRPORT_SERVICE_TYPE "_airport._tcp.local."
#define AIRPORT_DEFAULT_PORT 5009
#define RIOUSBPRINT_SERVICE_TYPE "_riousbprint._tcp.local."
#define RIOUSBPRINT_DEFAULT_PORT 10000
#define PDL_DATASTREAM_SERVICE_TYPE "_pdl-datastream._tcp.local."
#define PDL_DATASTREAM_DEFAULT_PORT 9100
#define AIRPORT_USB_PRINTER_MAX_TXT_ITEMS 12
#define RIOUSBPRINT_MAX_TXT_ITEMS AIRPORT_USB_PRINTER_MAX_TXT_ITEMS
#define PDL_DATASTREAM_MAX_TXT_ITEMS AIRPORT_USB_PRINTER_MAX_TXT_ITEMS
#define IEEE1284_DEVICE_ID_MAX 1024
#define ADISK_SYS_TXT_PREFIX "sys=waMA="
#define ADISK_SYS_TXT_SUFFIX ",adVF=" ADISK_SYS_ADVF
#define ADISK_DISK_TXT_ADVF_PREFIX "=adVF="
#define ADISK_DISK_TXT_ADVN_MID ",adVN="
#define ADISK_DISK_TXT_SUFFIX ",adVU="
#define SNAPSHOT_MAX_RECORDS 64
#define SNAPSHOT_MAX_TXT_ITEMS 16
#define SNAPSHOT_LINE_MAX 1024
#define SNAPSHOT_MAX_SERVICE_TYPES 64
#ifndef SNAPSHOT_CAPTURE_TIMEOUT_SECONDS
#define SNAPSHOT_CAPTURE_TIMEOUT_SECONDS 60
#endif
#ifndef SNAPSHOT_CAPTURE_RETRY_INTERVAL_SECONDS
#define SNAPSHOT_CAPTURE_RETRY_INTERVAL_SECONDS 5
#endif
#ifndef SNAPSHOT_CAPTURE_STEP_SECONDS
#define SNAPSHOT_CAPTURE_STEP_SECONDS 5
#endif
#define TAKEOVER_RETRY_COUNT 6
#define MAX_IFACE_CONTEXTS 16
#define AUTO_IP_STABILIZE_SECONDS 3
#define AUTO_IP_STARTUP_POLL_SECONDS 2
#define AUTO_IP_STABLE_POLL_SECONDS 30
#define MDNS_DEGRADED_RETRY_SECONDS 5
#define MDNS_MDNSRESPONDER_GUARD_SECONDS 5
#define MDNS_COUNTER_LOG_INTERVAL_MS 30000
#define ADVERTISER_VERSION_CODE 2220
#define NT_HASH_MAX_PASSWORD_BYTES 4096
#define DNS_SD_SERVICE_ENUMERATION_NAME "_services._dns-sd._udp.local."

#define DNS_TYPE_A 1
#define DNS_TYPE_PTR 12
#define DNS_TYPE_TXT 16
#define DNS_TYPE_AAAA 28
#define DNS_TYPE_SRV 33
#define DNS_TYPE_ANY 255
#define DNS_CLASS_IN 1
#define DNS_CLASS_ANY 255
#define DNS_CLASS_CACHE_FLUSH 0x8000
#define DNS_CLASS_QU 0x8000
#define DNS_CLASS_IN_UNIQUE (DNS_CLASS_IN | DNS_CLASS_CACHE_FLUSH)
#define MDNS_REPLY_UNICAST 1
#define MDNS_REPLY_MULTICAST 2
#define MDNS_REPLY_LEGACY_UNICAST 4
#define DNS_FLAG_QR 0x8000
#define DNS_FLAG_TC 0x0200
#define DNS_FLAG_AA 0x0400
#define LEGACY_UNICAST_TTL_MAX 10
#define TC_KNOWN_ANSWER_DEFER_MS 450
#define MDNS_MULTICAST_RESPONSE_DELAY_MIN_MS 20
#define MDNS_MULTICAST_RESPONSE_DELAY_MAX_MS 120
#define PLANNED_RR_MAX 192
#define PLANNED_RDATA_MAX 1024

#if !defined(IPV6_JOIN_GROUP) && defined(IPV6_ADD_MEMBERSHIP)
#define IPV6_JOIN_GROUP IPV6_ADD_MEMBERSHIP
#endif
#if !defined(IPV6_LEAVE_GROUP) && defined(IPV6_DROP_MEMBERSHIP)
#define IPV6_LEAVE_GROUP IPV6_DROP_MEMBERSHIP
#endif

static volatile sig_atomic_t g_stop = 0;

#ifndef TC_VA_COPY
#if defined(va_copy)
#define TC_VA_COPY(dst, src) va_copy(dst, src)
#elif defined(__va_copy)
#define TC_VA_COPY(dst, src) __va_copy(dst, src)
#else
#define TC_VA_COPY(dst, src) memcpy(&(dst), &(src), sizeof(va_list))
#endif
#endif
#if defined(__GNUC__)
#define TC_UNUSED __attribute__((unused))
#else
#define TC_UNUSED
#endif

static void log_timestamp_prefix(FILE *stream) {
    time_t now;
    struct tm *tm_info;
    char stamp[32];

    now = time(NULL);
    tm_info = localtime(&now);
    if (tm_info != NULL && strftime(stamp, sizeof(stamp), "%Y-%m-%d %H:%M:%S", tm_info) > 0) {
        fputs(stamp, stream);
        fputc(' ', stream);
    }
}

static int timestamped_write_message(FILE *stream, const char *message) {
    const char *cursor;

    cursor = message;
    while (*cursor != '\0') {
        log_timestamp_prefix(stream);
        while (*cursor != '\0') {
            int ch = (unsigned char)*cursor++;
            if (fputc(ch, stream) == EOF) {
                return -1;
            }
            if (ch == '\n') {
                break;
            }
        }
    }
    return 0;
}

static int timestamped_vfprintf(FILE *stream, const char *format, va_list ap) {
    char stack_message[4096];
    int result;

    if (stream != stderr && stream != stdout) {
        return vfprintf(stream, format, ap);
    }

    result = vsnprintf(stack_message, sizeof(stack_message), format, ap);
    if (result < 0) {
        return result;
    }
    if ((size_t)result >= sizeof(stack_message)) {
        stack_message[sizeof(stack_message) - 2] = '\n';
        stack_message[sizeof(stack_message) - 1] = '\0';
    }

    if (timestamped_write_message(stream, stack_message) != 0) {
        return -1;
    }
    fflush(stream);
    return result;
}

static int timestamped_fprintf(FILE *stream, const char *format, ...) {
    va_list ap;
    int result;

    va_start(ap, format);
    result = timestamped_vfprintf(stream, format, ap);
    va_end(ap);
    return result;
}

static void timestamped_perror(const char *message) {
    int saved_errno = errno;

    if (message != NULL && message[0] != '\0') {
        timestamped_fprintf(stderr, "%s: %s\n", message, strerror(saved_errno));
    } else {
        timestamped_fprintf(stderr, "%s\n", strerror(saved_errno));
    }
}

#define fprintf timestamped_fprintf
#define perror timestamped_perror

static ssize_t sendto_retry(int sockfd, const void *buf, size_t len, int flags,
                            const struct sockaddr *dest, socklen_t dest_len) {
    ssize_t sent;

    do {
        sent = sendto(sockfd, buf, len, flags, dest, dest_len);
    } while (sent < 0 && errno == EINTR);

    return sent;
}

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
    EXIT_INVALID_AIRPORT_TXT = 10,
    EXIT_AUTO_IP_UNAVAILABLE = 11,
    EXIT_SNAPSHOT_CAPTURE_FAILED = 12,
    EXIT_AUTO_IP_PROBE_FAILED = 13,
    EXIT_SNAPSHOT_NOT_FRESH = 14
};

struct adisk_disk {
    char share_name[MAX_NAME];
    char disk_key[MAX_LABEL + 1];
    char disk_advf[16];
    char uuid[ADISK_DISK_UUID_LEN + 1];
};

struct adisk_disk_set {
    struct adisk_disk disks[ADISK_MAX_DISKS];
    size_t count;
};

struct config {
    char save_all_snapshot_path[MAX_NAME];
    char save_airport_snapshot_path[MAX_NAME];
    char service_type[MAX_NAME];
    char instance_name[MAX_NAME];
    char host_label[MAX_LABEL + 1];
    char host_fqdn[MAX_NAME];
    char adisk_service_type[MAX_NAME];
    char adisk_shares_file[MAX_NAME];
    char adisk_sys_wama[18];
    struct adisk_disk_set adisk_disks;
    char afp_service_type[MAX_NAME];
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
    char riousbprint_instance_name[MAX_NAME];
    char riousbprint_note[MAX_NAME];
    char riousbprint_mfg[64];
    char riousbprint_mdl[128];
    char riousbprint_serial[128];
    char riousbprint_cmd[MAX_TXT_STRING + 1];
    unsigned int riousbprint_vendor_id;
    unsigned int riousbprint_product_id;
    uint16_t port;
    uint16_t adisk_port;
    uint16_t afp_port;
    uint16_t airport_port;
    uint16_t riousbprint_port;
    uint16_t pdl_datastream_port;
    uint32_t ttl;
    int diskless;
    int advertise_afp;
    int generated_airport_services;
    char load_snapshot_path[MAX_NAME];
    char save_snapshot_path[MAX_NAME];
    char skip_capture_if_snapshot_newer_than_boot_path[MAX_NAME];
    char snapshot_newer_than_boot_path[MAX_NAME];
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
    int truncated;
};

struct service_type_set {
    char types[SNAPSHOT_MAX_SERVICE_TYPES][MAX_NAME];
    size_t count;
};

struct mdns_socket_pair {
    int ipv4_fd;
    int ipv6_fd;
};

struct mdns_membership_delta {
    uint32_t ipv4[MAX_IFACE_CONTEXTS];
    size_t ipv4_count;
    unsigned int ipv6_ifindex[MAX_IFACE_CONTEXTS];
    char ipv6_name[MAX_IFACE_CONTEXTS][IFNAMSIZ];
    size_t ipv6_count;
};


struct planned_rr {
    char owner[MAX_NAME];
    uint16_t type;
    uint16_t rrclass;
    uint32_t ttl;
    uint8_t rdata[PLANNED_RDATA_MAX];
    uint16_t rdlength;
    int routes;
};

struct planned_rr_set {
    struct planned_rr records[PLANNED_RR_MAX];
    size_t count;
    int truncated;
};

struct response_question_section {
    const uint8_t *bytes;
    size_t len;
    uint16_t count;
};

struct stored_question_section {
    uint8_t bytes[BUF_SIZE];
    size_t len;
    uint16_t count;
};

struct deferred_response {
    int active;
    int sockfd;
    long long due_ms;
    uint16_t response_id;
    int use_snapshot_records;
    struct sockaddr_storage multicast_dest;
    socklen_t multicast_dest_len;
    struct sockaddr_storage source;
    socklen_t source_len;
    struct stored_question_section questions;
    struct planned_rr_set planned;
};

struct mdns_transport_requirements {
    int ipv4_required;
    int ipv6_required;
};

struct mdns_transport_status {
    int required_ipv4;
    int required_ipv6;
    int active_ipv4;
    int active_ipv6;
    int missing_required_ipv4;
    int missing_required_ipv6;
    int last_ipv4_errno;
    int last_ipv6_errno;
};

struct mdns_runtime_counters {
    unsigned long ipv4_packets_received;
    unsigned long ipv6_packets_received;
    unsigned long query_packets_matched;
    unsigned long responses_sent;
    unsigned long send_failures;
    char last_send_failure[160];
};

struct mdns_counter_log_state {
    unsigned long ipv4_packets_received;
    unsigned long ipv6_packets_received;
    unsigned long query_packets_matched;
    unsigned long responses_sent;
    unsigned long send_failures;
    long long last_log_ms;
    int logged_ipv4_packet;
    int logged_ipv6_packet;
    int logged_query_match;
};

static struct deferred_response g_deferred_response;
static struct mdns_runtime_counters g_mdns_counters;
static struct mdns_counter_log_state g_mdns_counter_log_state;
static int g_last_ipv4_socket_errno = 0;
static int g_last_ipv6_socket_errno = 0;
static int g_debug_logging = 0;
static const unsigned int g_startup_burst_offsets_ms[STARTUP_BURST_COUNT] = {0, 1000, 3000, 7000};

static long long monotonic_millis(void);
static int name_equals(const char *a, const char *b);
static int escape_dns_label(char *out, size_t out_len, const char *label);
static int unescape_dns_label(char *out, size_t out_len, const char *label);
static int build_instance_fqdn(char *out, size_t out_len, const char *instance_name, const char *service_type);
static int build_host_fqdn(char *out, size_t out_len, const char *host_label);
static int is_airport_enabled(const struct config *cfg);
static int is_riousbprint_enabled(const struct config *cfg);
static int is_pdl_datastream_enabled(const struct config *cfg);
static int snapshot_record_overridden_by_generated(const struct config *cfg, const struct service_record *record);
static int smb_enabled(const struct config *cfg);
static int adisk_enabled(const struct config *cfg);
static int afp_enabled(const struct config *cfg);
static int cfg_has_airport_identity_macs(const struct config *cfg);
static int add_rr_ptr(uint8_t *buf, size_t *off, size_t cap, const char *owner, const char *target, uint32_t ttl);
static int add_rr_txt_empty(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ttl);
static int add_rr_txt_items(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ttl,
                            const char **strings, const uint8_t *lengths, size_t string_count);
static int add_rr_txt_strings(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ttl,
                              const char **strings, size_t string_count);
static int add_rr_srv(uint8_t *buf, size_t *off, size_t cap, const char *owner, const char *target, uint16_t port, uint32_t ttl);
static int add_rr_a(uint8_t *buf, size_t *off, size_t cap, const char *owner, uint32_t ipv4_addr, uint32_t ttl);
static int add_rr_aaaa(uint8_t *buf, size_t *off, size_t cap, const char *owner, const struct in6_addr *ipv6_addr, uint32_t ttl);

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

#include "auto-ip-common.inc"

static uint32_t link_preferred_ipv4_source(const struct link_context *link);
static int link_set_has_ipv4_membership(const struct link_context_set *set, uint32_t ipv4_addr);
static int source_matches_link_ipv4_subnet(uint32_t source_ipv4_addr, const struct link_context *link);
static int flush_deferred_response_if_due(long long now_ms);
static long long deferred_response_adjust_wait_ms(long long now_ms, long long wait_ms);
static void clear_deferred_response_for_sockfd(int sockfd);

typedef int (*mdns_collect_link_contexts_fn)(struct link_context_set *out, void *userdata);
typedef void (*mdns_sleep_fn)(unsigned int seconds, void *userdata);

static int collect_usable_link_contexts_provider(struct link_context_set *out, void *userdata) {
    (void)userdata;
    return collect_usable_link_contexts(out);
}

static int collect_usable_advertise_link_contexts_provider(struct link_context_set *out, void *userdata) {
    struct link_context_set all_links;

    (void)userdata;
    memset(&all_links, 0, sizeof(all_links));
    if (collect_usable_link_contexts(&all_links) != 0) {
        return -1;
    }
    filter_advertise_link_contexts(out, &all_links);
    return 0;
}

static void mdns_sleep_provider(unsigned int seconds, void *userdata) {
    (void)userdata;
    sleep(seconds);
}

static int wait_for_auto_link_contexts_with_provider(struct link_context_set *out,
                                                     const char *role,
                                                     mdns_collect_link_contexts_fn collect_contexts,
                                                     mdns_sleep_fn sleep_fn,
                                                     void *userdata) {
    struct link_context_set first;

    if (collect_contexts == NULL || sleep_fn == NULL) {
        return -1;
    }

    memset(out, 0, sizeof(*out));
    while (!g_stop) {
        memset(&first, 0, sizeof(first));
        if (collect_contexts(&first, userdata) == 0 && first.count > 0) {
            fprintf(stderr, "%s auto-ip: first usable address link observed; waiting %ds for network stabilization\n",
                    role, AUTO_IP_STABILIZE_SECONDS);
            sleep_fn(AUTO_IP_STABILIZE_SECONDS, userdata);
            if (collect_contexts(out, userdata) == 0 && out->count > 0) {
                sort_link_contexts(out);
                return 0;
            }
            fprintf(stderr, "%s auto-ip: usable address links disappeared during stabilization; retrying\n", role);
        }
        sleep_fn(AUTO_IP_STARTUP_POLL_SECONDS, userdata);
    }
    return -1;
}

static int wait_for_auto_link_contexts(struct link_context_set *out, const char *role) {
    return wait_for_auto_link_contexts_with_provider(out,
                                                    role,
                                                    collect_usable_link_contexts_provider,
                                                    mdns_sleep_provider,
                                                    NULL);
}

static int wait_for_auto_advertise_link_contexts(struct link_context_set *out, const char *role) {
    return wait_for_auto_link_contexts_with_provider(out,
                                                    role,
                                                    collect_usable_advertise_link_contexts_provider,
                                                    mdns_sleep_provider,
                                                    NULL);
}

static int print_link_ipv4_cidrs(FILE *stream, const struct link_context_set *set) {
    int wrote = 0;
    size_t i;

    for (i = 0; i < set->count; i++) {
        size_t j;
        const struct link_context *link = &set->links[i];

        for (j = 0; j < link->ipv4_count; j++) {
            char cidr[INET_ADDRSTRLEN + 4];

            if (link_context_ipv4_cidr(cidr, sizeof(cidr), &link->ipv4[j]) != 0) {
                return -1;
            }
            if (wrote && fputc(' ', stream) == EOF) {
                return -1;
            }
            if (fputs(cidr, stream) == EOF) {
                return -1;
            }
            wrote = 1;
        }
    }
    if (!wrote) {
        return -1;
    }
    if (fputc('\n', stream) == EOF) {
        return -1;
    }
    return 0;
}

static int link_contexts_have_ipv4_addr(const struct link_context_set *set) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (set->links[i].ipv4_count > 0) {
            return 1;
        }
    }
    return 0;
}

static int print_auto_ip_cidrs_with_provider(FILE *stream,
                                             mdns_collect_link_contexts_fn collect_contexts,
                                             void *userdata) {
    struct link_context_set links;

    if (collect_contexts == NULL) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }

    memset(&links, 0, sizeof(links));
    if (collect_contexts(&links, userdata) != 0) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    if (links.count == 0 || !link_contexts_have_ipv4_addr(&links)) {
        return EXIT_AUTO_IP_UNAVAILABLE;
    }
    if (links.truncated) {
        fprintf(stderr, "auto-ip: usable address link list exceeded static capacity\n");
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    sort_link_contexts(&links);
    if (print_link_ipv4_cidrs(stream, &links) != 0) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    return EXIT_OK;
}

static int link_contexts_need_ipv4_socket(const struct link_context_set *set);
static int link_contexts_need_ipv6_socket(const struct link_context_set *set);

static void filter_smb_bind_link_contexts(struct link_context_set *out,
                                          const struct link_context_set *in,
                                          int lan_only,
                                          int unnamed_lan_fallback) {
    size_t i;

    memset(out, 0, sizeof(*out));
    for (i = 0; i < in->count; i++) {
        if (lan_only) {
            if (unnamed_lan_fallback) {
                if (!link_context_is_unnamed_private_lan_fallback(&in->links[i])) {
                    continue;
                }
            } else if (!iface_name_is_strong_lan(in->links[i].name)) {
                continue;
            }
        }
        if (!link_context_has_samba_address(&in->links[i])) {
            continue;
        }
        if (out->count >= MAX_IFACE_CONTEXTS) {
            out->truncated = 1;
            break;
        }
        out->links[out->count++] = in->links[i];
    }
    sort_link_contexts(out);
}

static int print_smb_bind_interfaces_with_policy(FILE *stream,
                                                 mdns_collect_link_contexts_fn collect_contexts,
                                                 void *userdata,
                                                 int lan_only) {
    struct link_context_set all_links;
    struct link_context_set bind_links;

    if (collect_contexts == NULL) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }

    memset(&all_links, 0, sizeof(all_links));
    memset(&bind_links, 0, sizeof(bind_links));
    if (collect_contexts(&all_links, userdata) != 0) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    if (all_links.count == 0) {
        return EXIT_AUTO_IP_UNAVAILABLE;
    }
    if (all_links.truncated) {
        fprintf(stderr, "auto-ip: Samba bind interface list exceeded static capacity\n");
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    filter_smb_bind_link_contexts(&bind_links, &all_links, lan_only, 0);
    if (lan_only && bind_links.count == 0) {
        /*
         * Some NetBSD 4 Time Capsules expose getifaddrs address rows without
         * interface names, so bridge0 appears as synthetic ip4-* links. In
         * that case keep LAN-only mode working by falling back to private LAN
         * Samba bind candidates instead of deferring startup forever.
         */
        filter_smb_bind_link_contexts(&bind_links, &all_links, lan_only, 1);
    }
    if (bind_links.count == 0) {
        return EXIT_AUTO_IP_UNAVAILABLE;
    }
    if (bind_links.truncated) {
        fprintf(stderr, "auto-ip: Samba bind interface list exceeded static capacity\n");
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    if (print_smb_link_bind_tokens(stream, &bind_links) != 0) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    return EXIT_OK;
}

static int print_smb_bind_interfaces_with_provider(FILE *stream,
                                                   mdns_collect_link_contexts_fn collect_contexts,
                                                   void *userdata) {
    return print_smb_bind_interfaces_with_policy(stream, collect_contexts, userdata, 0);
}

static int print_smb_bind_interfaces_lan_with_provider(FILE *stream,
                                                       mdns_collect_link_contexts_fn collect_contexts,
                                                       void *userdata) {
    return print_smb_bind_interfaces_with_policy(stream, collect_contexts, userdata, 1);
}

static int print_mdns_socket_families_with_provider(FILE *stream,
                                                    mdns_collect_link_contexts_fn collect_contexts,
                                                    void *userdata) {
    struct link_context_set all_links;
    struct link_context_set links;
    int need_ipv4;
    int need_ipv6;

    if (collect_contexts == NULL) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }

    memset(&all_links, 0, sizeof(all_links));
    memset(&links, 0, sizeof(links));
    if (collect_contexts(&all_links, userdata) != 0) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    if (all_links.count == 0) {
        return EXIT_AUTO_IP_UNAVAILABLE;
    }
    filter_advertise_link_contexts(&links, &all_links);
    if (all_links.truncated || links.truncated) {
        fprintf(stderr, "auto-ip: mDNS socket family link list exceeded static capacity\n");
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    if (links.count == 0) {
        return EXIT_AUTO_IP_UNAVAILABLE;
    }
    sort_link_contexts(&links);
    need_ipv4 = link_contexts_need_ipv4_socket(&links);
    need_ipv6 = link_contexts_need_ipv6_socket(&links);
    if (!need_ipv4 && !need_ipv6) {
        return EXIT_AUTO_IP_UNAVAILABLE;
    }
    if (need_ipv4 && fputs("ipv4", stream) == EOF) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    if (need_ipv6) {
        if (need_ipv4 && fputc(' ', stream) == EOF) {
            return EXIT_AUTO_IP_PROBE_FAILED;
        }
        if (fputs("ipv6", stream) == EOF) {
            return EXIT_AUTO_IP_PROBE_FAILED;
        }
    }
    if (fputc('\n', stream) == EOF) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    return EXIT_OK;
}

static int open_dualstack_mdns_sockets(int shared_bind,
                                       struct link_context_set *links,
                                       int log_bind_errors,
                                       struct mdns_socket_pair *out);
static void close_mdns_socket_pair(struct mdns_socket_pair *sockets);
static int set_outbound_multicast_interface(int sockfd, uint32_t ipv4_addr, const char *socket_role,
                                            int log_success, int log_errors);
static int set_outbound_multicast_interface6(int sockfd, unsigned int ifindex, const char *socket_role,
                                             int log_success, int log_errors);
static void drop_mdns_multicast_group_best_effort(int sockfd, uint32_t ipv4_addr, const char *socket_role);
static void drop_mdns_multicast_group6_best_effort(int sockfd, unsigned int ifindex, const char *ifname,
                                                   const char *socket_role);

static void log_startup_config(const struct config *cfg) {
    fprintf(stderr,
            "mdns startup: mode=%s instance=%s host=%s ipv4=%s service=%s afp=%s adisk=%s device_model=%s airport=%s advertise=%s\n",
            "exclusive",
            cfg->instance_name[0] != '\0' ? cfg->instance_name : "(empty)",
            cfg->host_label[0] != '\0' ? cfg->host_label : "(empty)",
            "auto",
            cfg->service_type[0] != '\0' ? cfg->service_type : "(empty)",
            afp_enabled(cfg) ? "enabled" : "disabled",
            adisk_enabled(cfg) ? "enabled" : "disabled",
            cfg->device_model[0] != '\0' ? cfg->device_model : "(empty)",
            is_airport_enabled(cfg) ? "enabled" : "disabled",
            cfg->diskless ? "diskless" : "diskful");
    if (cfg->generated_airport_services) {
        fprintf(stderr, "mdns startup: generated AirPort services enabled\n");
    }
    if (is_riousbprint_enabled(cfg)) {
        fprintf(stderr,
                "mdns startup: USB printer instance=%s mfg=%s mdl=%s cmd=%s riousbprint_port=%u pdl_datastream_port=%u\n",
                cfg->riousbprint_instance_name,
                cfg->riousbprint_mfg[0] != '\0' ? cfg->riousbprint_mfg : "(empty)",
                cfg->riousbprint_mdl[0] != '\0' ? cfg->riousbprint_mdl : "(empty)",
                cfg->riousbprint_cmd[0] != '\0' ? cfg->riousbprint_cmd : "(none)",
                (unsigned int)cfg->riousbprint_port,
                (unsigned int)cfg->pdl_datastream_port);
    }
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

static void remember_last_send_failure(const char *stage, int saved_errno) {
    int written;

    g_mdns_counters.send_failures++;
    written = snprintf(g_mdns_counters.last_send_failure,
                       sizeof(g_mdns_counters.last_send_failure),
                       "%s errno=%d (%s)",
                       stage,
                       saved_errno,
                       strerror(saved_errno));
    if (written < 0 || (size_t)written >= sizeof(g_mdns_counters.last_send_failure)) {
        g_mdns_counters.last_send_failure[sizeof(g_mdns_counters.last_send_failure) - 1] = '\0';
    }
}

static void log_mdns_counters(const char *reason) {
    fprintf(stderr,
            "mdns counters: reason=%s ipv4_rx=%lu ipv6_rx=%lu query_matches=%lu responses_sent=%lu send_failures=%lu last_send_failure=%s\n",
            reason,
            g_mdns_counters.ipv4_packets_received,
            g_mdns_counters.ipv6_packets_received,
            g_mdns_counters.query_packets_matched,
            g_mdns_counters.responses_sent,
            g_mdns_counters.send_failures,
            g_mdns_counters.last_send_failure[0] != '\0' ? g_mdns_counters.last_send_failure : "(none)");
}

static void remember_logged_mdns_counters(long long now_ms) {
    g_mdns_counter_log_state.ipv4_packets_received = g_mdns_counters.ipv4_packets_received;
    g_mdns_counter_log_state.ipv6_packets_received = g_mdns_counters.ipv6_packets_received;
    g_mdns_counter_log_state.query_packets_matched = g_mdns_counters.query_packets_matched;
    g_mdns_counter_log_state.responses_sent = g_mdns_counters.responses_sent;
    g_mdns_counter_log_state.send_failures = g_mdns_counters.send_failures;
    g_mdns_counter_log_state.last_log_ms = now_ms;
}

static int mdns_counters_changed_since_log(void) {
    return g_mdns_counter_log_state.ipv4_packets_received != g_mdns_counters.ipv4_packets_received ||
           g_mdns_counter_log_state.ipv6_packets_received != g_mdns_counters.ipv6_packets_received ||
           g_mdns_counter_log_state.query_packets_matched != g_mdns_counters.query_packets_matched ||
           g_mdns_counter_log_state.responses_sent != g_mdns_counters.responses_sent ||
           g_mdns_counter_log_state.send_failures != g_mdns_counters.send_failures;
}

static void log_mdns_counters_force(const char *reason) {
    long long now_ms = monotonic_millis();

    log_mdns_counters(reason);
    remember_logged_mdns_counters(now_ms);
}

static void maybe_log_mdns_counters(const char *reason, long long now_ms) {
    if (!g_debug_logging) {
        return;
    }
    if (!mdns_counters_changed_since_log()) {
        return;
    }
    if (g_mdns_counter_log_state.last_log_ms > 0 &&
        now_ms - g_mdns_counter_log_state.last_log_ms < MDNS_COUNTER_LOG_INTERVAL_MS) {
        return;
    }
    log_mdns_counters(reason);
    remember_logged_mdns_counters(now_ms);
}

static int note_mdns_ipv4_packet_received(void) {
    g_mdns_counters.ipv4_packets_received++;
    if (!g_mdns_counter_log_state.logged_ipv4_packet) {
        g_mdns_counter_log_state.logged_ipv4_packet = 1;
        return 1;
    }
    return 0;
}

static int note_mdns_ipv6_packet_received(void) {
    g_mdns_counters.ipv6_packets_received++;
    if (!g_mdns_counter_log_state.logged_ipv6_packet) {
        g_mdns_counter_log_state.logged_ipv6_packet = 1;
        return 1;
    }
    return 0;
}

static void log_mdns_receive_counters(const char *first_packet_reason,
                                      int first_packet,
                                      unsigned long query_matches_before,
                                      long long now_ms) {
    if (g_mdns_counters.query_packets_matched > query_matches_before &&
        !g_mdns_counter_log_state.logged_query_match) {
        g_mdns_counter_log_state.logged_query_match = 1;
        log_mdns_counters_force("first_query_match");
        return;
    }
    if (first_packet) {
        log_mdns_counters_force(first_packet_reason);
        return;
    }
    maybe_log_mdns_counters("traffic_summary", now_ms);
}

static void mdns_transport_requirements_from_links(const struct link_context_set *desired_links,
                                                   struct mdns_transport_requirements *requirements) {
    int wants_ipv4 = link_contexts_need_ipv4_socket(desired_links);
    int wants_ipv6 = link_contexts_need_ipv6_socket(desired_links);

    memset(requirements, 0, sizeof(*requirements));
    requirements->ipv4_required = wants_ipv4;
    requirements->ipv6_required = wants_ipv6;
}

static void mdns_transport_status_from_links(const struct link_context_set *desired_links,
                                             const struct link_context_set *active_links,
                                             const struct mdns_socket_pair *sockets,
                                             struct mdns_transport_status *status) {
    struct mdns_transport_requirements requirements;

    mdns_transport_requirements_from_links(desired_links, &requirements);
    memset(status, 0, sizeof(*status));
    status->required_ipv4 = requirements.ipv4_required;
    status->required_ipv6 = requirements.ipv6_required;
    status->active_ipv4 = sockets->ipv4_fd >= 0 && link_contexts_need_ipv4_socket(active_links);
    status->active_ipv6 = sockets->ipv6_fd >= 0 && link_contexts_need_ipv6_socket(active_links);
    status->missing_required_ipv4 = status->required_ipv4 && !status->active_ipv4;
    status->missing_required_ipv6 = status->required_ipv6 && !status->active_ipv6;
    status->last_ipv4_errno = g_last_ipv4_socket_errno;
    status->last_ipv6_errno = g_last_ipv6_socket_errno;
}

static int mdns_transport_has_active_socket(const struct mdns_transport_status *status) {
    return status->active_ipv4 || status->active_ipv6;
}

static int mdns_transport_missing_required(const struct mdns_transport_status *status) {
    return status->missing_required_ipv4 || status->missing_required_ipv6;
}

static int mdns_transport_is_healthy(const struct mdns_transport_status *status) {
    return mdns_transport_has_active_socket(status) && !mdns_transport_missing_required(status);
}

static const char *mdns_transport_health_label(const struct mdns_transport_status *status) {
    if (mdns_transport_is_healthy(status)) {
        return "healthy";
    }
    if (mdns_transport_has_active_socket(status)) {
        return "degraded";
    }
    return "down";
}

static void mdns_first_active_ipv4(char *out, size_t out_len, const struct link_context_set *active_links) {
    size_t i;

    for (i = 0; i < active_links->count; i++) {
        uint32_t ipv4_addr;
        if (!link_context_has_mdns_ipv4_transport(&active_links->links[i])) {
            continue;
        }
        ipv4_addr = link_preferred_ipv4_source(&active_links->links[i]);
        if (ipv4_addr != 0) {
            (void)ipv4_to_string(ipv4_addr, out, out_len);
            return;
        }
    }
    strncpy(out, "off", out_len - 1);
    out[out_len - 1] = '\0';
}

static void mdns_first_active_ipv6(char *out, size_t out_len, const struct link_context_set *active_links) {
    size_t i;

    for (i = 0; i < active_links->count; i++) {
        if (!link_context_has_mdns_ipv6_transport(&active_links->links[i])) {
            continue;
        }
        snprintf(out, out_len, "%s", active_links->links[i].name);
        return;
    }
    strncpy(out, "off", out_len - 1);
    out[out_len - 1] = '\0';
}

static void log_mdns_transport_status(const char *reason,
                                      const struct link_context_set *active_links,
                                      const struct mdns_transport_status *status) {
    char ipv4_buf[INET_ADDRSTRLEN];
    char ipv6_buf[IFNAMSIZ + 1];

    mdns_first_active_ipv4(ipv4_buf, sizeof(ipv4_buf), active_links);
    mdns_first_active_ipv6(ipv6_buf, sizeof(ipv6_buf), active_links);
    fprintf(stderr,
            "mdns transport active: reason=%s status=%s ipv4=%s ipv6=%s required_ipv4=%d required_ipv6=%d missing_required_ipv4=%d missing_required_ipv6=%d last_ipv4_errno=%d last_ipv6_errno=%d\n",
            reason,
            mdns_transport_health_label(status),
            status->active_ipv4 ? ipv4_buf : "off",
            status->active_ipv6 ? ipv6_buf : "off",
            status->required_ipv4,
            status->required_ipv6,
            status->missing_required_ipv4,
            status->missing_required_ipv6,
            status->last_ipv4_errno,
            status->last_ipv6_errno);
}

static int link_context_topology_equal(const struct link_context *a, const struct link_context *b) {
    size_t i;

    if (strcmp(a->name, b->name) != 0 ||
        a->flags != b->flags ||
        a->ifindex != b->ifindex ||
        a->ipv4_count != b->ipv4_count ||
        a->ipv6_count != b->ipv6_count) {
        return 0;
    }
    for (i = 0; i < a->ipv4_count; i++) {
        if (a->ipv4[i].addr != b->ipv4[i].addr ||
            a->ipv4[i].netmask != b->ipv4[i].netmask) {
            return 0;
        }
    }
    for (i = 0; i < a->ipv6_count; i++) {
        if (memcmp(&a->ipv6[i].addr, &b->ipv6[i].addr, sizeof(a->ipv6[i].addr)) != 0 ||
            a->ipv6[i].scope_id != b->ipv6[i].scope_id ||
            a->ipv6[i].prefix_len != b->ipv6[i].prefix_len ||
            a->ipv6[i].link_local != b->ipv6[i].link_local) {
            return 0;
        }
    }
    return 1;
}

static int link_context_set_contains_topology(const struct link_context_set *set,
                                              const struct link_context *ctx) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (link_context_topology_equal(&set->links[i], ctx)) {
            return 1;
        }
    }
    return 0;
}

static int link_context_topology_sets_equal(const struct link_context_set *a,
                                            const struct link_context_set *b) {
    size_t i;

    if (a->count != b->count) {
        return 0;
    }
    for (i = 0; i < a->count; i++) {
        if (!link_context_set_contains_topology(b, &a->links[i])) {
            return 0;
        }
    }
    return 1;
}

static void log_served_records(const struct config *cfg, const struct service_record_set *snapshot_records,
                               int use_snapshot_records) {
    fprintf(stderr, "serving summary: source=%s\n", use_snapshot_records ? "generated+snapshot" : "generated");
    if (smb_enabled(cfg)) {
        fprintf(stderr, "serving service: type=%s instance=%s port=%u host=%s\n",
                cfg->service_type, cfg->instance_name, (unsigned int)cfg->port, cfg->host_fqdn);
    }
    if (afp_enabled(cfg)) {
        fprintf(stderr, "serving service: type=%s instance=%s port=%u host=%s\n",
                cfg->afp_service_type, cfg->instance_name, (unsigned int)cfg->afp_port, cfg->host_fqdn);
    }
    if (cfg->device_model[0] != '\0') {
        fprintf(stderr, "serving service: type=%s instance=%s model=%s\n",
                cfg->device_info_service_type, cfg->instance_name, cfg->device_model);
    }
    if (adisk_enabled(cfg)) {
        size_t i;
        for (i = 0; i < cfg->adisk_disks.count; i++) {
            fprintf(stderr, "serving service: type=%s instance=%s share=%s disk_key=%s uuid=%s\n",
                    cfg->adisk_service_type, cfg->instance_name, cfg->adisk_disks.disks[i].share_name,
                    cfg->adisk_disks.disks[i].disk_key, cfg->adisk_disks.disks[i].uuid);
        }
    }
    if (is_airport_enabled(cfg)) {
        fprintf(stderr, "serving service: type=%s instance=%s syAP=%s syVs=%s srcv=%s\n",
                cfg->airport_service_type, cfg->instance_name,
                cfg->airport_syap[0] != '\0' ? cfg->airport_syap : "(none)",
                cfg->airport_syvs[0] != '\0' ? cfg->airport_syvs : "(none)",
                cfg->airport_srcv[0] != '\0' ? cfg->airport_srcv : "(none)");
    }
    if (is_riousbprint_enabled(cfg)) {
        fprintf(stderr, "serving service: type=%s instance=%s port=%u host=%s cmd=%s\n",
                RIOUSBPRINT_SERVICE_TYPE,
                cfg->riousbprint_instance_name,
                (unsigned int)cfg->riousbprint_port,
                cfg->host_fqdn,
                cfg->riousbprint_cmd[0] != '\0' ? cfg->riousbprint_cmd : "(none)");
    }
    if (is_pdl_datastream_enabled(cfg)) {
        fprintf(stderr, "serving service: type=%s instance=%s port=%u host=%s cmd=%s\n",
                PDL_DATASTREAM_SERVICE_TYPE,
                cfg->riousbprint_instance_name,
                (unsigned int)cfg->pdl_datastream_port,
                cfg->host_fqdn,
                cfg->riousbprint_cmd[0] != '\0' ? cfg->riousbprint_cmd : "(none)");
    }
    if (use_snapshot_records) {
        size_t i;
        for (i = 0; i < snapshot_records->count; i++) {
            const struct service_record *record = &snapshot_records->records[i];
            if (snapshot_record_overridden_by_generated(cfg, record)) {
                continue;
            }
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

static int snapshot_record_overridden_by_generated(const struct config *cfg, const struct service_record *record) {
    char generated_instance_fqdn[MAX_NAME];

    if (is_airport_enabled(cfg) && name_equals(record->service_type, cfg->airport_service_type)) {
        if (build_instance_fqdn(generated_instance_fqdn,
                                sizeof(generated_instance_fqdn),
                                cfg->instance_name,
                                cfg->airport_service_type) == 0 &&
            name_equals(record->instance_fqdn, generated_instance_fqdn)) {
            return 1;
        }
    }
    if (is_riousbprint_enabled(cfg) && name_equals(record->service_type, RIOUSBPRINT_SERVICE_TYPE)) {
        if (build_instance_fqdn(generated_instance_fqdn,
                                sizeof(generated_instance_fqdn),
                                cfg->riousbprint_instance_name,
                                RIOUSBPRINT_SERVICE_TYPE) == 0 &&
            name_equals(record->instance_fqdn, generated_instance_fqdn)) {
            return 1;
        }
    }
    if (is_pdl_datastream_enabled(cfg) && name_equals(record->service_type, PDL_DATASTREAM_SERVICE_TYPE)) {
        if (build_instance_fqdn(generated_instance_fqdn,
                                sizeof(generated_instance_fqdn),
                                cfg->riousbprint_instance_name,
                                PDL_DATASTREAM_SERVICE_TYPE) == 0 &&
            name_equals(record->instance_fqdn, generated_instance_fqdn)) {
            return 1;
        }
    }
    return 0;
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
    char escaped_instance[MAX_NAME];
    size_t fqdn_len;
    size_t service_len;
    size_t instance_len;

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
    instance_len = fqdn_len - service_len - 1;
    if (instance_len >= sizeof(escaped_instance)) {
        return -1;
    }
    memcpy(escaped_instance, fqdn_copy, instance_len);
    escaped_instance[instance_len] = '\0';
    return unescape_dns_label(out, out_len, escaped_instance);
}

static int build_host_label_from_fqdn(char *out, size_t out_len, const char *host_fqdn) {
    size_t i;
    size_t label_len = 0;
    char escaped_label[MAX_NAME];

    if (host_fqdn == NULL || host_fqdn[0] == '\0') {
        return -1;
    }
    for (i = 0; host_fqdn[i] != '\0'; i++) {
        if (host_fqdn[i] == '\\') {
            if (host_fqdn[i + 1] == '\0' || label_len + 2 >= sizeof(escaped_label)) {
                return -1;
            }
            escaped_label[label_len++] = host_fqdn[i++];
            escaped_label[label_len++] = host_fqdn[i];
            continue;
        }
        if (host_fqdn[i] == '.') {
            break;
        }
        if (label_len + 1 >= sizeof(escaped_label)) {
            return -1;
        }
        escaped_label[label_len++] = host_fqdn[i];
    }
    if (label_len == 0 || label_len >= sizeof(escaped_label)) {
        return -1;
    }
    escaped_label[label_len] = '\0';
    return unescape_dns_label(out, out_len, escaped_label);
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
        set->truncated = 1;
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

static int print_nt_hash_from_stdin(void) {
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

static void usage(const char *prog) {
    fprintf(stderr,
            "Usage: %s --instance <name> --host <label> --auto-ip [options]\n"
            "       %s --save-snapshot <path> [--save-all-snapshot <path>] --auto-ip [airport identity options]\n"
            "       %s --save-airport-snapshot <path> --instance <name> --host <label> [airport identity options]\n"
            "       %s --snapshot-newer-than-boot <path>\n"
            "       %s --print-nt-hash-from-stdin\n"
            "       %s --print-auto-ip-cidrs\n"
            "       %s --print-smb-bind-interfaces\n"
            "       %s --print-smb-bind-interfaces-lan\n"
            "       %s --print-mdns-socket-families\n"
            "       %s --version\n"
            "Options:\n"
            "  --auto-ip          Serve every usable live address link and track IP changes\n"
            "  --print-nt-hash-from-stdin Read a UTF-8 password from stdin and print its uppercase NT hash\n"
            "  --print-auto-ip-cidrs Print usable live IPv4 CIDRs and exit 0, or exit 11 if none exist\n"
            "  --print-smb-bind-interfaces Print live IPv4/IPv6 address CIDRs for Samba interfaces\n"
            "  --print-smb-bind-interfaces-lan Print Samba CIDRs only from bridge/br/lan owner interfaces\n"
            "  --print-mdns-socket-families Print required mDNS UDP socket families for live advertise links\n"
            "  --version          Print advertiser version code and exit\n"
            "  --debug-logging    Enable verbose mDNS traffic counter diagnostics\n"
            "  --save-all-snapshot <path> Capture raw LAN-wide mDNS records into a snapshot file\n"
            "  --save-snapshot <path> Capture Apple mDNS records into a snapshot file; without --load-snapshot, capture and exit\n"
            "  --skip-capture-if-snapshot-newer-than-boot <path> Reuse an existing snapshot created after boot\n"
            "  --snapshot-newer-than-boot <path> Exit 0 when snapshot exists, is non-empty, and is newer than boot\n"
            "  --save-airport-snapshot <path> Generate an AirPort-only Apple snapshot file and exit unless loading\n"
            "  --load-snapshot <path> Kill Apple mDNSResponder and replay snapshot records\n"
            "  --generated-airport-services Generate AirPort-owned services directly instead of snapshot replay\n"
            "  --diskless        Suppress generated _smb and _adisk records while replaying other snapshot records\n"
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
            prog, prog, prog, prog, prog, prog, prog, prog, prog, prog);
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

static int validate_dns_label_text(const char *value, const char *field_name, int allow_dots) {
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

static int validate_single_dns_label(const char *value, const char *field_name) {
    return validate_dns_label_text(value, field_name, 0);
}

static int validate_generated_dns_label(const char *value, const char *field_name) {
    return validate_dns_label_text(value, field_name, 1);
}

static int escape_dns_label(char *out, size_t out_len, const char *label) {
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

static int unescape_dns_label(char *out, size_t out_len, const char *label) {
    size_t in_i;
    size_t out_i = 0;

    if (label == NULL || label[0] == '\0') {
        return -1;
    }
    for (in_i = 0; label[in_i] != '\0'; in_i++) {
        unsigned char ch = (unsigned char)label[in_i];
        if (ch == '\\') {
            in_i++;
            if (label[in_i] == '\0') {
                return -1;
            }
            ch = (unsigned char)label[in_i];
        }
        if (out_i + 1 >= out_len) {
            return -1;
        }
        out[out_i++] = (char)ch;
    }
    out[out_i] = '\0';
    return 0;
}

static int build_instance_fqdn(char *out, size_t out_len, const char *instance_name, const char *service_type) {
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

static int build_host_fqdn(char *out, size_t out_len, const char *host_label) {
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

static int validate_adisk_disk_advf(const char *value) {
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

static int build_adisk_disk_txt(char *out, size_t out_len, const char *disk_key, const char *share_name, const char *adisk_uuid, const char *adisk_disk_advf) {
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

static void trim_ascii_whitespace(char *value) {
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

static int add_adisk_disk_config(struct config *cfg, const char *share_name, const char *disk_key,
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

static int parse_adisk_shares_file(struct config *cfg, const char *path) {
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

static int adisk_configured(const struct config *cfg) {
    return cfg->adisk_disks.count > 0;
}

static int adisk_enabled(const struct config *cfg) {
    return !cfg->diskless && adisk_configured(cfg);
}

static int smb_enabled(const struct config *cfg) {
    return !cfg->diskless;
}

static int afp_enabled(const struct config *cfg) {
    return cfg->advertise_afp;
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

static int is_airport_usb_printer_enabled(const struct config *cfg) {
    return cfg->riousbprint_instance_name[0] != '\0';
}

static int is_riousbprint_enabled(const struct config *cfg) {
    return is_airport_usb_printer_enabled(cfg);
}

static int is_pdl_datastream_enabled(const struct config *cfg) {
    return is_airport_usb_printer_enabled(cfg);
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

static int validate_txt_ascii_field(const char *value, const char *field_name) {
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

static int append_txt_itemf(char storage[][MAX_TXT_STRING + 1],
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

static int build_riousbprint_pdl(char *out, size_t out_len, const char *cmd) {
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

static int validate_airport_usb_printer_txt_fields(const struct config *cfg) {
    if (validate_txt_ascii_field(cfg->riousbprint_note, "USB printer note") != 0 ||
        validate_txt_ascii_field(cfg->riousbprint_mfg, "USB printer manufacturer") != 0 ||
        validate_txt_ascii_field(cfg->riousbprint_mdl, "USB printer model") != 0 ||
        validate_txt_ascii_field(cfg->riousbprint_serial, "USB printer serial") != 0 ||
        validate_txt_ascii_field(cfg->riousbprint_cmd, "USB printer command set") != 0) {
        return -1;
    }
    return 0;
}

static int append_airport_usb_printer_intro_txt_items(const struct config *cfg,
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

static int append_airport_usb_printer_device_txt_items(const struct config *cfg,
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

static int build_riousbprint_txt_items(const struct config *cfg,
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

static int build_pdl_datastream_txt_items(const struct config *cfg,
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

static int ieee1284_lookup_field(char *out,
                                 size_t out_len,
                                 const unsigned char *id,
                                 size_t id_len,
                                 const char *key) {
    size_t key_len;
    size_t pos = 0;

    if (out == NULL || out_len == 0 || id == NULL || key == NULL) {
        return -1;
    }
    out[0] = '\0';
    key_len = strlen(key);
    while (pos < id_len) {
        size_t start = pos;
        size_t end;
        size_t colon;
        size_t value_start;
        size_t value_end;
        size_t value_len;

        while (pos < id_len && id[pos] != ';') {
            pos++;
        }
        end = pos;
        if (pos < id_len && id[pos] == ';') {
            pos++;
        }
        colon = start;
        while (colon < end && id[colon] != ':') {
            colon++;
        }
        if (colon == end || colon - start != key_len ||
            strncasecmp((const char *)id + start, key, key_len) != 0) {
            continue;
        }
        value_start = colon + 1;
        value_end = end;
        while (value_start < value_end && isspace((unsigned char)id[value_start])) {
            value_start++;
        }
        while (value_end > value_start && isspace((unsigned char)id[value_end - 1])) {
            value_end--;
        }
        value_len = value_end - value_start;
        if (value_len == 0 || value_len >= out_len) {
            return -1;
        }
        memcpy(out, id + value_start, value_len);
        out[value_len] = '\0';
        return 0;
    }
    return -1;
}

static int TC_UNUSED extract_cmd_from_ieee1284_device_id(char *out,
                                                         size_t out_len,
                                                         const unsigned char *buf,
                                                         size_t actual_len) {
    size_t reported_len;
    size_t id_len;

    if (out == NULL || out_len == 0 || buf == NULL || actual_len <= 2) {
        return -1;
    }
    reported_len = ((size_t)buf[0] << 8) | (size_t)buf[1];
    if (reported_len > 2 && reported_len <= actual_len) {
        id_len = reported_len - 2;
    } else {
        id_len = actual_len - 2;
    }

    if (ieee1284_lookup_field(out, out_len, buf + 2, id_len, "CMD") == 0 ||
        ieee1284_lookup_field(out, out_len, buf + 2, id_len, "COMMAND SET") == 0) {
        return 0;
    }
    return -1;
}

static int TC_UNUSED sanitize_usb_printer_device_id_transfer(unsigned char *buf,
                                                             size_t buf_len,
                                                             int transferred_len,
                                                             int *actual_len) {
    if (buf == NULL || actual_len == NULL) {
        return -1;
    }
    *actual_len = 0;
    if (transferred_len < 0 || (size_t)transferred_len > buf_len) {
        memset(buf, 0, buf_len);
        return -1;
    }
    if ((size_t)transferred_len < buf_len) {
        memset(buf + transferred_len, 0, buf_len - (size_t)transferred_len);
    }
    if (transferred_len <= 2) {
        return -1;
    }
    *actual_len = transferred_len;
    return 0;
}

#if defined(__NetBSD__)
static int usb_device_info_has_ulpt(const struct usb_device_info *info) {
    size_t i;

    for (i = 0; i < USB_MAX_DEVNAMES; i++) {
        if (strncmp(info->udi_devnames[i], "ulpt", 4) == 0) {
            return 1;
        }
    }
    return 0;
}

static int usb_device_info_matches_riousbprint(const struct config *cfg,
                                               const struct usb_device_info *info) {
    if (cfg->riousbprint_vendor_id != 0 &&
        cfg->riousbprint_product_id != 0 &&
        info->udi_vendorNo == cfg->riousbprint_vendor_id &&
        info->udi_productNo == cfg->riousbprint_product_id) {
        return 1;
    }
    return usb_device_info_has_ulpt(info);
}

static int add_unique_usb_candidate(unsigned int candidates[], size_t *count, unsigned int value) {
    size_t i;

    for (i = 0; i < *count; i++) {
        if (candidates[i] == value) {
            return 0;
        }
    }
    if (*count >= 4) {
        return -1;
    }
    candidates[*count] = value;
    *count += 1;
    return 0;
}

static int query_usb_printer_device_id(int fd,
                                       int addr,
                                       const struct usb_device_info *info,
                                       unsigned char *buf,
                                       size_t buf_len,
                                       int *actual_len) {
    unsigned int configs[4];
    unsigned int indexes[4];
    size_t config_count = 0;
    size_t index_count = 0;
    size_t i;
    size_t j;

    if (info->udi_config != 0) {
        add_unique_usb_candidate(configs, &config_count, info->udi_config);
    }
    add_unique_usb_candidate(configs, &config_count, 1);
    add_unique_usb_candidate(configs, &config_count, 0);
    add_unique_usb_candidate(indexes, &index_count, 0);
    add_unique_usb_candidate(indexes, &index_count, 1);
    add_unique_usb_candidate(indexes, &index_count, 0x0100);
    add_unique_usb_candidate(indexes, &index_count, 0x0101);

    for (i = 0; i < config_count; i++) {
        for (j = 0; j < index_count; j++) {
            struct usb_ctl_request request;

            memset(buf, 0, buf_len);
            memset(&request, 0, sizeof(request));
            request.ucr_addr = addr;
            request.ucr_request.bmRequestType = UT_READ_CLASS_INTERFACE;
            request.ucr_request.bRequest = 0;
            USETW(request.ucr_request.wValue, configs[i]);
            USETW(request.ucr_request.wIndex, indexes[j]);
            USETW(request.ucr_request.wLength, buf_len);
            request.ucr_data = buf;
            request.ucr_flags = USBD_SHORT_XFER_OK;
            if (ioctl(fd, USB_REQUEST, &request) == 0) {
                if (sanitize_usb_printer_device_id_transfer(buf, buf_len, request.ucr_actlen, actual_len) == 0) {
                    return 0;
                }
            }
        }
    }
    return -1;
}

static int discover_riousbprint_usb_cmd(struct config *cfg) {
    int bus;

    if (!is_riousbprint_enabled(cfg) || cfg->riousbprint_cmd[0] != '\0') {
        return 0;
    }

    for (bus = 0; bus < 4; bus++) {
        char path[32];
        int fd;
        int addr;

        snprintf(path, sizeof(path), "/dev/usb%d", bus);
        fd = open(path, O_RDWR);
        if (fd < 0) {
            continue;
        }
        for (addr = 1; addr < USB_MAX_DEVICES; addr++) {
            struct usb_device_info info;
            unsigned char device_id[IEEE1284_DEVICE_ID_MAX];
            int actual_len = 0;
            char cmd[MAX_TXT_STRING + 1];

            memset(&info, 0, sizeof(info));
            info.udi_addr = (uint8_t)addr;
            if (ioctl(fd, USB_DEVICEINFO, &info) != 0) {
                continue;
            }
            if (!usb_device_info_matches_riousbprint(cfg, &info)) {
                continue;
            }
            if (query_usb_printer_device_id(fd,
                                            addr,
                                            &info,
                                            device_id,
                                            sizeof(device_id),
                                            &actual_len) != 0) {
                continue;
            }
            if (extract_cmd_from_ieee1284_device_id(cmd, sizeof(cmd), device_id, (size_t)actual_len) == 0) {
                strncpy(cfg->riousbprint_cmd, cmd, sizeof(cfg->riousbprint_cmd) - 1);
                cfg->riousbprint_cmd[sizeof(cfg->riousbprint_cmd) - 1] = '\0';
                close(fd);
                fprintf(stderr,
                        "riousbprint usb: found IEEE-1284 CMD via %s addr=%d vendor=0x%04x product=0x%04x\n",
                        path,
                        addr,
                        info.udi_vendorNo,
                        info.udi_productNo);
                return 0;
            }
        }
        close(fd);
    }

    fprintf(stderr, "riousbprint usb: IEEE-1284 CMD not available from NetBSD USB controller\n");
    return -1;
}
#else
static int discover_riousbprint_usb_cmd(struct config *cfg) {
    if (is_riousbprint_enabled(cfg) && cfg->riousbprint_cmd[0] == '\0') {
        fprintf(stderr, "riousbprint usb: IEEE-1284 CMD probing is unavailable on this platform\n");
    }
    return 0;
}
#endif

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
    size_t name_i = 0;
    size_t label_len = 0;
    uint8_t label[MAX_LABEL];

    if (name == NULL || name[0] == '\0') {
        return -1;
    }

    while (name[name_i] != '\0') {
        unsigned char ch = (unsigned char)name[name_i++];
        if (ch == '\\') {
            if (name[name_i] == '\0') {
                return -1;
            }
            ch = (unsigned char)name[name_i++];
        } else if (ch == '.') {
            uint8_t wire_len;
            if (label_len == 0) {
                if (name[name_i] == '\0') {
                    break;
                }
                return -1;
            }
            wire_len = (uint8_t)label_len;
            if (append_bytes(buf, off, cap, &wire_len, 1) != 0 ||
                append_bytes(buf, off, cap, label, label_len) != 0) {
                return -1;
            }
            label_len = 0;
            continue;
        }
        if (label_len >= MAX_LABEL) {
            return -1;
        }
        label[label_len++] = ch;
    }

    if (label_len > 0) {
        uint8_t wire_len = (uint8_t)label_len;
        if (append_bytes(buf, off, cap, &wire_len, 1) != 0 ||
            append_bytes(buf, off, cap, label, label_len) != 0) {
            return -1;
        }
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
        {
            size_t label_i;
            for (label_i = 0; label_i < len; label_i++) {
                unsigned char ch = packet[pos + 1 + label_i];
                if (ch == '.' || ch == '\\') {
                    if (out_pos + 2 >= out_len) {
                        return -1;
                    }
                    out[out_pos++] = '\\';
                    out[out_pos++] = (char)ch;
                } else {
                    if (out_pos + 1 >= out_len) {
                        return -1;
                    }
                    out[out_pos++] = (char)ch;
                }
            }
        }
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

static int append_host_address_records(uint8_t *buf,
                                       size_t *off,
                                       size_t cap,
                                       const char *owner,
                                       const struct link_context *link,
                                       int include_a,
                                       int include_aaaa,
                                       uint32_t ttl,
                                       int *answers) {
    size_t i;

    if (owner == NULL || owner[0] == '\0' || link == NULL) {
        return 0;
    }
    if (include_a) {
        for (i = 0; i < link->ipv4_count; i++) {
            if (add_rr_a(buf, off, cap, owner, link->ipv4[i].addr, ttl) != 0) {
                return -1;
            }
            *answers += 1;
        }
    }
    if (include_aaaa) {
        for (i = 0; i < link->ipv6_count; i++) {
            if (!link_ipv6_addr_is_samba_bindable(&link->ipv6[i])) {
                continue;
            }
            if (add_rr_aaaa(buf, off, cap, owner, &link->ipv6[i].addr, ttl) != 0) {
                return -1;
            }
            *answers += 1;
        }
    }
    return 0;
}

static int add_snapshot_host_address_records(uint8_t *buf,
                                             size_t *off,
                                             size_t cap,
                                             const struct service_record *record,
                                             const struct link_context *link,
                                             int include_a,
                                             int include_aaaa,
                                             uint32_t ttl,
                                             int *answers) {
    if (record->host_fqdn[0] == '\0') {
        return 0;
    }
    if (append_host_address_records(buf, off, cap, record->host_fqdn, link, include_a, include_aaaa, ttl, answers) != 0) {
        fprintf(stderr,
                "mdns snapshot rr failure: rr=ADDR type=%s instance=%s host=%s port=%u txt_count=%lu packet_len=%lu\n",
                record->service_type, record->instance_fqdn, record->host_fqdn,
                (unsigned int)record->port, (unsigned long)record->txt_count, (unsigned long)*off);
        return -1;
    }
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
        append_u16(buf, off, cap, DNS_CLASS_IN_UNIQUE) != 0 ||
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
        append_u16(buf, off, cap, DNS_CLASS_IN_UNIQUE) != 0 ||
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
        append_u16(buf, off, cap, DNS_CLASS_IN_UNIQUE) != 0 ||
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
        append_u16(buf, off, cap, DNS_CLASS_IN_UNIQUE) != 0 ||
        append_u32(buf, off, cap, ttl) != 0 ||
        append_u16(buf, off, cap, 4) != 0 ||
        append_bytes(buf, off, cap, &ipv4_addr, 4) != 0) {
        return -1;
    }
    return 0;
}

static int add_rr_aaaa(uint8_t *buf, size_t *off, size_t cap, const char *owner, const struct in6_addr *ipv6_addr, uint32_t ttl) {
    if (encode_name(buf, off, cap, owner) != 0 ||
        append_u16(buf, off, cap, DNS_TYPE_AAAA) != 0 ||
        append_u16(buf, off, cap, DNS_CLASS_IN_UNIQUE) != 0 ||
        append_u32(buf, off, cap, ttl) != 0 ||
        append_u16(buf, off, cap, 16) != 0 ||
        append_bytes(buf, off, cap, ipv6_addr->s6_addr, 16) != 0) {
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

static int build_airport_snapshot_set(const struct config *cfg, struct service_record_set *out) {
    struct service_record *record;
    char airport_txt[256];

    if (cfg->instance_name[0] == '\0' || cfg->host_label[0] == '\0' ||
        !cfg_has_airport_identity_macs(cfg)) {
        return -1;
    }
    if (build_airport_txt(airport_txt, sizeof(airport_txt), cfg) != 0) {
        return -1;
    }

    memset(out, 0, sizeof(*out));
    record = &out->records[out->count++];
    strncpy(record->service_type, AIRPORT_SERVICE_TYPE, sizeof(record->service_type) - 1);
    strncpy(record->instance_name, cfg->instance_name, sizeof(record->instance_name) - 1);
    if (build_instance_fqdn(record->instance_fqdn, sizeof(record->instance_fqdn),
                            record->instance_name, record->service_type) != 0) {
        return -1;
    }
    strncpy(record->host_label, cfg->host_label, sizeof(record->host_label) - 1);
    if (build_host_fqdn(record->host_fqdn, sizeof(record->host_fqdn), record->host_label) != 0) {
        return -1;
    }
    record->port = cfg->airport_port;
    strncpy(record->txt[0], airport_txt, sizeof(record->txt[0]) - 1);
    record->txt[0][sizeof(record->txt[0]) - 1] = '\0';
    record->txt_len[0] = (uint8_t)strlen(record->txt[0]);
    record->txt_count = 1;
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
            if (out->count >= SNAPSHOT_MAX_RECORDS) {
                out->truncated = 1;
                fclose(fp);
                return -1;
            }
            if (build_instance_fqdn(current.instance_fqdn, sizeof(current.instance_fqdn), current.instance_name, current.service_type) != 0 ||
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
            if (build_host_fqdn(current.host_fqdn, sizeof(current.host_fqdn), current.host_label) != 0) {
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

static int send_query_question_any(int sockfd,
                                   const struct sockaddr *dest,
                                   socklen_t dest_len,
                                   const char *qname,
                                   uint16_t qtype) {
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
    return sendto_retry(sockfd, packet, off, 0, dest, dest_len) >= 0 ? 0 : -1;
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

static int collect_mdns_responses_pair(const struct mdns_socket_pair *sockets,
                                       int seconds,
                                       struct service_record_set *set,
                                       struct service_type_set *service_types) {
    time_t deadline = time(NULL) + seconds;

    while (time(NULL) < deadline) {
        fd_set rfds;
        struct timeval tv;
        uint8_t packet[BUF_SIZE];
        int maxfd = -1;
        int selected;

        FD_ZERO(&rfds);
        if (sockets->ipv4_fd >= 0) {
            FD_SET(sockets->ipv4_fd, &rfds);
            if (sockets->ipv4_fd > maxfd) {
                maxfd = sockets->ipv4_fd;
            }
        }
        if (sockets->ipv6_fd >= 0) {
            FD_SET(sockets->ipv6_fd, &rfds);
            if (sockets->ipv6_fd > maxfd) {
                maxfd = sockets->ipv6_fd;
            }
        }
        if (maxfd < 0) {
            return -1;
        }
        tv.tv_sec = 1;
        tv.tv_usec = 0;
        selected = select(maxfd + 1, &rfds, NULL, NULL, &tv);
        if (selected <= 0) {
            continue;
        }
        if (sockets->ipv4_fd >= 0 && FD_ISSET(sockets->ipv4_fd, &rfds)) {
            ssize_t nread = recvfrom(sockets->ipv4_fd, packet, sizeof(packet), 0, NULL, NULL);
            if (nread > 0) {
                (void)parse_snapshot_rrs(packet, (size_t)nread, set, service_types);
            }
        }
        if (sockets->ipv6_fd >= 0 && FD_ISSET(sockets->ipv6_fd, &rfds)) {
            ssize_t nread = recvfrom(sockets->ipv6_fd, packet, sizeof(packet), 0, NULL, NULL);
            if (nread > 0) {
                (void)parse_snapshot_rrs(packet, (size_t)nread, set, service_types);
            }
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
            out->truncated = 1;
            break;
        }
        out->records[out->count++] = *record;
    }

    return out->count > 0 ? 0 : -1;
}

static void scoped_mdns_dest6_for_link(struct sockaddr_in6 *out,
                                       const struct sockaddr_in6 *base,
                                       const struct link_context *link) {
    *out = *base;
    if (link != NULL) {
        out->sin6_scope_id = link->ifindex;
    }
}

static void send_capture_query_to_all_links(const struct mdns_socket_pair *sockets,
                                            const struct link_context_set *links,
                                            const struct sockaddr_in *dest4,
                                            const struct sockaddr_in6 *dest6,
                                            const char *qname,
                                            uint16_t qtype) {
    size_t i;

    for (i = 0; i < links->count; i++) {
        uint32_t ipv4_source = link_preferred_ipv4_source(&links->links[i]);
        if (sockets->ipv4_fd >= 0 && link_context_has_mdns_ipv4_transport(&links->links[i]) &&
            ipv4_source != 0 &&
            set_outbound_multicast_interface(sockets->ipv4_fd, ipv4_source, "capture", 0, 0) == 0) {
            (void)send_query_question_any(sockets->ipv4_fd,
                                          (const struct sockaddr *)dest4,
                                          sizeof(*dest4),
                                          qname,
                                          qtype);
        }
        if (sockets->ipv6_fd >= 0 && link_context_has_mdns_ipv6_transport(&links->links[i]) &&
            set_outbound_multicast_interface6(sockets->ipv6_fd, links->links[i].ifindex, "capture", 0, 0) == 0) {
            struct sockaddr_in6 scoped_dest;
            scoped_mdns_dest6_for_link(&scoped_dest, dest6, &links->links[i]);
            (void)send_query_question_any(sockets->ipv6_fd,
                                          (const struct sockaddr *)&scoped_dest,
                                          sizeof(scoped_dest),
                                          qname,
                                          qtype);
        }
    }
}

static int capture_mdns_snapshot_links_raw(struct service_record_set *out,
                                           const struct link_context_set *links) {
    struct mdns_socket_pair sockets;
    struct link_context_set socket_links;
    struct sockaddr_in mdns_dest4;
    struct sockaddr_in6 mdns_dest6;
    size_t i;
    struct service_type_set service_types;

    memset(out, 0, sizeof(*out));
    memset(&service_types, 0, sizeof(service_types));
    socket_links = *links;
    sockets.ipv4_fd = -1;
    sockets.ipv6_fd = -1;
    if (open_dualstack_mdns_sockets(1, &socket_links, 1, &sockets) != 0) {
        return -1;
    }

    memset(&mdns_dest4, 0, sizeof(mdns_dest4));
    mdns_dest4.sin_family = AF_INET;
    mdns_dest4.sin_port = htons(MDNS_PORT);
    mdns_dest4.sin_addr.s_addr = inet_addr(MDNS_GROUP);

    memset(&mdns_dest6, 0, sizeof(mdns_dest6));
    mdns_dest6.sin6_family = AF_INET6;
    mdns_dest6.sin6_port = htons(MDNS_PORT);
    (void)inet_pton(AF_INET6, MDNS_GROUP_V6, &mdns_dest6.sin6_addr);

    send_capture_query_to_all_links(&sockets, &socket_links, &mdns_dest4, &mdns_dest6, "_services._dns-sd._udp.local.", DNS_TYPE_PTR);
    (void)collect_mdns_responses_pair(&sockets, SNAPSHOT_CAPTURE_STEP_SECONDS, out, &service_types);

    for (i = 0; i < service_types.count; i++) {
        send_capture_query_to_all_links(&sockets, &socket_links, &mdns_dest4, &mdns_dest6, service_types.types[i], DNS_TYPE_PTR);
    }
    (void)collect_mdns_responses_pair(&sockets, SNAPSHOT_CAPTURE_STEP_SECONDS, out, &service_types);

    for (i = 0; i < out->count; i++) {
        send_capture_query_to_all_links(&sockets, &socket_links, &mdns_dest4, &mdns_dest6, out->records[i].instance_fqdn, DNS_TYPE_SRV);
        send_capture_query_to_all_links(&sockets, &socket_links, &mdns_dest4, &mdns_dest6, out->records[i].instance_fqdn, DNS_TYPE_TXT);
    }
    (void)collect_mdns_responses_pair(&sockets, SNAPSHOT_CAPTURE_STEP_SECONDS, out, &service_types);
    close_mdns_socket_pair(&sockets);

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

static int capture_mdns_snapshot_links_with_retry(struct service_record_set *out,
                                                  const struct link_context_set *links) {
    time_t deadline = time(NULL) + SNAPSHOT_CAPTURE_TIMEOUT_SECONDS;

    do {
        if (capture_mdns_snapshot_links_raw(out, links) == 0) {
            return 0;
        }
        if (time(NULL) >= deadline) {
            break;
        }
        sleep(SNAPSHOT_CAPTURE_RETRY_INTERVAL_SECONDS);
    } while (time(NULL) < deadline);

    return -1;
}

static int get_boot_time_seconds(time_t *out) {
#if defined(CTL_KERN) && defined(KERN_BOOTTIME)
    int mib[2];
    struct timeval boot_time;
    size_t boot_time_len;

    if (out == NULL) {
        return -1;
    }

    mib[0] = CTL_KERN;
    mib[1] = KERN_BOOTTIME;
    boot_time_len = sizeof(boot_time);
    memset(&boot_time, 0, sizeof(boot_time));
    if (sysctl(mib, 2, &boot_time, &boot_time_len, NULL, 0) == 0) {
        *out = boot_time.tv_sec;
        return 0;
    }
#else
    (void)out;
#endif
    return -1;
}

static int snapshot_file_newer_than_boot(const char *path) {
    struct stat st;
    time_t boot_time;

    if (path == NULL || path[0] == '\0') {
        return 0;
    }
    if (stat(path, &st) != 0 || st.st_size <= 0) {
        return 0;
    }
    if (get_boot_time_seconds(&boot_time) != 0) {
        return -1;
    }

    return st.st_mtime > boot_time ? 1 : 0;
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

static unsigned int random_multicast_response_delay_ms(void) {
    static int seeded = 0;
    unsigned int span;

    if (!seeded) {
        srand((unsigned int)(time(NULL) ^ (time_t)getpid()));
        seeded = 1;
    }
    span = (MDNS_MULTICAST_RESPONSE_DELAY_MAX_MS - MDNS_MULTICAST_RESPONSE_DELAY_MIN_MS) + 1U;
    return MDNS_MULTICAST_RESPONSE_DELAY_MIN_MS + (unsigned int)(rand() % (int)span);
}

static void delay_multicast_query_response(void) {
    sleep_millis(random_multicast_response_delay_ms());
}

static long long monotonic_millis(void) {
    struct timeval tv;

    gettimeofday(&tv, NULL);
    return ((long long)tv.tv_sec * 1000LL) + ((long long)tv.tv_usec / 1000LL);
}

static void kill_mdnsresponder(int sig) {
    if (sig == SIGKILL) {
        (void)system("/usr/bin/pkill -9 '^mDNSResponder$' >/dev/null 2>&1 || true");
    } else {
        (void)system("/usr/bin/pkill '^mDNSResponder$' >/dev/null 2>&1 || true");
    }
}

static int join_mdns_multicast_group(int sockfd, uint32_t ipv4_addr, const char *socket_role) {
    struct ip_mreq mreq;
    char ipv4_buf[INET_ADDRSTRLEN];
    int explicit_errno = 0;

    memset(&mreq, 0, sizeof(mreq));
    mreq.imr_multiaddr.s_addr = inet_addr(MDNS_GROUP);
    if (ipv4_addr != 0) {
        mreq.imr_interface.s_addr = ipv4_addr;
        if (setsockopt(sockfd, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mreq, sizeof(mreq)) == 0) {
            fprintf(stderr, "mdns %s socket: multicast membership interface %s\n",
                    socket_role,
                    ipv4_to_string(ipv4_addr, ipv4_buf, sizeof(ipv4_buf)));
            return 0;
        }
        explicit_errno = errno;
        fprintf(stderr, "warning: mdns %s socket: IP_ADD_MEMBERSHIP failed for interface %s: %s; trying kernel-selected interface\n",
                socket_role,
                ipv4_to_string(ipv4_addr, ipv4_buf, sizeof(ipv4_buf)),
                strerror(explicit_errno));
    }

    mreq.imr_interface.s_addr = htonl(INADDR_ANY);
    if (setsockopt(sockfd, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mreq, sizeof(mreq)) == 0) {
        fprintf(stderr, "mdns %s socket: multicast membership interface kernel-selected\n",
                socket_role);
        return 0;
    }

    if (ipv4_addr != 0) {
        fprintf(stderr, "setsockopt(IP_ADD_MEMBERSHIP kernel-selected): %s\n", strerror(errno));
        errno = explicit_errno != 0 ? explicit_errno : errno;
    } else {
        perror("setsockopt(IP_ADD_MEMBERSHIP)");
    }
    return -1;
}

static int set_outbound_multicast_interface(int sockfd, uint32_t ipv4_addr, const char *socket_role,
                                            int log_success, int log_errors) {
    int explicit_errno = 0;
    int fallback_errno;
    struct in_addr multicast_if;
    char ipv4_buf[INET_ADDRSTRLEN];

    if (ipv4_addr != 0) {
        multicast_if.s_addr = ipv4_addr;
        if (setsockopt(sockfd, IPPROTO_IP, IP_MULTICAST_IF, &multicast_if, sizeof(multicast_if)) == 0) {
            if (log_success) {
                fprintf(stderr, "mdns %s socket: outbound multicast interface %s\n",
                        socket_role,
                        ipv4_to_string(ipv4_addr, ipv4_buf, sizeof(ipv4_buf)));
            }
            goto configure_multicast_options;
        }
        explicit_errno = errno;
        if (log_errors) {
            fprintf(stderr, "warning: mdns %s socket: IP_MULTICAST_IF failed for interface %s: %s; trying kernel-selected interface\n",
                    socket_role,
                    ipv4_to_string(ipv4_addr, ipv4_buf, sizeof(ipv4_buf)),
                    strerror(explicit_errno));
        }
    }

    multicast_if.s_addr = htonl(INADDR_ANY);
    if (setsockopt(sockfd, IPPROTO_IP, IP_MULTICAST_IF, &multicast_if, sizeof(multicast_if)) < 0) {
        fallback_errno = errno;
        if (ipv4_addr != 0) {
            if (log_errors) {
                fprintf(stderr, "setsockopt(IP_MULTICAST_IF kernel-selected): %s\n", strerror(fallback_errno));
            }
            errno = explicit_errno != 0 ? explicit_errno : fallback_errno;
        } else {
            errno = fallback_errno;
            if (log_errors) {
                perror("setsockopt(IP_MULTICAST_IF kernel-selected)");
            }
        }
        return -1;
    }
    if (log_success) {
        fprintf(stderr, "mdns %s socket: outbound multicast interface kernel-selected\n",
                socket_role);
    }

configure_multicast_options:
    return 0;
}

static void configure_unicast_response_hop_limit4(int sockfd) {
#ifdef IP_TTL
    int ttl = 255;
    if (setsockopt(sockfd, IPPROTO_IP, IP_TTL, &ttl, sizeof(ttl)) < 0) {
        fprintf(stderr, "warning: mdns socket: IP_TTL=255 failed: %s\n", strerror(errno));
    }
#else
    (void)sockfd;
#endif
}

static void configure_unicast_response_hop_limit6(int sockfd) {
#ifdef IPV6_UNICAST_HOPS
    int hops = 255;
    if (setsockopt(sockfd, IPPROTO_IPV6, IPV6_UNICAST_HOPS, &hops, sizeof(hops)) < 0) {
        fprintf(stderr, "warning: mdns socket: IPV6_UNICAST_HOPS=255 failed: %s\n", strerror(errno));
    }
#else
    (void)sockfd;
#endif
}

static int configure_multicast_socket_options(int sockfd) {
    int yes;

    yes = 255;
    (void)setsockopt(sockfd, IPPROTO_IP, IP_MULTICAST_TTL, &yes, sizeof(yes));
    yes = 1;
    (void)setsockopt(sockfd, IPPROTO_IP, IP_MULTICAST_LOOP, &yes, sizeof(yes));
    configure_unicast_response_hop_limit4(sockfd);
    return 0;
}

static int configure_outbound_multicast_socket(int sockfd, uint32_t ipv4_addr, const char *socket_role) {
    if (set_outbound_multicast_interface(sockfd, ipv4_addr, socket_role, 1, 1) != 0) {
        return -1;
    }
    return configure_multicast_socket_options(sockfd);
}

static int open_bound_mdns_socket(int shared_bind, int log_bind_errors) {
    int sockfd;
    int yes = 1;
    struct sockaddr_in addr;

    sockfd = socket(AF_INET, SOCK_DGRAM, 0);
    if (sockfd < 0) {
        perror("socket");
        return -1;
    }

    if (shared_bind) {
        if (setsockopt(sockfd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes)) < 0) {
            perror("setsockopt(SO_REUSEADDR)");
            close(sockfd);
            return -1;
        }
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
    return sockfd;
}

static void drop_mdns_multicast_group_best_effort(int sockfd, uint32_t ipv4_addr, const char *socket_role) {
#ifdef IP_DROP_MEMBERSHIP
    struct ip_mreq mreq;
    char ipv4_buf[INET_ADDRSTRLEN];
    int drop_errno;

    memset(&mreq, 0, sizeof(mreq));
    mreq.imr_multiaddr.s_addr = inet_addr(MDNS_GROUP);
    mreq.imr_interface.s_addr = ipv4_addr;
    if (setsockopt(sockfd, IPPROTO_IP, IP_DROP_MEMBERSHIP, &mreq, sizeof(mreq)) == 0) {
        fprintf(stderr, "mdns %s socket: dropped multicast membership interface %s\n",
                socket_role,
                ipv4_to_string(ipv4_addr, ipv4_buf, sizeof(ipv4_buf)));
        return;
    }
    drop_errno = errno;
    fprintf(stderr, "warning: mdns %s socket: IP_DROP_MEMBERSHIP failed for interface %s: %s\n",
            socket_role,
            ipv4_to_string(ipv4_addr, ipv4_buf, sizeof(ipv4_buf)),
            strerror(drop_errno));
#else
    (void)sockfd;
    (void)ipv4_addr;
    (void)socket_role;
#endif
}

static int link_has_any_mdns_transport(const struct link_context *link) {
    return link_context_has_mdns_ipv4_transport(link) ||
           link_context_has_mdns_ipv6_transport(link);
}

static void compact_link_contexts_for_mdns_transport(struct link_context_set *set) {
    size_t i;
    size_t write_i = 0;

    for (i = 0; i < set->count; i++) {
        if (!link_has_any_mdns_transport(&set->links[i])) {
            continue;
        }
        if (write_i != i) {
            set->links[write_i] = set->links[i];
        }
        write_i++;
    }
    set->count = write_i;
}

static int link_ipv4_source_score(uint32_t ipv4_addr) {
    if (ipv4_is_rfc1918(ipv4_addr)) {
        return 0;
    }
    if (!ipv4_is_link_local(ipv4_addr)) {
        return 100;
    }
    return 200;
}

static uint32_t link_preferred_ipv4_source(const struct link_context *link) {
    size_t i;
    uint32_t best = 0;
    int best_score = 0;

    if (link == NULL || link->ipv4_count == 0) {
        return 0;
    }
    if (link->mdns_ipv4_transport_addr != 0) {
        return link->mdns_ipv4_transport_addr;
    }
    for (i = 0; i < link->ipv4_count; i++) {
        int score = link_ipv4_source_score(link->ipv4[i].addr);
        if (best == 0 || score < best_score) {
            best = link->ipv4[i].addr;
            best_score = score;
        }
    }
    return best;
}

static uint32_t link_ipv4_source_for_peer(const struct link_context *link, uint32_t source_ipv4_addr) {
    size_t i;
    uint32_t best = 0;
    int best_score = 0;

    if (link == NULL || source_ipv4_addr == 0) {
        return link_preferred_ipv4_source(link);
    }
    for (i = 0; i < link->ipv4_count; i++) {
        uint32_t netmask = effective_ipv4_netmask(link->ipv4[i].addr, link->ipv4[i].netmask);
        int matches;
        int score;

        if (netmask == 0) {
            matches = source_ipv4_addr == link->ipv4[i].addr;
        } else {
            matches = (source_ipv4_addr & netmask) == (link->ipv4[i].addr & netmask);
        }
        if (!matches) {
            continue;
        }
        score = link_ipv4_source_score(link->ipv4[i].addr);
        if (best == 0 || score < best_score) {
            best = link->ipv4[i].addr;
            best_score = score;
        }
    }
    return best != 0 ? best : link_preferred_ipv4_source(link);
}

static int link_contexts_need_ipv4_socket(const struct link_context_set *set) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (link_context_has_mdns_ipv4_transport(&set->links[i])) {
            return 1;
        }
    }
    return 0;
}

static int link_contexts_need_ipv6_socket(const struct link_context_set *set) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (link_context_has_mdns_ipv6_transport(&set->links[i])) {
            return 1;
        }
    }
    return 0;
}

static void close_mdns_socket_pair(struct mdns_socket_pair *sockets) {
    if (sockets->ipv4_fd >= 0) {
        clear_deferred_response_for_sockfd(sockets->ipv4_fd);
        close(sockets->ipv4_fd);
        sockets->ipv4_fd = -1;
    }
    if (sockets->ipv6_fd >= 0) {
        clear_deferred_response_for_sockfd(sockets->ipv6_fd);
        close(sockets->ipv6_fd);
        sockets->ipv6_fd = -1;
    }
}

static void init_mdns_membership_delta(struct mdns_membership_delta *delta) {
    memset(delta, 0, sizeof(*delta));
}

static int record_mdns_membership_ipv4(struct mdns_membership_delta *delta, uint32_t ipv4_addr) {
    if (delta == NULL) {
        return 0;
    }
    if (delta->ipv4_count >= MAX_IFACE_CONTEXTS) {
        return -1;
    }
    delta->ipv4[delta->ipv4_count++] = ipv4_addr;
    return 0;
}

static int record_mdns_membership_ipv6(struct mdns_membership_delta *delta, unsigned int ifindex, const char *ifname) {
    if (delta == NULL) {
        return 0;
    }
    if (delta->ipv6_count >= MAX_IFACE_CONTEXTS) {
        return -1;
    }
    delta->ipv6_ifindex[delta->ipv6_count] = ifindex;
    if (ifname != NULL) {
        strncpy(delta->ipv6_name[delta->ipv6_count], ifname, sizeof(delta->ipv6_name[delta->ipv6_count]) - 1);
    }
    delta->ipv6_count++;
    return 0;
}

static void rollback_mdns_membership_delta(struct mdns_socket_pair *sockets,
                                           const struct mdns_membership_delta *delta) {
    size_t i;

    if (delta == NULL) {
        return;
    }
    if (sockets->ipv4_fd >= 0) {
        for (i = delta->ipv4_count; i > 0; i--) {
            drop_mdns_multicast_group_best_effort(sockets->ipv4_fd, delta->ipv4[i - 1], "runtime");
        }
    }
    if (sockets->ipv6_fd >= 0) {
        for (i = delta->ipv6_count; i > 0; i--) {
            drop_mdns_multicast_group6_best_effort(sockets->ipv6_fd,
                                                   delta->ipv6_ifindex[i - 1],
                                                   delta->ipv6_name[i - 1],
                                                   "runtime");
        }
    }
}

static int open_bound_mdns_socket6(int shared_bind, int log_bind_errors) {
    int sockfd;
    int yes = 1;
    struct sockaddr_in6 addr;

    sockfd = socket(AF_INET6, SOCK_DGRAM, 0);
    if (sockfd < 0) {
        if (log_bind_errors) {
            perror("socket(AF_INET6)");
        }
        return -1;
    }

    if (setsockopt(sockfd, IPPROTO_IPV6, IPV6_V6ONLY, &yes, sizeof(yes)) < 0 && log_bind_errors) {
        perror("setsockopt(IPV6_V6ONLY)");
    }
    if (shared_bind) {
        if (setsockopt(sockfd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes)) < 0) {
            perror("setsockopt(SO_REUSEADDR ipv6)");
            close(sockfd);
            return -1;
        }
#ifdef SO_REUSEPORT
        (void)setsockopt(sockfd, SOL_SOCKET, SO_REUSEPORT, &yes, sizeof(yes));
#endif
    }

    memset(&addr, 0, sizeof(addr));
    addr.sin6_family = AF_INET6;
    addr.sin6_port = htons(MDNS_PORT);
    addr.sin6_addr = in6addr_any;
    if (bind(sockfd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        if (log_bind_errors) {
            perror("bind(AF_INET6)");
        }
        close(sockfd);
        return -1;
    }
    return sockfd;
}

static int join_mdns_multicast_group6(int sockfd, unsigned int ifindex, const char *ifname, const char *socket_role) {
    struct ipv6_mreq mreq;

    if (ifindex == 0) {
        errno = EADDRNOTAVAIL;
        return -1;
    }
    memset(&mreq, 0, sizeof(mreq));
    if (inet_pton(AF_INET6, MDNS_GROUP_V6, &mreq.ipv6mr_multiaddr) != 1) {
        errno = EINVAL;
        return -1;
    }
    mreq.ipv6mr_interface = ifindex;
    if (setsockopt(sockfd, IPPROTO_IPV6, IPV6_JOIN_GROUP, &mreq, sizeof(mreq)) == 0) {
        fprintf(stderr, "mdns %s socket: IPv6 multicast membership iface=%s ifindex=%u\n",
                socket_role, ifname, ifindex);
        return 0;
    }
    fprintf(stderr,
            "warning: mdns %s socket: IPV6_JOIN_GROUP failed for iface=%s ifindex=%u: %s\n",
            socket_role,
            ifname,
            ifindex,
            strerror(errno));
    return -1;
}

static void drop_mdns_multicast_group6_best_effort(int sockfd, unsigned int ifindex, const char *ifname, const char *socket_role) {
#ifdef IPV6_LEAVE_GROUP
    struct ipv6_mreq mreq;
    int drop_errno;

    if (ifindex == 0) {
        return;
    }
    memset(&mreq, 0, sizeof(mreq));
    if (inet_pton(AF_INET6, MDNS_GROUP_V6, &mreq.ipv6mr_multiaddr) != 1) {
        return;
    }
    mreq.ipv6mr_interface = ifindex;
    if (setsockopt(sockfd, IPPROTO_IPV6, IPV6_LEAVE_GROUP, &mreq, sizeof(mreq)) == 0) {
        fprintf(stderr, "mdns %s socket: dropped IPv6 multicast membership iface=%s ifindex=%u\n",
                socket_role, ifname, ifindex);
        return;
    }
    drop_errno = errno;
    fprintf(stderr,
            "warning: mdns %s socket: IPV6_LEAVE_GROUP failed for iface=%s ifindex=%u: %s\n",
            socket_role,
            ifname,
            ifindex,
            strerror(drop_errno));
#else
    (void)sockfd;
    (void)ifindex;
    (void)ifname;
    (void)socket_role;
#endif
}

static int set_outbound_multicast_interface6(int sockfd, unsigned int ifindex, const char *socket_role,
                                             int log_success, int log_errors) {
    int hops = 255;
    int loop = 1;

    if (ifindex == 0) {
        errno = EADDRNOTAVAIL;
        return -1;
    }
    if (setsockopt(sockfd, IPPROTO_IPV6, IPV6_MULTICAST_IF, &ifindex, sizeof(ifindex)) < 0) {
        if (log_errors) {
            fprintf(stderr,
                    "warning: mdns %s socket: IPV6_MULTICAST_IF failed for ifindex=%u: %s\n",
                    socket_role,
                    ifindex,
                    strerror(errno));
        }
        return -1;
    }
    (void)setsockopt(sockfd, IPPROTO_IPV6, IPV6_MULTICAST_HOPS, &hops, sizeof(hops));
    (void)setsockopt(sockfd, IPPROTO_IPV6, IPV6_MULTICAST_LOOP, &loop, sizeof(loop));
    configure_unicast_response_hop_limit6(sockfd);
    if (log_success) {
        fprintf(stderr, "mdns %s socket: IPv6 outbound multicast ifindex=%u\n", socket_role, ifindex);
    }
    return 0;
}

static int join_mdns_multicast_group_for_link4(int sockfd,
                                               struct link_context *link,
                                               const struct link_context_set *old_links,
                                               const char *socket_role,
                                               struct mdns_membership_delta *delta) {
    size_t i;
    unsigned int tried_mask = 0;

    if (!link_context_has_mdns_ipv4_transport(link)) {
        return 0;
    }

    for (;;) {
        size_t best_i = 0;
        int best_score = 0;
        int found = 0;

        for (i = 0; i < link->ipv4_count; i++) {
            int score;
            if ((tried_mask & (1U << i)) != 0) {
                continue;
            }
            score = link_ipv4_source_score(link->ipv4[i].addr);
            if (!found || score < best_score) {
                best_i = i;
                best_score = score;
                found = 1;
            }
        }

        if (!found) {
            break;
        }
        tried_mask |= 1U << best_i;

        if (link_set_has_ipv4_membership(old_links, link->ipv4[best_i].addr)) {
            link->mdns_ipv4_transport_addr = link->ipv4[best_i].addr;
            return 1;
        }
        if (join_mdns_multicast_group(sockfd, link->ipv4[best_i].addr, socket_role) != 0) {
            continue;
        }
        if (record_mdns_membership_ipv4(delta, link->ipv4[best_i].addr) != 0) {
            drop_mdns_multicast_group_best_effort(sockfd, link->ipv4[best_i].addr, socket_role);
            errno = ENOMEM;
            return -1;
        }
        link->mdns_ipv4_transport_addr = link->ipv4[best_i].addr;
        return 1;
    }

    fprintf(stderr, "warning: mdns %s socket: disabling IPv4 transport on iface=%s; no IPv4 multicast membership succeeded\n",
            socket_role, link->name);
    link->mdns_ipv4_transport = 0;
    link->mdns_ipv4_transport_addr = 0;
    return 0;
}

static int configure_mdns_socket6_for_links(int sockfd, struct link_context_set *set, const char *socket_role) {
    size_t i;
    unsigned int first_ifindex = 0;

    for (i = 0; i < set->count; i++) {
        if (!link_context_has_mdns_ipv6_transport(&set->links[i])) {
            continue;
        }
        if (join_mdns_multicast_group6(sockfd, set->links[i].ifindex, set->links[i].name, socket_role) != 0) {
            set->links[i].mdns_ipv6_transport = 0;
            continue;
        }
        if (first_ifindex == 0) {
            first_ifindex = set->links[i].ifindex;
        }
    }
    compact_link_contexts_for_mdns_transport(set);
    if (first_ifindex == 0) {
        errno = EADDRNOTAVAIL;
        return -1;
    }
    return set_outbound_multicast_interface6(sockfd, first_ifindex, socket_role, 1, 1);
}

static int configure_mdns_socket4_for_links(int sockfd, struct link_context_set *set, const char *socket_role) {
    size_t i;
    uint32_t first_ipv4 = 0;

    for (i = 0; i < set->count; i++) {
        int status;

        if (!link_context_has_mdns_ipv4_transport(&set->links[i])) {
            continue;
        }
        status = join_mdns_multicast_group_for_link4(sockfd, &set->links[i], NULL, socket_role, NULL);
        if (status < 0) {
            return -1;
        }
        if (status > 0 && first_ipv4 == 0) {
            first_ipv4 = link_preferred_ipv4_source(&set->links[i]);
        }
    }
    compact_link_contexts_for_mdns_transport(set);
    if (first_ipv4 == 0) {
        errno = EADDRNOTAVAIL;
        return -1;
    }
    return configure_outbound_multicast_socket(sockfd, first_ipv4, socket_role);
}

static int link_set_has_ipv4_membership(const struct link_context_set *set, uint32_t ipv4_addr) {
    size_t i;

    if (set == NULL) {
        return 0;
    }
    for (i = 0; i < set->count; i++) {
        if (link_context_has_mdns_ipv4_transport(&set->links[i]) &&
            link_preferred_ipv4_source(&set->links[i]) == ipv4_addr) {
            return 1;
        }
    }
    return 0;
}

static int link_set_has_ipv6_membership(const struct link_context_set *set, unsigned int ifindex) {
    size_t i;

    if (set == NULL || ifindex == 0) {
        return 0;
    }
    for (i = 0; i < set->count; i++) {
        if (link_context_has_mdns_ipv6_transport(&set->links[i]) &&
            set->links[i].ifindex == ifindex) {
            return 1;
        }
    }
    return 0;
}

static int prepare_mdns_socket4_memberships(int sockfd,
                                            const struct link_context_set *old_links,
                                            struct link_context_set *new_links,
                                            const char *socket_role,
                                            struct mdns_membership_delta *delta) {
    size_t i;
    uint32_t first_ipv4 = 0;

    for (i = 0; i < new_links->count; i++) {
        int status;

        if (!link_context_has_mdns_ipv4_transport(&new_links->links[i])) {
            continue;
        }
        status = join_mdns_multicast_group_for_link4(sockfd, &new_links->links[i], old_links, socket_role, delta);
        if (status < 0) {
            return -1;
        }
        if (status > 0 && first_ipv4 == 0) {
            first_ipv4 = link_preferred_ipv4_source(&new_links->links[i]);
        }
    }
    compact_link_contexts_for_mdns_transport(new_links);
    if (first_ipv4 == 0) {
        errno = EADDRNOTAVAIL;
        return -1;
    }
    return configure_outbound_multicast_socket(sockfd, first_ipv4, socket_role);
}

static int prepare_mdns_socket6_memberships(int sockfd,
                                            const struct link_context_set *old_links,
                                            struct link_context_set *new_links,
                                            const char *socket_role,
                                            struct mdns_membership_delta *delta) {
    size_t i;
    unsigned int first_ifindex = 0;

    for (i = 0; i < new_links->count; i++) {
        if (!link_context_has_mdns_ipv6_transport(&new_links->links[i])) {
            continue;
        }
        if (link_set_has_ipv6_membership(old_links, new_links->links[i].ifindex)) {
            if (first_ifindex == 0) {
                first_ifindex = new_links->links[i].ifindex;
            }
            continue;
        }
        if (join_mdns_multicast_group6(sockfd, new_links->links[i].ifindex, new_links->links[i].name, socket_role) != 0) {
            new_links->links[i].mdns_ipv6_transport = 0;
            continue;
        }
        if (record_mdns_membership_ipv6(delta, new_links->links[i].ifindex, new_links->links[i].name) != 0) {
            drop_mdns_multicast_group6_best_effort(sockfd,
                                                   new_links->links[i].ifindex,
                                                   new_links->links[i].name,
                                                   socket_role);
            errno = ENOMEM;
            return -1;
        }
        if (first_ifindex == 0) {
            first_ifindex = new_links->links[i].ifindex;
        }
    }
    compact_link_contexts_for_mdns_transport(new_links);
    if (first_ifindex == 0) {
        errno = EADDRNOTAVAIL;
        return -1;
    }
    return set_outbound_multicast_interface6(sockfd, first_ifindex, socket_role, 1, 1);
}

static int open_dualstack_mdns_sockets(int shared_bind,
                                       struct link_context_set *links,
                                       int log_bind_errors,
                                       struct mdns_socket_pair *out) {
    int need_ipv4 = link_contexts_need_ipv4_socket(links);
    int need_ipv6 = link_contexts_need_ipv6_socket(links);
    int ipv4_errno = 0;
    int ipv6_errno = 0;

    out->ipv4_fd = -1;
    out->ipv6_fd = -1;
    if (!need_ipv4 && !need_ipv6) {
        errno = EADDRNOTAVAIL;
        return -1;
    }
    if (need_ipv4) {
        out->ipv4_fd = open_bound_mdns_socket(shared_bind, log_bind_errors);
        if (out->ipv4_fd < 0) {
            ipv4_errno = errno;
            g_last_ipv4_socket_errno = ipv4_errno;
            fprintf(stderr,
                    "warning: mdns runtime socket: IPv4 bind 0.0.0.0:%d failed: %s\n",
                    MDNS_PORT,
                    strerror(ipv4_errno));
            disable_link_contexts_mdns_ipv4_transport(links);
            compact_link_contexts_for_mdns_transport(links);
            need_ipv4 = 0;
            if (!need_ipv6) {
                errno = ipv4_errno;
                close_mdns_socket_pair(out);
                return -1;
            }
        } else if (configure_mdns_socket4_for_links(out->ipv4_fd, links, "runtime") != 0) {
            ipv4_errno = errno;
            g_last_ipv4_socket_errno = ipv4_errno;
            if (out->ipv4_fd >= 0) {
                clear_deferred_response_for_sockfd(out->ipv4_fd);
                close(out->ipv4_fd);
                out->ipv4_fd = -1;
            }
            disable_link_contexts_mdns_ipv4_transport(links);
            compact_link_contexts_for_mdns_transport(links);
            need_ipv4 = 0;
            if (!need_ipv6) {
                errno = ipv4_errno;
                close_mdns_socket_pair(out);
                return -1;
            }
            fprintf(stderr,
                    "warning: mdns runtime socket: IPv4 multicast setup failed after bind: %s; continuing with remaining mDNS transports\n",
                    strerror(ipv4_errno));
        } else {
            g_last_ipv4_socket_errno = 0;
        }
    }
    if (need_ipv6) {
        out->ipv6_fd = open_bound_mdns_socket6(shared_bind, log_bind_errors);
        if (out->ipv6_fd < 0 ||
            configure_mdns_socket6_for_links(out->ipv6_fd, links, "runtime") != 0) {
            ipv6_errno = errno;
            g_last_ipv6_socket_errno = ipv6_errno;
            if (out->ipv6_fd >= 0) {
                clear_deferred_response_for_sockfd(out->ipv6_fd);
                close(out->ipv6_fd);
                out->ipv6_fd = -1;
            }
            if (need_ipv4 && out->ipv4_fd >= 0) {
                fprintf(stderr,
                        "warning: mdns runtime socket: IPv6 setup failed (%s); continuing with remaining mDNS transports\n",
                        strerror(ipv6_errno));
                disable_link_contexts_mdns_ipv6_transport(links);
                compact_link_contexts_for_mdns_transport(links);
                return 0;
            }
            close_mdns_socket_pair(out);
            errno = ipv6_errno;
            return -1;
        } else {
            g_last_ipv6_socket_errno = 0;
        }
    }
    compact_link_contexts_for_mdns_transport(links);
    if (links->count == 0) {
        close_mdns_socket_pair(out);
        errno = EADDRNOTAVAIL;
        return -1;
    }
    return 0;
}

static int open_dualstack_mdns_sockets_for_desired(int shared_bind,
                                                   const struct link_context_set *desired_links,
                                                   struct link_context_set *active_links,
                                                   int log_bind_errors,
                                                   struct mdns_socket_pair *out,
                                                   struct mdns_transport_status *status) {
    int open_status;
    struct link_context_set candidate_links;

    candidate_links = *desired_links;
    open_status = open_dualstack_mdns_sockets(shared_bind, &candidate_links, log_bind_errors, out);
    if (open_status != 0) {
        memset(active_links, 0, sizeof(*active_links));
        mdns_transport_status_from_links(desired_links, active_links, out, status);
        return -1;
    }
    *active_links = candidate_links;
    mdns_transport_status_from_links(desired_links, active_links, out, status);
    return mdns_transport_is_healthy(status) ? 0 : 1;
}

static int acquire_dualstack_mdns_sockets(int shared_bind,
                                          const struct link_context_set *desired_links,
                                          struct link_context_set *active_links,
                                          struct mdns_socket_pair *out,
                                          struct mdns_transport_status *status) {
    static const unsigned int retry_delays_ms[TAKEOVER_RETRY_COUNT] = {0, 100, 200, 300, 400, 500};
    size_t i;
    int acquire_status;

    for (i = 0; i < TAKEOVER_RETRY_COUNT; i++) {
        kill_mdnsresponder(SIGTERM);
        sleep_millis(retry_delays_ms[i]);
        acquire_status = open_dualstack_mdns_sockets_for_desired(shared_bind, desired_links, active_links, 0, out, status);
        if (acquire_status >= 0) {
            if (mdns_transport_is_healthy(status)) {
                /* A successful exclusive bind means we own UDP 5353; a respawned
                 * mDNSResponder can no longer hold the port, so reap it best-effort
                 * but never release the socket we just won. */
                if (!shared_bind) {
                    kill_mdnsresponder(SIGKILL);
                }
                fprintf(stderr,
                        shared_bind
                            ? "mDNS required transport shared bind established after SIGTERM + %ums\n"
                            : "mDNS required transport takeover established after SIGTERM + %ums\n",
                        retry_delays_ms[i]);
                return 0;
            }
            fprintf(stderr,
                    "mDNS transport degraded after SIGTERM + %ums; missing required ipv4=%d ipv6=%d, retrying takeover\n",
                    retry_delays_ms[i],
                    status->missing_required_ipv4,
                    status->missing_required_ipv6);
            close_mdns_socket_pair(out);
            memset(active_links, 0, sizeof(*active_links));
        }
    }

    for (i = 0; i < TAKEOVER_RETRY_COUNT; i++) {
        kill_mdnsresponder(SIGKILL);
        sleep_millis(retry_delays_ms[i]);
        acquire_status = open_dualstack_mdns_sockets_for_desired(shared_bind, desired_links, active_links, 0, out, status);
        if (acquire_status >= 0) {
            if (mdns_transport_is_healthy(status)) {
                /* Exclusive bind won: hold the socket and reap any respawned
                 * mDNSResponder best-effort rather than releasing the port. */
                if (!shared_bind) {
                    kill_mdnsresponder(SIGKILL);
                }
                fprintf(stderr,
                        shared_bind
                            ? "mDNS required transport shared bind established after SIGKILL + %ums\n"
                            : "mDNS required transport takeover established after SIGKILL + %ums\n",
                        retry_delays_ms[i]);
                return 0;
            }
            fprintf(stderr,
                    "mDNS transport degraded after SIGKILL + %ums; missing required ipv4=%d ipv6=%d, retrying takeover\n",
                    retry_delays_ms[i],
                    status->missing_required_ipv4,
                    status->missing_required_ipv6);
            close_mdns_socket_pair(out);
            memset(active_links, 0, sizeof(*active_links));
        }
    }

    acquire_status = open_dualstack_mdns_sockets_for_desired(shared_bind, desired_links, active_links, 0, out, status);
    if (acquire_status >= 0) {
        if (mdns_transport_is_healthy(status)) {
            fprintf(stderr, "mDNS required transport acquired after bounded takeover retry\n");
            return 0;
        }
        fprintf(stderr,
                "mDNS transport degraded after bounded takeover retry; serving remaining transports with missing required ipv4=%d ipv6=%d\n",
                status->missing_required_ipv4,
                status->missing_required_ipv6);
        return 1;
    }
    fprintf(stderr, "mDNS required transport takeover failed: could not acquire any usable UDP %d transport\n", MDNS_PORT);
    errno = EADDRINUSE;
    return -1;
}

static void format_dest_addr(const struct sockaddr_in *dest, char *buf, size_t buf_size) {
    char ipbuf[INET_ADDRSTRLEN];

    snprintf(buf, buf_size, "%s:%u",
             ipv4_to_string(dest->sin_addr.s_addr, ipbuf, sizeof(ipbuf)),
             (unsigned int)ntohs(dest->sin_port));
}

static void format_sockaddr_addr(const struct sockaddr *dest, char *buf, size_t buf_size) {
    if (dest->sa_family == AF_INET6) {
        const struct sockaddr_in6 *sin6 = (const struct sockaddr_in6 *)dest;
        char ipbuf[INET6_ADDRSTRLEN];
        const char *printed = inet_ntop(AF_INET6, &sin6->sin6_addr, ipbuf, sizeof(ipbuf));
        if (printed == NULL) {
            printed = "invalid";
        }
        snprintf(buf, buf_size, "[%s%%%u]:%u",
                 printed,
                 sin6->sin6_scope_id,
                 (unsigned int)ntohs(sin6->sin6_port));
        return;
    }
    format_dest_addr((const struct sockaddr_in *)dest, buf, buf_size);
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

static void log_packet_send_failure_detail_any(const char *stage, const struct sockaddr *dest, size_t packet_len,
                                               int answers, int use_snapshot_records, int saved_errno) {
    char destbuf[96];

    remember_last_send_failure(stage, saved_errno);
    format_sockaddr_addr(dest, destbuf, sizeof(destbuf));
    fprintf(stderr,
            "mdns packet send failure: stage=%s dest=%s packet_len=%lu answers=%d records=%s errno=%d (%s)\n",
            stage,
            destbuf,
            (unsigned long)packet_len,
            answers,
            use_snapshot_records ? "snapshot" : "generated",
            saved_errno,
            strerror(saved_errno));
    log_mdns_counters_force("send_failure");
}

static int send_dns_packet_any(const char *stage, int sockfd, const uint8_t *buf, size_t packet_len,
                               const struct sockaddr *dest, socklen_t dest_len,
                               int answers, int use_snapshot_records) {
    static int logged_success_announcement = 0;
    static int logged_success_reply = 0;

    ssize_t sent;
    int saved_errno;

    errno = 0;
    sent = sendto_retry(sockfd, buf, packet_len, 0, dest, dest_len);
    saved_errno = errno;
    if (sent < 0) {
        errno = saved_errno;
        log_packet_send_failure_detail_any(stage, dest, packet_len, answers, use_snapshot_records, saved_errno);
        return -1;
    }

    g_mdns_counters.responses_sent++;
    if (strcmp(stage, "query_response") == 0) {
        if (!logged_success_reply) {
            char destbuf[96];
            format_sockaddr_addr(dest, destbuf, sizeof(destbuf));
            fprintf(stderr,
                    "mdns packet send success: stage=%s dest=%s packet_len=%lu answers=%d\n",
                    stage, destbuf, (unsigned long)packet_len, answers);
            logged_success_reply = 1;
        }
    } else if (!logged_success_announcement) {
        char destbuf[96];
        format_sockaddr_addr(dest, destbuf, sizeof(destbuf));
        fprintf(stderr,
                "mdns packet send success: stage=%s dest=%s packet_len=%lu answers=%d records=%s\n",
                stage, destbuf, (unsigned long)packet_len, answers,
                use_snapshot_records ? "snapshot" : "generated");
        logged_success_announcement = 1;
    }

    return 0;
}

static int add_adisk_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg, uint32_t ttl, int *answers) {
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

static int add_device_info_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg, uint32_t ttl, int *answers) {
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

static int add_airport_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg, uint32_t ttl, int *answers) {
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

static int add_riousbprint_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg, uint32_t ttl, int *answers) {
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

static int add_pdl_datastream_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg, uint32_t ttl, int *answers) {
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

static int add_empty_txt_service_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg,
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

static void init_announcement_packet(size_t *off, int *answers) {
    *off = sizeof(struct dns_header);
    *answers = 0;
}

static int finalize_and_send_announcement_packet_any(int sockfd,
                                                     uint8_t *buf,
                                                     size_t off,
                                                     int answers,
                                                     const struct sockaddr *dest,
                                                     socklen_t dest_len,
                                                     int use_snapshot_records) {
    struct dns_header hdr;

    if (answers <= 0) {
        return 0;
    }

    memset(&hdr, 0, sizeof(hdr));
    hdr.flags = htons(DNS_FLAG_QR | DNS_FLAG_AA);
    hdr.ancount = htons((uint16_t)answers);
    memcpy(buf, &hdr, sizeof(hdr));
    return send_dns_packet_any("announcement", sockfd, buf, off, dest, dest_len, answers, use_snapshot_records);
}

typedef int (*generated_record_adder)(uint8_t *buf,
                                      size_t *off,
                                      size_t cap,
                                      const struct config *cfg,
                                      uint32_t ttl,
                                      int *answers);

static int append_generated_records_with_flush(int sockfd,
                                               uint8_t *buf,
                                               size_t *off,
                                               size_t cap,
                                               int *answers,
                                               const struct sockaddr *dest,
                                               socklen_t dest_len,
                                               const struct config *cfg,
                                               uint32_t ttl,
                                               int use_snapshot_records,
                                               generated_record_adder add_records,
                                               const char *failure_stage) {
    size_t before_off = *off;
    int before_answers = *answers;

    if (add_records(buf, off, cap, cfg, ttl, answers) == 0) {
        return 0;
    }

    *off = before_off;
    *answers = before_answers;
    if (finalize_and_send_announcement_packet_any(sockfd, buf, *off, *answers, dest, dest_len, use_snapshot_records) != 0) {
        return -1;
    }
    init_announcement_packet(off, answers);
    if (add_records(buf, off, cap, cfg, ttl, answers) != 0) {
        log_packet_build_failure("announcement", failure_stage, *off, *answers, use_snapshot_records);
        return -1;
    }
    return 0;
}

static int append_generated_apple_records(int sockfd,
                                          uint8_t *buf,
                                          size_t *off,
                                          size_t cap,
                                          int *answers,
                                          const struct sockaddr *dest,
                                          socklen_t dest_len,
                                          const struct config *cfg,
                                          uint32_t ttl,
                                          int use_snapshot_records) {
    if (append_generated_records_with_flush(sockfd,
                                            buf,
                                            off,
                                            cap,
                                            answers,
                                            dest,
                                            dest_len,
                                            cfg,
                                            ttl,
                                            use_snapshot_records,
                                            add_pdl_datastream_records,
                                            "add_pdl_datastream_records") != 0) {
        return -1;
    }
    if (append_generated_records_with_flush(sockfd,
                                            buf,
                                            off,
                                            cap,
                                            answers,
                                            dest,
                                            dest_len,
                                            cfg,
                                            ttl,
                                            use_snapshot_records,
                                            add_riousbprint_records,
                                            "add_riousbprint_records") != 0) {
        return -1;
    }
    return append_generated_records_with_flush(sockfd,
                                               buf,
                                               off,
                                               cap,
                                               answers,
                                               dest,
                                               dest_len,
                                               cfg,
                                               ttl,
                                               use_snapshot_records,
                                               add_airport_records,
                                               "add_airport_records");
}

static int append_generated_base_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg,
                                         const struct link_context *response_link,
                                         int include_a, int include_aaaa,
                                         uint32_t ttl, int *answers) {
    if (smb_enabled(cfg)) {
        if (add_empty_txt_service_records(buf, off, cap, cfg, cfg->service_type, cfg->port, ttl, answers) != 0) {
            return -1;
        }
    }
    if (afp_enabled(cfg)) {
        if (add_empty_txt_service_records(buf, off, cap, cfg, cfg->afp_service_type, cfg->afp_port, ttl, answers) != 0) {
            return -1;
        }
    }
    if (append_host_address_records(buf, off, cap, cfg->host_fqdn, response_link, include_a, include_aaaa, ttl, answers) != 0) {
        return -1;
    }
    if (add_adisk_records(buf, off, cap, cfg, ttl, answers) != 0) {
        return -1;
    }
    if (add_device_info_records(buf, off, cap, cfg, ttl, answers) != 0) {
        return -1;
    }
    return 0;
}

struct announced_host_set {
    const char *hosts[SNAPSHOT_MAX_RECORDS];
    size_t count;
};

static int host_already_announced(const struct announced_host_set *set, const char *host_fqdn) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (name_equals(set->hosts[i], host_fqdn)) {
            return 1;
        }
    }
    return 0;
}

static int remember_announced_host(struct announced_host_set *set, const char *host_fqdn) {
    if (set->count >= SNAPSHOT_MAX_RECORDS) {
        return -1;
    }
    set->hosts[set->count++] = host_fqdn;
    return 0;
}

static int send_announcement_any(int sockfd,
                                 const struct sockaddr *dest,
                                 socklen_t dest_len,
                                 const struct config *cfg,
                                 const struct link_context *response_link,
                                 uint32_t ttl,
                                 const struct service_record_set *snapshot_records,
                                 int use_snapshot_records) {
    uint8_t buf[BUF_SIZE];
    size_t off;
    int answers;
    size_t i;
    struct announced_host_set announced_hosts;
    static int logged_duplicate_host_suppression = 0;

    memset(&announced_hosts, 0, sizeof(announced_hosts));
    init_announcement_packet(&off, &answers);
    if (append_generated_base_records(buf, &off, sizeof(buf), cfg, response_link, 1, 1, ttl, &answers) != 0) {
        log_packet_build_failure("announcement", "add_core_records", off, answers, use_snapshot_records);
        return -1;
    }
    if (append_generated_apple_records(sockfd,
                                       buf,
                                       &off,
                                       sizeof(buf),
                                       &answers,
                                       dest,
                                       dest_len,
                                       cfg,
                                       ttl,
                                       use_snapshot_records) != 0) {
        return -1;
    }
    if (use_snapshot_records) {
        if (finalize_and_send_announcement_packet_any(sockfd, buf, off, answers, dest, dest_len, use_snapshot_records) != 0) {
            return -1;
        }
        for (i = 0; i < snapshot_records->count; i++) {
            int include_host_a;
            size_t before_host_a_off;
            int before_host_a_answers;

            if (is_suppressed_snapshot_service_type(snapshot_records->records[i].service_type)) {
                continue;
            }
            if (snapshot_record_overridden_by_generated(cfg, &snapshot_records->records[i])) {
                continue;
            }
            init_announcement_packet(&off, &answers);
            if (add_service_record_answers(buf, &off, sizeof(buf), &snapshot_records->records[i], ttl, &answers) != 0) {
                log_snapshot_record_build_failure("announcement", "add_service_record_answers", i,
                                                  &snapshot_records->records[i], off, answers);
                log_packet_build_failure("announcement", "add_service_record_answers", off, answers, use_snapshot_records);
                return -1;
            }
            include_host_a = snapshot_records->records[i].host_fqdn[0] != '\0' &&
                             !host_already_announced(&announced_hosts, snapshot_records->records[i].host_fqdn);
            if (include_host_a) {
                before_host_a_off = off;
                before_host_a_answers = answers;
                if (add_snapshot_host_address_records(buf, &off, sizeof(buf), &snapshot_records->records[i], response_link, 1, 1, ttl, &answers) != 0) {
                    off = before_host_a_off;
                    answers = before_host_a_answers;
                    if (finalize_and_send_announcement_packet_any(sockfd, buf, off, answers, dest, dest_len, use_snapshot_records) != 0) {
                        return -1;
                    }
                    init_announcement_packet(&off, &answers);
                    if (add_snapshot_host_address_records(buf, &off, sizeof(buf), &snapshot_records->records[i], response_link, 1, 1, ttl, &answers) != 0) {
                        log_snapshot_record_build_failure("announcement", "add_snapshot_host_address_records", i,
                                                          &snapshot_records->records[i], off, answers);
                        log_packet_build_failure("announcement", "add_snapshot_host_address_records", off, answers, use_snapshot_records);
                        return -1;
                    }
                }
                if (remember_announced_host(&announced_hosts, snapshot_records->records[i].host_fqdn) != 0) {
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
            if (finalize_and_send_announcement_packet_any(sockfd, buf, off, answers, dest, dest_len, use_snapshot_records) != 0) {
                return -1;
            }
        }
    } else {
        if (finalize_and_send_announcement_packet_any(sockfd, buf, off, answers, dest, dest_len, use_snapshot_records) != 0) {
            return -1;
        }
    }
    return 0;
}

static int send_announcement(int sockfd, const struct sockaddr_in *dest, const struct config *cfg,
                             const struct link_context *response_link, uint32_t ttl,
                             const struct service_record_set *snapshot_records, int use_snapshot_records) {
    return send_announcement_any(sockfd,
                                 (const struct sockaddr *)dest,
                                 sizeof(*dest),
                                 cfg,
                                 response_link,
                                 ttl,
                                 snapshot_records,
                                 use_snapshot_records);
}

static int known_answer_ttl_is_fresh(uint32_t known_ttl, uint32_t advertised_ttl) {
    return known_ttl > advertised_ttl / 2;
}

static int planned_rr_rdata_equals(const struct planned_rr *rr, const uint8_t *rdata, uint16_t rdlength) {
    return rr->rdlength == rdlength && memcmp(rr->rdata, rdata, rdlength) == 0;
}

static int planned_rr_add_raw(struct planned_rr_set *set,
                              int routes,
                              const char *owner,
                              uint16_t type,
                              uint16_t rrclass,
                              uint32_t ttl,
                              const uint8_t *rdata,
                              uint16_t rdlength) {
    size_t i;

    if (routes == 0 || owner == NULL || owner[0] == '\0') {
        return 0;
    }
    if (rdlength > PLANNED_RDATA_MAX) {
        set->truncated = 1;
        return -1;
    }
    for (i = 0; i < set->count; i++) {
        if (set->records[i].type == type &&
            set->records[i].rrclass == rrclass &&
            name_equals(set->records[i].owner, owner) &&
            planned_rr_rdata_equals(&set->records[i], rdata, rdlength)) {
            set->records[i].routes |= routes;
            return 0;
        }
    }
    if (set->count >= PLANNED_RR_MAX) {
        set->truncated = 1;
        return -1;
    }
    strncpy(set->records[set->count].owner, owner, sizeof(set->records[set->count].owner) - 1);
    set->records[set->count].owner[sizeof(set->records[set->count].owner) - 1] = '\0';
    set->records[set->count].type = type;
    set->records[set->count].rrclass = rrclass;
    set->records[set->count].ttl = ttl;
    memcpy(set->records[set->count].rdata, rdata, rdlength);
    set->records[set->count].rdlength = rdlength;
    set->records[set->count].routes = routes;
    set->count++;
    return 0;
}

static int planned_rr_add_name(struct planned_rr_set *set,
                               int routes,
                               const char *owner,
                               uint16_t type,
                               uint16_t rrclass,
                               uint32_t ttl,
                               const char *target) {
    uint8_t rdata[PLANNED_RDATA_MAX];
    size_t off = 0;

    if (encode_name(rdata, &off, sizeof(rdata), target) != 0) {
        return -1;
    }
    return planned_rr_add_raw(set, routes, owner, type, rrclass, ttl, rdata, (uint16_t)off);
}

static int planned_rr_add_srv(struct planned_rr_set *set,
                              int routes,
                              const char *owner,
                              const char *target,
                              uint16_t port,
                              uint32_t ttl) {
    uint8_t rdata[PLANNED_RDATA_MAX];
    size_t off = 0;

    if (append_u16(rdata, &off, sizeof(rdata), 0) != 0 ||
        append_u16(rdata, &off, sizeof(rdata), 0) != 0 ||
        append_u16(rdata, &off, sizeof(rdata), port) != 0 ||
        encode_name(rdata, &off, sizeof(rdata), target) != 0) {
        return -1;
    }
    return planned_rr_add_raw(set, routes, owner, DNS_TYPE_SRV, DNS_CLASS_IN_UNIQUE, ttl, rdata, (uint16_t)off);
}

static int planned_rr_add_txt_items(struct planned_rr_set *set,
                                    int routes,
                                    const char *owner,
                                    const char **strings,
                                    const uint8_t *lengths,
                                    size_t string_count,
                                    uint32_t ttl) {
    uint8_t rdata[PLANNED_RDATA_MAX];
    size_t off = 0;
    size_t i;

    if (string_count == 0) {
        uint8_t zero = 0;
        return planned_rr_add_raw(set, routes, owner, DNS_TYPE_TXT, DNS_CLASS_IN_UNIQUE, ttl, &zero, 1);
    }
    for (i = 0; i < string_count; i++) {
        size_t slen = lengths != NULL ? lengths[i] : strlen(strings[i]);
        uint8_t len;
        if (slen > 255) {
            return -1;
        }
        len = (uint8_t)slen;
        if (append_bytes(rdata, &off, sizeof(rdata), &len, 1) != 0 ||
            append_bytes(rdata, &off, sizeof(rdata), strings[i], slen) != 0) {
            return -1;
        }
    }
    return planned_rr_add_raw(set, routes, owner, DNS_TYPE_TXT, DNS_CLASS_IN_UNIQUE, ttl, rdata, (uint16_t)off);
}

static int planned_rr_add_txt_empty(struct planned_rr_set *set, int routes, const char *owner, uint32_t ttl) {
    return planned_rr_add_txt_items(set, routes, owner, NULL, NULL, 0, ttl);
}

static int planned_rr_add_a(struct planned_rr_set *set, int routes, const char *owner, uint32_t ipv4_addr, uint32_t ttl) {
    return planned_rr_add_raw(set, routes, owner, DNS_TYPE_A, DNS_CLASS_IN_UNIQUE, ttl,
                              (const uint8_t *)&ipv4_addr, 4);
}

static int planned_rr_add_aaaa(struct planned_rr_set *set,
                               int routes,
                               const char *owner,
                               const struct in6_addr *ipv6_addr,
                               uint32_t ttl) {
    return planned_rr_add_raw(set, routes, owner, DNS_TYPE_AAAA, DNS_CLASS_IN_UNIQUE, ttl,
                              ipv6_addr->s6_addr, 16);
}

static int planned_rr_add_link_addresses(struct planned_rr_set *set,
                                         int routes,
                                         const char *owner,
                                         const struct link_context *link,
                                         int include_a,
                                         int include_aaaa,
                                         uint32_t ttl) {
    size_t i;

    if (owner == NULL || owner[0] == '\0' || link == NULL) {
        return 0;
    }
    if (include_a) {
        for (i = 0; i < link->ipv4_count; i++) {
            if (planned_rr_add_a(set, routes, owner, link->ipv4[i].addr, ttl) != 0) {
                return -1;
            }
        }
    }
    if (include_aaaa) {
        for (i = 0; i < link->ipv6_count; i++) {
            if (!link_ipv6_addr_is_samba_bindable(&link->ipv6[i])) {
                continue;
            }
            if (planned_rr_add_aaaa(set, routes, owner, &link->ipv6[i].addr, ttl) != 0) {
                return -1;
            }
        }
    }
    return 0;
}

static int plan_empty_txt_service_records(struct planned_rr_set *set,
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

static int plan_smb_records(struct planned_rr_set *set,
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

static int plan_afp_records(struct planned_rr_set *set,
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

static int plan_adisk_records(struct planned_rr_set *set,
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

static int plan_device_info_records(struct planned_rr_set *set,
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

static int plan_airport_records(struct planned_rr_set *set,
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

static int plan_riousbprint_records(struct planned_rr_set *set,
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

static int plan_pdl_datastream_records(struct planned_rr_set *set,
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

static int plan_snapshot_record(struct planned_rr_set *set,
                                int routes,
                                const struct service_record *record,
                                const struct link_context *link,
                                int include_ptr,
                                int include_srv,
                                int include_txt,
                                int include_a,
                                int include_aaaa,
                                uint32_t ttl) {
    const char *txts[SNAPSHOT_MAX_TXT_ITEMS];
    uint8_t txt_lengths[SNAPSHOT_MAX_TXT_ITEMS];
    size_t i;

    if (is_suppressed_snapshot_service_type(record->service_type)) {
        return 0;
    }
    if (include_ptr &&
        planned_rr_add_name(set, routes, record->service_type, DNS_TYPE_PTR, DNS_CLASS_IN, ttl, record->instance_fqdn) != 0) {
        return -1;
    }
    if (include_srv && planned_rr_add_srv(set, routes, record->instance_fqdn, record->host_fqdn, record->port, ttl) != 0) {
        return -1;
    }
    if (include_txt) {
        for (i = 0; i < record->txt_count; i++) {
            txts[i] = record->txt[i];
            txt_lengths[i] = record->txt_len[i];
        }
        if (planned_rr_add_txt_items(set, routes, record->instance_fqdn, txts, txt_lengths, record->txt_count, ttl) != 0) {
            return -1;
        }
    }
    return planned_rr_add_link_addresses(set, routes, record->host_fqdn, link, include_a, include_aaaa, ttl);
}

static int plan_service_type_enumeration_type(struct planned_rr_set *set,
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

static int plan_service_type_enumeration_records(struct planned_rr_set *set,
                                                 int routes,
                                                 const struct config *cfg,
                                                 const struct service_record_set *snapshot_records,
                                                 int use_snapshot_records) {
    size_t i;

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
    if (use_snapshot_records) {
        for (i = 0; i < snapshot_records->count; i++) {
            if (is_suppressed_snapshot_service_type(snapshot_records->records[i].service_type)) {
                continue;
            }
            if (plan_service_type_enumeration_type(set,
                                                   routes,
                                                   snapshot_records->records[i].service_type,
                                                   cfg->ttl) != 0) {
                return -1;
            }
        }
    }
    return 0;
}

static int planned_set_has_route(const struct planned_rr_set *set, int route) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if ((set->records[i].routes & route) != 0) {
            return 1;
        }
    }
    return 0;
}

static int planned_set_has_any_route(const struct planned_rr_set *set) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (set->records[i].routes != 0) {
            return 1;
        }
    }
    return 0;
}

static int planned_rr_matches_known_answer(const struct planned_rr *rr,
                                           const char *owner,
                                           uint16_t type,
                                           uint16_t rrclass,
                                           const uint8_t *packet,
                                           size_t packet_len,
                                           size_t rdata_cursor,
                                           uint16_t rdlength) {
    if (rr->type != type ||
        (rr->rrclass & 0x7FFF) != (rrclass & 0x7FFF) ||
        !name_equals(rr->owner, owner)) {
        return 0;
    }
    if (type == DNS_TYPE_PTR) {
        char known_name[MAX_NAME];
        char planned_name[MAX_NAME];
        size_t rdata_end = rdata_cursor + rdlength;
        size_t planned_cursor = 0;
        if (decode_name(packet, packet_len, &rdata_cursor, known_name, sizeof(known_name)) != 0 ||
            decode_name(rr->rdata, rr->rdlength, &planned_cursor, planned_name, sizeof(planned_name)) != 0) {
            return 0;
        }
        if (rdata_cursor != rdata_end || planned_cursor != rr->rdlength) {
            return 0;
        }
        return name_equals(known_name, planned_name);
    }
    if (type == DNS_TYPE_SRV) {
        char known_target[MAX_NAME];
        char planned_target[MAX_NAME];
        size_t rdata_end = rdata_cursor + rdlength;
        size_t known_cursor = rdata_cursor + 6;
        size_t planned_cursor = 6;
        if (rdlength < 6 || rr->rdlength < 6 || rdata_cursor + rdlength > packet_len ||
            memcmp(packet + rdata_cursor, rr->rdata, 6) != 0 ||
            decode_name(packet, packet_len, &known_cursor, known_target, sizeof(known_target)) != 0 ||
            decode_name(rr->rdata, rr->rdlength, &planned_cursor, planned_target, sizeof(planned_target)) != 0) {
            return 0;
        }
        if (known_cursor != rdata_end || planned_cursor != rr->rdlength) {
            return 0;
        }
        return name_equals(known_target, planned_target);
    }
    if (rdata_cursor + rdlength > packet_len) {
        return 0;
    }
    if (rr->rdlength != rdlength) {
        return 0;
    }
    return memcmp(packet + rdata_cursor, rr->rdata, rdlength) == 0;
}

static void suppress_planned_known_answers(const uint8_t *packet,
                                           size_t packet_len,
                                           size_t cursor,
                                           uint16_t answer_count,
                                           struct planned_rr_set *planned) {
    uint16_t i;

    for (i = 0; i < answer_count; i++) {
        char owner[MAX_NAME];
        uint16_t type;
        uint16_t rrclass;
        uint32_t ttl;
        uint16_t rdlength;
        size_t rdata_cursor;
        size_t j;

        if (decode_name(packet, packet_len, &cursor, owner, sizeof(owner)) != 0 || cursor + 10 > packet_len) {
            return;
        }
        memcpy(&type, packet + cursor, 2);
        memcpy(&rrclass, packet + cursor + 2, 2);
        memcpy(&ttl, packet + cursor + 4, 4);
        memcpy(&rdlength, packet + cursor + 8, 2);
        cursor += 10;
        type = ntohs(type);
        rrclass = ntohs(rrclass);
        ttl = ntohl(ttl);
        rdlength = ntohs(rdlength);
        if (cursor + rdlength > packet_len) {
            return;
        }
        rdata_cursor = cursor;
        cursor += rdlength;
        if ((rrclass & 0x7FFF) != DNS_CLASS_IN) {
            continue;
        }
        for (j = 0; j < planned->count; j++) {
            if (planned->records[j].routes == 0 ||
                !known_answer_ttl_is_fresh(ttl, planned->records[j].ttl)) {
                continue;
            }
            if (planned_rr_matches_known_answer(&planned->records[j],
                                                owner,
                                                type,
                                                rrclass,
                                                packet,
                                                packet_len,
                                                rdata_cursor,
                                                rdlength)) {
                planned->records[j].routes = 0;
            }
        }
    }
}

static uint16_t sockaddr_port_host(const struct sockaddr *addr) {
    if (addr == NULL) {
        return 0;
    }
    if (addr->sa_family == AF_INET) {
        const struct sockaddr_in *sin = (const struct sockaddr_in *)addr;
        return ntohs(sin->sin_port);
    }
    if (addr->sa_family == AF_INET6) {
        const struct sockaddr_in6 *sin6 = (const struct sockaddr_in6 *)addr;
        return ntohs(sin6->sin6_port);
    }
    return 0;
}

static int source_can_receive_unicast_response(const struct sockaddr *source,
                                               const struct link_context *response_link) {
    if (source == NULL || response_link == NULL) {
        return 0;
    }
    if (source->sa_family == AF_INET) {
        const struct sockaddr_in *sin = (const struct sockaddr_in *)source;
        return source_matches_link_ipv4_subnet(sin->sin_addr.s_addr, response_link);
    }
    if (source->sa_family == AF_INET6) {
        const struct sockaddr_in6 *sin6 = (const struct sockaddr_in6 *)source;
        size_t i;

        if (sin6->sin6_scope_id != 0 && sin6->sin6_scope_id == response_link->ifindex) {
            return 1;
        }
        for (i = 0; i < response_link->ipv6_count; i++) {
            if (response_link->ipv6[i].link_local) {
                continue;
            }
            if (ipv6_prefix_matches(&sin6->sin6_addr,
                                    &response_link->ipv6[i].addr,
                                    response_link->ipv6[i].prefix_len)) {
                return 1;
            }
        }
    }
    return 0;
}

static int plan_question_answers(struct planned_rr_set *planned,
                                 int route,
                                 const char *qname,
                                 uint16_t qtype,
                                 const struct config *cfg,
                                 const struct link_context *response_link,
                                 const struct service_record_set *snapshot_records,
                                 int use_snapshot_records,
                                 const char *instance_fqdn,
                                 const char *afp_instance_fqdn,
                                 const char *adisk_instance_fqdn,
                                 const char *device_info_instance_fqdn,
                                 const char *airport_instance_fqdn,
                                 const char *riousbprint_instance_fqdn,
                                 const char *pdl_datastream_instance_fqdn) {
    int planned_generated_apple_service_type = 0;

    if (name_equals(qname, DNS_SD_SERVICE_ENUMERATION_NAME) &&
        (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
        return plan_service_type_enumeration_records(planned,
                                                     route,
                                                     cfg,
                                                     snapshot_records,
                                                     use_snapshot_records);
    }
    if (smb_enabled(cfg) && name_equals(qname, cfg->service_type) &&
        (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
        return plan_smb_records(planned, route, cfg, instance_fqdn, response_link, 1, 1, 1, 1, 1);
    }
    if (afp_enabled(cfg) && name_equals(qname, cfg->afp_service_type) &&
        (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
        return plan_afp_records(planned, route, cfg, afp_instance_fqdn, response_link, 1, 1, 1, 1, 1);
    }
    if (adisk_enabled(cfg) && name_equals(qname, cfg->adisk_service_type) &&
        (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
        return plan_adisk_records(planned, route, cfg, adisk_instance_fqdn, response_link, 1, 1, 1, 1, 1);
    }
    if (cfg->device_model[0] != '\0' && name_equals(qname, cfg->device_info_service_type) &&
        (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
        return plan_device_info_records(planned, route, cfg, device_info_instance_fqdn, response_link, 1, 1, 1, 1, 1);
    }
    if (is_airport_enabled(cfg) && name_equals(qname, cfg->airport_service_type) &&
        (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
        if (plan_airport_records(planned, route, cfg, airport_instance_fqdn, response_link, 1, 1, 1, 1, 1) != 0) {
            return -1;
        }
        planned_generated_apple_service_type = 1;
    }
    if (is_riousbprint_enabled(cfg) && name_equals(qname, RIOUSBPRINT_SERVICE_TYPE) &&
        (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
        if (plan_riousbprint_records(planned, route, cfg, riousbprint_instance_fqdn, response_link, 1, 1, 1, 1, 1) != 0) {
            return -1;
        }
        planned_generated_apple_service_type = 1;
    }
    if (is_pdl_datastream_enabled(cfg) && name_equals(qname, PDL_DATASTREAM_SERVICE_TYPE) &&
        (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
        if (plan_pdl_datastream_records(planned, route, cfg, pdl_datastream_instance_fqdn, response_link, 1, 1, 1, 1, 1) != 0) {
            return -1;
        }
        planned_generated_apple_service_type = 1;
    }
    if (planned_generated_apple_service_type && !use_snapshot_records) {
        return 0;
    }
    if (smb_enabled(cfg) && name_equals(qname, instance_fqdn)) {
        return plan_smb_records(planned, route, cfg, instance_fqdn, response_link,
                                0,
                                qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY,
                                qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY);
    }
    if (afp_enabled(cfg) && name_equals(qname, afp_instance_fqdn)) {
        return plan_afp_records(planned, route, cfg, afp_instance_fqdn, response_link,
                                0,
                                qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY,
                                qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY);
    }
    if (adisk_enabled(cfg) && name_equals(qname, adisk_instance_fqdn)) {
        return plan_adisk_records(planned, route, cfg, adisk_instance_fqdn, response_link,
                                  0,
                                  qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                  qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY,
                                  qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                  qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY);
    }
    if (cfg->device_model[0] != '\0' && name_equals(qname, device_info_instance_fqdn)) {
        return plan_device_info_records(planned, route, cfg, device_info_instance_fqdn, response_link,
                                        0,
                                        qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                        qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY,
                                        qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                        qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY);
    }
    if (is_airport_enabled(cfg) && name_equals(qname, airport_instance_fqdn)) {
        return plan_airport_records(planned, route, cfg, airport_instance_fqdn, response_link,
                                    0,
                                    qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                    qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY,
                                    qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                    qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY);
    }
    if (is_riousbprint_enabled(cfg) && name_equals(qname, riousbprint_instance_fqdn)) {
        return plan_riousbprint_records(planned, route, cfg, riousbprint_instance_fqdn, response_link,
                                        0,
                                        qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                        qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY,
                                        qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                        qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY);
    }
    if (is_pdl_datastream_enabled(cfg) && name_equals(qname, pdl_datastream_instance_fqdn)) {
        return plan_pdl_datastream_records(planned, route, cfg, pdl_datastream_instance_fqdn, response_link,
                                           0,
                                           qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                           qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY,
                                           qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                           qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY);
    }
    if (name_equals(qname, cfg->host_fqdn)) {
        return planned_rr_add_link_addresses(planned,
                                             route,
                                             cfg->host_fqdn,
                                             response_link,
                                             qtype == DNS_TYPE_A || qtype == DNS_TYPE_ANY,
                                             qtype == DNS_TYPE_AAAA || qtype == DNS_TYPE_ANY,
                                             cfg->ttl);
    }
    if (use_snapshot_records) {
        size_t j;
        for (j = 0; j < snapshot_records->count; j++) {
            const struct service_record *record = &snapshot_records->records[j];
            if (is_suppressed_snapshot_service_type(record->service_type)) {
                continue;
            }
            if (snapshot_record_overridden_by_generated(cfg, record)) {
                continue;
            }
            if (name_equals(qname, record->service_type) && (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
                if (plan_snapshot_record(planned, route, record, response_link, 1, 1, 1, 1, 1, cfg->ttl) != 0) {
                    return -1;
                }
            } else if (name_equals(qname, record->instance_fqdn)) {
                if (plan_snapshot_record(planned,
                                         route,
                                         record,
                                         response_link,
                                         0,
                                         qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                         qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY,
                                         qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                         qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                         cfg->ttl) != 0) {
                    return -1;
                }
            } else if (name_equals(qname, record->host_fqdn)) {
                if (planned_rr_add_link_addresses(planned,
                                                  route,
                                                  record->host_fqdn,
                                                  response_link,
                                                  qtype == DNS_TYPE_A || qtype == DNS_TYPE_ANY,
                                                  qtype == DNS_TYPE_AAAA || qtype == DNS_TYPE_ANY,
                                                  cfg->ttl) != 0) {
                    return -1;
                }
            }
        }
    }
    return 0;
}

static int add_planned_rr_to_packet(uint8_t *reply,
                                    size_t *off,
                                    size_t reply_cap,
                                    const struct planned_rr *rr,
                                    int legacy_unicast) {
    uint16_t rrclass = rr->rrclass;
    uint32_t ttl = rr->ttl;

    if (legacy_unicast) {
        rrclass = (uint16_t)(rrclass & 0x7FFF);
        if (ttl > LEGACY_UNICAST_TTL_MAX) {
            ttl = LEGACY_UNICAST_TTL_MAX;
        }
    }
    return encode_name(reply, off, reply_cap, rr->owner) != 0 ||
           append_u16(reply, off, reply_cap, rr->type) != 0 ||
           append_u16(reply, off, reply_cap, rrclass) != 0 ||
           append_u32(reply, off, reply_cap, ttl) != 0 ||
           append_u16(reply, off, reply_cap, rr->rdlength) != 0 ||
           append_bytes(reply, off, reply_cap, rr->rdata, rr->rdlength) != 0
               ? -1
               : 0;
}

static int build_planned_response_packet(uint8_t *reply,
                                         size_t reply_cap,
                                         size_t *reply_len,
                                         int *answer_count,
                                         uint16_t response_id,
                                         int route,
                                         int legacy_unicast,
                                         const struct response_question_section *questions,
                                         const struct planned_rr_set *planned) {
    struct dns_header hdr;
    size_t off = sizeof(struct dns_header);
    int answers = 0;
    size_t i;

    memset(&hdr, 0, sizeof(hdr));
    hdr.id = response_id;
    hdr.flags = htons(DNS_FLAG_QR | DNS_FLAG_AA);
    if (legacy_unicast && questions != NULL && questions->count > 0) {
        if (questions->bytes == NULL || questions->len == 0 ||
            append_bytes(reply, &off, reply_cap, questions->bytes, questions->len) != 0) {
            return -1;
        }
        hdr.qdcount = htons(questions->count);
    }
    for (i = 0; i < planned->count; i++) {
        if ((planned->records[i].routes & route) == 0) {
            continue;
        }
        if (add_planned_rr_to_packet(reply, &off, reply_cap, &planned->records[i], legacy_unicast) != 0) {
            return -1;
        }
        answers++;
    }
    hdr.ancount = htons((uint16_t)answers);
    memcpy(reply, &hdr, sizeof(hdr));
    *reply_len = off;
    *answer_count = answers;
    return 0;
}

static void stored_question_section_as_response(const struct stored_question_section *stored,
                                                struct response_question_section *out) {
    out->bytes = stored->bytes;
    out->len = stored->len;
    out->count = stored->count;
}

static int send_planned_response_route(int sockfd,
                                       const struct planned_rr_set *planned,
                                       int route,
                                       uint16_t response_id,
                                       const struct response_question_section *questions,
                                       const struct sockaddr *dest,
                                       socklen_t dest_len,
                                       int use_snapshot_records,
                                       int delay_multicast) {
    uint8_t reply[BUF_SIZE];
    size_t reply_len;
    int answers;
    int legacy_unicast = route == MDNS_REPLY_LEGACY_UNICAST;

    if (build_planned_response_packet(reply,
                                      sizeof(reply),
                                      &reply_len,
                                      &answers,
                                      response_id,
                                      route,
                                      legacy_unicast,
                                      questions,
                                      planned) != 0) {
        return -1;
    }
    if (answers <= 0) {
        return 0;
    }
    if (delay_multicast && route == MDNS_REPLY_MULTICAST) {
        delay_multicast_query_response();
    }
    return send_dns_packet_any("query_response",
                               sockfd,
                               reply,
                               reply_len,
                               dest,
                               dest_len,
                               answers,
                               use_snapshot_records);
}

static void clear_deferred_response(void) {
    memset(&g_deferred_response, 0, sizeof(g_deferred_response));
}

static void clear_deferred_response_for_sockfd(int sockfd) {
    if (g_deferred_response.active && g_deferred_response.sockfd == sockfd) {
        clear_deferred_response();
    }
}

static int sockaddr_endpoint_equal(const struct sockaddr *a, socklen_t a_len,
                                   const struct sockaddr *b, socklen_t b_len) {
    if (a == NULL || b == NULL || a->sa_family != b->sa_family) {
        return 0;
    }
    if (a->sa_family == AF_INET) {
        const struct sockaddr_in *sin_a = (const struct sockaddr_in *)a;
        const struct sockaddr_in *sin_b = (const struct sockaddr_in *)b;
        if (a_len < (socklen_t)sizeof(*sin_a) || b_len < (socklen_t)sizeof(*sin_b)) {
            return 0;
        }
        return sin_a->sin_port == sin_b->sin_port &&
               sin_a->sin_addr.s_addr == sin_b->sin_addr.s_addr;
    }
    if (a->sa_family == AF_INET6) {
        const struct sockaddr_in6 *sin6_a = (const struct sockaddr_in6 *)a;
        const struct sockaddr_in6 *sin6_b = (const struct sockaddr_in6 *)b;
        if (a_len < (socklen_t)sizeof(*sin6_a) || b_len < (socklen_t)sizeof(*sin6_b)) {
            return 0;
        }
        return sin6_a->sin6_port == sin6_b->sin6_port &&
               sin6_a->sin6_scope_id == sin6_b->sin6_scope_id &&
               memcmp(&sin6_a->sin6_addr, &sin6_b->sin6_addr, sizeof(sin6_a->sin6_addr)) == 0;
    }
    return 0;
}

static int deferred_response_matches_source(int sockfd, const struct sockaddr *source, socklen_t source_len) {
    if (!g_deferred_response.active || g_deferred_response.sockfd != sockfd) {
        return 0;
    }
    return sockaddr_endpoint_equal((const struct sockaddr *)&g_deferred_response.source,
                                   g_deferred_response.source_len,
                                   source,
                                   source_len);
}

static int copy_sockaddr_storage(struct sockaddr_storage *out,
                                 socklen_t *out_len,
                                 const struct sockaddr *src,
                                 socklen_t src_len) {
    if (src == NULL || src_len > (socklen_t)sizeof(*out)) {
        return -1;
    }
    memset(out, 0, sizeof(*out));
    memcpy(out, src, src_len);
    *out_len = src_len;
    return 0;
}

static int flush_deferred_response_now(void) {
    int status = 0;
    struct response_question_section questions;

    if (!g_deferred_response.active) {
        return 0;
    }
    stored_question_section_as_response(&g_deferred_response.questions, &questions);
    if (planned_set_has_route(&g_deferred_response.planned, MDNS_REPLY_LEGACY_UNICAST)) {
        if (send_planned_response_route(g_deferred_response.sockfd,
                                        &g_deferred_response.planned,
                                        MDNS_REPLY_LEGACY_UNICAST,
                                        g_deferred_response.response_id,
                                        &questions,
                                        (const struct sockaddr *)&g_deferred_response.source,
                                        g_deferred_response.source_len,
                                        g_deferred_response.use_snapshot_records,
                                        0) != 0) {
            status = -1;
        }
    }
    if (planned_set_has_route(&g_deferred_response.planned, MDNS_REPLY_UNICAST)) {
        if (send_planned_response_route(g_deferred_response.sockfd,
                                        &g_deferred_response.planned,
                                        MDNS_REPLY_UNICAST,
                                        g_deferred_response.response_id,
                                        &questions,
                                        (const struct sockaddr *)&g_deferred_response.source,
                                        g_deferred_response.source_len,
                                        g_deferred_response.use_snapshot_records,
                                        0) != 0) {
            status = -1;
        }
    }
    if (planned_set_has_route(&g_deferred_response.planned, MDNS_REPLY_MULTICAST)) {
        if (send_planned_response_route(g_deferred_response.sockfd,
                                        &g_deferred_response.planned,
                                        MDNS_REPLY_MULTICAST,
                                        0,
                                        &questions,
                                        (const struct sockaddr *)&g_deferred_response.multicast_dest,
                                        g_deferred_response.multicast_dest_len,
                                        g_deferred_response.use_snapshot_records,
                                        0) != 0) {
            status = -1;
        }
    }
    clear_deferred_response();
    return status;
}

static int flush_deferred_response_if_due(long long now_ms) {
    if (!g_deferred_response.active || now_ms < g_deferred_response.due_ms) {
        return 0;
    }
    return flush_deferred_response_now();
}

static long long deferred_response_adjust_wait_ms(long long now_ms, long long wait_ms) {
    long long deferred_wait;

    if (!g_deferred_response.active) {
        return wait_ms;
    }
    deferred_wait = g_deferred_response.due_ms - now_ms;
    if (deferred_wait < 0) {
        deferred_wait = 0;
    }
    return deferred_wait < wait_ms ? deferred_wait : wait_ms;
}

static int defer_planned_response(int sockfd,
                                  uint16_t response_id,
                                  const struct sockaddr *multicast_dest,
                                  socklen_t multicast_dest_len,
                                  const struct sockaddr *source,
                                  socklen_t source_len,
                                  const struct response_question_section *questions,
                                  int use_snapshot_records,
                                  const struct planned_rr_set *planned) {
    if (!planned_set_has_any_route(planned)) {
        clear_deferred_response();
        return 0;
    }
    clear_deferred_response();
    g_deferred_response.active = 1;
    g_deferred_response.sockfd = sockfd;
    g_deferred_response.due_ms = monotonic_millis() + TC_KNOWN_ANSWER_DEFER_MS;
    g_deferred_response.response_id = response_id;
    g_deferred_response.use_snapshot_records = use_snapshot_records;
    g_deferred_response.planned = *planned;
    if (questions != NULL && questions->count > 0) {
        if (questions->bytes == NULL || questions->len > sizeof(g_deferred_response.questions.bytes)) {
            clear_deferred_response();
            return -1;
        }
        memcpy(g_deferred_response.questions.bytes, questions->bytes, questions->len);
        g_deferred_response.questions.len = questions->len;
        g_deferred_response.questions.count = questions->count;
    }
    if (copy_sockaddr_storage(&g_deferred_response.multicast_dest,
                              &g_deferred_response.multicast_dest_len,
                              multicast_dest,
                              multicast_dest_len) != 0 ||
        copy_sockaddr_storage(&g_deferred_response.source,
                              &g_deferred_response.source_len,
                              source,
                              source_len) != 0) {
        clear_deferred_response();
        return -1;
    }
    return 0;
}

static int handle_query_any(int sockfd,
                            const uint8_t *packet,
                            size_t packet_len,
                            const struct sockaddr *multicast_dest,
                            socklen_t multicast_dest_len,
                            const struct sockaddr *source,
                            socklen_t source_len,
                            const struct config *cfg,
                            const struct link_context *response_link,
                            const struct service_record_set *snapshot_records,
                            int use_snapshot_records) {
    struct dns_header hdr;
    size_t cursor = sizeof(struct dns_header);
    uint16_t qdcount;
    uint16_t ancount;
    uint16_t query_id;
    uint16_t flags;
    char instance_fqdn[MAX_NAME];
    char afp_instance_fqdn[MAX_NAME];
    char adisk_instance_fqdn[MAX_NAME];
    char device_info_instance_fqdn[MAX_NAME];
    char airport_instance_fqdn[MAX_NAME];
    char riousbprint_instance_fqdn[MAX_NAME];
    char pdl_datastream_instance_fqdn[MAX_NAME];
    uint16_t i;
    int status = 0;
    int source_port;
    int legacy_unicast_query;
    int source_allows_unicast;
    size_t question_section_start = sizeof(struct dns_header);
    struct response_question_section questions;
    static struct planned_rr_set planned;

    memset(&planned, 0, sizeof(planned));
    memset(&questions, 0, sizeof(questions));
    instance_fqdn[0] = '\0';
    afp_instance_fqdn[0] = '\0';
    adisk_instance_fqdn[0] = '\0';
    device_info_instance_fqdn[0] = '\0';
    airport_instance_fqdn[0] = '\0';
    riousbprint_instance_fqdn[0] = '\0';
    pdl_datastream_instance_fqdn[0] = '\0';

    if (packet_len < sizeof(struct dns_header)) {
        return 0;
    }
    memcpy(&hdr, packet, sizeof(hdr));
    flags = ntohs(hdr.flags);
    if (flags & DNS_FLAG_QR) {
        return 0;
    }

    qdcount = ntohs(hdr.qdcount);
    ancount = ntohs(hdr.ancount);
    query_id = hdr.id;
    source_port = sockaddr_port_host(source);
    legacy_unicast_query = source_port != 0 && source_port != MDNS_PORT;
    source_allows_unicast = source_can_receive_unicast_response(source, response_link);
    if (smb_enabled(cfg) &&
        build_instance_fqdn(instance_fqdn, sizeof(instance_fqdn), cfg->instance_name, cfg->service_type) != 0) {
        log_packet_build_failure("query_response", "build_instance_fqdn", sizeof(struct dns_header), 0, use_snapshot_records);
        return 0;
    }
    if (afp_enabled(cfg) &&
        build_instance_fqdn(afp_instance_fqdn, sizeof(afp_instance_fqdn), cfg->instance_name, cfg->afp_service_type) != 0) {
        log_packet_build_failure("query_response", "build_afp_instance_fqdn", sizeof(struct dns_header), 0, use_snapshot_records);
        return 0;
    }
    if (adisk_enabled(cfg) &&
        build_instance_fqdn(adisk_instance_fqdn, sizeof(adisk_instance_fqdn), cfg->instance_name, cfg->adisk_service_type) != 0) {
        log_packet_build_failure("query_response", "build_adisk_instance_fqdn", sizeof(struct dns_header), 0, use_snapshot_records);
        return 0;
    }
    if (cfg->device_model[0] != '\0' &&
        build_instance_fqdn(device_info_instance_fqdn, sizeof(device_info_instance_fqdn), cfg->instance_name, cfg->device_info_service_type) != 0) {
        log_packet_build_failure("query_response", "build_device_info_instance_fqdn", sizeof(struct dns_header), 0, use_snapshot_records);
        return 0;
    }
    if (is_airport_enabled(cfg) &&
        build_instance_fqdn(airport_instance_fqdn, sizeof(airport_instance_fqdn), cfg->instance_name, cfg->airport_service_type) != 0) {
        log_packet_build_failure("query_response", "build_airport_instance_fqdn", sizeof(struct dns_header), 0, use_snapshot_records);
        return 0;
    }
    if (is_riousbprint_enabled(cfg) &&
        build_instance_fqdn(riousbprint_instance_fqdn, sizeof(riousbprint_instance_fqdn), cfg->riousbprint_instance_name, RIOUSBPRINT_SERVICE_TYPE) != 0) {
        log_packet_build_failure("query_response", "build_riousbprint_instance_fqdn", sizeof(struct dns_header), 0, use_snapshot_records);
        return 0;
    }
    if (is_pdl_datastream_enabled(cfg) &&
        build_instance_fqdn(pdl_datastream_instance_fqdn, sizeof(pdl_datastream_instance_fqdn), cfg->riousbprint_instance_name, PDL_DATASTREAM_SERVICE_TYPE) != 0) {
        log_packet_build_failure("query_response", "build_pdl_datastream_instance_fqdn", sizeof(struct dns_header), 0, use_snapshot_records);
        return 0;
    }

    if (qdcount == 0) {
        if (deferred_response_matches_source(sockfd, source, source_len)) {
            suppress_planned_known_answers(packet, packet_len, cursor, ancount, &g_deferred_response.planned);
            if ((flags & DNS_FLAG_TC) == 0) {
                return flush_deferred_response_now();
            }
        }
        return 0;
    }

    if (deferred_response_matches_source(sockfd, source, source_len) && (flags & DNS_FLAG_TC) == 0) {
        clear_deferred_response();
    }

    for (i = 0; i < qdcount; i++) {
        char qname[MAX_NAME];
        uint16_t qtype;
        uint16_t qclass;
        uint16_t qclass_raw;
        uint16_t qclass_base;
        int reply_route;

        if (decode_name(packet, packet_len, &cursor, qname, sizeof(qname)) != 0 ||
            cursor + 4 > packet_len) {
            return 0;
        }
        memcpy(&qtype, packet + cursor, 2);
        memcpy(&qclass, packet + cursor + 2, 2);
        cursor += 4;
        qtype = ntohs(qtype);
        qclass_raw = ntohs(qclass);
        qclass_base = (uint16_t)(qclass_raw & 0x7FFF);
        if (qclass_base != DNS_CLASS_IN && qclass_base != DNS_CLASS_ANY) {
            continue;
        }
        if (legacy_unicast_query && source_allows_unicast) {
            reply_route = MDNS_REPLY_LEGACY_UNICAST;
        } else if ((qclass_raw & DNS_CLASS_QU) && source_allows_unicast) {
            reply_route = MDNS_REPLY_UNICAST;
            if (source_port == MDNS_PORT) {
                reply_route |= MDNS_REPLY_MULTICAST;
            }
        } else {
            reply_route = MDNS_REPLY_MULTICAST;
        }
        if (plan_question_answers(&planned,
                                  reply_route,
                                  qname,
                                  qtype,
                                  cfg,
                                  response_link,
                                  snapshot_records,
                                  use_snapshot_records,
                                  instance_fqdn,
                                  afp_instance_fqdn,
                                  adisk_instance_fqdn,
                                  device_info_instance_fqdn,
                                  airport_instance_fqdn,
                                  riousbprint_instance_fqdn,
                                  pdl_datastream_instance_fqdn) != 0) {
            log_packet_build_failure("query_response", "plan_question_answers", cursor, 0, use_snapshot_records);
            return -1;
        }
    }
    questions.bytes = packet + question_section_start;
    questions.len = cursor - question_section_start;
    questions.count = qdcount;

    suppress_planned_known_answers(packet, packet_len, cursor, ancount, &planned);
    if (planned.count > 0) {
        g_mdns_counters.query_packets_matched++;
    }

    if (flags & DNS_FLAG_TC) {
        if (defer_planned_response(sockfd,
                                   query_id,
                                   multicast_dest,
                                   multicast_dest_len,
                                   source,
                                   source_len,
                                   &questions,
                                   use_snapshot_records,
                                   &planned) != 0) {
            return -1;
        }
        return 0;
    }

    if (planned_set_has_route(&planned, MDNS_REPLY_LEGACY_UNICAST)) {
        if (send_planned_response_route(sockfd,
                                        &planned,
                                        MDNS_REPLY_LEGACY_UNICAST,
                                        query_id,
                                        &questions,
                                        source,
                                        source_len,
                                        use_snapshot_records,
                                        0) != 0) {
            status = -1;
        }
    }

    if (planned_set_has_route(&planned, MDNS_REPLY_UNICAST)) {
        if (send_planned_response_route(sockfd,
                                        &planned,
                                        MDNS_REPLY_UNICAST,
                                        query_id,
                                        &questions,
                                        source,
                                        source_len,
                                        use_snapshot_records,
                                        0) != 0) {
            status = -1;
        }
    }

    if (planned_set_has_route(&planned, MDNS_REPLY_MULTICAST)) {
        if (send_planned_response_route(sockfd,
                                        &planned,
                                        MDNS_REPLY_MULTICAST,
                                        0,
                                        &questions,
                                        multicast_dest,
                                        multicast_dest_len,
                                        use_snapshot_records,
                                        1) != 0) {
            status = -1;
        }
    }

    return status;
}

static int handle_query(int sockfd, const uint8_t *packet, size_t packet_len,
                        const struct sockaddr_in *multicast_dest, const struct sockaddr_in *source,
                        const struct config *cfg, const struct link_context *response_link,
                        const struct service_record_set *snapshot_records, int use_snapshot_records) {
    return handle_query_any(sockfd,
                            packet,
                            packet_len,
                            (const struct sockaddr *)multicast_dest,
                            sizeof(*multicast_dest),
                            (const struct sockaddr *)source,
                            sizeof(*source),
                            cfg,
                            response_link,
                            snapshot_records,
                            use_snapshot_records);
}

static int source_matches_link_ipv4_subnet(uint32_t source_ipv4_addr, const struct link_context *link) {
    size_t i;

    for (i = 0; i < link->ipv4_count; i++) {
        uint32_t netmask = effective_ipv4_netmask(link->ipv4[i].addr, link->ipv4[i].netmask);
        if (netmask == 0) {
            if (source_ipv4_addr == link->ipv4[i].addr) {
                return 1;
            }
        } else if ((source_ipv4_addr & netmask) == (link->ipv4[i].addr & netmask)) {
            return 1;
        }
    }
    return 0;
}

static const struct link_context *select_response_link_ipv4(const struct link_context_set *links,
                                                            const struct sockaddr_in *source) {
    size_t i;

    if (links->count == 0) {
        return NULL;
    }
    if (source != NULL && source->sin_addr.s_addr != 0) {
        for (i = 0; i < links->count; i++) {
            if (!link_context_has_mdns_ipv4_transport(&links->links[i])) {
                continue;
            }
            if (source_matches_link_ipv4_subnet(source->sin_addr.s_addr, &links->links[i])) {
                return &links->links[i];
            }
        }
    }
    for (i = 0; i < links->count; i++) {
        if (link_context_has_mdns_ipv4_transport(&links->links[i])) {
            return &links->links[i];
        }
    }
    return NULL;
}

static const struct link_context *select_response_link_ipv6(const struct link_context_set *links,
                                                            const struct sockaddr_in6 *source) {
    size_t i;

    if (links->count == 0) {
        return NULL;
    }
    if (source != NULL) {
        if (source->sin6_scope_id != 0) {
            for (i = 0; i < links->count; i++) {
                if (link_context_has_mdns_ipv6_transport(&links->links[i]) &&
                    links->links[i].ifindex == source->sin6_scope_id) {
                    return &links->links[i];
                }
            }
        }
        for (i = 0; i < links->count; i++) {
            size_t j;
            if (!link_context_has_mdns_ipv6_transport(&links->links[i])) {
                continue;
            }
            for (j = 0; j < links->links[i].ipv6_count; j++) {
                if (links->links[i].ipv6[j].link_local) {
                    continue;
                }
                if (ipv6_prefix_matches(&source->sin6_addr,
                                        &links->links[i].ipv6[j].addr,
                                        links->links[i].ipv6[j].prefix_len)) {
                    return &links->links[i];
                }
            }
        }
    }
    for (i = 0; i < links->count; i++) {
        if (link_context_has_mdns_ipv6_transport(&links->links[i])) {
            return &links->links[i];
        }
    }
    return NULL;
}

static int set_link_outbound_interface4(int sockfd, const struct link_context *link) {
    uint32_t ipv4_addr = link_preferred_ipv4_source(link);

    if (ipv4_addr == 0) {
        errno = EADDRNOTAVAIL;
        return -1;
    }
    return set_outbound_multicast_interface(sockfd, ipv4_addr, "runtime", 0, 0);
}

static int set_link_outbound_interface4_for_peer(int sockfd, const struct link_context *link, uint32_t source_ipv4_addr) {
    uint32_t ipv4_addr = link_ipv4_source_for_peer(link, source_ipv4_addr);

    if (ipv4_addr == 0) {
        errno = EADDRNOTAVAIL;
        return -1;
    }
    return set_outbound_multicast_interface(sockfd, ipv4_addr, "runtime", 0, 0);
}

static int set_link_outbound_interface6(int sockfd, const struct link_context *link) {
    return set_outbound_multicast_interface6(sockfd, link->ifindex, "runtime", 0, 0);
}

static void send_link_announcement_pair(const struct mdns_socket_pair *sockets,
                                        const struct link_context *link,
                                        const struct sockaddr_in *dest4,
                                        const struct sockaddr_in6 *dest6,
                                        const struct config *cfg,
                                        uint32_t ttl,
                                        const struct service_record_set *snapshot_records,
                                        int use_snapshot_records,
                                        const char *stage) {
    if (sockets->ipv4_fd >= 0 && link_context_has_mdns_ipv4_transport(link)) {
        char sourcebuf[INET_ADDRSTRLEN];
        uint32_t source_ipv4 = link_preferred_ipv4_source(link);
        fprintf(stderr,
                "mdns announce: stage=%s family=ipv4 iface=%s source=%s records=%s\n",
                stage,
                link->name,
                source_ipv4 != 0 ? ipv4_to_string(source_ipv4, sourcebuf, sizeof(sourcebuf)) : "unknown",
                use_snapshot_records ? "snapshot" : "generated");
        if (set_link_outbound_interface4(sockets->ipv4_fd, link) != 0 ||
            send_announcement(sockets->ipv4_fd, dest4, cfg, link, ttl, snapshot_records, use_snapshot_records) != 0) {
            char detail[160];
            snprintf(detail, sizeof(detail), "stage=%s iface=%s family=ipv4", stage, link->name);
            log_send_failure(stage, dest4, use_snapshot_records, detail);
        }
    }
    if (sockets->ipv6_fd >= 0 && link_context_has_mdns_ipv6_transport(link)) {
        struct sockaddr_in6 scoped_dest6;
        fprintf(stderr,
                "mdns announce: stage=%s family=ipv6 iface=%s source_ifindex=%u records=%s\n",
                stage,
                link->name,
                link->ifindex,
                use_snapshot_records ? "snapshot" : "generated");
        scoped_mdns_dest6_for_link(&scoped_dest6, dest6, link);
        if (set_link_outbound_interface6(sockets->ipv6_fd, link) != 0 ||
            send_announcement_any(sockets->ipv6_fd,
                                  (const struct sockaddr *)&scoped_dest6,
                                  sizeof(scoped_dest6),
                                  cfg,
                                  link,
                                  ttl,
                                  snapshot_records,
                                  use_snapshot_records) != 0) {
            char destbuf[96];
            format_sockaddr_addr((const struct sockaddr *)&scoped_dest6, destbuf, sizeof(destbuf));
            fprintf(stderr,
                    "mdns send failure: stage=%s dest=%s records=%s detail=iface=%s family=ipv6\n",
                    stage,
                    destbuf,
                    use_snapshot_records ? "snapshot" : "generated",
                    link->name);
        }
    }
}

static void announce_all_links(const struct mdns_socket_pair *sockets,
                               const struct link_context_set *links,
                               const struct sockaddr_in *dest4,
                               const struct sockaddr_in6 *dest6,
                               const struct config *cfg,
                               const struct service_record_set *snapshot_records,
                               int use_snapshot_records,
                               const char *stage) {
    size_t i;

    for (i = 0; i < links->count; i++) {
        send_link_announcement_pair(sockets,
                                    &links->links[i],
                                    dest4,
                                    dest6,
                                    cfg,
                                    cfg->ttl,
                                    snapshot_records,
                                    use_snapshot_records,
                                    stage);
    }
}

static void send_link_goodbyes(const struct mdns_socket_pair *sockets,
                               const struct link_context_set *links,
                               const struct sockaddr_in *dest4,
                               const struct sockaddr_in6 *dest6,
                               const struct config *cfg,
                               const struct service_record_set *snapshot_records,
                               int use_snapshot_records) {
    size_t i;

    for (i = 0; i < links->count; i++) {
        send_link_announcement_pair(sockets,
                                    &links->links[i],
                                    dest4,
                                    dest6,
                                    cfg,
                                    0,
                                    snapshot_records,
                                    use_snapshot_records,
                                    "goodbye");
    }
}

static void send_link_goodbyes_for_missing(const struct mdns_socket_pair *sockets,
                                           const struct link_context_set *old_links,
                                           const struct link_context_set *new_links,
                                           const struct sockaddr_in *dest4,
                                           const struct sockaddr_in6 *dest6,
                                           const struct config *cfg,
                                           const struct service_record_set *snapshot_records,
                                           int use_snapshot_records) {
    size_t i;

    for (i = 0; i < old_links->count; i++) {
        if (link_context_set_contains(new_links, &old_links->links[i])) {
            continue;
        }
        send_link_announcement_pair(sockets,
                                    &old_links->links[i],
                                    dest4,
                                    dest6,
                                    cfg,
                                    0,
                                    snapshot_records,
                                    use_snapshot_records,
                                    "goodbye");
    }
}

static int prepare_runtime_mdns_sockets_for_links(int shared_bind,
                                                  struct mdns_socket_pair *sockets,
                                                  const struct link_context_set *old_links,
                                                  struct link_context_set *new_links) {
    int need_ipv4 = link_contexts_need_ipv4_socket(new_links);
    int need_ipv6 = link_contexts_need_ipv6_socket(new_links);
    int opened_ipv4 = 0;
    int opened_ipv6 = 0;
    int ipv4_errno = 0;
    int ipv6_errno = 0;
    struct mdns_membership_delta delta;

    init_mdns_membership_delta(&delta);

    if (!need_ipv4 && !need_ipv6) {
        errno = EADDRNOTAVAIL;
        return -1;
    }

    if (need_ipv4 && sockets->ipv4_fd < 0) {
        sockets->ipv4_fd = open_bound_mdns_socket(shared_bind, 1);
        if (sockets->ipv4_fd < 0) {
            ipv4_errno = errno;
            g_last_ipv4_socket_errno = ipv4_errno;
            fprintf(stderr,
                    "warning: mdns runtime socket: IPv4 bind 0.0.0.0:%d failed: %s\n",
                    MDNS_PORT,
                    strerror(ipv4_errno));
            if (need_ipv6) {
                fprintf(stderr,
                        "warning: mdns runtime socket: IPv4 transport unavailable after bind failure; continuing with remaining mDNS transports\n");
                disable_link_contexts_mdns_ipv4_transport(new_links);
                compact_link_contexts_for_mdns_transport(new_links);
                need_ipv4 = 0;
            } else {
                goto fail;
            }
        } else {
            g_last_ipv4_socket_errno = 0;
        }
        if (sockets->ipv4_fd >= 0) {
            opened_ipv4 = 1;
        }
    }
    if (need_ipv6 && sockets->ipv6_fd < 0) {
        sockets->ipv6_fd = open_bound_mdns_socket6(shared_bind, 1);
        if (sockets->ipv6_fd < 0) {
            ipv6_errno = errno;
            g_last_ipv6_socket_errno = ipv6_errno;
            if (need_ipv4 && sockets->ipv4_fd >= 0) {
                fprintf(stderr,
                        "warning: mdns runtime socket: IPv6 socket open failed (%s); continuing with remaining mDNS transports\n",
                        strerror(ipv6_errno));
                disable_link_contexts_mdns_ipv6_transport(new_links);
                compact_link_contexts_for_mdns_transport(new_links);
                need_ipv6 = 0;
            } else {
                goto fail;
            }
        } else {
            g_last_ipv6_socket_errno = 0;
        }
        if (sockets->ipv6_fd >= 0) {
            opened_ipv6 = 1;
        }
    }

    if (need_ipv4 &&
        prepare_mdns_socket4_memberships(sockets->ipv4_fd,
                                         opened_ipv4 ? NULL : old_links,
                                         new_links,
                                         "runtime",
                                         &delta) != 0) {
        ipv4_errno = errno;
        g_last_ipv4_socket_errno = ipv4_errno;
        if (ipv4_errno == EADDRNOTAVAIL && need_ipv6 && sockets->ipv6_fd >= 0) {
            fprintf(stderr,
                    "warning: mdns runtime socket: IPv4 membership update found no usable links; continuing with remaining mDNS transports\n");
            disable_link_contexts_mdns_ipv4_transport(new_links);
            need_ipv4 = 0;
            if (opened_ipv4 && sockets->ipv4_fd >= 0) {
                clear_deferred_response_for_sockfd(sockets->ipv4_fd);
                close(sockets->ipv4_fd);
                sockets->ipv4_fd = -1;
            }
        } else {
            goto fail;
        }
    }
    if (need_ipv4) {
        g_last_ipv4_socket_errno = 0;
    }
    if (need_ipv6 &&
        prepare_mdns_socket6_memberships(sockets->ipv6_fd,
                                         opened_ipv6 ? NULL : old_links,
                                         new_links,
                                         "runtime",
                                         &delta) != 0) {
        ipv6_errno = errno;
        g_last_ipv6_socket_errno = ipv6_errno;
        if (need_ipv4 && sockets->ipv4_fd >= 0) {
            fprintf(stderr,
                    "warning: mdns runtime socket: IPv6 membership update failed (%s); continuing with remaining mDNS transports\n",
                    strerror(ipv6_errno));
            disable_link_contexts_mdns_ipv6_transport(new_links);
            if (opened_ipv6 && sockets->ipv6_fd >= 0) {
                clear_deferred_response_for_sockfd(sockets->ipv6_fd);
                close(sockets->ipv6_fd);
                sockets->ipv6_fd = -1;
            }
            compact_link_contexts_for_mdns_transport(new_links);
            return 0;
        }
        goto fail;
    }
    if (need_ipv6) {
        g_last_ipv6_socket_errno = 0;
    }
    compact_link_contexts_for_mdns_transport(new_links);
    if (new_links->count == 0) {
        errno = EADDRNOTAVAIL;
        goto fail;
    }
    return 0;

fail:
    rollback_mdns_membership_delta(sockets, &delta);
    if (opened_ipv4 && sockets->ipv4_fd >= 0) {
        clear_deferred_response_for_sockfd(sockets->ipv4_fd);
        close(sockets->ipv4_fd);
        sockets->ipv4_fd = -1;
    }
    if (opened_ipv6 && sockets->ipv6_fd >= 0) {
        clear_deferred_response_for_sockfd(sockets->ipv6_fd);
        close(sockets->ipv6_fd);
        sockets->ipv6_fd = -1;
    }
    return -1;
}

static void retire_runtime_mdns_memberships_for_missing(struct mdns_socket_pair *sockets,
                                                        const struct link_context_set *old_links,
                                                        const struct link_context_set *new_links) {
    size_t i;

    if (sockets->ipv4_fd >= 0) {
        for (i = 0; i < old_links->count; i++) {
            uint32_t ipv4_addr = link_preferred_ipv4_source(&old_links->links[i]);
            if (!link_context_has_mdns_ipv4_transport(&old_links->links[i]) ||
                ipv4_addr == 0 ||
                link_set_has_ipv4_membership(new_links, ipv4_addr)) {
                continue;
            }
            drop_mdns_multicast_group_best_effort(sockets->ipv4_fd, ipv4_addr, "runtime");
        }
    }
    if (sockets->ipv6_fd >= 0) {
        for (i = 0; i < old_links->count; i++) {
            if (!link_context_has_mdns_ipv6_transport(&old_links->links[i]) ||
                link_set_has_ipv6_membership(new_links, old_links->links[i].ifindex)) {
                continue;
            }
            drop_mdns_multicast_group6_best_effort(sockets->ipv6_fd,
                                                   old_links->links[i].ifindex,
                                                   old_links->links[i].name,
                                                   "runtime");
        }
    }
}

static void close_unused_runtime_mdns_socket_families(struct mdns_socket_pair *sockets,
                                                      const struct link_context_set *links) {
    if (!link_contexts_need_ipv4_socket(links) && sockets->ipv4_fd >= 0) {
        clear_deferred_response_for_sockfd(sockets->ipv4_fd);
        close(sockets->ipv4_fd);
        sockets->ipv4_fd = -1;
    }
    if (!link_contexts_need_ipv6_socket(links) && sockets->ipv6_fd >= 0) {
        clear_deferred_response_for_sockfd(sockets->ipv6_fd);
        close(sockets->ipv6_fd);
        sockets->ipv6_fd = -1;
    }
}

static int apply_runtime_link_change(int shared_bind,
                                     struct mdns_socket_pair *sockets,
                                     struct link_context_set *active_links,
                                     const struct link_context_set *new_links,
                                     const struct sockaddr_in *dest4,
                                     const struct sockaddr_in6 *dest6,
                                     const struct config *cfg,
                                     const struct service_record_set *snapshot_records,
                                     int use_snapshot_records) {
    struct link_context_set applied_links;

    applied_links = *new_links;
    if (prepare_runtime_mdns_sockets_for_links(shared_bind, sockets, active_links, &applied_links) != 0) {
        return -1;
    }
    send_link_goodbyes_for_missing(sockets,
                                   active_links,
                                   &applied_links,
                                   dest4,
                                   dest6,
                                   cfg,
                                   snapshot_records,
                                   use_snapshot_records);
    retire_runtime_mdns_memberships_for_missing(sockets, active_links, &applied_links);
    close_unused_runtime_mdns_socket_families(sockets, &applied_links);
    *active_links = applied_links;
    return 0;
}

static int recover_runtime_link_change_with_takeover(int shared_bind,
                                                     struct mdns_socket_pair *sockets,
                                                     struct link_context_set *active_links,
                                                     const struct link_context_set *desired_links,
                                                     const struct sockaddr_in *dest4,
                                                     const struct sockaddr_in6 *dest6,
                                                     const struct config *cfg,
                                                     const struct service_record_set *snapshot_records,
                                                     int use_snapshot_records,
                                                     struct mdns_transport_status *status) {
    static const unsigned int retry_delays_ms[TAKEOVER_RETRY_COUNT] = {0, 100, 200, 300, 400, 500};
    size_t i;

    if (apply_runtime_link_change(shared_bind,
                                  sockets,
                                  active_links,
                                  desired_links,
                                  dest4,
                                  dest6,
                                  cfg,
                                  snapshot_records,
                                  use_snapshot_records) == 0) {
        mdns_transport_status_from_links(desired_links, active_links, sockets, status);
        if (mdns_transport_is_healthy(status)) {
            return 0;
        }
    } else {
        mdns_transport_status_from_links(desired_links, active_links, sockets, status);
    }

    for (i = 0; i < TAKEOVER_RETRY_COUNT; i++) {
        kill_mdnsresponder(SIGTERM);
        sleep_millis(retry_delays_ms[i]);
        if (apply_runtime_link_change(shared_bind,
                                      sockets,
                                      active_links,
                                      desired_links,
                                      dest4,
                                      dest6,
                                      cfg,
                                      snapshot_records,
                                      use_snapshot_records) == 0) {
            mdns_transport_status_from_links(desired_links, active_links, sockets, status);
            if (mdns_transport_is_healthy(status)) {
                fprintf(stderr, "mDNS runtime required transport recovered after SIGTERM + %ums\n",
                        retry_delays_ms[i]);
                return 0;
            }
        } else {
            mdns_transport_status_from_links(desired_links, active_links, sockets, status);
        }
    }

    for (i = 0; i < TAKEOVER_RETRY_COUNT; i++) {
        kill_mdnsresponder(SIGKILL);
        sleep_millis(retry_delays_ms[i]);
        if (apply_runtime_link_change(shared_bind,
                                      sockets,
                                      active_links,
                                      desired_links,
                                      dest4,
                                      dest6,
                                      cfg,
                                      snapshot_records,
                                      use_snapshot_records) == 0) {
            mdns_transport_status_from_links(desired_links, active_links, sockets, status);
            if (mdns_transport_is_healthy(status)) {
                fprintf(stderr, "mDNS runtime required transport recovered after SIGKILL + %ums\n",
                        retry_delays_ms[i]);
                return 0;
            }
        } else {
            mdns_transport_status_from_links(desired_links, active_links, sockets, status);
        }
    }

    mdns_transport_status_from_links(desired_links, active_links, sockets, status);
    return mdns_transport_has_active_socket(status) ? 1 : -1;
}

int main(int argc, char **argv) {
    struct config cfg;
    struct service_record_set snapshot_records;
    struct sockaddr_in mdns_dest;
    struct sockaddr_in6 mdns_dest6;
    int i;
    int use_snapshot_records = 0;
    int auto_ip = 0;
    int print_auto_ip_cidrs = 0;
    int print_smb_bind_interfaces = 0;
    int print_smb_bind_interfaces_lan = 0;
    int print_mdns_socket_families = 0;
    int print_nt_hash_from_stdin_flag = 0;
    int auto_contexts_ready = 0;
    struct link_context_set desired_links;
    struct link_context_set active_links;
    int capture_only = 0;
    int snapshot_capture_failed = 0;
    int snapshot_capture_skipped = 0;
    int trusted_snapshot_written = 0;
    size_t startup_burst_index = 0;
    long long startup_burst_start_ms = 0;

    memset(&cfg, 0, sizeof(cfg));
    memset(&snapshot_records, 0, sizeof(snapshot_records));
    memset(&desired_links, 0, sizeof(desired_links));
    memset(&active_links, 0, sizeof(active_links));
    strcpy(cfg.service_type, "_smb._tcp.local.");
    strcpy(cfg.adisk_service_type, "_adisk._tcp.local.");
    strcpy(cfg.afp_service_type, AFP_SERVICE_TYPE);
    strcpy(cfg.device_info_service_type, "_device-info._tcp.local.");
    strcpy(cfg.airport_service_type, AIRPORT_SERVICE_TYPE);
    cfg.port = 445;
    cfg.adisk_port = 9;
    cfg.afp_port = AFP_DEFAULT_PORT;
    cfg.airport_port = AIRPORT_DEFAULT_PORT;
    cfg.riousbprint_port = RIOUSBPRINT_DEFAULT_PORT;
    cfg.pdl_datastream_port = PDL_DATASTREAM_DEFAULT_PORT;
    cfg.ttl = 120;

    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--save-all-snapshot") == 0 && i + 1 < argc) {
            strncpy(cfg.save_all_snapshot_path, argv[++i], sizeof(cfg.save_all_snapshot_path) - 1);
        } else if (strcmp(argv[i], "--save-airport-snapshot") == 0 && i + 1 < argc) {
            strncpy(cfg.save_airport_snapshot_path, argv[++i], sizeof(cfg.save_airport_snapshot_path) - 1);
        } else if (strcmp(argv[i], "--save-snapshot") == 0 && i + 1 < argc) {
            strncpy(cfg.save_snapshot_path, argv[++i], sizeof(cfg.save_snapshot_path) - 1);
        } else if (strcmp(argv[i], "--skip-capture-if-snapshot-newer-than-boot") == 0 && i + 1 < argc) {
            strncpy(cfg.skip_capture_if_snapshot_newer_than_boot_path,
                    argv[++i],
                    sizeof(cfg.skip_capture_if_snapshot_newer_than_boot_path) - 1);
        } else if (strcmp(argv[i], "--snapshot-newer-than-boot") == 0 && i + 1 < argc) {
            strncpy(cfg.snapshot_newer_than_boot_path,
                    argv[++i],
                    sizeof(cfg.snapshot_newer_than_boot_path) - 1);
        } else if (strcmp(argv[i], "--load-snapshot") == 0 && i + 1 < argc) {
            strncpy(cfg.load_snapshot_path, argv[++i], sizeof(cfg.load_snapshot_path) - 1);
        } else if (strcmp(argv[i], "--generated-airport-services") == 0) {
            cfg.generated_airport_services = 1;
        } else if (strcmp(argv[i], "--diskless") == 0) {
            cfg.diskless = 1;
        } else if (strcmp(argv[i], "--afp") == 0) {
            cfg.advertise_afp = 1;
        } else if (strcmp(argv[i], "--auto-ip") == 0) {
            auto_ip = 1;
        } else if (strcmp(argv[i], "--print-auto-ip-cidrs") == 0) {
            print_auto_ip_cidrs = 1;
        } else if (strcmp(argv[i], "--print-smb-bind-interfaces") == 0) {
            print_smb_bind_interfaces = 1;
        } else if (strcmp(argv[i], "--print-smb-bind-interfaces-lan") == 0) {
            print_smb_bind_interfaces_lan = 1;
        } else if (strcmp(argv[i], "--print-mdns-socket-families") == 0) {
            print_mdns_socket_families = 1;
        } else if (strcmp(argv[i], "--print-nt-hash-from-stdin") == 0) {
            print_nt_hash_from_stdin_flag = 1;
        } else if (strcmp(argv[i], "--version") == 0) {
            printf("%d\n", ADVERTISER_VERSION_CODE);
            return EXIT_OK;
        } else if (strcmp(argv[i], "--debug-logging") == 0) {
            g_debug_logging = 1;
        } else if (strcmp(argv[i], "--adisk-shares-file") == 0 && i + 1 < argc) {
            strncpy(cfg.adisk_shares_file, argv[++i], sizeof(cfg.adisk_shares_file) - 1);
        } else if (strcmp(argv[i], "--adisk-sys-wama") == 0 && i + 1 < argc) {
            strncpy(cfg.adisk_sys_wama, argv[++i], sizeof(cfg.adisk_sys_wama) - 1);
        } else if (strcmp(argv[i], "--device-model") == 0 && i + 1 < argc) {
            strncpy(cfg.device_model, argv[++i], sizeof(cfg.device_model) - 1);
        } else if (strcmp(argv[i], "--riousbprint-name") == 0 && i + 1 < argc) {
            strncpy(cfg.riousbprint_instance_name, argv[++i], sizeof(cfg.riousbprint_instance_name) - 1);
        } else if (strcmp(argv[i], "--riousbprint-note") == 0 && i + 1 < argc) {
            strncpy(cfg.riousbprint_note, argv[++i], sizeof(cfg.riousbprint_note) - 1);
        } else if (strcmp(argv[i], "--riousbprint-mfg") == 0 && i + 1 < argc) {
            strncpy(cfg.riousbprint_mfg, argv[++i], sizeof(cfg.riousbprint_mfg) - 1);
        } else if (strcmp(argv[i], "--riousbprint-mdl") == 0 && i + 1 < argc) {
            strncpy(cfg.riousbprint_mdl, argv[++i], sizeof(cfg.riousbprint_mdl) - 1);
        } else if (strcmp(argv[i], "--riousbprint-serial") == 0 && i + 1 < argc) {
            strncpy(cfg.riousbprint_serial, argv[++i], sizeof(cfg.riousbprint_serial) - 1);
        } else if (strcmp(argv[i], "--riousbprint-cmd") == 0 && i + 1 < argc) {
            strncpy(cfg.riousbprint_cmd, argv[++i], sizeof(cfg.riousbprint_cmd) - 1);
        } else if (strcmp(argv[i], "--riousbprint-vendor-id") == 0 && i + 1 < argc) {
            cfg.riousbprint_vendor_id = (unsigned int)strtoul(argv[++i], NULL, 0);
        } else if (strcmp(argv[i], "--riousbprint-product-id") == 0 && i + 1 < argc) {
            cfg.riousbprint_product_id = (unsigned int)strtoul(argv[++i], NULL, 0);
        } else if (strcmp(argv[i], "--riousbprint-port") == 0 && i + 1 < argc) {
            cfg.riousbprint_port = (uint16_t)atoi(argv[++i]);
        } else if (strcmp(argv[i], "--pdl-datastream-port") == 0 && i + 1 < argc) {
            cfg.pdl_datastream_port = (uint16_t)atoi(argv[++i]);
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
        } else {
            usage(argv[0]);
            return EXIT_USAGE;
        }
    }

    if (print_nt_hash_from_stdin_flag) {
        return print_nt_hash_from_stdin();
    }
    if (print_auto_ip_cidrs) {
        return print_auto_ip_cidrs_with_provider(stdout,
                                                 collect_usable_link_contexts_provider,
                                                 NULL);
    }
    if (print_smb_bind_interfaces) {
        return print_smb_bind_interfaces_with_provider(stdout,
                                                       collect_usable_link_contexts_provider,
                                                       NULL);
    }
    if (print_smb_bind_interfaces_lan) {
        return print_smb_bind_interfaces_lan_with_provider(stdout,
                                                           collect_usable_link_contexts_provider,
                                                           NULL);
    }
    if (print_mdns_socket_families) {
        return print_mdns_socket_families_with_provider(stdout,
                                                       collect_usable_link_contexts_provider,
                                                       NULL);
    }
    if (cfg.snapshot_newer_than_boot_path[0] != '\0') {
        int snapshot_freshness = snapshot_file_newer_than_boot(cfg.snapshot_newer_than_boot_path);
        if (snapshot_freshness > 0) {
            fprintf(stderr,
                    "mDNS snapshot is newer than current boot: %s\n",
                    cfg.snapshot_newer_than_boot_path);
            return EXIT_OK;
        }
        if (snapshot_freshness < 0) {
            fprintf(stderr,
                    "mDNS snapshot freshness check unavailable for %s\n",
                    cfg.snapshot_newer_than_boot_path);
        } else {
            fprintf(stderr,
                    "mDNS snapshot is missing, empty, or not newer than current boot: %s\n",
                    cfg.snapshot_newer_than_boot_path);
        }
        return EXIT_SNAPSHOT_NOT_FRESH;
    }

    capture_only = (cfg.load_snapshot_path[0] == '\0' &&
                    (cfg.save_all_snapshot_path[0] != '\0' ||
                     cfg.save_airport_snapshot_path[0] != '\0' ||
                     cfg.save_snapshot_path[0] != '\0'));

    if (!capture_only && (cfg.instance_name[0] == '\0' || cfg.host_label[0] == '\0' || !auto_ip)) {
        usage(argv[0]);
        return EXIT_MISSING_REQUIRED_ARGS;
    }
    if (cfg.save_airport_snapshot_path[0] != '\0' &&
        (cfg.instance_name[0] == '\0' || cfg.host_label[0] == '\0' || !cfg_has_airport_identity_macs(&cfg))) {
        fprintf(stderr, "--save-airport-snapshot requires --instance, --host, and at least one AirPort identity MAC\n");
        usage(argv[0]);
        return EXIT_MISSING_REQUIRED_ARGS;
    }

    if ((cfg.instance_name[0] != '\0' && validate_generated_dns_label(cfg.instance_name, "instance name") != 0) ||
        (cfg.host_label[0] != '\0' && validate_generated_dns_label(cfg.host_label, "host label") != 0)) {
        return EXIT_INVALID_DNS_LABEL;
    }
    if ((cfg.save_all_snapshot_path[0] != '\0' || cfg.save_snapshot_path[0] != '\0') && !auto_ip) {
        fprintf(stderr, "mDNS snapshot capture requires --auto-ip\n");
        usage(argv[0]);
        return EXIT_MISSING_REQUIRED_ARGS;
    }
    if (validate_dns_name(cfg.service_type, "service type") != 0) {
        return EXIT_INVALID_SERVICE_TYPE;
    }
    if (cfg.adisk_shares_file[0] != '\0' && parse_adisk_shares_file(&cfg, cfg.adisk_shares_file) != 0) {
        return EXIT_INVALID_ADISK_DISK;
    }
    if (adisk_enabled(&cfg)) {
        char adisk_sys_txt[128];
        if (build_adisk_system_txt(adisk_sys_txt, sizeof(adisk_sys_txt), cfg.adisk_sys_wama) != 0) {
            return EXIT_INVALID_ADISK_SYSTEM;
        }
    }
    if (cfg.device_model[0] != '\0') {
        char model_txt[MAX_NAME + 16];
        if (build_model_txt(model_txt, sizeof(model_txt), cfg.device_model) != 0) {
            return EXIT_INVALID_DEVICE_MODEL;
        }
    }
    if (is_riousbprint_enabled(&cfg)) {
        char txt_storage[RIOUSBPRINT_MAX_TXT_ITEMS][MAX_TXT_STRING + 1];
        const char *txts[RIOUSBPRINT_MAX_TXT_ITEMS];
        size_t txt_count;

        if (validate_generated_dns_label(cfg.riousbprint_instance_name, "riousbprint name") != 0) {
            return EXIT_INVALID_DNS_LABEL;
        }
        if (cfg.riousbprint_port == 0) {
            fprintf(stderr, "riousbprint port must not be zero\n");
            return EXIT_INVALID_SERVICE_TYPE;
        }
        if (cfg.pdl_datastream_port == 0) {
            fprintf(stderr, "pdl-datastream port must not be zero\n");
            return EXIT_INVALID_SERVICE_TYPE;
        }
        discover_riousbprint_usb_cmd(&cfg);
        if (build_riousbprint_txt_items(&cfg, txt_storage, txts, &txt_count) != 0) {
            return EXIT_INVALID_AIRPORT_TXT;
        }
        if (build_pdl_datastream_txt_items(&cfg, txt_storage, txts, &txt_count) != 0) {
            return EXIT_INVALID_AIRPORT_TXT;
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

    if (!capture_only) {
        if (build_host_fqdn(cfg.host_fqdn, sizeof(cfg.host_fqdn), cfg.host_label) != 0) {
            return EXIT_INVALID_DNS_LABEL;
        }
        log_startup_config(&cfg);
    } else {
        fprintf(stderr, "mdns capture-only: save_all=%s save_airport=%s save_trusted=%s airport_identity=%s\n",
                cfg.save_all_snapshot_path[0] != '\0' ? cfg.save_all_snapshot_path : "(none)",
                cfg.save_airport_snapshot_path[0] != '\0' ? cfg.save_airport_snapshot_path : "(none)",
                cfg.save_snapshot_path[0] != '\0' ? cfg.save_snapshot_path : "(none)",
                cfg_has_airport_identity_macs(&cfg) ? "present" : "missing");
    }

    if (cfg.save_airport_snapshot_path[0] != '\0') {
        struct service_record_set airport_records;
        memset(&airport_records, 0, sizeof(airport_records));
        if (build_airport_snapshot_set(&cfg, &airport_records) == 0 &&
            write_snapshot_file_atomic(cfg.save_airport_snapshot_path, &airport_records) == 0) {
            fprintf(stderr, "airport snapshot: wrote 1 record to %s\n", cfg.save_airport_snapshot_path);
        } else {
            fprintf(stderr, "failed to write airport snapshot file: %s\n", cfg.save_airport_snapshot_path);
            return EXIT_INVALID_AIRPORT_TXT;
        }
    }

    if (cfg.save_all_snapshot_path[0] != '\0' || cfg.save_snapshot_path[0] != '\0') {
        struct service_record_set captured_records;
        struct service_record_set filtered_records;
        memset(&captured_records, 0, sizeof(captured_records));
        memset(&filtered_records, 0, sizeof(filtered_records));

        if (cfg.skip_capture_if_snapshot_newer_than_boot_path[0] != '\0') {
            int snapshot_freshness = snapshot_file_newer_than_boot(cfg.skip_capture_if_snapshot_newer_than_boot_path);
            if (snapshot_freshness > 0) {
                fprintf(stderr,
                        "mDNS snapshot capture skipped; %s is newer than current boot\n",
                        cfg.skip_capture_if_snapshot_newer_than_boot_path);
                snapshot_capture_skipped = 1;
                trusted_snapshot_written = cfg.save_snapshot_path[0] != '\0';
            } else {
                if (snapshot_freshness < 0) {
                    fprintf(stderr,
                            "mDNS snapshot freshness check unavailable for %s; capturing snapshot\n",
                            cfg.skip_capture_if_snapshot_newer_than_boot_path);
                }
                if (cfg.save_all_snapshot_path[0] != '\0') {
                    unlink(cfg.save_all_snapshot_path);
                }
                if (cfg.save_snapshot_path[0] != '\0') {
                    unlink(cfg.save_snapshot_path);
                }
            }
        }

        if (!snapshot_capture_skipped) {
            struct link_context_set capture_links;
            int capture_links_ready = 0;
            memset(&capture_links, 0, sizeof(capture_links));
            if (auto_ip) {
                if (wait_for_auto_link_contexts(&capture_links, "mdns capture") == 0) {
                    capture_links_ready = 1;
                    log_link_contexts("mdns capture auto-ip", &capture_links);
                }
            }
            if (capture_links_ready &&
                capture_mdns_snapshot_links_with_retry(&captured_records, &capture_links) == 0) {
                fprintf(stderr, "snapshot capture: captured %lu records\n", (unsigned long)captured_records.count);
                if (captured_records.truncated) {
                    fprintf(stderr,
                            "snapshot capture: record list truncated; kept first %lu unique records in receive order\n",
                            (unsigned long)captured_records.count);
                }
                if (cfg.save_all_snapshot_path[0] != '\0' &&
                    write_snapshot_file_atomic(cfg.save_all_snapshot_path, &captured_records) != 0) {
                    fprintf(stderr, "failed to write all snapshot file: %s\n", cfg.save_all_snapshot_path);
                    snapshot_capture_failed = 1;
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
                            snapshot_capture_failed = 1;
                        } else {
                            trusted_snapshot_written = 1;
                        }
                    } else {
                        fprintf(stderr, "warning: could not identify local Apple mDNS records for snapshot file: %s\n",
                                cfg.save_snapshot_path);
                        snapshot_capture_failed = 1;
                    }
                }
            } else {
                fprintf(stderr, "warning: could not capture Apple mDNS snapshot\n");
                snapshot_capture_failed = 1;
            }
        }
    }

    if (capture_only) {
        fprintf(stderr, "mdns capture-only: exiting without UDP 5353 takeover or advertisement\n");
        if (snapshot_capture_failed ||
            (cfg.save_snapshot_path[0] != '\0' && !trusted_snapshot_written)) {
            return EXIT_SNAPSHOT_CAPTURE_FAILED;
        }
        return EXIT_OK;
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

    memset(&mdns_dest, 0, sizeof(mdns_dest));
    mdns_dest.sin_family = AF_INET;
    mdns_dest.sin_port = htons(MDNS_PORT);
    mdns_dest.sin_addr.s_addr = inet_addr(MDNS_GROUP);

    memset(&mdns_dest6, 0, sizeof(mdns_dest6));
    mdns_dest6.sin6_family = AF_INET6;
    mdns_dest6.sin6_port = htons(MDNS_PORT);
    (void)inet_pton(AF_INET6, MDNS_GROUP_V6, &mdns_dest6.sin6_addr);

    if (auto_ip) {
        time_t last_iface_poll;
        time_t last_degraded_retry;
        time_t last_mdnsresponder_guard;
        struct mdns_socket_pair sockets;
        struct mdns_transport_status transport_status;
        int startup_counters_logged = 0;

        if (!auto_contexts_ready) {
            if (wait_for_auto_advertise_link_contexts(&desired_links, "mdns runtime") != 0) {
                return EXIT_AUTO_IP_UNAVAILABLE;
            }
            auto_contexts_ready = 1;
        }
        log_link_contexts("mdns runtime desired", &desired_links);
        sockets.ipv4_fd = -1;
        sockets.ipv6_fd = -1;
        if (acquire_dualstack_mdns_sockets(0, &desired_links, &active_links, &sockets, &transport_status) < 0) {
            return EXIT_SOCKET_ACQUIRE_FAILED;
        }
        log_link_contexts("mdns runtime active", &active_links);
        log_mdns_transport_status("startup", &active_links, &transport_status);
        log_mdns_counters_force("startup");

        startup_burst_start_ms = monotonic_millis();
        last_iface_poll = time(NULL);
        last_degraded_retry = time(NULL);
        last_mdnsresponder_guard = time(NULL);

        while (!g_stop) {
            fd_set rfds;
            struct timeval tv;
            uint8_t packet[BUF_SIZE];
            long long now_ms;
            long long next_burst_ms = -1;
            long long wait_ms = 1000;
            int maxfd = -1;

            if (time(NULL) - last_iface_poll >= AUTO_IP_STABLE_POLL_SECONDS) {
                struct link_context_set next_links;
                memset(&next_links, 0, sizeof(next_links));
                if (collect_usable_advertise_link_contexts_provider(&next_links, NULL) == 0 &&
                    !link_context_sets_equal(&desired_links, &next_links)) {
                    struct link_context_set stabilized_links;
                    fprintf(stderr,
                            link_context_topology_sets_equal(&desired_links, &next_links)
                                ? "mdns desired transport state changed; confirming after %ds stabilization\n"
                                : "mdns desired link topology changed; confirming after %ds stabilization\n",
                            AUTO_IP_STABILIZE_SECONDS);
                    log_link_contexts("mdns desired old", &desired_links);
                    log_link_contexts("mdns desired observed", &next_links);
                    sleep(AUTO_IP_STABILIZE_SECONDS);
                    memset(&stabilized_links, 0, sizeof(stabilized_links));
                    if (collect_usable_advertise_link_contexts_provider(&stabilized_links, NULL) == 0) {
                        if (link_context_sets_equal(&desired_links, &stabilized_links)) {
                            fprintf(stderr, "mdns desired link change did not persist after stabilization\n");
                        } else {
                            desired_links = stabilized_links;
                            if (stabilized_links.count > 0) {
                                log_link_contexts("mdns desired stabilized", &desired_links);
                                if (recover_runtime_link_change_with_takeover(0,
                                                                              &sockets,
                                                                              &active_links,
                                                                              &desired_links,
                                                                              &mdns_dest,
                                                                              &mdns_dest6,
                                                                              &cfg,
                                                                              &snapshot_records,
                                                                              use_snapshot_records,
                                                                              &transport_status) < 0) {
                                    fprintf(stderr, "mdns auto-ip: could not apply stabilized desired links; keeping existing active transport until next retry\n");
                                    last_iface_poll = time(NULL);
                                    continue;
                                }
                            } else {
                                fprintf(stderr, "mdns auto-ip: no usable address links after stabilization; sending goodbyes and waiting\n");
                                send_link_goodbyes(&sockets, &active_links, &mdns_dest, &mdns_dest6, &cfg, &snapshot_records, use_snapshot_records);
                                close_mdns_socket_pair(&sockets);
                                memset(&active_links, 0, sizeof(active_links));
                                memset(&desired_links, 0, sizeof(desired_links));
                                if (wait_for_auto_advertise_link_contexts(&desired_links, "mdns runtime") != 0) {
                                    break;
                                }
                                if (acquire_dualstack_mdns_sockets(0, &desired_links, &active_links, &sockets, &transport_status) < 0) {
                                    fprintf(stderr, "mdns auto-ip: usable address links returned but sockets could not be acquired\n");
                                    last_iface_poll = time(NULL);
                                    continue;
                                }
                            }
                            log_link_contexts("mdns auto-ip active", &active_links);
                            log_mdns_transport_status("link_change", &active_links, &transport_status);
                            fprintf(stderr, "mdns auto-ip: re-announcing after link change\n");
                            startup_burst_start_ms = monotonic_millis();
                            startup_burst_index = 0;
                            startup_counters_logged = 0;
                            log_mdns_counters_force("link_change");
                            last_degraded_retry = time(NULL);
                        }
                    }
                }
                last_iface_poll = time(NULL);
            }

            mdns_transport_status_from_links(&desired_links, &active_links, &sockets, &transport_status);
            if (mdns_transport_missing_required(&transport_status) &&
                time(NULL) - last_degraded_retry >= MDNS_DEGRADED_RETRY_SECONDS) {
                fprintf(stderr,
                        "mdns desired transport state changed: retrying missing required transports ipv4=%d ipv6=%d\n",
                        transport_status.missing_required_ipv4,
                        transport_status.missing_required_ipv6);
                if (recover_runtime_link_change_with_takeover(0,
                                                              &sockets,
                                                              &active_links,
                                                              &desired_links,
                                                              &mdns_dest,
                                                              &mdns_dest6,
                                                              &cfg,
                                                              &snapshot_records,
                                                              use_snapshot_records,
                                                              &transport_status) >= 0) {
                    log_link_contexts("mdns auto-ip active", &active_links);
                    log_mdns_transport_status("degraded_retry", &active_links, &transport_status);
                    fprintf(stderr, "mdns auto-ip: re-announcing after degraded transport retry\n");
                    startup_burst_start_ms = monotonic_millis();
                    startup_burst_index = 0;
                    startup_counters_logged = 0;
                    log_mdns_counters_force("degraded_retry");
                } else {
                    log_mdns_transport_status("degraded_retry_failed", &active_links, &transport_status);
                    log_mdns_counters_force("degraded_retry_failed");
                }
                last_degraded_retry = time(NULL);
            }

            /* We hold UDP 5353; a respawned mDNSResponder cannot reclaim the port, but
             * reap it so it stops answering queries out of band. Never touch sockets. */
            if (time(NULL) - last_mdnsresponder_guard >= MDNS_MDNSRESPONDER_GUARD_SECONDS) {
                if (mdnsresponder_is_alive()) {
                    fprintf(stderr, "mdns guard: Apple mDNSResponder respawned while we hold UDP %d; reaping\n", MDNS_PORT);
                    kill_mdnsresponder(SIGKILL);
                }
                last_mdnsresponder_guard = time(NULL);
            }

            now_ms = monotonic_millis();
            (void)flush_deferred_response_if_due(now_ms);
            while (startup_burst_index < STARTUP_BURST_COUNT &&
                   now_ms - startup_burst_start_ms >= (long long)g_startup_burst_offsets_ms[startup_burst_index]) {
                announce_all_links(&sockets, &active_links, &mdns_dest, &mdns_dest6, &cfg, &snapshot_records, use_snapshot_records, "startup_announce");
                startup_burst_index++;
                now_ms = monotonic_millis();
            }
            if (startup_burst_index >= STARTUP_BURST_COUNT && !startup_counters_logged) {
                log_mdns_counters_force("startup_announcements_complete");
                startup_counters_logged = 1;
            }
            maybe_log_mdns_counters("traffic_summary", now_ms);

            FD_ZERO(&rfds);
            if (sockets.ipv4_fd >= 0) {
                FD_SET(sockets.ipv4_fd, &rfds);
                if (sockets.ipv4_fd > maxfd) {
                    maxfd = sockets.ipv4_fd;
                }
            }
            if (sockets.ipv6_fd >= 0) {
                FD_SET(sockets.ipv6_fd, &rfds);
                if (sockets.ipv6_fd > maxfd) {
                    maxfd = sockets.ipv6_fd;
                }
            }
            if (startup_burst_index < STARTUP_BURST_COUNT) {
                next_burst_ms = startup_burst_start_ms + (long long)g_startup_burst_offsets_ms[startup_burst_index];
                wait_ms = next_burst_ms - now_ms;
                if (wait_ms < 0) {
                    wait_ms = 0;
                } else if (wait_ms > 1000) {
                    wait_ms = 1000;
                }
            }
            wait_ms = deferred_response_adjust_wait_ms(now_ms, wait_ms);
            tv.tv_sec = (time_t)(wait_ms / 1000);
            tv.tv_usec = (suseconds_t)((wait_ms % 1000) * 1000);

            {
                int selected;
                if (maxfd < 0) {
                    sleep_millis(1000);
                    continue;
                }
                selected = select(maxfd + 1, &rfds, NULL, NULL, &tv);
                if (selected < 0) {
                    if (errno == EINTR) {
                        continue;
                    }
                    perror("select");
                    break;
                }
                if (selected > 0 && sockets.ipv4_fd >= 0 && FD_ISSET(sockets.ipv4_fd, &rfds)) {
                    struct sockaddr_in src;
                    socklen_t src_len = sizeof(src);
                    ssize_t nread = recvfrom(sockets.ipv4_fd, packet, sizeof(packet), 0, (struct sockaddr *)&src, &src_len);
                    if (nread > 0) {
                        const struct link_context *link = select_response_link_ipv4(&active_links, &src);
                        int first_packet = note_mdns_ipv4_packet_received();
                        unsigned long query_matches_before = g_mdns_counters.query_packets_matched;
                        if (link != NULL &&
                            (set_link_outbound_interface4_for_peer(sockets.ipv4_fd, link, src.sin_addr.s_addr) != 0 ||
                             handle_query(sockets.ipv4_fd, packet, (size_t)nread, &mdns_dest, &src, &cfg, link, &snapshot_records, use_snapshot_records) != 0)) {
                            char detail[160];
                            snprintf(detail, sizeof(detail), "iface=%s packet_len=%ld from=%s:%u",
                                     link->name,
                                     (long)nread, inet_ntoa(src.sin_addr), (unsigned int)ntohs(src.sin_port));
                            log_send_failure("query_response", &mdns_dest, use_snapshot_records, detail);
                        }
                        log_mdns_receive_counters("first_ipv4_packet", first_packet, query_matches_before, now_ms);
                    }
                }
                if (selected > 0 && sockets.ipv6_fd >= 0 && FD_ISSET(sockets.ipv6_fd, &rfds)) {
                    struct sockaddr_in6 src6;
                    socklen_t src6_len = sizeof(src6);
                    ssize_t nread = recvfrom(sockets.ipv6_fd, packet, sizeof(packet), 0, (struct sockaddr *)&src6, &src6_len);
                    if (nread > 0) {
                        const struct link_context *link = select_response_link_ipv6(&active_links, &src6);
                        int first_packet = note_mdns_ipv6_packet_received();
                        unsigned long query_matches_before = g_mdns_counters.query_packets_matched;
                        if (link != NULL) {
                            struct sockaddr_in6 scoped_dest6;
                            int query_status;
                            scoped_mdns_dest6_for_link(&scoped_dest6, &mdns_dest6, link);
                            query_status = set_link_outbound_interface6(sockets.ipv6_fd, link);
                            if (query_status == 0) {
                                query_status = handle_query_any(sockets.ipv6_fd,
                                                                packet,
                                                                (size_t)nread,
                                                                (const struct sockaddr *)&scoped_dest6,
                                                                sizeof(scoped_dest6),
                                                                (const struct sockaddr *)&src6,
                                                                src6_len,
                                                                &cfg,
                                                                link,
                                                                &snapshot_records,
                                                                use_snapshot_records);
                            }
                            if (query_status != 0) {
                                char srcbuf[96];
                                format_sockaddr_addr((const struct sockaddr *)&src6, srcbuf, sizeof(srcbuf));
                                fprintf(stderr,
                                        "mdns send failure: stage=query_response records=%s detail=iface=%s packet_len=%ld from=%s\n",
                                        use_snapshot_records ? "snapshot" : "generated",
                                        link->name,
                                        (long)nread,
                                        srcbuf);
                            }
                        }
                        log_mdns_receive_counters("first_ipv6_packet", first_packet, query_matches_before, now_ms);
                    }
                }
            }

        }

        send_link_goodbyes(&sockets, &active_links, &mdns_dest, &mdns_dest6, &cfg, &snapshot_records, use_snapshot_records);
        log_mdns_counters_force("shutdown");
        close_mdns_socket_pair(&sockets);
        return 0;
    }

    usage(argv[0]);
    return EXIT_MISSING_REQUIRED_ARGS;
}

#undef fprintf
#undef perror
