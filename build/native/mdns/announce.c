#include "mdns.h"
TC_LOCAL void format_dest_addr(const struct sockaddr_in *dest, char *buf, size_t buf_size);
TC_LOCAL void log_packet_send_failure_detail_any(const char *stage, const struct sockaddr *dest, size_t packet_len,
                                               int answers, int saved_errno);
TC_LOCAL void init_announcement_packet(size_t *off, int *answers);
TC_LOCAL int finalize_and_send_announcement_packet_any(int sockfd,
                                                     uint8_t *buf,
                                                     size_t off,
                                                     int answers,
                                                     const struct sockaddr *dest,
                                                     socklen_t dest_len);
TC_LOCAL int append_generated_records_with_flush(int sockfd,
                                               uint8_t *buf,
                                               size_t *off,
                                               size_t cap,
                                               int *answers,
                                               const struct sockaddr *dest,
                                               socklen_t dest_len,
                                               const struct config *cfg,
                                               uint32_t ttl,
                                               generated_record_adder add_records,
                                               const char *failure_stage);
TC_LOCAL int append_generated_apple_records(int sockfd,
                                          uint8_t *buf,
                                          size_t *off,
                                          size_t cap,
                                          int *answers,
                                          const struct sockaddr *dest,
                                          socklen_t dest_len,
                                          const struct config *cfg,
                                          uint32_t ttl);
TC_LOCAL int append_generated_base_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg,
                                         const struct link_context *response_link,
                                         int include_a, int include_aaaa,
                                         uint32_t ttl, int *answers);
TC_LOCAL int send_announcement_any_scoped(int sockfd,
                                        const struct sockaddr *dest,
                                        socklen_t dest_len,
                                        const struct config *cfg,
                                        const struct link_context *response_link,
                                        uint32_t ttl,
                                        enum mdns_service_scope scope);
TC_LOCAL int send_announcement_any(int sockfd,
                                 const struct sockaddr *dest,
                                 socklen_t dest_len,
                                 const struct config *cfg,
                                 const struct link_context *response_link,
                                 uint32_t ttl);
TC_LOCAL int TC_UNUSED send_announcement(int sockfd, const struct sockaddr_in *dest, const struct config *cfg,
                                       const struct link_context *response_link, uint32_t ttl);
TC_LOCAL int set_link_outbound_interface4(int sockfd, const struct link_context *link);
TC_LOCAL void send_link_announcement_pair(const struct mdns_socket_pair *sockets,
                                        const struct link_context *link,
                                        const struct sockaddr_in *dest4,
                                        const struct sockaddr_in6 *dest6,
                                        const struct config *cfg,
                                        uint32_t ttl,
                                        enum mdns_service_scope scope,
                                        const char *stage);
TC_LOCAL void format_dest_addr(const struct sockaddr_in *dest, char *buf, size_t buf_size) {
    char ipbuf[INET_ADDRSTRLEN];

    snprintf(buf, buf_size, "%s:%u",
             ipv4_to_string(dest->sin_addr.s_addr, ipbuf, sizeof(ipbuf)),
             (unsigned int)ntohs(dest->sin_port));
}

void format_sockaddr_addr(const struct sockaddr *dest, char *buf, size_t buf_size) {
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

void log_packet_build_failure(const char *stage, const char *step, size_t packet_len, int answers) {
    fprintf(stderr,
            "mdns packet build failure: stage=%s step=%s packet_len=%lu answers=%d\n",
            stage,
            step,
            (unsigned long)packet_len,
            answers);
}

TC_LOCAL void log_packet_send_failure_detail_any(const char *stage, const struct sockaddr *dest, size_t packet_len,
                                               int answers, int saved_errno) {
    char destbuf[96];

    remember_last_send_failure(stage, saved_errno);
    format_sockaddr_addr(dest, destbuf, sizeof(destbuf));
    fprintf(stderr,
            "mdns packet send failure: stage=%s dest=%s packet_len=%lu answers=%d errno=%d (%s)\n",
            stage,
            destbuf,
            (unsigned long)packet_len,
            answers,
            saved_errno,
            strerror(saved_errno));
    log_mdns_counters_force("send_failure");
}

int send_dns_packet_any(const char *stage, int sockfd, const uint8_t *buf, size_t packet_len,
                               const struct sockaddr *dest, socklen_t dest_len,
                               int answers) {
    static int logged_success_announcement = 0;
    static int logged_success_reply = 0;

    ssize_t sent;
    int saved_errno;

    errno = 0;
    sent = sendto_retry(sockfd, buf, packet_len, 0, dest, dest_len);
    saved_errno = errno;
    if (sent < 0) {
        errno = saved_errno;
        log_packet_send_failure_detail_any(stage, dest, packet_len, answers, saved_errno);
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
                "mdns packet send success: stage=%s dest=%s packet_len=%lu answers=%d\n",
                stage, destbuf, (unsigned long)packet_len, answers);
        logged_success_announcement = 1;
    }

    return 0;
}

TC_LOCAL void init_announcement_packet(size_t *off, int *answers) {
    *off = sizeof(struct dns_header);
    *answers = 0;
}

TC_LOCAL int finalize_and_send_announcement_packet_any(int sockfd,
                                                     uint8_t *buf,
                                                     size_t off,
                                                     int answers,
                                                     const struct sockaddr *dest,
                                                     socklen_t dest_len) {
    struct dns_header hdr;

    if (answers <= 0) {
        return 0;
    }

    memset(&hdr, 0, sizeof(hdr));
    hdr.flags = htons(DNS_FLAG_QR | DNS_FLAG_AA);
    hdr.ancount = htons((uint16_t)answers);
    memcpy(buf, &hdr, sizeof(hdr));
    return send_dns_packet_any("announcement", sockfd, buf, off, dest, dest_len, answers);
}



TC_LOCAL int append_generated_records_with_flush(int sockfd,
                                               uint8_t *buf,
                                               size_t *off,
                                               size_t cap,
                                               int *answers,
                                               const struct sockaddr *dest,
                                               socklen_t dest_len,
                                               const struct config *cfg,
                                               uint32_t ttl,
                                               generated_record_adder add_records,
                                               const char *failure_stage) {
    size_t before_off = *off;
    int before_answers = *answers;

    if (add_records(buf, off, cap, cfg, ttl, answers) == 0) {
        return 0;
    }

    *off = before_off;
    *answers = before_answers;
    if (finalize_and_send_announcement_packet_any(sockfd, buf, *off, *answers, dest, dest_len) != 0) {
        return -1;
    }
    init_announcement_packet(off, answers);
    if (add_records(buf, off, cap, cfg, ttl, answers) != 0) {
        log_packet_build_failure("announcement", failure_stage, *off, *answers);
        return -1;
    }
    return 0;
}

TC_LOCAL int append_generated_apple_records(int sockfd,
                                          uint8_t *buf,
                                          size_t *off,
                                          size_t cap,
                                          int *answers,
                                          const struct sockaddr *dest,
                                          socklen_t dest_len,
                                          const struct config *cfg,
                                          uint32_t ttl) {
    if (append_generated_records_with_flush(sockfd,
                                            buf,
                                            off,
                                            cap,
                                            answers,
                                            dest,
                                            dest_len,
                                            cfg,
                                            ttl,
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
                                               add_airport_records,
                                               "add_airport_records");
}

TC_LOCAL int append_generated_base_records(uint8_t *buf, size_t *off, size_t cap, const struct config *cfg,
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

TC_LOCAL int send_announcement_any_scoped(int sockfd,
                                        const struct sockaddr *dest,
                                        socklen_t dest_len,
                                        const struct config *cfg,
                                        const struct link_context *response_link,
                                        uint32_t ttl,
                                        enum mdns_service_scope scope) {
    uint8_t buf[BUF_SIZE];
    size_t off;
    int answers;
    struct config scoped_cfg;

    cfg = mdns_config_for_scope(&scoped_cfg, cfg, scope);
    init_announcement_packet(&off, &answers);
    if (append_generated_base_records(buf, &off, sizeof(buf), cfg, response_link, 1, 1, ttl, &answers) != 0) {
        log_packet_build_failure("announcement", "add_core_records", off, answers);
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
                                       ttl) != 0) {
        return -1;
    }
    return finalize_and_send_announcement_packet_any(sockfd, buf, off, answers, dest, dest_len);
}

TC_LOCAL int send_announcement_any(int sockfd,
                                 const struct sockaddr *dest,
                                 socklen_t dest_len,
                                 const struct config *cfg,
                                 const struct link_context *response_link,
                                 uint32_t ttl) {
    return send_announcement_any_scoped(sockfd,
                                        dest,
                                        dest_len,
                                        cfg,
                                        response_link,
                                        ttl,
                                        MDNS_SERVICE_SCOPE_LAN);
}

TC_LOCAL int TC_UNUSED send_announcement(int sockfd, const struct sockaddr_in *dest, const struct config *cfg,
                                       const struct link_context *response_link, uint32_t ttl) {
    return send_announcement_any(sockfd,
                                 (const struct sockaddr *)dest,
                                 sizeof(*dest),
                                 cfg,
                                 response_link,
                                 ttl);
}

int source_matches_link_ipv4_subnet(uint32_t source_ipv4_addr, const struct link_context *link) {
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

const struct link_context *select_response_link_ipv4(const struct link_context_set *links,
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

const struct link_context *select_response_link_ipv6(const struct link_context_set *links,
                                                            const struct sockaddr_in6 *source,
                                                            unsigned int ingress_ifindex) {
    size_t i;

    if (links->count == 0) {
        return NULL;
    }
    if (source != NULL) {
        unsigned int source_ifindex = ingress_ifindex != 0
                                          ? ingress_ifindex
                                          : ipv6_sockaddr_effective_ifindex(source);
        if (source_ifindex != 0) {
            for (i = 0; i < links->count; i++) {
                if (link_context_has_mdns_ipv6_transport(&links->links[i]) &&
                    links->links[i].ifindex == source_ifindex) {
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

TC_LOCAL int set_link_outbound_interface4(int sockfd, const struct link_context *link) {
    uint32_t ipv4_addr = link_preferred_ipv4_source(link);

    if (ipv4_addr == 0) {
        errno = EADDRNOTAVAIL;
        return -1;
    }
    return set_outbound_multicast_interface(sockfd, ipv4_addr, "runtime", 0, 0);
}

int set_link_outbound_interface4_for_peer(int sockfd, const struct link_context *link, uint32_t source_ipv4_addr) {
    uint32_t ipv4_addr = link_ipv4_source_for_peer(link, source_ipv4_addr);

    if (ipv4_addr == 0) {
        errno = EADDRNOTAVAIL;
        return -1;
    }
    return set_outbound_multicast_interface(sockfd, ipv4_addr, "runtime", 0, 0);
}

int set_link_outbound_interface6(int sockfd, const struct link_context *link) {
    return set_outbound_multicast_interface6(sockfd, link->ifindex, "runtime", 0, 0);
}

TC_LOCAL void send_link_announcement_pair(const struct mdns_socket_pair *sockets,
                                        const struct link_context *link,
                                        const struct sockaddr_in *dest4,
                                        const struct sockaddr_in6 *dest6,
                                        const struct config *cfg,
                                        uint32_t ttl,
                                        enum mdns_service_scope scope,
                                        const char *stage) {
    if (sockets->ipv4_fd >= 0 && link_context_has_mdns_ipv4_transport(link)) {
        char sourcebuf[INET_ADDRSTRLEN];
        uint32_t source_ipv4 = link_preferred_ipv4_source(link);
        fprintf(stderr,
                "mdns announce: stage=%s family=ipv4 iface=%s scope=%s source=%s\n",
                stage,
                link->name,
                mdns_service_scope_name(scope),
                source_ipv4 != 0 ? ipv4_to_string(source_ipv4, sourcebuf, sizeof(sourcebuf)) : "unknown");
        if (set_link_outbound_interface4(sockets->ipv4_fd, link) != 0 ||
            send_announcement_any_scoped(sockets->ipv4_fd,
                                         (const struct sockaddr *)dest4,
                                         sizeof(*dest4),
                                         cfg,
                                         link,
                                         ttl,
                                         scope) != 0) {
            char detail[160];
            snprintf(detail, sizeof(detail), "stage=%s iface=%s family=ipv4", stage, link->name);
            log_send_failure(stage, dest4, detail);
        }
    }
    if (sockets->ipv6_fd >= 0 && link_context_has_mdns_ipv6_transport(link)) {
        struct sockaddr_in6 scoped_dest6;
        fprintf(stderr,
                "mdns announce: stage=%s family=ipv6 iface=%s scope=%s source_ifindex=%u\n",
                stage,
                link->name,
                mdns_service_scope_name(scope),
                link->ifindex);
        scoped_mdns_dest6_for_link(&scoped_dest6, dest6, link);
        if (set_link_outbound_interface6(sockets->ipv6_fd, link) != 0 ||
            send_announcement_any_scoped(sockets->ipv6_fd,
                                         (const struct sockaddr *)&scoped_dest6,
                                         sizeof(scoped_dest6),
                                         cfg,
                                         link,
                                         ttl,
                                         scope) != 0) {
            char destbuf[96];
            format_sockaddr_addr((const struct sockaddr *)&scoped_dest6, destbuf, sizeof(destbuf));
            fprintf(stderr,
                    "mdns send failure: stage=%s dest=%s detail=iface=%s family=ipv6\n",
                    stage,
                    destbuf,
                    link->name);
        }
    }
}

void announce_all_links(const struct mdns_socket_pair *sockets,
                               const struct link_context_set *links,
                               const struct sockaddr_in *dest4,
                               const struct sockaddr_in6 *dest6,
                               const struct config *cfg,
                               const char *stage) {
    size_t i;

    for (i = 0; i < links->count; i++) {
        send_link_announcement_pair(sockets,
                                    &links->links[i],
                                    dest4,
                                    dest6,
                                    cfg,
                                    cfg->ttl,
                                    mdns_service_scope_for_link(links, &links->links[i]),
                                    stage);
    }
}

void send_link_goodbyes(const struct mdns_socket_pair *sockets,
                               const struct link_context_set *links,
                               const struct sockaddr_in *dest4,
                               const struct sockaddr_in6 *dest6,
                               const struct config *cfg) {
    size_t i;

    for (i = 0; i < links->count; i++) {
        send_link_announcement_pair(sockets,
                                    &links->links[i],
                                    dest4,
                                    dest6,
                                    cfg,
                                    0,
                                    mdns_service_scope_for_link(links, &links->links[i]),
                                    "goodbye");
    }
}

void send_link_goodbyes_for_missing(const struct mdns_socket_pair *sockets,
                                           const struct link_context_set *old_links,
                                           const struct link_context_set *new_links,
                                           const struct sockaddr_in *dest4,
                                           const struct sockaddr_in6 *dest6,
                                           const struct config *cfg) {
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
                                    mdns_service_scope_for_link(old_links, &old_links->links[i]),
                                    "goodbye");
    }
}
