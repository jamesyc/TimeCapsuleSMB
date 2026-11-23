#include "dns.h"
#include "../common/acp.h"
#include "telemetry.h"
#include "errclass.h"

#define TYPE_A 1
#define TYPE_CNAME 5
#define TYPE_AAAA 28
#define NAME_MAX_TEXT 256
#define MAX_ALIASES 8
#define TTL_MIN 60
#define TTL_MAX 86400
#define STALE_MS (7LL * 86400 * 1000)

static struct {
    char name[NAME_MAX_TEXT];
    struct tc_dns_addr addrs[TC_DNS_MAX_ADDRS];
    size_t count;
    long long fresh_until, stale_until;
} cache;

struct answers {
    struct tc_dns_addr v4[TC_DNS_MAX_ADDRS], v6[TC_DNS_MAX_ADDRS];
    size_t n4, n6;
    unsigned long ttl;
    int done[2];      /* A, AAAA answered */
    int nxdomain;
};

static unsigned query_id(void) {
    unsigned char bytes[2];
    int fd = open("/dev/urandom", O_RDONLY);
    ssize_t n = fd >= 0 ? read(fd, bytes, sizeof(bytes)) : -1;
    if (fd >= 0) close(fd);
    if (n == (ssize_t)sizeof(bytes)) return (unsigned)bytes[0] << 8 | bytes[1];
    return (unsigned)(acp_monotonic_ms() ^ getpid()) & 0xffff;
}

/* A query for name (dotted, no trailing dot) of the given type. */
static size_t build_query(unsigned char *out, size_t size, unsigned id, const char *name, unsigned type) {
    size_t used = 12, label;
    const char *p = name;
    if (size < 12 + strlen(name) + 2 + 4) return 0;
    memset(out, 0, 12);
    out[0] = (unsigned char)(id >> 8); out[1] = (unsigned char)id;
    out[2] = 0x01; /* recursion desired */
    out[5] = 1;    /* one question */
    while (*p) {
        const char *dot = strchr(p, '.');
        label = dot ? (size_t)(dot - p) : strlen(p);
        if (label == 0 || label > 63) return 0;
        out[used++] = (unsigned char)label;
        memcpy(out + used, p, label);
        used += label;
        p += label;
        if (*p == '.') p++;
    }
    out[used++] = 0;
    out[used++] = (unsigned char)(type >> 8); out[used++] = (unsigned char)type;
    out[used++] = 0; out[used++] = 1; /* class IN */
    return used;
}

/* Decodes the (possibly compressed) name at *pos into lowercase dotted text
 * and moves *pos past it. Returns -1 for a malformed name. */
static int read_name(const unsigned char *msg, size_t len, size_t *pos, char *out, size_t size) {
    size_t p = *pos, used = 0;
    int jumped = 0, hops = 0;
    for (;;) {
        unsigned c;
        if (p >= len) return -1;
        c = msg[p];
        if ((c & 0xc0) == 0xc0) {
            if (p + 1 >= len || ++hops > 16) return -1;
            if (!jumped) *pos = p + 2;
            jumped = 1;
            p = (size_t)(c & 0x3f) << 8 | msg[p + 1];
            continue;
        }
        if (c & 0xc0) return -1;
        p++;
        if (c == 0) break;
        if (p + c > len || used + c + 2 > size) return -1;
        if (used) out[used++] = '.';
        for (; c; c--, p++) {
            /* A dot or NUL inside a label would make two names compare equal. */
            if (msg[p] == '.' || msg[p] == 0) return -1;
            out[used++] = (char)tolower(msg[p]);
        }
    }
    out[used] = 0;
    if (!jumped) *pos = p;
    return 0;
}

static int in_set(char set[][NAME_MAX_TEXT], size_t n, const char *name) {
    size_t i;
    for (i = 0; i < n; i++)
        if (!strcmp(set[i], name)) return 1;
    return 0;
}

/* Parses one reply into part (cleared first); returns which query it
 * answers (0 A, 1 AAAA) or -1 for anything that is not a complete, valid
 * answer to one of ours. The caller merges part only on success. */
static int parse_reply(const unsigned char *msg, size_t len, const unsigned ids[2], const char *qname,
                       struct answers *part) {
    char names[MAX_ALIASES][NAME_MAX_TEXT], owner[NAME_MAX_TEXT], target[NAME_MAX_TEXT];
    size_t pos = 12, count = 1, records, i, start;
    unsigned id, rcode, which, qtype, ancount;
    int changed, pass;
    memset(part, 0, sizeof(*part));
    part->ttl = TTL_MAX;
    if (len < 12) return -1;
    id = (unsigned)msg[0] << 8 | msg[1];
    if (id != ids[0] && id != ids[1]) return -1;
    which = id == ids[0] ? 0 : 1;
    if (!(msg[2] & 0x80) || (msg[2] & 0x78) || (msg[2] & 0x02)) return -1; /* reply, QUERY, not truncated */
    if (msg[4] != 0 || msg[5] != 1) return -1;
    if (read_name(msg, len, &pos, owner, sizeof(owner)) || strcasecmp(owner, qname) || pos + 4 > len) return -1;
    qtype = (unsigned)msg[pos] << 8 | msg[pos + 1];
    if (qtype != (which ? TYPE_AAAA : TYPE_A)) return -1;
    pos += 4;
    rcode = msg[3] & 0x0f;
    if (rcode == 3) { part->nxdomain = 1; return (int)which; }
    if (rcode != 0) return -1;
    ancount = (unsigned)msg[6] << 8 | msg[7];
    snprintf(names[0], sizeof(names[0]), "%s", qname);
    for (i = 0; names[0][i]; i++) names[0][i] = (char)tolower((unsigned char)names[0][i]);
    start = pos;
    /* CNAMEs first, until the chain stops growing, then the addresses of
     * every name on it. */
    for (pass = 0, changed = 1; changed && pass < MAX_ALIASES; pass++) {
        changed = 0;
        for (pos = start, records = 0; records < ancount; records++) {
            unsigned type, rdlen;
            size_t rdata;
            if (read_name(msg, len, &pos, owner, sizeof(owner)) || pos + 10 > len) return -1;
            type = (unsigned)msg[pos] << 8 | msg[pos + 1];
            rdlen = (unsigned)msg[pos + 8] << 8 | msg[pos + 9];
            rdata = pos + 10;
            if (rdata + rdlen > len) return -1;
            if (type == TYPE_CNAME && in_set(names, count, owner)) {
                size_t t = rdata;
                if (read_name(msg, len, &t, target, sizeof(target))) return -1;
                if (!in_set(names, count, target) && count < MAX_ALIASES) {
                    snprintf(names[count++], sizeof(names[0]), "%s", target);
                    changed = 1;
                }
            }
            pos = rdata + rdlen;
        }
    }
    for (pos = start, records = 0; records < ancount; records++) {
        unsigned type, rdlen, klass;
        unsigned long ttl;
        size_t rdata;
        if (read_name(msg, len, &pos, owner, sizeof(owner)) || pos + 10 > len) return -1;
        type = (unsigned)msg[pos] << 8 | msg[pos + 1];
        klass = (unsigned)msg[pos + 2] << 8 | msg[pos + 3];
        ttl = (unsigned long)msg[pos + 4] << 24 | (unsigned long)msg[pos + 5] << 16 |
              (unsigned long)msg[pos + 6] << 8 | msg[pos + 7];
        rdlen = (unsigned)msg[pos + 8] << 8 | msg[pos + 9];
        rdata = pos + 10;
        if (rdata + rdlen > len) return -1;
        pos = rdata + rdlen;
        if (klass != 1 || !in_set(names, count, owner)) continue;
        if (type == TYPE_A && rdlen == 4 && part->n4 < TC_DNS_MAX_ADDRS) {
            part->v4[part->n4].family = AF_INET;
            memcpy(part->v4[part->n4++].addr, msg + rdata, 4);
        } else if (type == TYPE_AAAA && rdlen == 16 && part->n6 < TC_DNS_MAX_ADDRS) {
            part->v6[part->n6].family = AF_INET6;
            memcpy(part->v6[part->n6++].addr, msg + rdata, 16);
        } else {
            continue;
        }
        if (ttl < part->ttl) part->ttl = ttl;
    }
    return (int)which;
}

/* The first valid answer to each query counts; a repeat adds nothing. */
static void merge(struct answers *a, const struct answers *part, int which) {
    size_t i;
    if (a->done[which]) return;
    a->done[which] = 1;
    a->nxdomain |= part->nxdomain;
    for (i = 0; i < part->n4 && a->n4 < TC_DNS_MAX_ADDRS; i++) a->v4[a->n4++] = part->v4[i];
    for (i = 0; i < part->n6 && a->n6 < TC_DNS_MAX_ADDRS; i++) a->v6[a->n6++] = part->v6[i];
    if (part->ttl < a->ttl) a->ttl = part->ttl;
}

/* Up to TC_DNS_MAX_SERVERS nameservers from resolv.conf; 127.0.0.1 when it
 * names none, as libc does. A link-local server needs a scope, and NetBSD 4
 * has no reliable if_nametoindex(); the interface table (iflist) could map
 * the name, but no device seen so far has only link-local nameservers, so
 * they are skipped until one does. */
static size_t nameservers(struct sockaddr_storage *out) {
    char line[256], text[64];
    size_t n = 0;
    FILE *f = fopen(TC_RESOLV_CONF, "r");
    while (f && n < TC_DNS_MAX_SERVERS && fgets(line, sizeof(line), f)) {
        struct sockaddr_in *v4 = (struct sockaddr_in *)&out[n];
        struct sockaddr_in6 *v6 = (struct sockaddr_in6 *)&out[n];
        char *scope;
        if (sscanf(line, "nameserver %63s", text) != 1) continue;
        if ((scope = strchr(text, '%'))) *scope = 0;
        memset(&out[n], 0, sizeof(out[n]));
        if (inet_pton(AF_INET, text, &v4->sin_addr) == 1) {
            v4->sin_family = AF_INET;
            v4->sin_port = htons(TC_DNS_PORT);
            n++;
        } else if (inet_pton(AF_INET6, text, &v6->sin6_addr) == 1 && !IN6_IS_ADDR_LINKLOCAL(&v6->sin6_addr)) {
            v6->sin6_family = AF_INET6;
            v6->sin6_port = htons(TC_DNS_PORT);
            n++;
        }
    }
    if (f) fclose(f);
    if (!n) {
        struct sockaddr_in *v4 = (struct sockaddr_in *)&out[0];
        memset(&out[0], 0, sizeof(out[0]));
        v4->sin_family = AF_INET;
        v4->sin_port = htons(TC_DNS_PORT);
        v4->sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        n = 1;
    }
    return n;
}

/* Asks one server for both records. A query still unanswered halfway
 * through the server's time is sent once more (UDP loses datagrams). Returns
 * 1 when it answered (addresses or NXDOMAIN), 0 to try the next one. */
static int ask(const struct sockaddr_storage *server, const char *name, struct answers *a, long long deadline_ms,
               int *resource) {
    unsigned char query[2][300], reply[1500];
    unsigned ids[2];
    size_t lengths[2], i;
    socklen_t salen = server->ss_family == AF_INET ? sizeof(struct sockaddr_in) : sizeof(struct sockaddr_in6);
    long long until = acp_monotonic_ms() + TC_DNS_SERVER_MS, resend_at = acp_monotonic_ms() + TC_DNS_SERVER_MS / 2;
    int fd = socket(server->ss_family, SOCK_DGRAM, 0), result = 0, resent = 0;
    if (until > deadline_ms) until = deadline_ms;
    if (fd < 0) {
        if (tc_errno_record(TC_OP_SOCKET, errno) == TC_ERR_RESOURCE) *resource = 1;
        return 0;
    }
    if (fd >= FD_SETSIZE) goto done;
    ids[0] = query_id();
    ids[1] = (ids[0] + 1) & 0xffff;
    lengths[0] = build_query(query[0], sizeof(query[0]), ids[0], name, TYPE_A);
    lengths[1] = build_query(query[1], sizeof(query[1]), ids[1], name, TYPE_AAAA);
    /* connect() makes the kernel drop datagrams from anyone else. */
    if (!lengths[0] || !lengths[1] || connect(fd, (const struct sockaddr *)server, salen)) goto done;
    for (i = 0; i < 2; i++)
        if (send(fd, query[i], lengths[i], 0) != (ssize_t)lengths[i]) goto done;
    while (!(a->done[0] && a->done[1]) && !telemetry_stop) {
        fd_set reads;
        struct timeval timeout;
        long long now = acp_monotonic_ms(), left = (resent ? until : resend_at) - now;
        struct answers part;
        int ready, which;
        ssize_t n;
        if (now >= until) break;
        if (!resent && left <= 0) {
            for (i = 0; i < 2; i++)
                if (!a->done[i]) (void)send(fd, query[i], lengths[i], 0);
            resent = 1;
            continue;
        }
        if (left > 1000) left = 1000; /* recheck the stop flag every second */
        FD_ZERO(&reads);
        FD_SET(fd, &reads);
        timeout.tv_sec = (time_t)(left / 1000);
        timeout.tv_usec = (suseconds_t)(left % 1000) * 1000;
        ready = select(fd + 1, &reads, NULL, NULL, &timeout);
        if (ready == 0) continue;
        if (ready < 0) {
            if (errno == EINTR) continue;
            break;
        }
        n = recv(fd, reply, sizeof(reply), 0);
        if (n > 0 && (which = parse_reply(reply, (size_t)n, ids, name, &part)) >= 0) merge(a, &part, which);
        else if (n < 0 && errno != EINTR && errno != EAGAIN) break; /* ICMP refused */
    }
    result = a->nxdomain || a->n4 || a->n6;
done:
    close(fd);
    return result;
}

static size_t interleave(const struct answers *a, struct tc_dns_addr *out, size_t max) {
    size_t n = 0, i;
    for (i = 0; (i < a->n6 || i < a->n4) && n < max; i++) {
        if (i < a->n6 && n < max) out[n++] = a->v6[i];
        if (i < a->n4 && n < max) out[n++] = a->v4[i];
    }
    return n;
}

int tc_dns_resolve(const char *name, struct tc_dns_addr *out, size_t max, size_t *count, long long deadline_ms,
                   int *resource) {
    struct sockaddr_storage servers[TC_DNS_MAX_SERVERS];
    struct answers a;
    size_t n, i;
    long long now = acp_monotonic_ms();
    *count = 0;
    if (!max || now < 0) return -1;
    if (inet_pton(AF_INET6, name, out[0].addr) == 1) {
        out[0].family = AF_INET6;
        *count = 1;
        return 0;
    }
    if (inet_pton(AF_INET, name, out[0].addr) == 1) {
        out[0].family = AF_INET;
        *count = 1;
        return 0;
    }
    if (strlen(name) >= NAME_MAX_TEXT - 2) return -1;
    if (cache.count && !strcasecmp(cache.name, name) && now < cache.fresh_until) goto cached;
    n = nameservers(servers);
    for (i = 0; i < n && acp_monotonic_ms() < deadline_ms; i++) {
        memset(&a, 0, sizeof(a));
        a.ttl = TTL_MAX;
        if (!ask(&servers[i], name, &a, deadline_ms, resource)) continue;
        if (a.nxdomain && !a.n4 && !a.n6) break;
        /* Half an answer (the other reply lost twice) serves this request
         * but is not kept: an AAAA-only answer on a device whose IPv6 does
         * not reach the server would fail every request until its TTL. */
        if (!(a.done[0] && a.done[1])) {
            *count = interleave(&a, out, max);
            return 0;
        }
        if (a.ttl < TTL_MIN) a.ttl = TTL_MIN;
        snprintf(cache.name, sizeof(cache.name), "%s", name);
        cache.count = interleave(&a, cache.addrs, TC_DNS_MAX_ADDRS);
        now = acp_monotonic_ms();
        cache.fresh_until = now + (long long)a.ttl * 1000;
        cache.stale_until = now + STALE_MS;
        goto cached;
    }
    /* No answer now: the last one, if it is not too old. */
    if (cache.count && !strcasecmp(cache.name, name) && acp_monotonic_ms() < cache.stale_until) goto cached;
    return -1;
cached:
    for (i = 0; i < cache.count && i < max; i++) out[i] = cache.addrs[i];
    *count = i;
    return 0;
}
