#include "nbns.h"
TC_LOCAL void on_signal(int signo);
TC_LOCAL void keep_only_nbns_ipv4_link_contexts(struct link_context_set *set);
TC_LOCAL int link_contexts_need_nbns_ipv4_socket(const struct link_context_set *set);
TC_LOCAL void filter_nbns_link_contexts(struct link_context_set *out,
                                      const struct link_context_set *all_links);
TC_LOCAL int collect_usable_nbns_link_contexts(struct link_context_set *out);
TC_LOCAL int print_nbns_socket_families(FILE *stream);
TC_LOCAL int wait_for_auto_link_contexts(struct link_context_set *out);
TC_LOCAL void log_nbns_ipv4_link_miss(const struct link_context_set *links, uint32_t peer_addr);
TC_LOCAL int source_matches_link_ipv4_subnet(uint32_t source_ipv4_addr,
                                           const struct link_ipv4_addr *addr);
TC_LOCAL uint32_t choose_response_ipv4_from_links(const struct link_context_set *links, uint32_t peer_addr);
TC_LOCAL int refresh_auto_link_contexts_if_needed(struct link_context_set *contexts,
                                                time_t *last_link_poll);
TC_LOCAL int open_nbns_ipv4_socket(void);
TC_LOCAL void write_stdout_line(const char *value);
TC_LOCAL void write_stdout_int_line(int value);
TC_LOCAL void usage(const char *prog);
TC_LOCAL void on_signal(int signo) {
    (void)signo;
    g_stop = 1;
}

TC_LOCAL void keep_only_nbns_ipv4_link_contexts(struct link_context_set *set) {
    size_t read_pos;
    size_t write_pos = 0;

    for (read_pos = 0; read_pos < set->count; read_pos++) {
        struct link_context link = set->links[read_pos];
        if (!link_context_has_advertisable_ipv4(&link)) {
            continue;
        }
        link.ipv6_count = 0;
        link.mdns_ipv6_transport = 0;
        set->links[write_pos++] = link;
    }
    set->count = write_pos;
}

TC_LOCAL int link_contexts_need_nbns_ipv4_socket(const struct link_context_set *set) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (link_context_has_advertisable_ipv4(&set->links[i])) {
            return 1;
        }
    }
    return 0;
}

TC_LOCAL void filter_nbns_link_contexts(struct link_context_set *out,
                                      const struct link_context_set *all_links) {
    filter_smb_bind_link_contexts(out, all_links, 1, 0);
    if (out->count == 0) {
        filter_smb_bind_link_contexts(out, all_links, 1, 1);
    }
    keep_only_nbns_ipv4_link_contexts(out);
}

TC_LOCAL int collect_usable_nbns_link_contexts(struct link_context_set *out) {
    struct link_context_set all_links;

    memset(&all_links, 0, sizeof(all_links));
    memset(out, 0, sizeof(*out));
    if (collect_usable_link_contexts(&all_links) != 0) {
        return -1;
    }
    filter_nbns_link_contexts(out, &all_links);
    if (all_links.truncated || out->truncated) {
        fprintf(stderr, "auto-ip: NBNS link list exceeded static capacity\n");
        return -1;
    }
    return 0;
}

TC_LOCAL int print_nbns_socket_families(FILE *stream) {
    struct link_context_set links;

    memset(&links, 0, sizeof(links));
    if (collect_usable_nbns_link_contexts(&links) != 0) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    if (!link_contexts_need_nbns_ipv4_socket(&links)) {
        return EXIT_AUTO_IP_UNAVAILABLE;
    }
    if (fputs("ipv4", stream) == EOF) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    if (fputc('\n', stream) == EOF) {
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
    return EXIT_OK;
}

TC_LOCAL int wait_for_auto_link_contexts(struct link_context_set *out) {
    struct link_context_set first;

    memset(out, 0, sizeof(*out));
    while (!g_stop) {
        memset(&first, 0, sizeof(first));
        if (collect_usable_nbns_link_contexts(&first) == 0 && link_contexts_need_nbns_ipv4_socket(&first)) {
            fprintf(stderr, "nbns auto-ip: first usable IPv4 address observed; waiting %ds for network stabilization\n",
                    AUTO_IP_STABILIZE_SECONDS);
            sleep(AUTO_IP_STABILIZE_SECONDS);
            if (collect_usable_nbns_link_contexts(out) == 0 && link_contexts_need_nbns_ipv4_socket(out)) {
                return 0;
            }
            fprintf(stderr, "nbns auto-ip: usable IPv4 address disappeared during stabilization; retrying\n");
        }
        sleep(AUTO_IP_STARTUP_POLL_SECONDS);
    }
    return -1;
}

TC_LOCAL void log_nbns_ipv4_link_miss(const struct link_context_set *links, uint32_t peer_addr) {
    static time_t last_log = 0;
    time_t now = time(NULL);

    if (last_log != 0 && now - last_log < 60) {
        return;
    }
    last_log = now;
    {
        char peer_buf[INET_ADDRSTRLEN];
        fprintf(stderr,
                "nbns auto-ip: ignoring query from %s; no matching subnet among %lu links\n",
                ipv4_to_string(peer_addr, peer_buf, sizeof(peer_buf)),
                (unsigned long)links->count);
    }
}

TC_LOCAL int source_matches_link_ipv4_subnet(uint32_t source_ipv4_addr,
                                           const struct link_ipv4_addr *addr) {
    uint32_t netmask = effective_ipv4_netmask(addr->addr, addr->netmask);

    if (netmask == 0) {
        return source_ipv4_addr == addr->addr;
    }
    return (source_ipv4_addr & netmask) == (addr->addr & netmask);
}

TC_LOCAL uint32_t choose_response_ipv4_from_links(const struct link_context_set *links, uint32_t peer_addr) {
    uint32_t only_addr = 0;
    size_t addr_count = 0;
    size_t i;

    for (i = 0; i < links->count; i++) {
        size_t j;
        for (j = 0; j < links->links[i].ipv4_count; j++) {
            if (source_matches_link_ipv4_subnet(peer_addr, &links->links[i].ipv4[j])) {
                return links->links[i].ipv4[j].addr;
            }
            only_addr = links->links[i].ipv4[j].addr;
            addr_count++;
        }
    }
    return addr_count == 1 ? only_addr : 0;
}

TC_LOCAL int refresh_auto_link_contexts_if_needed(struct link_context_set *contexts,
                                                time_t *last_link_poll) {
    if (time(NULL) - *last_link_poll >= AUTO_IP_STABLE_POLL_SECONDS) {
        struct link_context_set next_contexts;
        memset(&next_contexts, 0, sizeof(next_contexts));
        if (collect_usable_nbns_link_contexts(&next_contexts) == 0 &&
            !link_context_sets_equal(contexts, &next_contexts)) {
            fprintf(stderr, "nbns auto-ip: interface table changed; rebuilding links after %ds stabilization\n",
                    AUTO_IP_STABILIZE_SECONDS);
            log_link_contexts("nbns auto-ip observed", &next_contexts);
            sleep(AUTO_IP_STABILIZE_SECONDS);
            if (collect_usable_nbns_link_contexts(&next_contexts) == 0 &&
                link_contexts_need_nbns_ipv4_socket(&next_contexts)) {
                *contexts = next_contexts;
            } else if (wait_for_auto_link_contexts(contexts) != 0) {
                return -1;
            }
            log_link_contexts("nbns auto-ip active", contexts);
        }
        *last_link_poll = time(NULL);
    }
    return 0;
}

TC_LOCAL int open_nbns_ipv4_socket(void) {
    struct sockaddr_in addr;
    int sock;
    int yes = 1;

    sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        perror("socket(AF_INET)");
        return -1;
    }
    if (setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes)) < 0) {
        perror("setsockopt(SO_REUSEADDR)");
        close(sock);
        return -1;
    }
#ifdef SO_REUSEPORT
    setsockopt(sock, SOL_SOCKET, SO_REUSEPORT, &yes, sizeof(yes));
#endif
    if (setsockopt(sock, SOL_SOCKET, SO_BROADCAST, &yes, sizeof(yes)) < 0) {
        perror("setsockopt(SO_BROADCAST)");
        close(sock);
        return -1;
    }
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(NBNS_PORT);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind(AF_INET)");
        close(sock);
        return -1;
    }
    return sock;
}

int nbns_main(int argc, char **argv) {
    struct config cfg;
    int sock4 = -1;
    int i;
    int auto_ip = 0;
    int print_socket_families = 0;
    time_t last_link_poll = 0;
    struct link_context_set link_contexts;

    memset(&cfg, 0, sizeof(cfg));
    memset(&link_contexts, 0, sizeof(link_contexts));
    cfg.ttl = 120;

    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--name") == 0 && i + 1 < argc) {
            const char *name_arg = argv[++i];
            size_t name_len;
            if (validate_netbios_name(name_arg) != 0) {
                return 2;
            }
            name_len = strlen(name_arg);
            if (name_len >= sizeof(cfg.netbios_name)) {
                fprintf(stderr, "netbios name must be 15 bytes or fewer\n");
                return 2;
            }
            memcpy(cfg.netbios_name, name_arg, name_len + 1);
        } else if (strcmp(argv[i], "--auto-ip") == 0) {
            auto_ip = 1;
        } else if (strcmp(argv[i], "--print-nbns-socket-families") == 0) {
            print_socket_families = 1;
        } else if (strcmp(argv[i], "--version") == 0) {
            write_stdout_int_line(ADVERTISER_VERSION_CODE);
            return EXIT_OK;
        } else if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            usage(argv[0]);
            return EXIT_OK;
        } else {
            usage(argv[0]);
            return EXIT_USAGE;
        }
    }

    if (print_socket_families) {
        return print_nbns_socket_families(stdout);
    }
    if (cfg.netbios_name[0] == '\0') {
        fprintf(stderr, "missing required option: --name\n");
        return EXIT_USAGE;
    }
    if (!auto_ip) {
        fprintf(stderr, "missing required option: --auto-ip\n");
        usage(argv[0]);
        return EXIT_USAGE;
    }
    if (wait_for_auto_link_contexts(&link_contexts) != 0) {
        return EXIT_RUNTIME_ERROR;
    }
    log_link_contexts("nbns auto-ip active", &link_contexts);
    last_link_poll = time(NULL);

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    if (link_contexts_need_nbns_ipv4_socket(&link_contexts)) {
        sock4 = open_nbns_ipv4_socket();
        if (sock4 < 0) {
            return EXIT_RUNTIME_ERROR;
        }
    }
    while (!g_stop) {
        fd_set readfds;
        struct timeval timeout;
        int maxfd = -1;
        int selected;
        int need_ipv4;

        FD_ZERO(&readfds);
        timeout.tv_sec = 1;
        timeout.tv_usec = 0;


        if (refresh_auto_link_contexts_if_needed(&link_contexts, &last_link_poll) != 0) {
            break;
        }

        need_ipv4 = link_contexts_need_nbns_ipv4_socket(&link_contexts);
        if (need_ipv4 && sock4 < 0) {
            sock4 = open_nbns_ipv4_socket();
            if (sock4 < 0) {
                break;
            }
        } else if (!need_ipv4 && sock4 >= 0) {
            close(sock4);
            sock4 = -1;
        }

        if (sock4 >= 0) {
            FD_SET(sock4, &readfds);
            maxfd = sock4 > maxfd ? sock4 : maxfd;
        }
        if (maxfd < 0) {
            sleep(1);
            continue;
        }

        selected = select(maxfd + 1, &readfds, NULL, NULL, &timeout);
        if (selected < 0) {
            if (errno == EINTR) {
                continue;
            }
            perror("select");
            if (sock4 >= 0) {
                close(sock4);
            }
            return EXIT_RUNTIME_ERROR;
        }

        if (sock4 >= 0 && FD_ISSET(sock4, &readfds)) {
            uint8_t buf[BUF_SIZE];
            struct sockaddr_in peer;
            socklen_t peer_len = sizeof(peer);
            ssize_t nread;

            nread = recvfrom(sock4, buf, sizeof(buf), 0, (struct sockaddr *)&peer, &peer_len);
            if (nread < 0) {
                if (errno != EINTR) {
                    perror("recvfrom(AF_INET)");
                }
            } else {
                uint32_t response_ip;
                struct config context_cfg = cfg;
                response_ip = choose_response_ipv4_from_links(&link_contexts, peer.sin_addr.s_addr);
                if (response_ip == 0) {
                    log_nbns_ipv4_link_miss(&link_contexts, peer.sin_addr.s_addr);
                } else {
                    context_cfg.ipv4_addr = response_ip;
                    (void)maybe_respond_to_query_addr(sock4,
                                                      &context_cfg,
                                                      buf,
                                                      (size_t)nread,
                                                      (const struct sockaddr *)&peer,
                                                      peer_len);
                }
            }
        }
    }

    if (sock4 >= 0) {
        close(sock4);
    }
    return EXIT_OK;
}

#undef fprintf
#undef perror

TC_LOCAL void write_stdout_line(const char *value) {
    if (value != NULL) {
        fputs(value, stdout);
    }
    fputc('\n', stdout);
}

TC_LOCAL void write_stdout_int_line(int value) {
    char text[32];

    (void)snprintf(text, sizeof(text), "%d", value);
    write_stdout_line(text);
}

ssize_t sendto_retry(int sockfd, const void *buf, size_t len, int flags,
                            const struct sockaddr *dest, socklen_t dest_len) {
    ssize_t sent;

    do {
        sent = sendto(sockfd, buf, len, flags, dest, dest_len);
    } while (sent < 0 && errno == EINTR);

    return sent;
}


TC_LOCAL void usage(const char *prog) {
    fprintf(stderr, "Usage: %s --name <netbios-name> --auto-ip [options]\n"
                    "       %s --print-nbns-socket-families | --version\n", prog, prog);
}

volatile sig_atomic_t g_stop = 0;
