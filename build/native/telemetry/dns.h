#ifndef TC_TELEMETRY_DNS_H
#define TC_TELEMETRY_DNS_H
#include "../common/platform.h"

/* A small DNS client for the telemetry host, so a heartbeat needs no fork.
 *
 * libc's getaddrinfo() would add about 250 KB (nsswitch, the full resolver)
 * to the static service image on /mnt/Flash; this reads the nameservers
 * from /etc/resolv.conf and asks each, in order, for A and AAAA records over
 * UDP, following CNAMEs. A numeric address skips DNS. The answer is kept in
 * this process's memory for its TTL (60 s to a day) and reused for up to a
 * week if a later lookup fails, so a heartbeat during a DNS outage still
 * finds the server. There is no /etc/hosts lookup: nothing on the device
 * names the telemetry host there. */

#ifndef TC_RESOLV_CONF
#define TC_RESOLV_CONF "/etc/resolv.conf"
#endif
#ifndef TC_DNS_PORT
#define TC_DNS_PORT 53
#endif
/* Each nameserver gets this long for both answers. */
#ifndef TC_DNS_SERVER_MS
#define TC_DNS_SERVER_MS 2000
#endif
#define TC_DNS_MAX_ADDRS 8
#define TC_DNS_MAX_SERVERS 3

struct tc_dns_addr {
    int family; /* AF_INET or AF_INET6 */
    unsigned char addr[16];
};

/* Addresses for name, IPv6 and IPv4 alternating (IPv6 first), or -1 when
 * there are none by deadline_ms (acp_monotonic_ms). *resource is set when a
 * socket could not be made for want of descriptors or memory (it is never
 * cleared, so a caller can pass one flag through several steps). */
int tc_dns_resolve(const char *name, struct tc_dns_addr *out, size_t max, size_t *count, long long deadline_ms,
                   int *resource);
#endif
