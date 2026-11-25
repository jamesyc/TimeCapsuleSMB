#ifndef TC_TELEMETRY_DNS_H
#define TC_TELEMETRY_DNS_H
#include "../common/platform.h"

/* A small DNS client for the telemetry host, so a heartbeat needs no fork.
 *
 * libc's getaddrinfo() would add about 250 KB (nsswitch, the full resolver)
 * to the static service image on /mnt/Flash; this reads the nameservers
 * from /etc/resolv.conf and asks each, in order, for A and AAAA records over
 * UDP, following CNAMEs. A numeric address skips DNS. An answer with a clean
 * A reply is kept in this process's memory for the shortest TTL among its
 * addresses and CNAMEs (60 s to a day) and reused for up to a week if a
 * later lookup gets no answer, so a heartbeat during a DNS outage still
 * finds the server. An answer without a clean A reply is used once, not
 * kept, and followed by the IPv4 addresses of the kept answer, up to a week
 * old. NXDOMAIN for A, or no record of either type, fails the lookup and
 * forgets the kept answer for the name. There is no /etc/hosts lookup:
 * nothing on the device names the telemetry host there. */

#ifndef TC_RESOLV_CONF
#define TC_RESOLV_CONF "/etc/resolv.conf"
#endif
#ifndef TC_DNS_PORT
#define TC_DNS_PORT 53
#endif
/* Time per nameserver as NetBSD's libc resolver gives it (BIND res_send,
 * what curl's getaddrinfo used): TC_DNS_TRIES rounds over the list, each
 * server getting TC_DNS_RETRANS_MS on the first and that doubled per round,
 * shared among the servers, after it, at least 1 s (RES_TIMEOUT 5 s,
 * RES_DFLRETRY 2). One server: 5 s then 10 s; three: 5 s each, then 3.3 s
 * each. A round asks a server only the queries it has not answered, on the
 * socket it was first asked on, and a server that answered both is not
 * asked again. */
#ifndef TC_DNS_RETRANS_MS
#define TC_DNS_RETRANS_MS 5000
#endif
#define TC_DNS_TRIES 2
#define TC_DNS_MIN_SERVER_MS 1000
#define TC_DNS_MAX_ADDRS 8
#define TC_DNS_MAX_SERVERS 3

struct tc_dns_addr {
    int family; /* AF_INET or AF_INET6 */
    unsigned char addr[16];
};

/* Addresses for name, each once, IPv6 and IPv4 alternating (IPv6 first),
 * followed, when this answer is not kept, by the IPv4 addresses of the kept
 * answer for name still in its week; or -1 when there are none by
 * deadline_ms (acp_monotonic_ms) or the name has none. *resource is 1 only when the lookup
 * failed and the device's want of descriptors or memory was part of why
 * (resolv.conf or a socket could not be opened, or a socket had no buffers
 * for its traffic); 0 otherwise. */
int tc_dns_resolve(const char *name, struct tc_dns_addr *out, size_t max, size_t *count, long long deadline_ms,
                   int *resource);
#endif
