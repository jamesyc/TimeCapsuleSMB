#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
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
#define ANNOUNCE_INTERVAL 30
#define MODEL_TXT_PREFIX "model="
#define ADISK_DEFAULT_DISK_KEY "dk0"
#define ADISK_SYS_ADVF "0x1010"
#define ADISK_DEFAULT_DISK_ADVF "0x1093"
#define ADISK_MAX_DISKS 16
#define ADISK_DISK_UUID_LEN 36
#define AIRPORT_SERVICE_TYPE "_airport._tcp.local."
#define AIRPORT_DEFAULT_PORT 5009
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
#define STARTUP_BURST_COUNT 7
#define MAX_IFACE_CONTEXTS 16
#define AUTO_IP_STABILIZE_SECONDS 3
#define AUTO_IP_STARTUP_POLL_SECONDS 2
#define AUTO_IP_STABLE_POLL_SECONDS 30
#define ADVERTISER_VERSION_CODE 2104

#define DNS_TYPE_A 1
#define DNS_TYPE_PTR 12
#define DNS_TYPE_TXT 16
#define DNS_TYPE_AAAA 28
#define DNS_TYPE_SRV 33
#define DNS_TYPE_ANY 255
#define DNS_CLASS_IN 1
#define DNS_CLASS_CACHE_FLUSH 0x8000
#define DNS_CLASS_IN_UNIQUE (DNS_CLASS_IN | DNS_CLASS_CACHE_FLUSH)
#define MDNS_REPLY_UNICAST 1
#define MDNS_REPLY_MULTICAST 2
#define DNS_FLAG_QR 0x8000
#define DNS_FLAG_AA 0x0400

#if !defined(IPV6_JOIN_GROUP) && defined(IPV6_ADD_MEMBERSHIP)
#define IPV6_JOIN_GROUP IPV6_ADD_MEMBERSHIP
#endif
#if !defined(IPV6_LEAVE_GROUP) && defined(IPV6_DROP_MEMBERSHIP)
#define IPV6_LEAVE_GROUP IPV6_DROP_MEMBERSHIP
#endif

static volatile sig_atomic_t g_stop = 0;

#if defined(__GNUC__)
#define MDNS_UNUSED __attribute__((unused))
#else
#define MDNS_UNUSED
#endif

#ifndef TC_VA_COPY
#if defined(va_copy)
#define TC_VA_COPY(dst, src) va_copy(dst, src)
#elif defined(__va_copy)
#define TC_VA_COPY(dst, src) __va_copy(dst, src)
#else
#define TC_VA_COPY(dst, src) memcpy(&(dst), &(src), sizeof(va_list))
#endif
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
    char *message = stack_message;
    va_list sizing_ap;
    va_list format_ap;
    int result;

    if (stream != stderr && stream != stdout) {
        return vfprintf(stream, format, ap);
    }

    TC_VA_COPY(sizing_ap, ap);
    result = vsnprintf(stack_message, sizeof(stack_message), format, sizing_ap);
    va_end(sizing_ap);
    if (result < 0) {
        return result;
    }

    if ((size_t)result >= sizeof(stack_message)) {
        size_t message_size = (size_t)result + 1;
        message = malloc(message_size);
        if (message == NULL) {
            (void)timestamped_write_message(stream, stack_message);
            (void)timestamped_write_message(stream, "\n[log message truncated: allocation failed]\n");
            fflush(stream);
            return result;
        }
        TC_VA_COPY(format_ap, ap);
        result = vsnprintf(message, message_size, format, format_ap);
        va_end(format_ap);
        if (result < 0) {
            free(message);
            return result;
        }
    }

    if (timestamped_write_message(stream, message) != 0) {
        if (message != stack_message) {
            free(message);
        }
        return -1;
    }
    fflush(stream);
    if (message != stack_message) {
        free(message);
    }
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
    EXIT_AUTO_IP_PROBE_FAILED = 13
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
    char adisk_share_name[MAX_NAME];
    char adisk_disk_key[MAX_LABEL + 1];
    char adisk_disk_advf[16];
    char adisk_uuid[ADISK_DISK_UUID_LEN + 1];
    char adisk_shares_file[MAX_NAME];
    char adisk_sys_wama[18];
    struct adisk_disk_set adisk_disks;
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
    int diskless;
    char load_snapshot_path[MAX_NAME];
    char save_snapshot_path[MAX_NAME];
    char skip_capture_if_snapshot_newer_than_boot_path[MAX_NAME];
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

struct mdns_socket_pair {
    int ipv4_fd;
    int ipv6_fd;
};

struct query_answer_routes {
    int smb_ptr;
    int smb_srv;
    int smb_txt;
    int host_a;
    int host_aaaa;
    int adisk_ptr;
    int adisk_srv;
    int adisk_txt;
    int device_info_ptr;
    int device_info_srv;
    int device_info_txt;
    int airport_ptr;
    int airport_srv;
    int airport_txt;
    int snapshot_ptr[SNAPSHOT_MAX_RECORDS];
    int snapshot_srv[SNAPSHOT_MAX_RECORDS];
    int snapshot_txt[SNAPSHOT_MAX_RECORDS];
    int snapshot_a[SNAPSHOT_MAX_RECORDS];
    int snapshot_aaaa[SNAPSHOT_MAX_RECORDS];
};

static int name_equals(const char *a, const char *b);
static int build_instance_fqdn(char *out, size_t out_len, const char *instance_name, const char *service_type);
static int open_mdns_socket(int shared_bind, int log_bind_errors, uint32_t ipv4_addr, const char *socket_role);
static int is_airport_enabled(const struct config *cfg);
static int smb_enabled(const struct config *cfg);
static int adisk_enabled(const struct config *cfg);
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

typedef int (*mdns_collect_iface_contexts_fn)(struct iface_context_set *out, void *userdata);
typedef int (*mdns_collect_link_contexts_fn)(struct link_context_set *out, void *userdata);
typedef void (*mdns_sleep_fn)(unsigned int seconds, void *userdata);

static int collect_usable_iface_contexts_provider(struct iface_context_set *out, void *userdata) {
    (void)userdata;
    return collect_usable_iface_contexts(out);
}

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

static int wait_for_auto_iface_contexts_with_provider(struct iface_context_set *out,
                                                     const char *role,
                                                     mdns_collect_iface_contexts_fn collect_contexts,
                                                     mdns_sleep_fn sleep_fn,
                                                     void *userdata) {
    struct iface_context_set first;

    if (collect_contexts == NULL || sleep_fn == NULL) {
        return -1;
    }

    memset(out, 0, sizeof(*out));
    while (!g_stop) {
        memset(&first, 0, sizeof(first));
        if (collect_contexts(&first, userdata) == 0 && first.count > 0) {
            fprintf(stderr, "%s auto-ip: first usable IPv4 observed; waiting %ds for network stabilization\n",
                    role, AUTO_IP_STABILIZE_SECONDS);
            sleep_fn(AUTO_IP_STABILIZE_SECONDS, userdata);
            if (collect_contexts(out, userdata) == 0 && out->count > 0) {
                sort_iface_contexts(out);
                return 0;
            }
            fprintf(stderr, "%s auto-ip: usable IPv4 disappeared during stabilization; retrying\n", role);
        }
        sleep_fn(AUTO_IP_STARTUP_POLL_SECONDS, userdata);
    }
    return -1;
}

static int MDNS_UNUSED wait_for_auto_iface_contexts(struct iface_context_set *out, const char *role) {
    return wait_for_auto_iface_contexts_with_provider(out,
                                                     role,
                                                     collect_usable_iface_contexts_provider,
                                                     mdns_sleep_provider,
                                                     NULL);
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

static int print_auto_ip_cidrs_with_provider(FILE *stream,
                                             mdns_collect_iface_contexts_fn collect_contexts,
                                             void *userdata) {
    struct iface_context_set contexts;

    if (collect_contexts == NULL) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }

    memset(&contexts, 0, sizeof(contexts));
    if (collect_contexts(&contexts, userdata) != 0) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    if (contexts.count == 0) {
        return EXIT_AUTO_IP_UNAVAILABLE;
    }
    sort_iface_contexts(&contexts);
    if (print_iface_context_cidrs(stream, &contexts) != 0) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    return EXIT_OK;
}

static int link_contexts_need_ipv4_socket(const struct link_context_set *set);
static int link_contexts_need_ipv6_socket(const struct link_context_set *set);

static int print_smb_bind_interfaces_with_provider(FILE *stream,
                                                   mdns_collect_link_contexts_fn collect_contexts,
                                                   void *userdata) {
    struct link_context_set links;
    size_t i;
    int has_samba_address = 0;

    if (collect_contexts == NULL) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }

    memset(&links, 0, sizeof(links));
    if (collect_contexts(&links, userdata) != 0) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    if (links.count == 0) {
        return EXIT_AUTO_IP_UNAVAILABLE;
    }
    sort_link_contexts(&links);
    for (i = 0; i < links.count; i++) {
        if (link_context_has_samba_address(&links.links[i])) {
            has_samba_address = 1;
            break;
        }
    }
    if (!has_samba_address) {
        return EXIT_AUTO_IP_UNAVAILABLE;
    }
    if (print_smb_link_bind_tokens(stream, &links) != 0) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    return EXIT_OK;
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
                                       const struct link_context_set *links,
                                       int log_bind_errors,
                                       struct mdns_socket_pair *out);
static void close_mdns_socket_pair(struct mdns_socket_pair *sockets);
static int set_outbound_multicast_interface(int sockfd, uint32_t ipv4_addr, const char *socket_role,
                                            int log_success, int log_errors);
static int set_outbound_multicast_interface6(int sockfd, unsigned int ifindex, const char *socket_role,
                                             int log_success, int log_errors);

static void log_startup_config(const struct config *cfg, int shared_bind, int auto_ip) {
    char ipv4_buf[INET_ADDRSTRLEN];

    fprintf(stderr,
            "mdns startup: mode=%s instance=%s host=%s ipv4=%s service=%s adisk=%s device_model=%s airport=%s advertise=%s\n",
            shared_bind ? "shared" : "exclusive",
            cfg->instance_name[0] != '\0' ? cfg->instance_name : "(empty)",
            cfg->host_label[0] != '\0' ? cfg->host_label : "(empty)",
            auto_ip ? "auto" : ipv4_to_string(cfg->ipv4_addr, ipv4_buf, sizeof(ipv4_buf)),
            cfg->service_type[0] != '\0' ? cfg->service_type : "(empty)",
            adisk_enabled(cfg) ? "enabled" : "disabled",
            cfg->device_model[0] != '\0' ? cfg->device_model : "(empty)",
            is_airport_enabled(cfg) ? "enabled" : "disabled",
            cfg->diskless ? "diskless" : "diskful");
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
    if (smb_enabled(cfg)) {
        fprintf(stderr, "serving service: type=%s instance=%s port=%u host=%s\n",
                cfg->service_type, cfg->instance_name, (unsigned int)cfg->port, cfg->host_fqdn);
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
            "Usage: %s --instance <name> --host <label> (--ipv4 <address>|--auto-ip) [options]\n"
            "       %s --save-snapshot <path> [--save-all-snapshot <path>] [airport identity options]\n"
            "       %s --save-airport-snapshot <path> --instance <name> --host <label> [airport identity options]\n"
            "       %s --print-auto-ip-cidrs\n"
            "       %s --print-smb-bind-interfaces\n"
            "       %s --print-mdns-socket-families\n"
            "       %s --version\n"
            "Options:\n"
            "  --auto-ip          Serve every usable live address link and track IP changes\n"
            "  --print-auto-ip-cidrs Print usable live IPv4 CIDRs and exit 0, or exit 11 if none exist\n"
            "  --print-smb-bind-interfaces Print live IPv4/IPv6 address CIDRs for Samba interfaces\n"
            "  --print-mdns-socket-families Print required mDNS UDP socket families for live advertise links\n"
            "  --version          Print advertiser version code and exit\n"
            "  --save-all-snapshot <path> Capture raw LAN-wide mDNS records into a snapshot file\n"
            "  --save-snapshot <path> Capture Apple mDNS records into a snapshot file; without --load-snapshot, capture and exit\n"
            "  --skip-capture-if-snapshot-newer-than-boot <path> Reuse an existing snapshot created after boot\n"
            "  --save-airport-snapshot <path> Generate an AirPort-only Apple snapshot file and exit unless loading\n"
            "  --load-snapshot <path> Kill Apple mDNSResponder and replay snapshot records\n"
            "  --shared-bind     Allow shared UDP 5353 binding instead of exclusive takeover\n"
            "  --diskless        Suppress generated _smb and _adisk records while replaying other snapshot records\n"
            "  --service <type>   Service type (default: _smb._tcp.local.)\n"
            "  --adisk-share <n>  Also advertise _adisk._tcp for Time Machine\n"
            "  --adisk-shares-file <p> Tab-separated share,disk-key,uuid,adVF rows\n"
            "  --adisk-disk-key <k> Disk key for _adisk TXT (default: dk0)\n"
            "  --adisk-disk-advf <v> Volume flags for _adisk TXT (default: 0x1093)\n"
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
            prog, prog, prog, prog, prog, prog, prog);
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

static int MDNS_UNUSED add_snapshot_host_a_record(uint8_t *buf, size_t *off, size_t cap, const struct service_record *record,
                                                  uint32_t response_ipv4_addr, uint32_t ttl, int *answers) {
    if (record->host_fqdn[0] == '\0') {
        return 0;
    }
    if (add_rr_a(buf, off, cap, record->host_fqdn, response_ipv4_addr, ttl) != 0) {
        fprintf(stderr,
                "mdns snapshot rr failure: rr=A type=%s instance=%s host=%s port=%u txt_count=%lu packet_len=%lu\n",
                record->service_type, record->instance_fqdn, record->host_fqdn,
                (unsigned int)record->port, (unsigned long)record->txt_count, (unsigned long)*off);
        return -1;
    }
    *answers += 1;
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
    int written;

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
    written = snprintf(record->host_fqdn, sizeof(record->host_fqdn), "%s.local.", cfg->host_label);
    if (written < 0 || (size_t)written >= sizeof(record->host_fqdn)) {
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

static int send_query_question(int sockfd, const struct sockaddr_in *dest, const char *qname, uint16_t qtype) {
    return send_query_question_any(sockfd, (const struct sockaddr *)dest, sizeof(*dest), qname, qtype);
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

static int open_capture_socket(uint32_t ipv4_addr) {
    return open_mdns_socket(1, 1, ipv4_addr, "capture");
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
            break;
        }
        out->records[out->count++] = *record;
    }

    return out->count > 0 ? 0 : -1;
}

static int capture_mdns_snapshot_raw(struct service_record_set *out, uint32_t ipv4_addr) {
    int sockfd = -1;
    struct sockaddr_in mdns_dest;
    size_t i;
    struct service_type_set service_types;

    memset(out, 0, sizeof(*out));
    memset(&service_types, 0, sizeof(service_types));
    sockfd = open_capture_socket(ipv4_addr);
    if (sockfd < 0) {
        return -1;
    }

    memset(&mdns_dest, 0, sizeof(mdns_dest));
    mdns_dest.sin_family = AF_INET;
    mdns_dest.sin_port = htons(MDNS_PORT);
    mdns_dest.sin_addr.s_addr = inet_addr(MDNS_GROUP);

    (void)send_query_question(sockfd, &mdns_dest, "_services._dns-sd._udp.local.", DNS_TYPE_PTR);
    (void)collect_mdns_responses(sockfd, SNAPSHOT_CAPTURE_STEP_SECONDS, out, &service_types);

    for (i = 0; i < service_types.count; i++) {
        (void)send_query_question(sockfd, &mdns_dest, service_types.types[i], DNS_TYPE_PTR);
    }
    (void)collect_mdns_responses(sockfd, SNAPSHOT_CAPTURE_STEP_SECONDS, out, &service_types);

    for (i = 0; i < out->count; i++) {
        (void)send_query_question(sockfd, &mdns_dest, out->records[i].instance_fqdn, DNS_TYPE_SRV);
        (void)send_query_question(sockfd, &mdns_dest, out->records[i].instance_fqdn, DNS_TYPE_TXT);
    }
    (void)collect_mdns_responses(sockfd, SNAPSHOT_CAPTURE_STEP_SECONDS, out, &service_types);
    close(sockfd);

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
        if (sockets->ipv4_fd >= 0 && links->links[i].ipv4_count > 0 &&
            set_outbound_multicast_interface(sockets->ipv4_fd, links->links[i].ipv4[0].addr, "capture", 0, 0) == 0) {
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
    struct sockaddr_in mdns_dest4;
    struct sockaddr_in6 mdns_dest6;
    size_t i;
    struct service_type_set service_types;

    memset(out, 0, sizeof(*out));
    memset(&service_types, 0, sizeof(service_types));
    sockets.ipv4_fd = -1;
    sockets.ipv6_fd = -1;
    if (open_dualstack_mdns_sockets(1, links, 1, &sockets) != 0) {
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

    send_capture_query_to_all_links(&sockets, links, &mdns_dest4, &mdns_dest6, "_services._dns-sd._udp.local.", DNS_TYPE_PTR);
    (void)collect_mdns_responses_pair(&sockets, SNAPSHOT_CAPTURE_STEP_SECONDS, out, &service_types);

    for (i = 0; i < service_types.count; i++) {
        send_capture_query_to_all_links(&sockets, links, &mdns_dest4, &mdns_dest6, service_types.types[i], DNS_TYPE_PTR);
    }
    (void)collect_mdns_responses_pair(&sockets, SNAPSHOT_CAPTURE_STEP_SECONDS, out, &service_types);

    for (i = 0; i < out->count; i++) {
        send_capture_query_to_all_links(&sockets, links, &mdns_dest4, &mdns_dest6, out->records[i].instance_fqdn, DNS_TYPE_SRV);
        send_capture_query_to_all_links(&sockets, links, &mdns_dest4, &mdns_dest6, out->records[i].instance_fqdn, DNS_TYPE_TXT);
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

static int capture_mdns_snapshot_raw_with_retry(struct service_record_set *out, uint32_t ipv4_addr) {
    time_t deadline = time(NULL) + SNAPSHOT_CAPTURE_TIMEOUT_SECONDS;

    do {
        if (capture_mdns_snapshot_raw(out, ipv4_addr) == 0) {
            return 0;
        }
        if (time(NULL) >= deadline) {
            break;
        }
        sleep(SNAPSHOT_CAPTURE_RETRY_INTERVAL_SECONDS);
    } while (time(NULL) < deadline);

    return -1;
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

static int service_records_match(const struct service_record *a, const struct service_record *b) {
    return strcmp(a->service_type, b->service_type) == 0 &&
           strcmp(a->instance_fqdn, b->instance_fqdn) == 0 &&
           strcmp(a->host_fqdn, b->host_fqdn) == 0 &&
           a->port == b->port;
}

static void append_snapshot_records_unique(struct service_record_set *dst, const struct service_record_set *src) {
    size_t i;
    size_t j;

    for (i = 0; i < src->count; i++) {
        int exists = 0;
        for (j = 0; j < dst->count; j++) {
            if (service_records_match(&dst->records[j], &src->records[i])) {
                exists = 1;
                break;
            }
        }
        if (!exists && dst->count < SNAPSHOT_MAX_RECORDS) {
            dst->records[dst->count++] = src->records[i];
        }
    }
}

typedef int (*mdns_capture_context_fn)(struct service_record_set *out,
                                       const struct iface_context *ctx,
                                       void *userdata);

static int capture_mdns_snapshot_context_raw(struct service_record_set *out,
                                             const struct iface_context *ctx,
                                             void *userdata) {
    (void)userdata;
    return capture_mdns_snapshot_raw_with_retry(out, ctx->ipv4_addr);
}

static int capture_mdns_snapshot_auto_with_provider(struct service_record_set *out,
                                                   const struct iface_context_set *contexts,
                                                   const struct config *cfg,
                                                   int require_trusted_snapshot,
                                                   mdns_capture_context_fn capture_context,
                                                   void *userdata) {
    size_t i;

    if (capture_context == NULL || (require_trusted_snapshot && cfg == NULL)) {
        return -1;
    }

    memset(out, 0, sizeof(*out));
    for (i = 0; i < contexts->count; i++) {
        struct service_record_set captured;
        char ip_buf[INET_ADDRSTRLEN];
        memset(&captured, 0, sizeof(captured));
        fprintf(stderr, "snapshot capture: probing auto-ip context iface=%s ip=%s\n",
                contexts->contexts[i].name,
                ipv4_to_string(contexts->contexts[i].ipv4_addr, ip_buf, sizeof(ip_buf)));
        if (capture_context(&captured, &contexts->contexts[i], userdata) == 0) {
            append_snapshot_records_unique(out, &captured);
            fprintf(stderr, "snapshot capture: merged auto-ip context iface=%s ip=%s records=%lu\n",
                    contexts->contexts[i].name,
                    ipv4_to_string(contexts->contexts[i].ipv4_addr, ip_buf, sizeof(ip_buf)),
                    (unsigned long)captured.count);
        }
    }
    if (out->count == 0) {
        return -1;
    }
    if (require_trusted_snapshot) {
        struct service_record_set filtered;
        memset(&filtered, 0, sizeof(filtered));
        if (prepare_loaded_snapshot_for_advertising(cfg, out, &filtered) != 0) {
            fprintf(stderr, "snapshot capture: merged auto-ip records did not match local AirPort identity\n");
            return -1;
        }
    }
    return 0;
}

static int MDNS_UNUSED capture_mdns_snapshot_auto_with_retry(struct service_record_set *out,
                                                            const struct iface_context_set *contexts,
                                                            const struct config *cfg,
                                                            int require_trusted_snapshot) {
    return capture_mdns_snapshot_auto_with_provider(out,
                                                   contexts,
                                                   cfg,
                                                   require_trusted_snapshot,
                                                   capture_mdns_snapshot_context_raw,
                                                   NULL);
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

static int configure_multicast_socket_options(int sockfd) {
    int yes;

    yes = 255;
    (void)setsockopt(sockfd, IPPROTO_IP, IP_MULTICAST_TTL, &yes, sizeof(yes));
    yes = 1;
    (void)setsockopt(sockfd, IPPROTO_IP, IP_MULTICAST_LOOP, &yes, sizeof(yes));
    return 0;
}

static int configure_outbound_multicast_socket(int sockfd, uint32_t ipv4_addr, const char *socket_role) {
    if (set_outbound_multicast_interface(sockfd, ipv4_addr, socket_role, 1, 1) != 0) {
        return -1;
    }
    return configure_multicast_socket_options(sockfd);
}

static int mdns_takeover_confirmed(int shared_bind) {
    return shared_bind || !mdnsresponder_is_alive();
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

static int configure_mdns_socket_for_ipv4(int sockfd, uint32_t ipv4_addr, const char *socket_role) {
    if (join_mdns_multicast_group(sockfd, ipv4_addr, socket_role) != 0) {
        return -1;
    }
    if (configure_outbound_multicast_socket(sockfd, ipv4_addr, socket_role) != 0) {
        return -1;
    }
    return 0;
}

static int configure_mdns_socket_for_contexts(int sockfd, const struct iface_context_set *set, const char *socket_role) {
    size_t i;

    if (set->count == 0) {
        errno = EADDRNOTAVAIL;
        return -1;
    }
    for (i = 0; i < set->count; i++) {
        if (join_mdns_multicast_group(sockfd, set->contexts[i].ipv4_addr, socket_role) != 0) {
            return -1;
        }
    }
    if (configure_outbound_multicast_socket(sockfd, set->contexts[0].ipv4_addr, socket_role) != 0) {
        return -1;
    }
    return 0;
}

static int iface_context_set_has_ipv4(const struct iface_context_set *set, uint32_t ipv4_addr) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (set->contexts[i].ipv4_addr == ipv4_addr) {
            return 1;
        }
    }
    return 0;
}

static int MDNS_UNUSED prepare_mdns_auto_socket_for_contexts(int sockfd,
                                                            const struct iface_context_set *old_contexts,
                                                            const struct iface_context_set *new_contexts) {
    size_t i;

    if (new_contexts->count == 0) {
        return 0;
    }
    for (i = 0; i < new_contexts->count; i++) {
        if (iface_context_set_has_ipv4(old_contexts, new_contexts->contexts[i].ipv4_addr)) {
            continue;
        }
        if (join_mdns_multicast_group(sockfd, new_contexts->contexts[i].ipv4_addr, "runtime") != 0) {
            return -1;
        }
    }
    return configure_outbound_multicast_socket(sockfd, new_contexts->contexts[0].ipv4_addr, "runtime");
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

static void MDNS_UNUSED retire_mdns_auto_socket_contexts(int sockfd,
                                                         const struct iface_context_set *old_contexts,
                                                         const struct iface_context_set *new_contexts) {
    size_t i;

    for (i = 0; i < old_contexts->count; i++) {
        if (iface_context_set_has_ipv4(new_contexts, old_contexts->contexts[i].ipv4_addr)) {
            continue;
        }
        drop_mdns_multicast_group_best_effort(sockfd, old_contexts->contexts[i].ipv4_addr, "runtime");
    }
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
            if (mdns_takeover_confirmed(shared_bind)) {
                fprintf(stderr,
                        shared_bind
                            ? "mDNS shared bind established after SIGTERM + %ums\n"
                            : "mDNS takeover established after SIGTERM + %ums using exclusive bind\n",
                        retry_delays_ms[i]);
                return sockfd;
            }
            fprintf(stderr, "mDNS socket acquired after SIGTERM + %ums but Apple mDNSResponder is still alive; retrying\n",
                    retry_delays_ms[i]);
            close(sockfd);
        }
    }

    for (i = 0; i < TAKEOVER_RETRY_COUNT; i++) {
        kill_mdnsresponder(SIGKILL);
        sleep_millis(retry_delays_ms[i]);
        sockfd = open_mdns_socket(shared_bind, 0, ipv4_addr, "runtime");
        if (sockfd >= 0) {
            if (mdns_takeover_confirmed(shared_bind)) {
                fprintf(stderr,
                        shared_bind
                            ? "mDNS shared bind established after SIGKILL + %ums\n"
                            : "mDNS takeover established after SIGKILL + %ums using exclusive bind\n",
                        retry_delays_ms[i]);
                return sockfd;
            }
            fprintf(stderr, "mDNS socket acquired after SIGKILL + %ums but Apple mDNSResponder is still alive; retrying\n",
                    retry_delays_ms[i]);
            close(sockfd);
        }
    }

    if (!shared_bind && mdnsresponder_is_alive()) {
        fprintf(stderr, "mDNS takeover failed: Apple mDNSResponder is still alive after retry ladder\n");
    } else {
        fprintf(stderr, "mDNS takeover failed: could not acquire UDP %d socket using %s mode\n",
                MDNS_PORT, shared_bind ? "shared" : "exclusive");
    }
    errno = EADDRINUSE;
    return -1;
}

static int open_auto_mdns_socket(int shared_bind, const struct iface_context_set *set, int log_bind_errors) {
    int sockfd;

    sockfd = open_bound_mdns_socket(shared_bind, log_bind_errors);
    if (sockfd < 0) {
        return -1;
    }
    if (configure_mdns_socket_for_contexts(sockfd, set, "runtime") != 0) {
        close(sockfd);
        return -1;
    }
    return sockfd;
}

static int MDNS_UNUSED acquire_mdns_auto_socket(int shared_bind, const struct iface_context_set *set) {
    static const unsigned int retry_delays_ms[TAKEOVER_RETRY_COUNT] = {0, 100, 200, 300, 400, 500};
    size_t i;
    int sockfd;

    for (i = 0; i < TAKEOVER_RETRY_COUNT; i++) {
        kill_mdnsresponder(SIGTERM);
        sleep_millis(retry_delays_ms[i]);
        sockfd = open_auto_mdns_socket(shared_bind, set, 0);
        if (sockfd >= 0) {
            if (mdns_takeover_confirmed(shared_bind)) {
                fprintf(stderr,
                        shared_bind
                            ? "mDNS auto-ip shared bind established after SIGTERM + %ums using single socket\n"
                            : "mDNS auto-ip takeover established after SIGTERM + %ums using exclusive single socket\n",
                        retry_delays_ms[i]);
                return sockfd;
            }
            fprintf(stderr, "mDNS auto-ip socket acquired after SIGTERM + %ums but Apple mDNSResponder is still alive; retrying\n",
                    retry_delays_ms[i]);
            close(sockfd);
        }
    }

    for (i = 0; i < TAKEOVER_RETRY_COUNT; i++) {
        kill_mdnsresponder(SIGKILL);
        sleep_millis(retry_delays_ms[i]);
        sockfd = open_auto_mdns_socket(shared_bind, set, 0);
        if (sockfd >= 0) {
            if (mdns_takeover_confirmed(shared_bind)) {
                fprintf(stderr,
                        shared_bind
                            ? "mDNS auto-ip shared bind established after SIGKILL + %ums using single socket\n"
                            : "mDNS auto-ip takeover established after SIGKILL + %ums using exclusive single socket\n",
                        retry_delays_ms[i]);
                return sockfd;
            }
            fprintf(stderr, "mDNS auto-ip socket acquired after SIGKILL + %ums but Apple mDNSResponder is still alive; retrying\n",
                    retry_delays_ms[i]);
            close(sockfd);
        }
    }

    if (!shared_bind && mdnsresponder_is_alive()) {
        fprintf(stderr, "mDNS auto-ip takeover failed: Apple mDNSResponder is still alive after retry ladder\n");
    } else {
        fprintf(stderr, "mDNS auto-ip takeover failed: could not acquire single UDP %d socket\n", MDNS_PORT);
    }
    errno = EADDRINUSE;
    return -1;
}

static int link_contexts_need_ipv4_socket(const struct link_context_set *set) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (link_context_has_advertisable_ipv4(&set->links[i])) {
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
        close(sockets->ipv4_fd);
        sockets->ipv4_fd = -1;
    }
    if (sockets->ipv6_fd >= 0) {
        close(sockets->ipv6_fd);
        sockets->ipv6_fd = -1;
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
    if (log_success) {
        fprintf(stderr, "mdns %s socket: IPv6 outbound multicast ifindex=%u\n", socket_role, ifindex);
    }
    return 0;
}

static int configure_mdns_socket6_for_links(int sockfd, const struct link_context_set *set, const char *socket_role) {
    size_t i;
    int joined = 0;

    for (i = 0; i < set->count; i++) {
        if (!link_context_has_mdns_ipv6_transport(&set->links[i])) {
            continue;
        }
        if (join_mdns_multicast_group6(sockfd, set->links[i].ifindex, set->links[i].name, socket_role) != 0) {
            return -1;
        }
        joined = 1;
    }
    if (!joined) {
        errno = EADDRNOTAVAIL;
        return -1;
    }
    for (i = 0; i < set->count; i++) {
        if (link_context_has_mdns_ipv6_transport(&set->links[i])) {
            return set_outbound_multicast_interface6(sockfd, set->links[i].ifindex, socket_role, 1, 1);
        }
    }
    return -1;
}

static int configure_mdns_socket4_for_links(int sockfd, const struct link_context_set *set, const char *socket_role) {
    size_t i;
    uint32_t first_ipv4 = 0;

    for (i = 0; i < set->count; i++) {
        if (set->links[i].ipv4_count == 0) {
            continue;
        }
        if (join_mdns_multicast_group(sockfd, set->links[i].ipv4[0].addr, socket_role) != 0) {
            return -1;
        }
        if (first_ipv4 == 0) {
            first_ipv4 = set->links[i].ipv4[0].addr;
        }
    }
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
        if (set->links[i].ipv4_count > 0 && set->links[i].ipv4[0].addr == ipv4_addr) {
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
                                            const struct link_context_set *new_links,
                                            const char *socket_role) {
    size_t i;
    uint32_t first_ipv4 = 0;

    for (i = 0; i < new_links->count; i++) {
        if (new_links->links[i].ipv4_count == 0) {
            continue;
        }
        if (first_ipv4 == 0) {
            first_ipv4 = new_links->links[i].ipv4[0].addr;
        }
        if (link_set_has_ipv4_membership(old_links, new_links->links[i].ipv4[0].addr)) {
            continue;
        }
        if (join_mdns_multicast_group(sockfd, new_links->links[i].ipv4[0].addr, socket_role) != 0) {
            return -1;
        }
    }
    if (first_ipv4 == 0) {
        errno = EADDRNOTAVAIL;
        return -1;
    }
    return configure_outbound_multicast_socket(sockfd, first_ipv4, socket_role);
}

static int prepare_mdns_socket6_memberships(int sockfd,
                                            const struct link_context_set *old_links,
                                            const struct link_context_set *new_links,
                                            const char *socket_role) {
    size_t i;
    unsigned int first_ifindex = 0;

    for (i = 0; i < new_links->count; i++) {
        if (!link_context_has_mdns_ipv6_transport(&new_links->links[i])) {
            continue;
        }
        if (first_ifindex == 0) {
            first_ifindex = new_links->links[i].ifindex;
        }
        if (link_set_has_ipv6_membership(old_links, new_links->links[i].ifindex)) {
            continue;
        }
        if (join_mdns_multicast_group6(sockfd, new_links->links[i].ifindex, new_links->links[i].name, socket_role) != 0) {
            return -1;
        }
    }
    if (first_ifindex == 0) {
        errno = EADDRNOTAVAIL;
        return -1;
    }
    return set_outbound_multicast_interface6(sockfd, first_ifindex, socket_role, 1, 1);
}

static int open_dualstack_mdns_sockets(int shared_bind,
                                       const struct link_context_set *links,
                                       int log_bind_errors,
                                       struct mdns_socket_pair *out) {
    int need_ipv4 = link_contexts_need_ipv4_socket(links);
    int need_ipv6 = link_contexts_need_ipv6_socket(links);

    out->ipv4_fd = -1;
    out->ipv6_fd = -1;
    if (!need_ipv4 && !need_ipv6) {
        errno = EADDRNOTAVAIL;
        return -1;
    }
    if (need_ipv4) {
        out->ipv4_fd = open_bound_mdns_socket(shared_bind, log_bind_errors);
        if (out->ipv4_fd < 0 ||
            configure_mdns_socket4_for_links(out->ipv4_fd, links, "runtime") != 0) {
            close_mdns_socket_pair(out);
            return -1;
        }
    }
    if (need_ipv6) {
        out->ipv6_fd = open_bound_mdns_socket6(shared_bind, log_bind_errors);
        if (out->ipv6_fd < 0 ||
            configure_mdns_socket6_for_links(out->ipv6_fd, links, "runtime") != 0) {
            close_mdns_socket_pair(out);
            return -1;
        }
    }
    return 0;
}

static int acquire_dualstack_mdns_sockets(int shared_bind,
                                          const struct link_context_set *links,
                                          struct mdns_socket_pair *out) {
    static const unsigned int retry_delays_ms[TAKEOVER_RETRY_COUNT] = {0, 100, 200, 300, 400, 500};
    size_t i;

    for (i = 0; i < TAKEOVER_RETRY_COUNT; i++) {
        kill_mdnsresponder(SIGTERM);
        sleep_millis(retry_delays_ms[i]);
        if (open_dualstack_mdns_sockets(shared_bind, links, 0, out) == 0) {
            if (mdns_takeover_confirmed(shared_bind)) {
                fprintf(stderr,
                        shared_bind
                            ? "mDNS dual-stack shared bind established after SIGTERM + %ums\n"
                            : "mDNS dual-stack takeover established after SIGTERM + %ums\n",
                        retry_delays_ms[i]);
                return 0;
            }
            fprintf(stderr, "mDNS dual-stack sockets acquired after SIGTERM + %ums but Apple mDNSResponder is still alive; retrying\n",
                    retry_delays_ms[i]);
            close_mdns_socket_pair(out);
        }
    }

    for (i = 0; i < TAKEOVER_RETRY_COUNT; i++) {
        kill_mdnsresponder(SIGKILL);
        sleep_millis(retry_delays_ms[i]);
        if (open_dualstack_mdns_sockets(shared_bind, links, 0, out) == 0) {
            if (mdns_takeover_confirmed(shared_bind)) {
                fprintf(stderr,
                        shared_bind
                            ? "mDNS dual-stack shared bind established after SIGKILL + %ums\n"
                            : "mDNS dual-stack takeover established after SIGKILL + %ums\n",
                        retry_delays_ms[i]);
                return 0;
            }
            fprintf(stderr, "mDNS dual-stack sockets acquired after SIGKILL + %ums but Apple mDNSResponder is still alive; retrying\n",
                    retry_delays_ms[i]);
            close_mdns_socket_pair(out);
        }
    }

    if (!shared_bind && mdnsresponder_is_alive()) {
        fprintf(stderr, "mDNS dual-stack takeover failed: Apple mDNSResponder is still alive after retry ladder\n");
    } else {
        fprintf(stderr, "mDNS dual-stack takeover failed: could not acquire required UDP %d sockets\n", MDNS_PORT);
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

static void MDNS_UNUSED log_packet_send_failure_detail(const char *stage, const struct sockaddr_in *dest, size_t packet_len,
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

static void log_packet_send_failure_detail_any(const char *stage, const struct sockaddr *dest, size_t packet_len,
                                               int answers, int use_snapshot_records, int saved_errno) {
    char destbuf[96];

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

static int send_dns_packet(const char *stage, int sockfd, const uint8_t *buf, size_t packet_len,
                           const struct sockaddr_in *dest, int answers, int use_snapshot_records) {
    return send_dns_packet_any(stage,
                               sockfd,
                               buf,
                               packet_len,
                               (const struct sockaddr *)dest,
                               sizeof(*dest),
                               answers,
                               use_snapshot_records);
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

static void init_announcement_packet(size_t *off, int *answers) {
    *off = sizeof(struct dns_header);
    *answers = 0;
}

static int MDNS_UNUSED finalize_and_send_announcement_packet(int sockfd, uint8_t *buf, size_t off, int answers,
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

static int append_generated_base_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg,
                                         const struct link_context *response_link,
                                         int include_a, int include_aaaa,
                                         uint32_t ttl, int *answers) {
    char instance_fqdn[MAX_NAME];

    if (smb_enabled(cfg)) {
        if (build_instance_fqdn(instance_fqdn, sizeof(instance_fqdn), cfg->instance_name, cfg->service_type) != 0) {
            return -1;
        }
        if (add_rr_ptr(buf, off, cap, cfg->service_type, instance_fqdn, ttl) != 0 ||
            add_rr_srv(buf, off, cap, instance_fqdn, cfg->host_fqdn, cfg->port, ttl) != 0 ||
            add_rr_txt_empty(buf, off, cap, instance_fqdn, ttl) != 0) {
            return -1;
        }
        *answers += 3;
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
    char announced_hosts[SNAPSHOT_MAX_RECORDS][MAX_NAME];
    size_t announced_host_count = 0;
    static int logged_duplicate_host_suppression = 0;

    init_announcement_packet(&off, &answers);
    if (append_generated_base_records(buf, &off, sizeof(buf), cfg, response_link, 1, 1, ttl, &answers) != 0) {
        log_packet_build_failure("announcement", "add_core_records", off, answers, use_snapshot_records);
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
            init_announcement_packet(&off, &answers);
            if (add_service_record_answers(buf, &off, sizeof(buf), &snapshot_records->records[i], ttl, &answers) != 0) {
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
                if (add_snapshot_host_address_records(buf, &off, sizeof(buf), &snapshot_records->records[i], response_link, 1, 1, ttl, &answers) != 0) {
                    off = before_host_a_off;
                    answers = before_host_a_answers;
                    if (finalize_and_send_announcement_packet_any(sockfd, buf, off, answers, dest, dest_len, use_snapshot_records) != 0) {
                        return -1;
                    }
                    init_announcement_packet(&off, &answers);
                    if (add_snapshot_host_address_records(buf, &off, sizeof(buf), &snapshot_records->records[i], response_link, 1, 1, ttl, &answers) != 0) {
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
            if (finalize_and_send_announcement_packet_any(sockfd, buf, off, answers, dest, dest_len, use_snapshot_records) != 0) {
                return -1;
            }
        }
    } else {
        size_t before_airport_off = off;
        int before_airport_answers = answers;
        if (add_airport_records(buf, &off, sizeof(buf), cfg, ttl, &answers) != 0) {
            off = before_airport_off;
            answers = before_airport_answers;
            if (finalize_and_send_announcement_packet_any(sockfd, buf, off, answers, dest, dest_len, use_snapshot_records) != 0) {
                return -1;
            }
            init_announcement_packet(&off, &answers);
            if (add_airport_records(buf, &off, sizeof(buf), cfg, ttl, &answers) != 0) {
                log_packet_build_failure("announcement", "add_airport_records", off, answers, use_snapshot_records);
                return -1;
            }
        }
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

static int query_routes_have_destination(const struct query_answer_routes *routes,
                                         const struct service_record_set *snapshot_records,
                                         int use_snapshot_records,
                                         int route) {
    size_t j;

    if ((routes->smb_ptr | routes->smb_srv | routes->smb_txt | routes->host_a | routes->host_aaaa |
         routes->adisk_ptr | routes->adisk_srv | routes->adisk_txt |
         routes->device_info_ptr | routes->device_info_srv | routes->device_info_txt |
         routes->airport_ptr | routes->airport_srv | routes->airport_txt) & route) {
        return 1;
    }
    if (!use_snapshot_records) {
        return 0;
    }
    for (j = 0; j < snapshot_records->count; j++) {
        if ((routes->snapshot_ptr[j] | routes->snapshot_srv[j] |
             routes->snapshot_txt[j] | routes->snapshot_a[j] | routes->snapshot_aaaa[j]) & route) {
            return 1;
        }
    }
    return 0;
}

static int build_query_response_packet(uint8_t *reply, size_t reply_cap, size_t *reply_len, int *answer_count,
                                       uint16_t response_id, int route,
                                       const struct query_answer_routes *routes,
                                       const char *instance_fqdn,
                                       const char *adisk_instance_fqdn,
                                       const char *device_info_instance_fqdn,
                                       const char *airport_instance_fqdn,
                                       const struct config *cfg,
                                       const struct link_context *response_link,
                                       const struct service_record_set *snapshot_records,
                                       int use_snapshot_records) {
    struct dns_header hdr;
    size_t off = sizeof(struct dns_header);
    int answers = 0;

    memset(&hdr, 0, sizeof(hdr));
    hdr.id = response_id;
    hdr.flags = htons(DNS_FLAG_QR | DNS_FLAG_AA);

    if (routes->smb_ptr & route) {
        if (add_rr_ptr(reply, &off, reply_cap, cfg->service_type, instance_fqdn, cfg->ttl) != 0) {
            log_packet_build_failure("query_response", "add_ptr", off, answers, use_snapshot_records);
            return -1;
        }
        answers++;
    }
    if (routes->smb_srv & route) {
        if (add_rr_srv(reply, &off, reply_cap, instance_fqdn, cfg->host_fqdn, cfg->port, cfg->ttl) != 0) {
            log_packet_build_failure("query_response", "add_srv", off, answers, use_snapshot_records);
            return -1;
        }
        answers++;
    }
    if (routes->smb_txt & route) {
        if (add_rr_txt_empty(reply, &off, reply_cap, instance_fqdn, cfg->ttl) != 0) {
            log_packet_build_failure("query_response", "add_txt", off, answers, use_snapshot_records);
            return -1;
        }
        answers++;
    }
    if ((routes->adisk_ptr | routes->adisk_srv | routes->adisk_txt) & route) {
        char txt1[128];
        char disk_txts[ADISK_MAX_DISKS][256];
        const char *txts[ADISK_MAX_DISKS + 1];
        size_t disk_i;

        if (build_adisk_system_txt(txt1, sizeof(txt1), cfg->adisk_sys_wama) != 0) {
            log_packet_build_failure("query_response", "build_adisk_system_txt", off, answers, use_snapshot_records);
            return -1;
        }
        txts[0] = txt1;
        for (disk_i = 0; disk_i < cfg->adisk_disks.count; disk_i++) {
            const struct adisk_disk *disk = &cfg->adisk_disks.disks[disk_i];
            if (build_adisk_disk_txt(disk_txts[disk_i], sizeof(disk_txts[disk_i]), disk->disk_key, disk->share_name, disk->uuid, disk->disk_advf) != 0) {
                log_packet_build_failure("query_response", "build_adisk_disk_txt", off, answers, use_snapshot_records);
                return -1;
            }
            txts[disk_i + 1] = disk_txts[disk_i];
        }

        if (routes->adisk_ptr & route) {
            if (add_rr_ptr(reply, &off, reply_cap, cfg->adisk_service_type, adisk_instance_fqdn, cfg->ttl) != 0) {
                log_packet_build_failure("query_response", "add_adisk_ptr", off, answers, use_snapshot_records);
                return -1;
            }
            answers++;
        }
        if (routes->adisk_srv & route) {
            if (add_rr_srv(reply, &off, reply_cap, adisk_instance_fqdn, cfg->host_fqdn, cfg->adisk_port, cfg->ttl) != 0) {
                log_packet_build_failure("query_response", "add_adisk_srv", off, answers, use_snapshot_records);
                return -1;
            }
            answers++;
        }
        if (routes->adisk_txt & route) {
            if (add_rr_txt_strings(reply, &off, reply_cap, adisk_instance_fqdn, cfg->ttl, txts, cfg->adisk_disks.count + 1) != 0) {
                log_packet_build_failure("query_response", "add_adisk_txt", off, answers, use_snapshot_records);
                return -1;
            }
            answers++;
        }
    }
    if ((routes->device_info_ptr | routes->device_info_srv | routes->device_info_txt) & route) {
        char model_txt[MAX_NAME + 16];
        const char *txts[1];

        if (build_model_txt(model_txt, sizeof(model_txt), cfg->device_model) != 0) {
            log_packet_build_failure("query_response", "build_model_txt", off, answers, use_snapshot_records);
            return -1;
        }
        txts[0] = model_txt;

        if (routes->device_info_ptr & route) {
            if (add_rr_ptr(reply, &off, reply_cap, cfg->device_info_service_type, device_info_instance_fqdn, cfg->ttl) != 0) {
                log_packet_build_failure("query_response", "add_device_info_ptr", off, answers, use_snapshot_records);
                return -1;
            }
            answers++;
        }
        if (routes->device_info_srv & route) {
            if (add_rr_srv(reply, &off, reply_cap, device_info_instance_fqdn, cfg->host_fqdn, 0, cfg->ttl) != 0) {
                log_packet_build_failure("query_response", "add_device_info_srv", off, answers, use_snapshot_records);
                return -1;
            }
            answers++;
        }
        if (routes->device_info_txt & route) {
            if (add_rr_txt_strings(reply, &off, reply_cap, device_info_instance_fqdn, cfg->ttl, txts, 1) != 0) {
                log_packet_build_failure("query_response", "add_device_info_txt", off, answers, use_snapshot_records);
                return -1;
            }
            answers++;
        }
    }
    if (!use_snapshot_records && ((routes->airport_ptr | routes->airport_srv | routes->airport_txt) & route)) {
        char airport_txt[256];
        const char *txts[1];

        if (build_airport_txt(airport_txt, sizeof(airport_txt), cfg) != 0) {
            log_packet_build_failure("query_response", "build_airport_txt", off, answers, use_snapshot_records);
            return -1;
        }
        txts[0] = airport_txt;

        if (routes->airport_ptr & route) {
            if (add_rr_ptr(reply, &off, reply_cap, cfg->airport_service_type, airport_instance_fqdn, cfg->ttl) != 0) {
                log_packet_build_failure("query_response", "add_airport_ptr", off, answers, use_snapshot_records);
                return -1;
            }
            answers++;
        }
        if (routes->airport_srv & route) {
            if (add_rr_srv(reply, &off, reply_cap, airport_instance_fqdn, cfg->host_fqdn, cfg->airport_port, cfg->ttl) != 0) {
                log_packet_build_failure("query_response", "add_airport_srv", off, answers, use_snapshot_records);
                return -1;
            }
            answers++;
        }
        if (routes->airport_txt & route) {
            if (add_rr_txt_strings(reply, &off, reply_cap, airport_instance_fqdn, cfg->ttl, txts, 1) != 0) {
                log_packet_build_failure("query_response", "add_airport_txt", off, answers, use_snapshot_records);
                return -1;
            }
            answers++;
        }
    }
    if (routes->host_a & route) {
        if (append_host_address_records(reply, &off, reply_cap, cfg->host_fqdn, response_link, 1, 0, cfg->ttl, &answers) != 0) {
            log_packet_build_failure("query_response", "add_a", off, answers, use_snapshot_records);
            return -1;
        }
    }
    if (routes->host_aaaa & route) {
        if (append_host_address_records(reply, &off, reply_cap, cfg->host_fqdn, response_link, 0, 1, cfg->ttl, &answers) != 0) {
            log_packet_build_failure("query_response", "add_aaaa", off, answers, use_snapshot_records);
            return -1;
        }
    }

    if (use_snapshot_records) {
        size_t j;
        char announced_hosts[SNAPSHOT_MAX_RECORDS][MAX_NAME];
        size_t announced_host_count = 0;

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
            if (routes->snapshot_ptr[j] & route) {
                if (add_rr_ptr(reply, &off, reply_cap, record->service_type, record->instance_fqdn, cfg->ttl) != 0) {
                    log_snapshot_record_build_failure("query_response", "add_snapshot_ptr", j, record, off, answers);
                    log_packet_build_failure("query_response", "add_snapshot_ptr", off, answers, use_snapshot_records);
                    return -1;
                }
                answers++;
            }
            if (routes->snapshot_srv[j] & route) {
                if (add_rr_srv(reply, &off, reply_cap, record->instance_fqdn, record->host_fqdn, record->port, cfg->ttl) != 0) {
                    log_snapshot_record_build_failure("query_response", "add_snapshot_srv", j, record, off, answers);
                    log_packet_build_failure("query_response", "add_snapshot_srv", off, answers, use_snapshot_records);
                    return -1;
                }
                answers++;
            }
            if (routes->snapshot_txt[j] & route) {
                if (record->txt_count > 0) {
                    if (add_rr_txt_items(reply, &off, reply_cap, record->instance_fqdn, cfg->ttl, txts, txt_lengths, record->txt_count) != 0) {
                        log_snapshot_record_build_failure("query_response", "add_snapshot_txt", j, record, off, answers);
                        log_packet_build_failure("query_response", "add_snapshot_txt", off, answers, use_snapshot_records);
                        return -1;
                    }
                } else {
                    if (add_rr_txt_empty(reply, &off, reply_cap, record->instance_fqdn, cfg->ttl) != 0) {
                        log_snapshot_record_build_failure("query_response", "add_snapshot_txt_empty", j, record, off, answers);
                        log_packet_build_failure("query_response", "add_snapshot_txt_empty", off, answers, use_snapshot_records);
                        return -1;
                    }
                }
                answers++;
            }
            if (((routes->snapshot_a[j] | routes->snapshot_aaaa[j]) & route) && record->host_fqdn[0] != '\0' &&
                !host_already_announced(announced_hosts, announced_host_count, record->host_fqdn)) {
                int include_a = (routes->snapshot_a[j] & route) != 0;
                int include_aaaa = (routes->snapshot_aaaa[j] & route) != 0;
                if (add_snapshot_host_address_records(reply, &off, reply_cap, record, response_link, include_a, include_aaaa, cfg->ttl, &answers) != 0) {
                    log_snapshot_record_build_failure("query_response", "add_snapshot_a", j, record, off, answers);
                    log_packet_build_failure("query_response", "add_snapshot_a", off, answers, use_snapshot_records);
                    return -1;
                }
                if (remember_announced_host(announced_hosts, &announced_host_count, record->host_fqdn) != 0) {
                    log_packet_build_failure("query_response", "remember_snapshot_a_host", off, answers, use_snapshot_records);
                    return -1;
                }
            }
        }
    }

    hdr.ancount = htons((uint16_t)answers);
    memcpy(reply, &hdr, sizeof(hdr));
    *reply_len = off;
    *answer_count = answers;
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
    uint16_t query_id;
    uint8_t reply[BUF_SIZE];
    char instance_fqdn[MAX_NAME];
    char adisk_instance_fqdn[MAX_NAME];
    char device_info_instance_fqdn[MAX_NAME];
    char airport_instance_fqdn[MAX_NAME];
    struct query_answer_routes routes;
    uint16_t i;
    int status = 0;

    memset(&routes, 0, sizeof(routes));
    instance_fqdn[0] = '\0';
    adisk_instance_fqdn[0] = '\0';
    device_info_instance_fqdn[0] = '\0';
    airport_instance_fqdn[0] = '\0';

    if (packet_len < sizeof(struct dns_header)) {
        return 0;
    }
    memcpy(&hdr, packet, sizeof(hdr));
    if (ntohs(hdr.flags) & DNS_FLAG_QR) {
        return 0;
    }

    qdcount = ntohs(hdr.qdcount);
    query_id = hdr.id;
    if (smb_enabled(cfg) &&
        build_instance_fqdn(instance_fqdn, sizeof(instance_fqdn), cfg->instance_name, cfg->service_type) != 0) {
        log_packet_build_failure("query_response", "build_instance_fqdn", sizeof(struct dns_header), 0, use_snapshot_records);
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
    if (!use_snapshot_records && is_airport_enabled(cfg) &&
        build_instance_fqdn(airport_instance_fqdn, sizeof(airport_instance_fqdn), cfg->instance_name, cfg->airport_service_type) != 0) {
        log_packet_build_failure("query_response", "build_airport_instance_fqdn", sizeof(struct dns_header), 0, use_snapshot_records);
        return 0;
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
        if (qclass_base != DNS_CLASS_IN) {
            continue;
        }
        reply_route = (qclass_raw & DNS_CLASS_CACHE_FLUSH) ? MDNS_REPLY_UNICAST : MDNS_REPLY_MULTICAST;

        if (smb_enabled(cfg) &&
            name_equals(qname, cfg->service_type) &&
            (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
            routes.smb_ptr |= reply_route;
            routes.smb_srv |= reply_route;
            routes.smb_txt |= reply_route;
            routes.host_a |= reply_route;
            routes.host_aaaa |= reply_route;
        } else if (adisk_enabled(cfg) &&
                   name_equals(qname, cfg->adisk_service_type) &&
                   (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
            routes.adisk_ptr |= reply_route;
            routes.adisk_srv |= reply_route;
            routes.adisk_txt |= reply_route;
            routes.host_a |= reply_route;
            routes.host_aaaa |= reply_route;
        } else if (cfg->device_model[0] != '\0' &&
                   name_equals(qname, cfg->device_info_service_type) &&
                   (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
            routes.device_info_ptr |= reply_route;
            routes.device_info_srv |= reply_route;
            routes.device_info_txt |= reply_route;
            routes.host_a |= reply_route;
            routes.host_aaaa |= reply_route;
        } else if (!use_snapshot_records && is_airport_enabled(cfg) &&
                   name_equals(qname, cfg->airport_service_type) &&
                   (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
            routes.airport_ptr |= reply_route;
            routes.airport_srv |= reply_route;
            routes.airport_txt |= reply_route;
            routes.host_a |= reply_route;
            routes.host_aaaa |= reply_route;
        } else if (smb_enabled(cfg) && name_equals(qname, instance_fqdn)) {
            if (qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY) {
                routes.smb_srv |= reply_route;
                routes.host_a |= reply_route;
                routes.host_aaaa |= reply_route;
            }
            if (qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY) {
                routes.smb_txt |= reply_route;
            }
        } else if (adisk_enabled(cfg) &&
                   name_equals(qname, adisk_instance_fqdn)) {
            if (qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY) {
                routes.adisk_srv |= reply_route;
                routes.host_a |= reply_route;
                routes.host_aaaa |= reply_route;
            }
            if (qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY) {
                routes.adisk_txt |= reply_route;
            }
        } else if (cfg->device_model[0] != '\0' &&
                   name_equals(qname, device_info_instance_fqdn)) {
            if (qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY) {
                routes.device_info_srv |= reply_route;
                routes.host_a |= reply_route;
                routes.host_aaaa |= reply_route;
            }
            if (qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY) {
                routes.device_info_txt |= reply_route;
            }
        } else if (!use_snapshot_records && is_airport_enabled(cfg) &&
                   name_equals(qname, airport_instance_fqdn)) {
            if (qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY) {
                routes.airport_srv |= reply_route;
                routes.host_a |= reply_route;
                routes.host_aaaa |= reply_route;
            }
            if (qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY) {
                routes.airport_txt |= reply_route;
            }
        } else if (name_equals(qname, cfg->host_fqdn) && (qtype == DNS_TYPE_A || qtype == DNS_TYPE_ANY)) {
            routes.host_a |= reply_route;
            if (qtype == DNS_TYPE_ANY) {
                routes.host_aaaa |= reply_route;
            }
        } else if (name_equals(qname, cfg->host_fqdn) && qtype == DNS_TYPE_AAAA) {
            routes.host_aaaa |= reply_route;
        } else if (use_snapshot_records) {
            size_t j;
            for (j = 0; j < snapshot_records->count; j++) {
                const struct service_record *record = &snapshot_records->records[j];
                if (is_suppressed_snapshot_service_type(record->service_type)) {
                    continue;
                }
                if (name_equals(qname, record->service_type) && (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
                    routes.snapshot_ptr[j] |= reply_route;
                    routes.snapshot_srv[j] |= reply_route;
                    routes.snapshot_txt[j] |= reply_route;
                    routes.snapshot_a[j] |= reply_route;
                    routes.snapshot_aaaa[j] |= reply_route;
                } else if (name_equals(qname, record->instance_fqdn)) {
                    if (qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY) {
                        routes.snapshot_srv[j] |= reply_route;
                        routes.snapshot_a[j] |= reply_route;
                        routes.snapshot_aaaa[j] |= reply_route;
                    }
                    if (qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY) {
                        routes.snapshot_txt[j] |= reply_route;
                    }
                } else if (name_equals(qname, record->host_fqdn) && (qtype == DNS_TYPE_A || qtype == DNS_TYPE_ANY)) {
                    routes.snapshot_a[j] |= reply_route;
                    if (qtype == DNS_TYPE_ANY) {
                        routes.snapshot_aaaa[j] |= reply_route;
                    }
                } else if (name_equals(qname, record->host_fqdn) && qtype == DNS_TYPE_AAAA) {
                    routes.snapshot_aaaa[j] |= reply_route;
                }
            }
        }
    }

    if (query_routes_have_destination(&routes, snapshot_records, use_snapshot_records, MDNS_REPLY_UNICAST)) {
        size_t reply_len;
        int answers;
        if (build_query_response_packet(reply, sizeof(reply), &reply_len, &answers, query_id, MDNS_REPLY_UNICAST,
                                        &routes, instance_fqdn, adisk_instance_fqdn, device_info_instance_fqdn,
                                        airport_instance_fqdn, cfg, response_link,
                                        snapshot_records, use_snapshot_records) != 0 ||
            (answers > 0 &&
             send_dns_packet_any("query_response", sockfd, reply, reply_len, source, source_len, answers, use_snapshot_records) != 0)) {
            status = -1;
        }
    }

    if (query_routes_have_destination(&routes, snapshot_records, use_snapshot_records, MDNS_REPLY_MULTICAST)) {
        size_t reply_len;
        int answers;
        if (build_query_response_packet(reply, sizeof(reply), &reply_len, &answers, 0, MDNS_REPLY_MULTICAST,
                                        &routes, instance_fqdn, adisk_instance_fqdn, device_info_instance_fqdn,
                                        airport_instance_fqdn, cfg, response_link,
                                        snapshot_records, use_snapshot_records) != 0 ||
            (answers > 0 &&
             send_dns_packet_any("query_response", sockfd, reply, reply_len, multicast_dest, multicast_dest_len, answers, use_snapshot_records) != 0)) {
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

static const struct iface_context MDNS_UNUSED *select_response_context(const struct iface_context_set *contexts,
                                                                       const struct sockaddr_in *source) {
    size_t i;

    if (contexts->count == 0) {
        return NULL;
    }
    if (source != NULL && source->sin_addr.s_addr != 0) {
        for (i = 0; i < contexts->count; i++) {
            if (source_matches_context_subnet(source->sin_addr.s_addr, &contexts->contexts[i])) {
                return &contexts->contexts[i];
            }
        }
    }
    return &contexts->contexts[0];
}

static void link_context_from_iface_context(struct link_context *out, const struct iface_context *ctx) {
    memset(out, 0, sizeof(*out));
    strncpy(out->name, ctx->name, sizeof(out->name) - 1);
    out->flags = ctx->flags;
    out->ipv4[0].addr = ctx->ipv4_addr;
    out->ipv4[0].netmask = ctx->netmask;
    out->ipv4_count = 1;
}

static int set_context_outbound_interface(int sockfd, const struct iface_context *ctx) {
    return set_outbound_multicast_interface(sockfd, ctx->ipv4_addr, "runtime", 0, 0);
}

static int send_context_announcement(int sockfd,
                                     const struct iface_context *ctx,
                                     const struct sockaddr_in *dest,
                                     const struct config *cfg,
                                     const struct service_record_set *snapshot_records,
                                     int use_snapshot_records) {
    struct link_context link;

    if (set_context_outbound_interface(sockfd, ctx) != 0) {
        return -1;
    }
    link_context_from_iface_context(&link, ctx);
    return send_announcement(sockfd, dest, cfg, &link, cfg->ttl, snapshot_records, use_snapshot_records);
}

static void MDNS_UNUSED send_context_goodbyes(int sockfd,
                                              struct iface_context_set *contexts,
                                              const struct sockaddr_in *dest,
                                              const struct config *cfg,
                                              const struct service_record_set *snapshot_records,
                                              int use_snapshot_records) {
    size_t i;

    for (i = 0; i < contexts->count; i++) {
        if (set_context_outbound_interface(sockfd, &contexts->contexts[i]) == 0) {
            struct link_context link;
            link_context_from_iface_context(&link, &contexts->contexts[i]);
            (void)send_announcement(sockfd, dest, cfg, &link, 0, snapshot_records, use_snapshot_records);
        }
    }
}

static void MDNS_UNUSED send_context_goodbyes_for_missing(int sockfd,
                                                          struct iface_context_set *old_contexts,
                                                          const struct iface_context_set *active_contexts,
                                                          const struct sockaddr_in *dest,
                                                          const struct config *cfg,
                                                          const struct service_record_set *snapshot_records,
                                                          int use_snapshot_records) {
    size_t i;

    for (i = 0; i < old_contexts->count; i++) {
        if (iface_context_set_contains(active_contexts, &old_contexts->contexts[i])) {
            continue;
        }
        if (set_context_outbound_interface(sockfd, &old_contexts->contexts[i]) == 0) {
            struct link_context link;
            link_context_from_iface_context(&link, &old_contexts->contexts[i]);
            (void)send_announcement(sockfd, dest, cfg, &link, 0, snapshot_records, use_snapshot_records);
        }
    }
}

static void MDNS_UNUSED announce_all_contexts(int sockfd,
                                              struct iface_context_set *contexts,
                                              const struct sockaddr_in *dest,
                                              const struct config *cfg,
                                              const struct service_record_set *snapshot_records,
                                              int use_snapshot_records,
                                              const char *stage) {
    size_t i;

    for (i = 0; i < contexts->count; i++) {
        if (send_context_announcement(sockfd, &contexts->contexts[i], dest, cfg, snapshot_records, use_snapshot_records) != 0) {
            char detail[128];
            char ip_buf[INET_ADDRSTRLEN];
            snprintf(detail, sizeof(detail), "stage=%s iface=%s ip=%s",
                     stage,
                     contexts->contexts[i].name,
                     ipv4_to_string(contexts->contexts[i].ipv4_addr, ip_buf, sizeof(ip_buf)));
            log_send_failure(stage, dest, use_snapshot_records, detail);
        }
    }
}

static int source_matches_link_ipv4_subnet(uint32_t source_ipv4_addr, const struct link_context *link) {
    size_t i;

    for (i = 0; i < link->ipv4_count; i++) {
        uint32_t netmask = link->ipv4[i].netmask;
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
            if (source_matches_link_ipv4_subnet(source->sin_addr.s_addr, &links->links[i])) {
                return &links->links[i];
            }
        }
    }
    return &links->links[0];
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
                if (links->links[i].ifindex == source->sin6_scope_id) {
                    return &links->links[i];
                }
            }
        }
        for (i = 0; i < links->count; i++) {
            size_t j;
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
    return links->count == 1 ? &links->links[0] : &links->links[0];
}

static int set_link_outbound_interface4(int sockfd, const struct link_context *link) {
    if (link->ipv4_count == 0) {
        errno = EADDRNOTAVAIL;
        return -1;
    }
    return set_outbound_multicast_interface(sockfd, link->ipv4[0].addr, "runtime", 0, 0);
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
    if (sockets->ipv4_fd >= 0 && link->ipv4_count > 0) {
        if (set_link_outbound_interface4(sockets->ipv4_fd, link) != 0 ||
            send_announcement(sockets->ipv4_fd, dest4, cfg, link, ttl, snapshot_records, use_snapshot_records) != 0) {
            char detail[160];
            snprintf(detail, sizeof(detail), "stage=%s iface=%s family=ipv4", stage, link->name);
            log_send_failure(stage, dest4, use_snapshot_records, detail);
        }
    }
    if (sockets->ipv6_fd >= 0 && link_context_has_mdns_ipv6_transport(link)) {
        struct sockaddr_in6 scoped_dest6;
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
                                                  const struct link_context_set *new_links) {
    int need_ipv4 = link_contexts_need_ipv4_socket(new_links);
    int need_ipv6 = link_contexts_need_ipv6_socket(new_links);
    int opened_ipv4 = 0;
    int opened_ipv6 = 0;

    if (!need_ipv4 && !need_ipv6) {
        errno = EADDRNOTAVAIL;
        return -1;
    }

    if (need_ipv4 && sockets->ipv4_fd < 0) {
        sockets->ipv4_fd = open_bound_mdns_socket(shared_bind, 1);
        if (sockets->ipv4_fd < 0) {
            goto fail;
        }
        opened_ipv4 = 1;
    }
    if (need_ipv6 && sockets->ipv6_fd < 0) {
        sockets->ipv6_fd = open_bound_mdns_socket6(shared_bind, 1);
        if (sockets->ipv6_fd < 0) {
            goto fail;
        }
        opened_ipv6 = 1;
    }

    if (need_ipv4 &&
        prepare_mdns_socket4_memberships(sockets->ipv4_fd,
                                         opened_ipv4 ? NULL : old_links,
                                         new_links,
                                         "runtime") != 0) {
        goto fail;
    }
    if (need_ipv6 &&
        prepare_mdns_socket6_memberships(sockets->ipv6_fd,
                                         opened_ipv6 ? NULL : old_links,
                                         new_links,
                                         "runtime") != 0) {
        goto fail;
    }
    return 0;

fail:
    if (opened_ipv4 && sockets->ipv4_fd >= 0) {
        close(sockets->ipv4_fd);
        sockets->ipv4_fd = -1;
    }
    if (opened_ipv6 && sockets->ipv6_fd >= 0) {
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
            if (old_links->links[i].ipv4_count == 0 ||
                link_set_has_ipv4_membership(new_links, old_links->links[i].ipv4[0].addr)) {
                continue;
            }
            drop_mdns_multicast_group_best_effort(sockets->ipv4_fd, old_links->links[i].ipv4[0].addr, "runtime");
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
        close(sockets->ipv4_fd);
        sockets->ipv4_fd = -1;
    }
    if (!link_contexts_need_ipv6_socket(links) && sockets->ipv6_fd >= 0) {
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
    if (prepare_runtime_mdns_sockets_for_links(shared_bind, sockets, active_links, new_links) != 0) {
        return -1;
    }
    send_link_goodbyes_for_missing(sockets,
                                   active_links,
                                   new_links,
                                   dest4,
                                   dest6,
                                   cfg,
                                   snapshot_records,
                                   use_snapshot_records);
    retire_runtime_mdns_memberships_for_missing(sockets, active_links, new_links);
    close_unused_runtime_mdns_socket_families(sockets, new_links);
    *active_links = *new_links;
    return 0;
}

static int open_mdns_socket(int shared_bind, int log_bind_errors, uint32_t ipv4_addr, const char *socket_role) {
    int sockfd;

    sockfd = open_bound_mdns_socket(shared_bind, log_bind_errors);
    if (sockfd < 0) {
        return -1;
    }
    if (configure_mdns_socket_for_ipv4(sockfd, ipv4_addr, socket_role) != 0) {
        close(sockfd);
        return -1;
    }

    return sockfd;
}

int main(int argc, char **argv) {
    struct config cfg;
    struct service_record_set snapshot_records;
    int sockfd = -1;
    struct sockaddr_in mdns_dest;
    struct sockaddr_in6 mdns_dest6;
    int i;
    time_t last_announce = 0;
    int use_snapshot_records = 0;
    int shared_bind = 0;
    int auto_ip = 0;
    int explicit_ipv4 = 0;
    int print_auto_ip_cidrs = 0;
    int print_smb_bind_interfaces = 0;
    int print_mdns_socket_families = 0;
    int auto_contexts_ready = 0;
    struct link_context_set auto_links;
    int capture_only = 0;
    int snapshot_capture_failed = 0;
    int snapshot_capture_skipped = 0;
    int trusted_snapshot_written = 0;
    static const unsigned int startup_burst_offsets_ms[STARTUP_BURST_COUNT] = {0, 250, 1000, 2000, 4000, 8000, 16000};
    size_t startup_burst_index = 0;
    long long startup_burst_start_ms = 0;

    memset(&cfg, 0, sizeof(cfg));
    memset(&snapshot_records, 0, sizeof(snapshot_records));
    memset(&auto_links, 0, sizeof(auto_links));
    strcpy(cfg.service_type, "_smb._tcp.local.");
    strcpy(cfg.adisk_service_type, "_adisk._tcp.local.");
    strcpy(cfg.adisk_disk_key, ADISK_DEFAULT_DISK_KEY);
    strcpy(cfg.adisk_disk_advf, ADISK_DEFAULT_DISK_ADVF);
    strcpy(cfg.device_info_service_type, "_device-info._tcp.local.");
    strcpy(cfg.airport_service_type, AIRPORT_SERVICE_TYPE);
    cfg.port = 445;
    cfg.adisk_port = 9;
    cfg.airport_port = AIRPORT_DEFAULT_PORT;
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
        } else if (strcmp(argv[i], "--load-snapshot") == 0 && i + 1 < argc) {
            strncpy(cfg.load_snapshot_path, argv[++i], sizeof(cfg.load_snapshot_path) - 1);
        } else if (strcmp(argv[i], "--shared-bind") == 0) {
            shared_bind = 1;
        } else if (strcmp(argv[i], "--diskless") == 0) {
            cfg.diskless = 1;
        } else if (strcmp(argv[i], "--auto-ip") == 0) {
            auto_ip = 1;
        } else if (strcmp(argv[i], "--print-auto-ip-cidrs") == 0) {
            print_auto_ip_cidrs = 1;
        } else if (strcmp(argv[i], "--print-smb-bind-interfaces") == 0) {
            print_smb_bind_interfaces = 1;
        } else if (strcmp(argv[i], "--print-mdns-socket-families") == 0) {
            print_mdns_socket_families = 1;
        } else if (strcmp(argv[i], "--version") == 0) {
            printf("%d\n", ADVERTISER_VERSION_CODE);
            return EXIT_OK;
        } else if (strcmp(argv[i], "--service") == 0 && i + 1 < argc) {
            strncpy(cfg.service_type, argv[++i], sizeof(cfg.service_type) - 1);
        } else if (strcmp(argv[i], "--adisk-share") == 0 && i + 1 < argc) {
            strncpy(cfg.adisk_share_name, argv[++i], sizeof(cfg.adisk_share_name) - 1);
        } else if (strcmp(argv[i], "--adisk-shares-file") == 0 && i + 1 < argc) {
            strncpy(cfg.adisk_shares_file, argv[++i], sizeof(cfg.adisk_shares_file) - 1);
        } else if (strcmp(argv[i], "--adisk-disk-key") == 0 && i + 1 < argc) {
            strncpy(cfg.adisk_disk_key, argv[++i], sizeof(cfg.adisk_disk_key) - 1);
        } else if (strcmp(argv[i], "--adisk-disk-advf") == 0 && i + 1 < argc) {
            strncpy(cfg.adisk_disk_advf, argv[++i], sizeof(cfg.adisk_disk_advf) - 1);
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
            explicit_ipv4 = 1;
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

    if (print_auto_ip_cidrs) {
        return print_auto_ip_cidrs_with_provider(stdout,
                                                 collect_usable_iface_contexts_provider,
                                                 NULL);
    }
    if (print_smb_bind_interfaces) {
        return print_smb_bind_interfaces_with_provider(stdout,
                                                       collect_usable_link_contexts_provider,
                                                       NULL);
    }
    if (print_mdns_socket_families) {
        return print_mdns_socket_families_with_provider(stdout,
                                                       collect_usable_link_contexts_provider,
                                                       NULL);
    }

    capture_only = (cfg.load_snapshot_path[0] == '\0' &&
                    (cfg.save_all_snapshot_path[0] != '\0' ||
                     cfg.save_airport_snapshot_path[0] != '\0' ||
                     cfg.save_snapshot_path[0] != '\0'));

    if (auto_ip && explicit_ipv4) {
        fprintf(stderr, "--auto-ip and --ipv4 are mutually exclusive\n");
        usage(argv[0]);
        return EXIT_USAGE;
    }
    if (!capture_only && (cfg.instance_name[0] == '\0' || cfg.host_label[0] == '\0' || (!auto_ip && cfg.ipv4_addr == 0))) {
        usage(argv[0]);
        return EXIT_MISSING_REQUIRED_ARGS;
    }
    if (cfg.save_airport_snapshot_path[0] != '\0' &&
        (cfg.instance_name[0] == '\0' || cfg.host_label[0] == '\0' || !cfg_has_airport_identity_macs(&cfg))) {
        fprintf(stderr, "--save-airport-snapshot requires --instance, --host, and at least one AirPort identity MAC\n");
        usage(argv[0]);
        return EXIT_MISSING_REQUIRED_ARGS;
    }

    if ((cfg.instance_name[0] != '\0' && validate_single_dns_label(cfg.instance_name, "instance name") != 0) ||
        (cfg.host_label[0] != '\0' && validate_single_dns_label(cfg.host_label, "host label") != 0)) {
        return EXIT_INVALID_DNS_LABEL;
    }
    if (validate_dns_name(cfg.service_type, "service type") != 0) {
        return EXIT_INVALID_SERVICE_TYPE;
    }
    if (cfg.adisk_shares_file[0] != '\0' && parse_adisk_shares_file(&cfg, cfg.adisk_shares_file) != 0) {
        return EXIT_INVALID_ADISK_DISK;
    }
    if (cfg.adisk_share_name[0] != '\0' &&
        add_adisk_disk_config(&cfg, cfg.adisk_share_name, cfg.adisk_disk_key, cfg.adisk_uuid, cfg.adisk_disk_advf) != 0) {
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
        snprintf(cfg.host_fqdn, sizeof(cfg.host_fqdn), "%s.local.", cfg.host_label);
        log_startup_config(&cfg, shared_bind, auto_ip);
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
            if ((auto_ip && capture_links_ready &&
                 capture_mdns_snapshot_links_with_retry(&captured_records, &capture_links) == 0) ||
                (!auto_ip && capture_mdns_snapshot_raw_with_retry(&captured_records, cfg.ipv4_addr) == 0)) {
                fprintf(stderr, "snapshot capture: captured %lu records\n", (unsigned long)captured_records.count);
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
        struct mdns_socket_pair sockets;

        if (!auto_contexts_ready) {
            if (wait_for_auto_advertise_link_contexts(&auto_links, "mdns runtime") != 0) {
                return EXIT_AUTO_IP_UNAVAILABLE;
            }
            auto_contexts_ready = 1;
        }
        log_link_contexts("mdns runtime auto-ip", &auto_links);
        sockets.ipv4_fd = -1;
        sockets.ipv6_fd = -1;
        if (acquire_dualstack_mdns_sockets(shared_bind, &auto_links, &sockets) != 0) {
            return EXIT_SOCKET_ACQUIRE_FAILED;
        }

        startup_burst_start_ms = monotonic_millis();
        last_announce = time(NULL);
        last_iface_poll = time(NULL);

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
                    !link_context_sets_equal(&auto_links, &next_links)) {
                    struct link_context_set stabilized_links;
                    fprintf(stderr, "mdns auto-ip: interface table changed; confirming after %ds stabilization\n",
                            AUTO_IP_STABILIZE_SECONDS);
                    log_link_contexts("mdns auto-ip old", &auto_links);
                    log_link_contexts("mdns auto-ip observed", &next_links);
                    sleep(AUTO_IP_STABILIZE_SECONDS);
                    memset(&stabilized_links, 0, sizeof(stabilized_links));
                    if (collect_usable_advertise_link_contexts_provider(&stabilized_links, NULL) == 0) {
                        if (link_context_sets_equal(&auto_links, &stabilized_links)) {
                            fprintf(stderr, "mdns auto-ip: observed interface change did not persist after stabilization\n");
                        } else {
                            if (stabilized_links.count > 0) {
                                log_link_contexts("mdns auto-ip stabilized", &stabilized_links);
                                if (apply_runtime_link_change(shared_bind,
                                                              &sockets,
                                                              &auto_links,
                                                              &stabilized_links,
                                                              &mdns_dest,
                                                              &mdns_dest6,
                                                              &cfg,
                                                              &snapshot_records,
                                                              use_snapshot_records) != 0) {
                                    fprintf(stderr, "mdns auto-ip: could not apply stabilized address links; keeping previous links until next poll\n");
                                    last_iface_poll = time(NULL);
                                    continue;
                                }
                            } else {
                                fprintf(stderr, "mdns auto-ip: no usable address links after stabilization; sending goodbyes and waiting\n");
                                send_link_goodbyes(&sockets, &auto_links, &mdns_dest, &mdns_dest6, &cfg, &snapshot_records, use_snapshot_records);
                                close_mdns_socket_pair(&sockets);
                                memset(&auto_links, 0, sizeof(auto_links));
                                if (wait_for_auto_advertise_link_contexts(&stabilized_links, "mdns runtime") != 0) {
                                    break;
                                }
                                if (acquire_dualstack_mdns_sockets(shared_bind, &stabilized_links, &sockets) != 0) {
                                    fprintf(stderr, "mdns auto-ip: usable address links returned but sockets could not be acquired\n");
                                    last_iface_poll = time(NULL);
                                    continue;
                                }
                                auto_links = stabilized_links;
                            }
                            log_link_contexts("mdns auto-ip active", &auto_links);
                            startup_burst_start_ms = monotonic_millis();
                            startup_burst_index = 0;
                            last_announce = time(NULL);
                        }
                    }
                }
                last_iface_poll = time(NULL);
            }

            now_ms = monotonic_millis();
            while (startup_burst_index < STARTUP_BURST_COUNT &&
                   now_ms - startup_burst_start_ms >= (long long)startup_burst_offsets_ms[startup_burst_index]) {
                announce_all_links(&sockets, &auto_links, &mdns_dest, &mdns_dest6, &cfg, &snapshot_records, use_snapshot_records, "startup_announce");
                startup_burst_index++;
                now_ms = monotonic_millis();
            }

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

            {
                int selected = maxfd >= 0 ? select(maxfd + 1, &rfds, NULL, NULL, &tv) : -1;
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
                        const struct link_context *link = select_response_link_ipv4(&auto_links, &src);
                        if (link != NULL &&
                            (set_link_outbound_interface4(sockets.ipv4_fd, link) != 0 ||
                             handle_query(sockets.ipv4_fd, packet, (size_t)nread, &mdns_dest, &src, &cfg, link, &snapshot_records, use_snapshot_records) != 0)) {
                            char detail[160];
                            snprintf(detail, sizeof(detail), "iface=%s packet_len=%ld from=%s:%u",
                                     link->name,
                                     (long)nread, inet_ntoa(src.sin_addr), (unsigned int)ntohs(src.sin_port));
                            log_send_failure("query_response", &mdns_dest, use_snapshot_records, detail);
                        }
                    }
                }
                if (selected > 0 && sockets.ipv6_fd >= 0 && FD_ISSET(sockets.ipv6_fd, &rfds)) {
                    struct sockaddr_in6 src6;
                    socklen_t src6_len = sizeof(src6);
                    ssize_t nread = recvfrom(sockets.ipv6_fd, packet, sizeof(packet), 0, (struct sockaddr *)&src6, &src6_len);
                    if (nread > 0) {
                        const struct link_context *link = select_response_link_ipv6(&auto_links, &src6);
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
                    }
                }
            }

            if (time(NULL) - last_announce >= ANNOUNCE_INTERVAL) {
                announce_all_links(&sockets, &auto_links, &mdns_dest, &mdns_dest6, &cfg, &snapshot_records, use_snapshot_records, "periodic_announce");
                last_announce = time(NULL);
            }
        }

        send_link_goodbyes(&sockets, &auto_links, &mdns_dest, &mdns_dest6, &cfg, &snapshot_records, use_snapshot_records);
        close_mdns_socket_pair(&sockets);
        return 0;
    }

    sockfd = acquire_mdns_socket(shared_bind, cfg.ipv4_addr);
    if (sockfd < 0) {
        return EXIT_SOCKET_ACQUIRE_FAILED;
    }

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
            struct link_context link;
            memset(&link, 0, sizeof(link));
            strncpy(link.name, "explicit", sizeof(link.name) - 1);
            link.ipv4[0].addr = cfg.ipv4_addr;
            link.ipv4[0].netmask = htonl(0xffffffffU);
            link.ipv4_count = 1;
            if (send_announcement(sockfd, &mdns_dest, &cfg, &link, cfg.ttl, &snapshot_records, use_snapshot_records) != 0) {
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
                struct link_context link;
                memset(&link, 0, sizeof(link));
                strncpy(link.name, "explicit", sizeof(link.name) - 1);
                link.ipv4[0].addr = cfg.ipv4_addr;
                link.ipv4[0].netmask = htonl(0xffffffffU);
                link.ipv4_count = 1;
                if (handle_query(sockfd, packet, (size_t)nread, &mdns_dest, &src, &cfg, &link, &snapshot_records, use_snapshot_records) != 0) {
                    char detail[128];
                    snprintf(detail, sizeof(detail), "packet_len=%ld from=%s:%u",
                             (long)nread, inet_ntoa(src.sin_addr), (unsigned int)ntohs(src.sin_port));
                    log_send_failure("query_response", &mdns_dest, use_snapshot_records, detail);
                }
            }
        }

        if (time(NULL) - last_announce >= ANNOUNCE_INTERVAL) {
            struct link_context link;
            memset(&link, 0, sizeof(link));
            strncpy(link.name, "explicit", sizeof(link.name) - 1);
            link.ipv4[0].addr = cfg.ipv4_addr;
            link.ipv4[0].netmask = htonl(0xffffffffU);
            link.ipv4_count = 1;
            if (send_announcement(sockfd, &mdns_dest, &cfg, &link, cfg.ttl, &snapshot_records, use_snapshot_records) != 0) {
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

#undef fprintf
#undef perror
#undef MDNS_UNUSED
