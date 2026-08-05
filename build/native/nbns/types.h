#ifndef TC_NBNS_TYPES_H
#define TC_NBNS_TYPES_H
#include "../common/network.h"
#include "../common/log.h"
#define NBNS_PORT 137
#define BUF_SIZE 576
#define MAX_NAME 16
#define MAX_PACKET_NAME 34
#define DNS_CLASS_IN 1
#define NB_TYPE_NULL 0x000A
#define NB_TYPE_NB 0x0020
#define NB_TYPE_NBSTAT 0x0021
#define NBNS_FLAG_RESPONSE 0x8000
#define NBNS_FLAG_AUTHORITATIVE 0x0400
#define NBNS_FLAG_RECURSION_AVAILABLE 0x0080
#define NBNS_FLAG_BROADCAST 0x0010
#define NBNS_RCODE_POSITIVE 0x0000
#define NBNS_RCODE_NAME_ERROR 0x0003
#define NBNS_SUFFIX_WORKSTATION 0x00
#define NBNS_SUFFIX_SERVER 0x20
#define NBNS_NAME_FLAGS_ACTIVE 0x0400
#define NBNS_NODE_STATUS_NAME_COUNT 2
#define NBNS_NODE_STATUS_STATS_LEN 46
#define MAX_IFACE_CONTEXTS 16
#define AUTO_IP_STABILIZE_SECONDS 3
#define AUTO_IP_STARTUP_POLL_SECONDS 2
#define AUTO_IP_STABLE_POLL_SECONDS 30
#define ADVERTISER_VERSION_CODE 2200
#define EXIT_OK 0
#define EXIT_RUNTIME_ERROR 1
#define EXIT_USAGE 2
#define EXIT_AUTO_IP_UNAVAILABLE 11
#define EXIT_AUTO_IP_PROBE_FAILED 13
#define TC_HEARTBEAT_LANE "heartbeat6"
struct config {
    char netbios_name[MAX_NAME];
    uint32_t ipv4_addr;
    uint32_t ttl;
};

struct nbns_header {
    uint16_t id;
    uint16_t flags;
    uint16_t qdcount;
    uint16_t ancount;
    uint16_t nscount;
    uint16_t arcount;
};


extern volatile sig_atomic_t g_stop;
#endif
