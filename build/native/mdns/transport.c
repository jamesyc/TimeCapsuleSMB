#include "mdns.h"
TC_LOCAL unsigned int random_multicast_response_delay_ms(void);
TC_LOCAL int join_mdns_multicast_group(int sockfd, uint32_t ipv4_addr, const char *socket_role);
TC_LOCAL void configure_unicast_response_hop_limit4(int sockfd);
TC_LOCAL void configure_unicast_response_hop_limit6(int sockfd);
TC_LOCAL int configure_multicast_socket_options(int sockfd);
TC_LOCAL int configure_outbound_multicast_socket(int sockfd, uint32_t ipv4_addr, const char *socket_role);
TC_LOCAL int open_bound_mdns_socket(int shared_bind, int log_bind_errors);
TC_LOCAL void drop_mdns_multicast_group_best_effort(int sockfd, uint32_t ipv4_addr, const char *socket_role);
TC_LOCAL int link_has_any_mdns_transport(const struct link_context *link);
TC_LOCAL void compact_link_contexts_for_mdns_transport(struct link_context_set *set);
TC_LOCAL int link_ipv4_source_score(uint32_t ipv4_addr);
TC_LOCAL void init_mdns_membership_delta(struct mdns_membership_delta *delta);
TC_LOCAL int record_mdns_membership_ipv4(struct mdns_membership_delta *delta, uint32_t ipv4_addr);
TC_LOCAL int record_mdns_membership_ipv6(struct mdns_membership_delta *delta, unsigned int ifindex, const char *ifname);
TC_LOCAL void rollback_mdns_membership_delta(struct mdns_socket_pair *sockets,
                                           const struct mdns_membership_delta *delta);
TC_LOCAL int open_bound_mdns_socket6(int shared_bind, int log_bind_errors);
TC_LOCAL int join_mdns_multicast_group6(int sockfd, unsigned int ifindex, const char *ifname, const char *socket_role);
TC_LOCAL void drop_mdns_multicast_group6_best_effort(int sockfd, unsigned int ifindex, const char *ifname, const char *socket_role);
TC_LOCAL int join_mdns_multicast_group_for_link4(int sockfd,
                                               struct link_context *link,
                                               const struct link_context_set *old_links,
                                               const char *socket_role,
                                               struct mdns_membership_delta *delta);
TC_LOCAL int configure_mdns_socket6_for_links(int sockfd, struct link_context_set *set, const char *socket_role);
TC_LOCAL int configure_mdns_socket4_for_links(int sockfd, struct link_context_set *set, const char *socket_role);
TC_LOCAL int link_set_has_ipv4_membership(const struct link_context_set *set, uint32_t ipv4_addr);
TC_LOCAL int link_set_has_ipv6_membership(const struct link_context_set *set, unsigned int ifindex);
TC_LOCAL int prepare_mdns_socket4_memberships(int sockfd,
                                            const struct link_context_set *old_links,
                                            struct link_context_set *new_links,
                                            const char *socket_role,
                                            struct mdns_membership_delta *delta);
TC_LOCAL int prepare_mdns_socket6_memberships(int sockfd,
                                            const struct link_context_set *old_links,
                                            struct link_context_set *new_links,
                                            const char *socket_role,
                                            struct mdns_membership_delta *delta);
TC_LOCAL int open_dualstack_mdns_sockets(int shared_bind,
                                       struct link_context_set *links,
                                       int log_bind_errors,
                                       struct mdns_socket_pair *out);
TC_LOCAL int open_dualstack_mdns_sockets_for_desired(int shared_bind,
                                                   const struct link_context_set *desired_links,
                                                   struct link_context_set *active_links,
                                                   int log_bind_errors,
                                                   struct mdns_socket_pair *out,
                                                   struct mdns_transport_status *status);
void scoped_mdns_dest6_for_link(struct sockaddr_in6 *out,
                                       const struct sockaddr_in6 *base,
                                       const struct link_context *link) {
    *out = *base;
    if (link != NULL) {
        out->sin6_scope_id = link->ifindex;
    }
}

unsigned int ipv6_sockaddr_effective_ifindex(const struct sockaddr_in6 *addr) {
    if (addr == NULL) {
        return 0;
    }
    if (addr->sin6_scope_id != 0) {
        return (unsigned int)addr->sin6_scope_id;
    }
    if (ipv6_is_link_local_addr(&addr->sin6_addr)) {
        return ((unsigned int)addr->sin6_addr.s6_addr[2] << 8) |
               (unsigned int)addr->sin6_addr.s6_addr[3];
    }
    return 0;
}

int mdnsresponder_is_alive(void) {
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

void sleep_millis(unsigned int delay_ms) {
    if (delay_ms == 0) {
        return;
    }
    (void)usleep((useconds_t)delay_ms * 1000U);
}

TC_LOCAL unsigned int random_multicast_response_delay_ms(void) {
    static int seeded = 0;
    unsigned int span;

    if (!seeded) {
        srand((unsigned int)(time(NULL) ^ (time_t)getpid()));
        seeded = 1;
    }
    span = (MDNS_MULTICAST_RESPONSE_DELAY_MAX_MS - MDNS_MULTICAST_RESPONSE_DELAY_MIN_MS) + 1U;
    return MDNS_MULTICAST_RESPONSE_DELAY_MIN_MS + (unsigned int)(rand() % (int)span);
}

void delay_multicast_query_response(void) {
    sleep_millis(random_multicast_response_delay_ms());
}

long long monotonic_millis(void) {
    struct timeval tv;

    gettimeofday(&tv, NULL);
    return ((long long)tv.tv_sec * 1000LL) + ((long long)tv.tv_usec / 1000LL);
}

void kill_mdnsresponder(int sig) {
    if (sig == SIGKILL) {
        (void)system("/usr/bin/pkill -9 '^mDNSResponder$' >/dev/null 2>&1 || true");
    } else {
        (void)system("/usr/bin/pkill '^mDNSResponder$' >/dev/null 2>&1 || true");
    }
}

TC_LOCAL int join_mdns_multicast_group(int sockfd, uint32_t ipv4_addr, const char *socket_role) {
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

int set_outbound_multicast_interface(int sockfd, uint32_t ipv4_addr, const char *socket_role,
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

TC_LOCAL void configure_unicast_response_hop_limit4(int sockfd) {
#ifdef IP_TTL
    int ttl = 255;
    if (setsockopt(sockfd, IPPROTO_IP, IP_TTL, &ttl, sizeof(ttl)) < 0) {
        fprintf(stderr, "warning: mdns socket: IP_TTL=255 failed: %s\n", strerror(errno));
    }
#else
    (void)sockfd;
#endif
}

TC_LOCAL void configure_unicast_response_hop_limit6(int sockfd) {
#ifdef IPV6_UNICAST_HOPS
    int hops = 255;
    if (setsockopt(sockfd, IPPROTO_IPV6, IPV6_UNICAST_HOPS, &hops, sizeof(hops)) < 0) {
        fprintf(stderr, "warning: mdns socket: IPV6_UNICAST_HOPS=255 failed: %s\n", strerror(errno));
    }
#else
    (void)sockfd;
#endif
}

TC_LOCAL int configure_multicast_socket_options(int sockfd) {
    int yes;

    yes = 255;
    (void)setsockopt(sockfd, IPPROTO_IP, IP_MULTICAST_TTL, &yes, sizeof(yes));
    yes = 1;
    (void)setsockopt(sockfd, IPPROTO_IP, IP_MULTICAST_LOOP, &yes, sizeof(yes));
    configure_unicast_response_hop_limit4(sockfd);
    return 0;
}

TC_LOCAL int configure_outbound_multicast_socket(int sockfd, uint32_t ipv4_addr, const char *socket_role) {
    if (set_outbound_multicast_interface(sockfd, ipv4_addr, socket_role, 1, 1) != 0) {
        return -1;
    }
    return configure_multicast_socket_options(sockfd);
}

TC_LOCAL int open_bound_mdns_socket(int shared_bind, int log_bind_errors) {
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

TC_LOCAL void drop_mdns_multicast_group_best_effort(int sockfd, uint32_t ipv4_addr, const char *socket_role) {
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

TC_LOCAL int link_has_any_mdns_transport(const struct link_context *link) {
    return link_context_has_mdns_ipv4_transport(link) ||
           link_context_has_mdns_ipv6_transport(link);
}

TC_LOCAL void compact_link_contexts_for_mdns_transport(struct link_context_set *set) {
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

TC_LOCAL int link_ipv4_source_score(uint32_t ipv4_addr) {
    if (ipv4_is_rfc1918(ipv4_addr)) {
        return 0;
    }
    if (!ipv4_is_link_local(ipv4_addr)) {
        return 100;
    }
    return 200;
}

uint32_t link_preferred_ipv4_source(const struct link_context *link) {
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

uint32_t link_ipv4_source_for_peer(const struct link_context *link, uint32_t source_ipv4_addr) {
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

int link_contexts_need_ipv4_socket(const struct link_context_set *set) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (link_context_has_mdns_ipv4_transport(&set->links[i])) {
            return 1;
        }
    }
    return 0;
}

int link_contexts_need_ipv6_socket(const struct link_context_set *set) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (link_context_has_mdns_ipv6_transport(&set->links[i])) {
            return 1;
        }
    }
    return 0;
}

void close_mdns_socket_pair(struct mdns_socket_pair *sockets) {
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

TC_LOCAL void init_mdns_membership_delta(struct mdns_membership_delta *delta) {
    memset(delta, 0, sizeof(*delta));
}

TC_LOCAL int record_mdns_membership_ipv4(struct mdns_membership_delta *delta, uint32_t ipv4_addr) {
    if (delta == NULL) {
        return 0;
    }
    if (delta->ipv4_count >= MAX_IFACE_CONTEXTS) {
        return -1;
    }
    delta->ipv4[delta->ipv4_count++] = ipv4_addr;
    return 0;
}

TC_LOCAL int record_mdns_membership_ipv6(struct mdns_membership_delta *delta, unsigned int ifindex, const char *ifname) {
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

TC_LOCAL void rollback_mdns_membership_delta(struct mdns_socket_pair *sockets,
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

TC_LOCAL int open_bound_mdns_socket6(int shared_bind, int log_bind_errors) {
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
#if defined(IPV6_RECVPKTINFO) && defined(IPV6_PKTINFO)
    if (setsockopt(sockfd, IPPROTO_IPV6, IPV6_RECVPKTINFO, &yes, sizeof(yes)) < 0 && log_bind_errors) {
        perror("setsockopt(IPV6_RECVPKTINFO)");
    }
#endif
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

ssize_t receive_ipv6_packet(int sockfd,
                                   uint8_t *packet,
                                   size_t packet_len,
                                   struct sockaddr_in6 *source,
                                   socklen_t *source_len,
                                   unsigned int *received_ifindex) {
#if defined(IPV6_RECVPKTINFO) && defined(IPV6_PKTINFO)
    struct msghdr message;
    struct iovec iov;
    union {
        struct cmsghdr align;
        unsigned char bytes[CMSG_SPACE(sizeof(struct in6_pktinfo))];
    } control;
    struct cmsghdr *cmsg;
    ssize_t nread;

    memset(&message, 0, sizeof(message));
    memset(&control, 0, sizeof(control));
    iov.iov_base = packet;
    iov.iov_len = packet_len;
    message.msg_name = source;
    message.msg_namelen = *source_len;
    message.msg_iov = &iov;
    message.msg_iovlen = 1;
    message.msg_control = control.bytes;
    message.msg_controllen = sizeof(control.bytes);
    nread = recvmsg(sockfd, &message, 0);
    *source_len = message.msg_namelen;
    if (nread <= 0) {
        return nread;
    }
    for (cmsg = CMSG_FIRSTHDR(&message); cmsg != NULL; cmsg = CMSG_NXTHDR(&message, cmsg)) {
        if (cmsg->cmsg_level == IPPROTO_IPV6 && cmsg->cmsg_type == IPV6_PKTINFO &&
            cmsg->cmsg_len >= CMSG_LEN(sizeof(struct in6_pktinfo))) {
            const struct in6_pktinfo *pktinfo = (const struct in6_pktinfo *)(const void *)CMSG_DATA(cmsg);
            *received_ifindex = pktinfo->ipi6_ifindex;
            break;
        }
    }
    return nread;
#else
    (void)received_ifindex;
    return recvfrom(sockfd, packet, packet_len, 0, (struct sockaddr *)source, source_len);
#endif
}

TC_LOCAL int join_mdns_multicast_group6(int sockfd, unsigned int ifindex, const char *ifname, const char *socket_role) {
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

TC_LOCAL void drop_mdns_multicast_group6_best_effort(int sockfd, unsigned int ifindex, const char *ifname, const char *socket_role) {
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

int set_outbound_multicast_interface6(int sockfd, unsigned int ifindex, const char *socket_role,
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

TC_LOCAL int join_mdns_multicast_group_for_link4(int sockfd,
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

TC_LOCAL int configure_mdns_socket6_for_links(int sockfd, struct link_context_set *set, const char *socket_role) {
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

TC_LOCAL int configure_mdns_socket4_for_links(int sockfd, struct link_context_set *set, const char *socket_role) {
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

TC_LOCAL int link_set_has_ipv4_membership(const struct link_context_set *set, uint32_t ipv4_addr) {
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

TC_LOCAL int link_set_has_ipv6_membership(const struct link_context_set *set, unsigned int ifindex) {
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

TC_LOCAL int prepare_mdns_socket4_memberships(int sockfd,
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

TC_LOCAL int prepare_mdns_socket6_memberships(int sockfd,
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

TC_LOCAL int open_dualstack_mdns_sockets(int shared_bind,
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

TC_LOCAL int open_dualstack_mdns_sockets_for_desired(int shared_bind,
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

int acquire_dualstack_mdns_sockets(int shared_bind,
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

int prepare_runtime_mdns_sockets_for_links(int shared_bind,
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

void retire_runtime_mdns_memberships_for_missing(struct mdns_socket_pair *sockets,
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

void close_unused_runtime_mdns_socket_families(struct mdns_socket_pair *sockets,
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


int g_last_ipv4_socket_errno = 0;

int g_last_ipv6_socket_errno = 0;
