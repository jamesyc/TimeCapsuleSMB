#include "dns.h"
#include "../common/acp.h"
#include "telemetry.h"
#include "errclass.h"
#include "entropy.h"
#include "../common/iflist.h"

#define TYPE_A 1
#define TYPE_CNAME 5
#define TYPE_AAAA 28
#define CLASS_IN 1
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

/* What one nameserver said to each query (done[0] A, done[1] AAAA). FAILED
 * covers SERVFAIL, REFUSED and the like and a truncated reply; NO_NAME is
 * NXDOMAIN. Every state but WAITING is final for that server. */
enum { WAITING, CLEAN, FAILED, NO_NAME };

struct answers {
    struct tc_dns_addr v4[TC_DNS_MAX_ADDRS], v6[TC_DNS_MAX_ADDRS];
    size_t n4, n6;
    unsigned long ttl;
    int done[2];
};

/* What tc_dns_resolve does with one nameserver's answers (judge()). */
enum verdict {
    ASK_NEXT,           /* nothing usable yet: the next nameserver, then the kept answer */
    NO_ADDRESS,         /* the name has no address: fail this lookup, ask no one else */
    KEEP,               /* cache the answer and use it */
    USE_WITH_KEPT_IPV4, /* the next nameserver first; with no better answer, use it
                           once, then the kept answer's IPv4 addresses */
    USE_NOW             /* as USE_WITH_KEPT_IPV4, but at once: no one else is asked */
};

/* The verdict on one nameserver's answers once ask() is done with it, by
 * its A reply, whether either reply brought an address, and its AAAA reply:
 *
 *   A reply          addresses  AAAA reply       verdict
 *   NO_NAME          some       any              USE_NOW
 *   NO_NAME          none       any              NO_ADDRESS
 *   CLEAN            some       any              KEEP
 *   CLEAN            none       CLEAN            NO_ADDRESS
 *   CLEAN            none       not CLEAN        ASK_NEXT
 *   WAITING, FAILED  some       any              USE_WITH_KEPT_IPV4
 *   WAITING, FAILED  none       any              ASK_NEXT
 *
 * NXDOMAIN for A, or a clean empty reply to both (NODATA), fails the
 * lookup as libc's does: libc answers from every query that brought
 * records, so the AAAA records beside an NXDOMAIN for A are used (USE_NOW),
 * and with none the lookup fails without asking another nameserver of the
 * same network, which would say the same. The kept answer stays for a
 * later lookup that no nameserver answers: one wrong reply must not cost
 * the outage fallback. ask() waits for the AAAA reply after an NXDOMAIN
 * for A; if this server's time ends first, the lookup fails (NO_ADDRESS),
 * where libc would send that AAAA query to the next nameserver: a server
 * that answers A at once and loses AAAA is broken, and asking the others
 * for AAAA alone after a definite NXDOMAIN would take far more code.
 * NXDOMAIN for AAAA alone is only that type answered with nothing.
 * An answer is kept only with a clean A reply: an AAAA-only answer
 * from a server whose A reply failed or was lost would, kept, fail every
 * request until its TTL on a device whose IPv6 does not reach the server.
 * libc sends A and AAAA as separate queries and takes a failed or lost A
 * query to the next nameserver (and round) on its own, so such an answer
 * waits until every server had its turn; then it is used once with the IPv4
 * addresses from the last clean answer behind it. A clean A reply with no
 * address beside AAAA records is the name's whole answer, and kept. */
static enum verdict judge(const struct answers *a) {
    int found = a->n4 || a->n6;
    if (a->done[0] == NO_NAME) return found ? USE_NOW : NO_ADDRESS;
    if (a->done[0] == CLEAN) return found ? KEEP : a->done[1] == CLEAN ? NO_ADDRESS : ASK_NEXT;
    return found ? USE_WITH_KEPT_IPV4 : ASK_NEXT;
}

#ifdef TC_NATIVE_TEST
/* judge() by state and verdict names, for the table test in test_http.c. */
const char *tc_dns_judge(const char *a_reply, const char *aaaa_reply, size_t n4, size_t n6) {
    static const char *const states[] = {"waiting", "clean", "failed", "no_name"};
    static const char *const verdicts[] = {"ask_next", "no_address", "keep", "use_with_kept_ipv4", "use_now"};
    struct answers a;
    int i;
    memset(&a, 0, sizeof(a));
    a.done[0] = a.done[1] = -1;
    for (i = 0; i < 4; i++) {
        if (!strcmp(a_reply, states[i])) a.done[0] = i;
        if (!strcmp(aaaa_reply, states[i])) a.done[1] = i;
    }
    if (a.done[0] < 0 || a.done[1] < 0) return "bad state";
    a.n4 = n4;
    a.n6 = n6;
    return verdicts[judge(&a)];
}
#endif

/* A query id; guessable only until the first seed read works (entropy.h),
 * and connect() already drops replies from anyone but the nameserver. */
static unsigned query_id(void) {
    unsigned char bytes[2];
    if (!tc_random_bytes(bytes, sizeof(bytes))) return (unsigned)bytes[0] << 8 | bytes[1];
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

/* Appends addr to list (*n entries, at most max) unless it is there
 * already: a repeated record, or the same address reached by two names on a
 * CNAME chain, would give a dead address two attempts. */
static void add_addr(struct tc_dns_addr *list, size_t *n, size_t max, int family, const unsigned char *addr) {
    size_t i, len = family == AF_INET ? 4 : 16;
    for (i = 0; i < *n; i++)
        if (list[i].family == family && !memcmp(list[i].addr, addr, len)) return;
    if (*n >= max) return;
    list[*n].family = family;
    memcpy(list[*n].addr, addr, len);
    ++*n;
}

static int in_set(char set[][NAME_MAX_TEXT], size_t n, const char *name) {
    size_t i;
    for (i = 0; i < n; i++)
        if (!strcmp(set[i], name)) return 1;
    return 0;
}

/* One answer record: its owner (read into a caller's buffer) and fixed
 * fields. */
struct record {
    unsigned type, klass, rdlen;
    unsigned long ttl;
    size_t rdata;
};

/* Reads the record at *pos, its owner into owner (NAME_MAX_TEXT bytes), and
 * moves *pos past it. Returns -1 for one that runs past the reply. RFC 2181:
 * a TTL with the top bit set means zero. */
static int read_record(const unsigned char *msg, size_t len, size_t *pos, char *owner, struct record *r) {
    size_t p = *pos;
    if (read_name(msg, len, &p, owner, NAME_MAX_TEXT) || p + 10 > len) return -1;
    r->type = (unsigned)msg[p] << 8 | msg[p + 1];
    r->klass = (unsigned)msg[p + 2] << 8 | msg[p + 3];
    r->ttl = (unsigned long)msg[p + 4] << 24 | (unsigned long)msg[p + 5] << 16 |
             (unsigned long)msg[p + 6] << 8 | msg[p + 7];
    if (r->ttl > 0x7fffffffUL) r->ttl = 0;
    r->rdlen = (unsigned)msg[p + 8] << 8 | msg[p + 9];
    r->rdata = p + 10;
    if (r->rdata + r->rdlen > len) return -1;
    *pos = r->rdata + r->rdlen;
    return 0;
}

/* Parses one reply into part (cleared first); returns which query it
 * answers (0 A, 1 AAAA) or -1 for anything that is not a valid reply to one
 * of ours. The caller merges part only on success. */
static int parse_reply(const unsigned char *msg, size_t len, const unsigned ids[2], const char *qname,
                       struct answers *part) {
    char names[MAX_ALIASES][NAME_MAX_TEXT], owner[NAME_MAX_TEXT], target[NAME_MAX_TEXT];
    size_t pos = 12, count = 1, records, i, start;
    unsigned id, rcode, which, qtype, ancount;
    struct record r;
    int changed, pass;
    memset(part, 0, sizeof(*part));
    part->ttl = TTL_MAX;
    if (len < 12) return -1;
    id = (unsigned)msg[0] << 8 | msg[1];
    if (id != ids[0] && id != ids[1]) return -1;
    which = id == ids[0] ? 0 : 1;
    if (!(msg[2] & 0x80) || (msg[2] & 0x78)) return -1; /* a reply to a QUERY */
    if (msg[4] != 0 || msg[5] != 1) return -1;
    if (read_name(msg, len, &pos, owner, sizeof(owner)) || strcasecmp(owner, qname) || pos + 4 > len) return -1;
    qtype = (unsigned)msg[pos] << 8 | msg[pos + 1];
    if (qtype != (which ? TYPE_AAAA : TYPE_A) || msg[pos + 2] != 0 || msg[pos + 3] != CLASS_IN) return -1;
    pos += 4;
    rcode = msg[3] & 0x0f;
    if (rcode == 3) { part->done[which] = NO_NAME; return (int)which; }
    /* SERVFAIL, REFUSED and the like, or an answer too long for UDP: this
     * server has nothing usable of this type, and waiting will not bring
     * it, so the type counts as answered with no records. */
    if (rcode != 0 || (msg[2] & 0x02)) { part->done[which] = FAILED; return (int)which; }
    part->done[which] = CLEAN;
    ancount = (unsigned)msg[6] << 8 | msg[7];
    snprintf(names[0], sizeof(names[0]), "%s", qname);
    for (i = 0; names[0][i]; i++) names[0][i] = (char)tolower((unsigned char)names[0][i]);
    start = pos;
    /* CNAMEs first, until the chain stops growing, then the addresses of
     * every name on it. The answer is kept for the shortest TTL on the
     * chain: a CNAME that expires first can point elsewhere after it. */
    for (pass = 0, changed = 1; changed && pass < MAX_ALIASES; pass++) {
        changed = 0;
        for (pos = start, records = 0; records < ancount; records++) {
            if (read_record(msg, len, &pos, owner, &r)) return -1;
            if (r.type != TYPE_CNAME || r.klass != CLASS_IN || !in_set(names, count, owner)) continue;
            if (r.ttl < part->ttl) part->ttl = r.ttl;
            if (read_name(msg, len, &r.rdata, target, sizeof(target))) return -1;
            if (!in_set(names, count, target) && count < MAX_ALIASES) {
                snprintf(names[count++], sizeof(names[0]), "%s", target);
                changed = 1;
            }
        }
    }
    for (pos = start, records = 0; records < ancount; records++) {
        if (read_record(msg, len, &pos, owner, &r)) return -1;
        if (r.klass != CLASS_IN || !in_set(names, count, owner)) continue;
        if (r.type == TYPE_A && r.rdlen == 4)
            add_addr(part->v4, &part->n4, TC_DNS_MAX_ADDRS, AF_INET, msg + r.rdata);
        else if (r.type == TYPE_AAAA && r.rdlen == 16)
            add_addr(part->v6, &part->n6, TC_DNS_MAX_ADDRS, AF_INET6, msg + r.rdata);
        else
            continue;
        if (r.ttl < part->ttl) part->ttl = r.ttl;
    }
    return (int)which;
}

/* The first valid answer to each query counts; a repeat adds nothing. */
static void merge(struct answers *a, const struct answers *part, int which) {
    size_t i;
    if (a->done[which] != WAITING) return;
    a->done[which] = part->done[which];
    for (i = 0; i < part->n4; i++) add_addr(a->v4, &a->n4, TC_DNS_MAX_ADDRS, AF_INET, part->v4[i].addr);
    for (i = 0; i < part->n6; i++) add_addr(a->v6, &a->n6, TC_DNS_MAX_ADDRS, AF_INET6, part->v6[i].addr);
    if (part->ttl < a->ttl) a->ttl = part->ttl;
}

/* The interface index a nameserver's scope names (NULL: none), or 0. */
static unsigned scope_index(const char *scope) {
    static struct if_table table; /* static: it is large */
    char *end;
    unsigned long index;
    if (!scope || !*scope) return 0;
    index = strtoul(scope, &end, 10);
    if (!*end) return index <= UINT_MAX ? (unsigned)index : 0;
    return iflist_collect(&table) == 0 ? iflist_index_of(&table, scope) : 0;
}

/* Up to TC_DNS_MAX_SERVERS nameservers from resolv.conf; 127.0.0.1 when it
 * is missing or names none, as libc does. Any other failure to open it
 * gives none, and sets *resource when the device is short of descriptors or
 * memory: asking 127.0.0.1 then would report the wrong failure. A
 * link-local server needs its scope, fe80::1%bridge0 or fe80::1%3, as libc
 * reads it; one without (or naming no interface) is skipped, since it
 * cannot be reached. Apple's dns-update-script writes the server learned
 * from router adverts as a comment, so these come only from a hand-edited
 * file. */
static size_t nameservers(struct sockaddr_storage *out, int *resource) {
    char line[256], text[64];
    size_t n = 0;
    FILE *f = fopen(TC_RESOLV_CONF, "r");
    if (!f && errno != ENOENT) {
        if (tc_errno_class(TC_OP_OPEN, errno) == TC_ERR_RESOURCE) *resource = 1;
        return 0;
    }
    while (f && n < TC_DNS_MAX_SERVERS && fgets(line, sizeof(line), f)) {
        struct sockaddr_in *v4 = (struct sockaddr_in *)&out[n];
        struct sockaddr_in6 *v6 = (struct sockaddr_in6 *)&out[n];
        char *scope;
        if (sscanf(line, "nameserver %63s", text) != 1) continue;
        if ((scope = strchr(text, '%'))) *scope++ = 0;
        memset(&out[n], 0, sizeof(out[n]));
        if (inet_pton(AF_INET, text, &v4->sin_addr) == 1) {
            v4->sin_family = AF_INET;
            v4->sin_port = htons(TC_DNS_PORT);
            n++;
        } else if (inet_pton(AF_INET6, text, &v6->sin6_addr) == 1 &&
                   (!IN6_IS_ADDR_LINKLOCAL(&v6->sin6_addr) || (v6->sin6_scope_id = scope_index(scope)) != 0)) {
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

/* Asks one server the queries a still waits for (both on the first round,
 * on the next only those left unanswered, under the same ids, as libc
 * resends a query) and collects its replies into a until each is answered,
 * until passes, or TERM.
 * *fd is the server's socket for the whole lookup, made on its first round
 * and kept, as libc keeps one per nameserver across its tries, so a reply
 * to the first round's query that comes late is still read on the next. A
 * socket that cannot be made or used is closed (*fd -1, made again next
 * round) and leaves the queries waiting, which judge() takes as nothing
 * usable. */
static void ask(const struct sockaddr_storage *server, int *fd_kept, const char *name, struct answers *a,
                const unsigned ids[2], long long until, int *resource) {
    static const unsigned types[2] = {TYPE_A, TYPE_AAAA};
    unsigned char query[300], reply[1500];
    size_t length, i;
    socklen_t salen = server->ss_family == AF_INET ? sizeof(struct sockaddr_in) : sizeof(struct sockaddr_in6);
    int fd = *fd_kept;
    if (fd >= 0) goto ready;
    fd = socket(server->ss_family, SOCK_DGRAM, 0);
    if (fd < 0) {
        if (tc_errno_record(TC_OP_SOCKET, errno) == TC_ERR_RESOURCE) *resource = 1;
        return;
    }
    /* connect() makes the kernel drop datagrams from anyone else. */
    if (fd >= FD_SETSIZE) goto drop;
    if (tc_test_enobufs("connect") || connect(fd, (const struct sockaddr *)server, salen)) goto failed;
    *fd_kept = fd;
ready:
    for (i = 0; i < 2; i++) {
        if (a->done[i] != WAITING) continue;
        length = build_query(query, sizeof(query), ids[i], name, types[i]);
        if (!length) return;
        if (tc_test_enobufs("send") || send(fd, query, length, 0) != (ssize_t)length) goto failed;
    }
    while ((a->done[0] == WAITING || a->done[1] == WAITING) && !telemetry_stop) {
        fd_set reads;
        struct timeval timeout;
        long long left = until - acp_monotonic_ms();
        struct answers part;
        int ready, which;
        ssize_t n;
        if (left <= 0) break;
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
        else if (n < 0 && errno != EINTR && errno != EAGAIN) goto failed; /* ICMP refused, or no buffers */
    }
    return;
failed:
    /* Out of buffers or memory is the device's, for the retry rules. */
    if (tc_errno_record(TC_OP_IO, errno) == TC_ERR_RESOURCE) *resource = 1;
drop:
    close(fd);
    *fd_kept = -1;
}

/* One server's time on round (from 0) of a list of n (dns.h). */
static long long server_ms(unsigned round, size_t n) {
    long long ms = (long long)TC_DNS_RETRANS_MS << round;
    if (round) ms /= (long long)n;
    return ms < TC_DNS_MIN_SERVER_MS ? TC_DNS_MIN_SERVER_MS : ms;
}

static size_t interleave(const struct answers *a, struct tc_dns_addr *out, size_t max) {
    size_t n = 0, i;
    for (i = 0; (i < a->n6 || i < a->n4) && n < max; i++) {
        if (i < a->n6 && n < max) out[n++] = a->v6[i];
        if (i < a->n4 && n < max) out[n++] = a->v4[i];
    }
    return n;
}

/* Whether the kept answer is for name and the clock is short of until
 * (cache.fresh_until or cache.stale_until). */
static int kept(const char *name, long long until) {
    return cache.count && !strcasecmp(cache.name, name) && acp_monotonic_ms() < until;
}

/* An answer with no clean A reply (judge's USE_WITH_KEPT_IPV4, USE_NOW): its own
 * addresses, then the IPv4 addresses from the last clean answer while it is
 * within its stale window, so the request moves on to them when IPv6
 * fails. Not kept. */
static int use_with_kept_ipv4(const char *name, const struct answers *a, struct tc_dns_addr *out, size_t max,
                              size_t *count) {
    size_t k;
    *count = interleave(a, out, max);
    if (kept(name, cache.stale_until))
        for (k = 0; k < cache.count; k++)
            if (cache.addrs[k].family == AF_INET) add_addr(out, count, max, AF_INET, cache.addrs[k].addr);
    return 0;
}

/* Keeps an answer with a clean A reply (judge's KEEP) for its TTL. */
static void keep_answer(const char *name, const struct answers *a) {
    long long now;
    snprintf(cache.name, sizeof(cache.name), "%s", name);
    cache.count = interleave(a, cache.addrs, TC_DNS_MAX_ADDRS);
    now = acp_monotonic_ms();
    cache.fresh_until = now + (long long)(a->ttl < TTL_MIN ? TTL_MIN : a->ttl) * 1000;
    cache.stale_until = now + STALE_MS;
}

int tc_dns_resolve(const char *name, struct tc_dns_addr *out, size_t max, size_t *count, long long deadline_ms,
                   int *resource) {
    struct sockaddr_storage servers[TC_DNS_MAX_SERVERS];
    struct answers a[TC_DNS_MAX_SERVERS]; /* each server's replies, kept across rounds */
    unsigned ids[TC_DNS_MAX_SERVERS][2];  /* each server's query ids (A, AAAA), the same every round */
    int fds[TC_DNS_MAX_SERVERS];          /* each server's socket, kept across rounds (ask()) */
    size_t n, i;
    int result; /* -1 failed, 0 out is filled, 1 the kept answer serves */
    unsigned round;
    long long now = acp_monotonic_ms();
    int short_of = 0;
    *count = 0;
    *resource = 0;
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
    if (kept(name, cache.fresh_until)) goto cached;
    n = nameservers(servers, &short_of);
    for (i = 0; i < n; i++) {
        memset(&a[i], 0, sizeof(a[i]));
        a[i].ttl = TTL_MAX;
        ids[i][0] = query_id();
        ids[i][1] = (ids[i][0] + 1) & 0xffff;
        fds[i] = -1;
    }
    for (round = 0; round < TC_DNS_TRIES; round++) {
        for (i = 0; i < n; i++) {
            enum verdict verdict;
            long long until;
            now = acp_monotonic_ms();
            if (telemetry_stop || now >= deadline_ms) goto unanswered;
            if (a[i].done[0] != WAITING && a[i].done[1] != WAITING) continue; /* it said all it will */
            until = now + server_ms(round, n);
            ask(&servers[i], &fds[i], name, &a[i], ids[i], until < deadline_ms ? until : deadline_ms, &short_of);
            verdict = judge(&a[i]);
            /* Half an answer goes on to the next server, and its lost A
             * query to the next round, before it is used (unanswered:). */
            if (verdict == ASK_NEXT || verdict == USE_WITH_KEPT_IPV4) continue;
            if (verdict == NO_ADDRESS) {
                result = -1;
                goto close_all;
            }
            if (verdict == USE_NOW) {
                result = use_with_kept_ipv4(name, &a[i], out, max, count);
                goto close_all;
            }
            keep_answer(name, &a[i]);
            result = 1;
            goto close_all;
        }
    }
unanswered:
    result = -1;
    /* Every server had its turn, or time ran out (or TERM), with half an
     * answer: use the first. */
    for (i = 0; i < n; i++)
        if (judge(&a[i]) == USE_WITH_KEPT_IPV4) {
            result = use_with_kept_ipv4(name, &a[i], out, max, count);
            goto close_all;
        }
    /* No answer now: the last one, if it is not too old. */
    if (kept(name, cache.stale_until)) result = 1;
    else *resource = short_of;
close_all:
    for (i = 0; i < n; i++)
        if (fds[i] >= 0) close(fds[i]);
    if (result != 1) return result;
cached:
    for (i = 0; i < cache.count && i < max; i++) out[i] = cache.addrs[i];
    *count = i;
    return 0;
}
