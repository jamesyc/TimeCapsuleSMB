#ifndef TC_MDNS_TYPES_H
#define TC_MDNS_TYPES_H
#include "../common/network.h"
#include "../common/log.h"
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
#define TAKEOVER_RETRY_COUNT 6
#define MAX_IFACE_CONTEXTS 16
#define AUTO_IP_STABILIZE_SECONDS 3
#define AUTO_IP_STARTUP_POLL_SECONDS 2
#define AUTO_IP_STABLE_POLL_SECONDS 30
#define MDNS_DEGRADED_RETRY_SECONDS 5
#define MDNS_MDNSRESPONDER_GUARD_SECONDS 5
#define MDNS_COUNTER_LOG_INTERVAL_MS 30000
#define ADVERTISER_VERSION_CODE 2224
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

enum mdns_service_scope {
    MDNS_SERVICE_SCOPE_LAN = 0,
    MDNS_SERVICE_SCOPE_WAN = 1
};

#if !defined(IPV6_JOIN_GROUP) && defined(IPV6_ADD_MEMBERSHIP)
#define IPV6_JOIN_GROUP IPV6_ADD_MEMBERSHIP
#endif
#if !defined(IPV6_LEAVE_GROUP) && defined(IPV6_DROP_MEMBERSHIP)
#define IPV6_LEAVE_GROUP IPV6_DROP_MEMBERSHIP
#endif

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

struct dns_header {
    uint16_t id;
    uint16_t flags;
    uint16_t qdcount;
    uint16_t ancount;
    uint16_t nscount;
    uint16_t arcount;
};

typedef int (*mdns_collect_link_contexts_fn)(struct link_context_set *, void *);
typedef void (*mdns_sleep_fn)(unsigned int, void *);
extern volatile sig_atomic_t g_stop;
extern struct deferred_response g_deferred_response;
extern struct mdns_runtime_counters g_mdns_counters;
extern struct mdns_counter_log_state g_mdns_counter_log_state;
extern int g_last_ipv4_socket_errno;
extern int g_last_ipv6_socket_errno;
extern int g_debug_logging;
extern const unsigned int g_startup_burst_offsets_ms[STARTUP_BURST_COUNT];
ssize_t sendto_retry(int, const void *, size_t, int, const struct sockaddr *, socklen_t);

#endif
