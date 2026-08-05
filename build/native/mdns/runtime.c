#include "mdns.h"
TC_LOCAL void on_signal(int signo);
TC_LOCAL int collect_usable_link_contexts_provider(struct link_context_set *out, void *userdata);
TC_LOCAL int collect_usable_advertise_link_contexts_provider(struct link_context_set *out, void *userdata);
TC_LOCAL void mdns_sleep_provider(unsigned int seconds, void *userdata);
TC_LOCAL int wait_for_auto_link_contexts_with_provider(struct link_context_set *out,
                                                     const char *role,
                                                     mdns_collect_link_contexts_fn collect_contexts,
                                                     mdns_sleep_fn sleep_fn,
                                                     void *userdata);
TC_LOCAL int wait_for_auto_advertise_link_contexts(struct link_context_set *out, const char *role);
TC_LOCAL int print_mdns_socket_families_with_provider(FILE *stream,
                                                    mdns_collect_link_contexts_fn collect_contexts,
                                                    void *userdata);
TC_LOCAL int link_context_topology_equal(const struct link_context *a, const struct link_context *b);
TC_LOCAL int link_context_set_contains_topology(const struct link_context_set *set,
                                              const struct link_context *ctx);
TC_LOCAL int link_context_topology_sets_equal(const struct link_context_set *a,
                                            const struct link_context_set *b);
TC_LOCAL int apply_runtime_link_change(int shared_bind,
                                     struct mdns_socket_pair *sockets,
                                     struct link_context_set *active_links,
                                     const struct link_context_set *new_links,
                                     const struct sockaddr_in *dest4,
                                     const struct sockaddr_in6 *dest6,
                                     const struct config *cfg);
TC_LOCAL int recover_runtime_link_change_with_takeover(int shared_bind,
                                                     struct mdns_socket_pair *sockets,
                                                     struct link_context_set *active_links,
                                                     const struct link_context_set *desired_links,
                                                     const struct sockaddr_in *dest4,
                                                     const struct sockaddr_in6 *dest6,
                                                     const struct config *cfg,
                                                     struct mdns_transport_status *status);
TC_LOCAL void on_signal(int signo) {
    (void)signo;
    g_stop = 1;
}

TC_LOCAL int collect_usable_link_contexts_provider(struct link_context_set *out, void *userdata) {
    (void)userdata;
    return collect_usable_link_contexts(out);
}

TC_LOCAL int collect_usable_advertise_link_contexts_provider(struct link_context_set *out, void *userdata) {
    struct link_context_set all_links;

    (void)userdata;
    memset(&all_links, 0, sizeof(all_links));
    if (collect_usable_link_contexts(&all_links) != 0) {
        return -1;
    }
    filter_advertise_link_contexts(out, &all_links);
    return 0;
}

TC_LOCAL void mdns_sleep_provider(unsigned int seconds, void *userdata) {
    (void)userdata;
    sleep(seconds);
}

TC_LOCAL int wait_for_auto_link_contexts_with_provider(struct link_context_set *out,
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

TC_LOCAL int wait_for_auto_advertise_link_contexts(struct link_context_set *out, const char *role) {
    return wait_for_auto_link_contexts_with_provider(out,
                                                    role,
                                                    collect_usable_advertise_link_contexts_provider,
                                                    mdns_sleep_provider,
                                                    NULL);
}

TC_LOCAL int print_mdns_socket_families_with_provider(FILE *stream,
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
    if (all_links.truncated || links.truncated) {
        fprintf(stderr, "auto-ip: mDNS socket family link list exceeded static capacity\n");
        return EXIT_AUTO_IP_PROBE_FAILED;
    }
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

TC_LOCAL int link_context_topology_equal(const struct link_context *a, const struct link_context *b) {
    size_t i;

    if (strcmp(a->name, b->name) != 0 ||
        a->flags != b->flags ||
        a->ifindex != b->ifindex ||
        a->is_wan != b->is_wan ||
        a->ipv4_count != b->ipv4_count ||
        a->ipv6_count != b->ipv6_count) {
        return 0;
    }
    for (i = 0; i < a->ipv4_count; i++) {
        if (a->ipv4[i].addr != b->ipv4[i].addr ||
            a->ipv4[i].netmask != b->ipv4[i].netmask) {
            return 0;
        }
    }
    for (i = 0; i < a->ipv6_count; i++) {
        if (memcmp(&a->ipv6[i].addr, &b->ipv6[i].addr, sizeof(a->ipv6[i].addr)) != 0 ||
            a->ipv6[i].scope_id != b->ipv6[i].scope_id ||
            a->ipv6[i].prefix_len != b->ipv6[i].prefix_len ||
            a->ipv6[i].link_local != b->ipv6[i].link_local) {
            return 0;
        }
    }
    return 1;
}

TC_LOCAL int link_context_set_contains_topology(const struct link_context_set *set,
                                              const struct link_context *ctx) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (link_context_topology_equal(&set->links[i], ctx)) {
            return 1;
        }
    }
    return 0;
}

TC_LOCAL int link_context_topology_sets_equal(const struct link_context_set *a,
                                            const struct link_context_set *b) {
    size_t i;

    if (a->count != b->count) {
        return 0;
    }
    for (i = 0; i < a->count; i++) {
        if (!link_context_set_contains_topology(b, &a->links[i])) {
            return 0;
        }
    }
    return 1;
}

enum mdns_service_scope mdns_service_scope_for_link(const struct link_context_set *links,
                                                           const struct link_context *link) {
    struct link_context_set lan_links;

    filter_smb_bind_link_contexts(&lan_links, links, 1, 0);
    if (lan_links.count == 0) {
        filter_smb_bind_link_contexts(&lan_links, links, 1, 1);
    }
    return link_context_set_contains_topology(&lan_links, link)
               ? MDNS_SERVICE_SCOPE_LAN
               : MDNS_SERVICE_SCOPE_WAN;
}

const char *mdns_service_scope_name(enum mdns_service_scope scope) {
    return scope == MDNS_SERVICE_SCOPE_LAN ? "lan" : "wan";
}

TC_LOCAL int apply_runtime_link_change(int shared_bind,
                                     struct mdns_socket_pair *sockets,
                                     struct link_context_set *active_links,
                                     const struct link_context_set *new_links,
                                     const struct sockaddr_in *dest4,
                                     const struct sockaddr_in6 *dest6,
                                     const struct config *cfg) {
    struct link_context_set applied_links;

    applied_links = *new_links;
    if (prepare_runtime_mdns_sockets_for_links(shared_bind, sockets, active_links, &applied_links) != 0) {
        return -1;
    }
    send_link_goodbyes_for_missing(sockets,
                                   active_links,
                                   &applied_links,
                                   dest4,
                                   dest6,
                                   cfg);
    retire_runtime_mdns_memberships_for_missing(sockets, active_links, &applied_links);
    close_unused_runtime_mdns_socket_families(sockets, &applied_links);
    *active_links = applied_links;
    return 0;
}

TC_LOCAL int recover_runtime_link_change_with_takeover(int shared_bind,
                                                     struct mdns_socket_pair *sockets,
                                                     struct link_context_set *active_links,
                                                     const struct link_context_set *desired_links,
                                                     const struct sockaddr_in *dest4,
                                                     const struct sockaddr_in6 *dest6,
                                                     const struct config *cfg,
                                                     struct mdns_transport_status *status) {
    static const unsigned int retry_delays_ms[TAKEOVER_RETRY_COUNT] = {0, 100, 200, 300, 400, 500};
    size_t i;

    if (apply_runtime_link_change(shared_bind,
                                  sockets,
                                  active_links,
                                  desired_links,
                                  dest4,
                                  dest6,
                                  cfg) == 0) {
        mdns_transport_status_from_links(desired_links, active_links, sockets, status);
        if (mdns_transport_is_healthy(status)) {
            return 0;
        }
    } else {
        mdns_transport_status_from_links(desired_links, active_links, sockets, status);
    }

    for (i = 0; i < TAKEOVER_RETRY_COUNT; i++) {
        kill_mdnsresponder(SIGTERM);
        sleep_millis(retry_delays_ms[i]);
        if (apply_runtime_link_change(shared_bind,
                                      sockets,
                                      active_links,
                                      desired_links,
                                      dest4,
                                      dest6,
                                      cfg) == 0) {
            mdns_transport_status_from_links(desired_links, active_links, sockets, status);
            if (mdns_transport_is_healthy(status)) {
                fprintf(stderr, "mDNS runtime required transport recovered after SIGTERM + %ums\n",
                        retry_delays_ms[i]);
                return 0;
            }
        } else {
            mdns_transport_status_from_links(desired_links, active_links, sockets, status);
        }
    }

    for (i = 0; i < TAKEOVER_RETRY_COUNT; i++) {
        kill_mdnsresponder(SIGKILL);
        sleep_millis(retry_delays_ms[i]);
        if (apply_runtime_link_change(shared_bind,
                                      sockets,
                                      active_links,
                                      desired_links,
                                      dest4,
                                      dest6,
                                      cfg) == 0) {
            mdns_transport_status_from_links(desired_links, active_links, sockets, status);
            if (mdns_transport_is_healthy(status)) {
                fprintf(stderr, "mDNS runtime required transport recovered after SIGKILL + %ums\n",
                        retry_delays_ms[i]);
                return 0;
            }
        } else {
            mdns_transport_status_from_links(desired_links, active_links, sockets, status);
        }
    }

    mdns_transport_status_from_links(desired_links, active_links, sockets, status);
    return mdns_transport_has_active_socket(status) ? 1 : -1;
}

int mdns_main(int argc, char **argv) {
    struct config cfg;
    struct sockaddr_in mdns_dest;
    struct sockaddr_in6 mdns_dest6;
    int i;
    int auto_ip = 0;
    int print_mdns_socket_families = 0;
    int auto_contexts_ready = 0;
    struct link_context_set desired_links;
    struct link_context_set active_links;
    size_t startup_burst_index = 0;
    long long startup_burst_start_ms = 0;

    memset(&cfg, 0, sizeof(cfg));
    memset(&desired_links, 0, sizeof(desired_links));
    memset(&active_links, 0, sizeof(active_links));
    strcpy(cfg.service_type, "_smb._tcp.local.");
    strcpy(cfg.adisk_service_type, "_adisk._tcp.local.");
    strcpy(cfg.afp_service_type, AFP_SERVICE_TYPE);
    strcpy(cfg.device_info_service_type, "_device-info._tcp.local.");
    strcpy(cfg.airport_service_type, AIRPORT_SERVICE_TYPE);
    cfg.port = 445;
    cfg.adisk_port = 9;
    cfg.afp_port = AFP_DEFAULT_PORT;
    cfg.airport_port = AIRPORT_DEFAULT_PORT;
    cfg.riousbprint_port = RIOUSBPRINT_DEFAULT_PORT;
    cfg.pdl_datastream_port = PDL_DATASTREAM_DEFAULT_PORT;
    cfg.ttl = 120;

    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--diskless") == 0) {
            cfg.diskless = 1;
        } else if (strcmp(argv[i], "--afp") == 0) {
            cfg.advertise_afp = 1;
        } else if (strcmp(argv[i], "--auto-ip") == 0) {
            auto_ip = 1;
        } else if (strcmp(argv[i], "--print-mdns-socket-families") == 0) {
            print_mdns_socket_families = 1;
        } else if (strcmp(argv[i], "--version") == 0) {
            printf("%d\n", ADVERTISER_VERSION_CODE);
            return EXIT_OK;
        } else if (strcmp(argv[i], "--debug-logging") == 0) {
            g_debug_logging = 1;
        } else if (strcmp(argv[i], "--adisk-shares-file") == 0 && i + 1 < argc) {
            strncpy(cfg.adisk_shares_file, argv[++i], sizeof(cfg.adisk_shares_file) - 1);
        } else if (strcmp(argv[i], "--adisk-sys-wama") == 0 && i + 1 < argc) {
            strncpy(cfg.adisk_sys_wama, argv[++i], sizeof(cfg.adisk_sys_wama) - 1);
        } else if (strcmp(argv[i], "--device-model") == 0 && i + 1 < argc) {
            strncpy(cfg.device_model, argv[++i], sizeof(cfg.device_model) - 1);
        } else if (strcmp(argv[i], "--riousbprint-name") == 0 && i + 1 < argc) {
            strncpy(cfg.riousbprint_instance_name, argv[++i], sizeof(cfg.riousbprint_instance_name) - 1);
        } else if (strcmp(argv[i], "--riousbprint-note") == 0 && i + 1 < argc) {
            strncpy(cfg.riousbprint_note, argv[++i], sizeof(cfg.riousbprint_note) - 1);
        } else if (strcmp(argv[i], "--riousbprint-mfg") == 0 && i + 1 < argc) {
            strncpy(cfg.riousbprint_mfg, argv[++i], sizeof(cfg.riousbprint_mfg) - 1);
        } else if (strcmp(argv[i], "--riousbprint-mdl") == 0 && i + 1 < argc) {
            strncpy(cfg.riousbprint_mdl, argv[++i], sizeof(cfg.riousbprint_mdl) - 1);
        } else if (strcmp(argv[i], "--riousbprint-serial") == 0 && i + 1 < argc) {
            strncpy(cfg.riousbprint_serial, argv[++i], sizeof(cfg.riousbprint_serial) - 1);
        } else if (strcmp(argv[i], "--riousbprint-cmd") == 0 && i + 1 < argc) {
            strncpy(cfg.riousbprint_cmd, argv[++i], sizeof(cfg.riousbprint_cmd) - 1);
        } else if (strcmp(argv[i], "--riousbprint-vendor-id") == 0 && i + 1 < argc) {
            cfg.riousbprint_vendor_id = (unsigned int)strtoul(argv[++i], NULL, 0);
        } else if (strcmp(argv[i], "--riousbprint-product-id") == 0 && i + 1 < argc) {
            cfg.riousbprint_product_id = (unsigned int)strtoul(argv[++i], NULL, 0);
        } else if (strcmp(argv[i], "--riousbprint-port") == 0 && i + 1 < argc) {
            cfg.riousbprint_port = (uint16_t)atoi(argv[++i]);
        } else if (strcmp(argv[i], "--pdl-datastream-port") == 0 && i + 1 < argc) {
            cfg.pdl_datastream_port = (uint16_t)atoi(argv[++i]);
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
        } else {
            usage(argv[0]);
            return EXIT_USAGE;
        }
    }

    if (print_mdns_socket_families) {
        return print_mdns_socket_families_with_provider(stdout,
                                                       collect_usable_link_contexts_provider,
                                                       NULL);
    }
    if (cfg.instance_name[0] == '\0' || cfg.host_label[0] == '\0' || !auto_ip) {
        usage(argv[0]);
        return EXIT_MISSING_REQUIRED_ARGS;
    }

    if ((cfg.instance_name[0] != '\0' && validate_generated_dns_label(cfg.instance_name, "instance name") != 0) ||
        (cfg.host_label[0] != '\0' && validate_generated_dns_label(cfg.host_label, "host label") != 0)) {
        return EXIT_INVALID_DNS_LABEL;
    }
    if (validate_dns_name(cfg.service_type, "service type") != 0) {
        return EXIT_INVALID_SERVICE_TYPE;
    }
    if (cfg.adisk_shares_file[0] != '\0' && parse_adisk_shares_file(&cfg, cfg.adisk_shares_file) != 0) {
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
    if (is_riousbprint_enabled(&cfg)) {
        char txt_storage[RIOUSBPRINT_MAX_TXT_ITEMS][MAX_TXT_STRING + 1];
        const char *txts[RIOUSBPRINT_MAX_TXT_ITEMS];
        size_t txt_count;

        if (validate_generated_dns_label(cfg.riousbprint_instance_name, "riousbprint name") != 0) {
            return EXIT_INVALID_DNS_LABEL;
        }
        if (cfg.riousbprint_port == 0) {
            fprintf(stderr, "riousbprint port must not be zero\n");
            return EXIT_INVALID_SERVICE_TYPE;
        }
        if (cfg.pdl_datastream_port == 0) {
            fprintf(stderr, "pdl-datastream port must not be zero\n");
            return EXIT_INVALID_SERVICE_TYPE;
        }
        discover_riousbprint_usb_cmd(&cfg);
        if (build_riousbprint_txt_items(&cfg, txt_storage, txts, &txt_count) != 0) {
            return EXIT_INVALID_AIRPORT_TXT;
        }
        if (build_pdl_datastream_txt_items(&cfg, txt_storage, txts, &txt_count) != 0) {
            return EXIT_INVALID_AIRPORT_TXT;
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

    if (build_host_fqdn(cfg.host_fqdn, sizeof(cfg.host_fqdn), cfg.host_label) != 0) {
        return EXIT_INVALID_DNS_LABEL;
    }
    log_startup_config(&cfg);

    log_served_records(&cfg);

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
        time_t last_degraded_retry;
        time_t last_mdnsresponder_guard;
        struct mdns_socket_pair sockets;
        struct mdns_transport_status transport_status;
        int startup_counters_logged = 0;

        if (!auto_contexts_ready) {
            if (wait_for_auto_advertise_link_contexts(&desired_links, "mdns runtime") != 0) {
                return EXIT_AUTO_IP_UNAVAILABLE;
            }
            auto_contexts_ready = 1;
        }
        log_link_contexts("mdns runtime desired", &desired_links);
        sockets.ipv4_fd = -1;
        sockets.ipv6_fd = -1;
        if (acquire_dualstack_mdns_sockets(0, &desired_links, &active_links, &sockets, &transport_status) < 0) {
            return EXIT_SOCKET_ACQUIRE_FAILED;
        }
        log_link_contexts("mdns runtime active", &active_links);
        log_mdns_transport_status("startup", &active_links, &transport_status);
        log_mdns_counters_force("startup");

        startup_burst_start_ms = monotonic_millis();
        last_iface_poll = time(NULL);
        last_degraded_retry = time(NULL);
        last_mdnsresponder_guard = time(NULL);

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
                    !link_context_sets_equal(&desired_links, &next_links)) {
                    struct link_context_set stabilized_links;
                    fprintf(stderr,
                            link_context_topology_sets_equal(&desired_links, &next_links)
                                ? "mdns desired transport state changed; confirming after %ds stabilization\n"
                                : "mdns desired link topology changed; confirming after %ds stabilization\n",
                            AUTO_IP_STABILIZE_SECONDS);
                    log_link_contexts("mdns desired old", &desired_links);
                    log_link_contexts("mdns desired observed", &next_links);
                    sleep(AUTO_IP_STABILIZE_SECONDS);
                    memset(&stabilized_links, 0, sizeof(stabilized_links));
                    if (collect_usable_advertise_link_contexts_provider(&stabilized_links, NULL) == 0) {
                        if (link_context_sets_equal(&desired_links, &stabilized_links)) {
                            fprintf(stderr, "mdns desired link change did not persist after stabilization\n");
                        } else {
                            desired_links = stabilized_links;
                            if (stabilized_links.count > 0) {
                                log_link_contexts("mdns desired stabilized", &desired_links);
                                if (recover_runtime_link_change_with_takeover(0,
                                                                              &sockets,
                                                                              &active_links,
                                                                              &desired_links,
                                                                              &mdns_dest,
                                                                              &mdns_dest6,
                                                                              &cfg,
                                                                              &transport_status) < 0) {
                                    fprintf(stderr, "mdns auto-ip: could not apply stabilized desired links; keeping existing active transport until next retry\n");
                                    last_iface_poll = time(NULL);
                                    continue;
                                }
                            } else {
                                fprintf(stderr, "mdns auto-ip: no usable address links after stabilization; sending goodbyes and waiting\n");
                                send_link_goodbyes(&sockets, &active_links, &mdns_dest, &mdns_dest6, &cfg);
                                close_mdns_socket_pair(&sockets);
                                memset(&active_links, 0, sizeof(active_links));
                                memset(&desired_links, 0, sizeof(desired_links));
                                if (wait_for_auto_advertise_link_contexts(&desired_links, "mdns runtime") != 0) {
                                    break;
                                }
                                if (acquire_dualstack_mdns_sockets(0, &desired_links, &active_links, &sockets, &transport_status) < 0) {
                                    fprintf(stderr, "mdns auto-ip: usable address links returned but sockets could not be acquired\n");
                                    last_iface_poll = time(NULL);
                                    continue;
                                }
                            }
                            log_link_contexts("mdns auto-ip active", &active_links);
                            log_mdns_transport_status("link_change", &active_links, &transport_status);
                            fprintf(stderr, "mdns auto-ip: re-announcing after link change\n");
                            startup_burst_start_ms = monotonic_millis();
                            startup_burst_index = 0;
                            startup_counters_logged = 0;
                            log_mdns_counters_force("link_change");
                            last_degraded_retry = time(NULL);
                        }
                    }
                }
                last_iface_poll = time(NULL);
            }

            mdns_transport_status_from_links(&desired_links, &active_links, &sockets, &transport_status);
            if (mdns_transport_missing_required(&transport_status) &&
                time(NULL) - last_degraded_retry >= MDNS_DEGRADED_RETRY_SECONDS) {
                fprintf(stderr,
                        "mdns desired transport state changed: retrying missing required transports ipv4=%d ipv6=%d\n",
                        transport_status.missing_required_ipv4,
                        transport_status.missing_required_ipv6);
                if (recover_runtime_link_change_with_takeover(0,
                                                              &sockets,
                                                              &active_links,
                                                              &desired_links,
                                                              &mdns_dest,
                                                              &mdns_dest6,
                                                              &cfg,
                                                              &transport_status) >= 0) {
                    log_link_contexts("mdns auto-ip active", &active_links);
                    log_mdns_transport_status("degraded_retry", &active_links, &transport_status);
                    fprintf(stderr, "mdns auto-ip: re-announcing after degraded transport retry\n");
                    startup_burst_start_ms = monotonic_millis();
                    startup_burst_index = 0;
                    startup_counters_logged = 0;
                    log_mdns_counters_force("degraded_retry");
                } else {
                    log_mdns_transport_status("degraded_retry_failed", &active_links, &transport_status);
                    log_mdns_counters_force("degraded_retry_failed");
                }
                last_degraded_retry = time(NULL);
            }

            /* We hold UDP 5353; a respawned mDNSResponder cannot reclaim the port, but
             * reap it so it stops answering queries out of band. Never touch sockets. */
            if (time(NULL) - last_mdnsresponder_guard >= MDNS_MDNSRESPONDER_GUARD_SECONDS) {
                if (mdnsresponder_is_alive()) {
                    fprintf(stderr, "mdns guard: Apple mDNSResponder respawned while we hold UDP %d; reaping\n", MDNS_PORT);
                    kill_mdnsresponder(SIGKILL);
                }
                last_mdnsresponder_guard = time(NULL);
            }

            now_ms = monotonic_millis();
            (void)flush_deferred_response_if_due(now_ms);
            while (startup_burst_index < STARTUP_BURST_COUNT &&
                   now_ms - startup_burst_start_ms >= (long long)g_startup_burst_offsets_ms[startup_burst_index]) {
                announce_all_links(&sockets, &active_links, &mdns_dest, &mdns_dest6, &cfg, "startup_announce");
                startup_burst_index++;
                now_ms = monotonic_millis();
            }
            if (startup_burst_index >= STARTUP_BURST_COUNT && !startup_counters_logged) {
                log_mdns_counters_force("startup_announcements_complete");
                startup_counters_logged = 1;
            }
            maybe_log_mdns_counters("traffic_summary", now_ms);

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
                next_burst_ms = startup_burst_start_ms + (long long)g_startup_burst_offsets_ms[startup_burst_index];
                wait_ms = next_burst_ms - now_ms;
                if (wait_ms < 0) {
                    wait_ms = 0;
                } else if (wait_ms > 1000) {
                    wait_ms = 1000;
                }
            }
            wait_ms = deferred_response_adjust_wait_ms(now_ms, wait_ms);
            tv.tv_sec = (time_t)(wait_ms / 1000);
            tv.tv_usec = (suseconds_t)((wait_ms % 1000) * 1000);

            {
                int selected;
                if (maxfd < 0) {
                    sleep_millis(1000);
                    continue;
                }
                selected = select(maxfd + 1, &rfds, NULL, NULL, &tv);
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
                        const struct link_context *link = select_response_link_ipv4(&active_links, &src);
                        int first_packet = note_mdns_ipv4_packet_received();
                        unsigned long query_matches_before = g_mdns_counters.query_packets_matched;
                        if (link != NULL &&
                            (set_link_outbound_interface4_for_peer(sockets.ipv4_fd, link, src.sin_addr.s_addr) != 0 ||
                             handle_query_scoped(sockets.ipv4_fd,
                                                 packet,
                                                 (size_t)nread,
                                                 &mdns_dest,
                                                 &src,
                                                 &cfg,
                                                 link,
                                                 mdns_service_scope_for_link(&active_links, link)) != 0)) {
                            char detail[160];
                            snprintf(detail, sizeof(detail), "iface=%s packet_len=%ld from=%s:%u",
                                     link->name,
                                     (long)nread, inet_ntoa(src.sin_addr), (unsigned int)ntohs(src.sin_port));
                            log_send_failure("query_response", &mdns_dest, detail);
                        }
                        log_mdns_receive_counters("first_ipv4_packet", first_packet, query_matches_before, now_ms);
                    }
                }
                if (selected > 0 && sockets.ipv6_fd >= 0 && FD_ISSET(sockets.ipv6_fd, &rfds)) {
                    struct sockaddr_in6 src6;
                    socklen_t src6_len = sizeof(src6);
                    unsigned int received_ifindex = 0;
                    ssize_t nread = receive_ipv6_packet(sockets.ipv6_fd,
                                                        packet,
                                                        sizeof(packet),
                                                        &src6,
                                                        &src6_len,
                                                        &received_ifindex);
                    if (nread > 0) {
                        if (ipv6_is_link_local_addr(&src6.sin6_addr) &&
                            ipv6_sockaddr_effective_ifindex(&src6) == 0 &&
                            received_ifindex != 0) {
                            src6.sin6_scope_id = received_ifindex;
                        }
                        const struct link_context *link = select_response_link_ipv6(&active_links,
                                                                                    &src6,
                                                                                    received_ifindex);
                        int first_packet = note_mdns_ipv6_packet_received();
                        unsigned long query_matches_before = g_mdns_counters.query_packets_matched;
                        if (link != NULL) {
                            struct sockaddr_in6 scoped_dest6;
                            int query_status;
                            scoped_mdns_dest6_for_link(&scoped_dest6, &mdns_dest6, link);
                            query_status = set_link_outbound_interface6(sockets.ipv6_fd, link);
                            if (query_status == 0) {
                                query_status = handle_query_any_scoped(sockets.ipv6_fd,
                                                                       packet,
                                                                       (size_t)nread,
                                                                       (const struct sockaddr *)&scoped_dest6,
                                                                       sizeof(scoped_dest6),
                                                                       (const struct sockaddr *)&src6,
                                                                       src6_len,
                                                                       received_ifindex,
                                                                       &cfg,
                                                                       link,
                                                                       mdns_service_scope_for_link(&active_links, link));
                            }
                            if (query_status != 0) {
                                char srcbuf[96];
                                format_sockaddr_addr((const struct sockaddr *)&src6, srcbuf, sizeof(srcbuf));
                                fprintf(stderr,
                                        "mdns send failure: stage=query_response detail=iface=%s packet_len=%ld from=%s\n",
                                        link->name,
                                        (long)nread,
                                        srcbuf);
                            }
                        }
                        log_mdns_receive_counters("first_ipv6_packet", first_packet, query_matches_before, now_ms);
                    }
                }
            }

        }

        send_link_goodbyes(&sockets, &active_links, &mdns_dest, &mdns_dest6, &cfg);
        log_mdns_counters_force("shutdown");
        close_mdns_socket_pair(&sockets);
        return 0;
    }

    usage(argv[0]);
    return EXIT_MISSING_REQUIRED_ARGS;
}

#undef fprintf
#undef perror

volatile sig_atomic_t g_stop = 0;
const unsigned int g_startup_burst_offsets_ms[STARTUP_BURST_COUNT] = {0, 1000, 3000, 7000};
ssize_t sendto_retry(int sockfd, const void *buf, size_t len, int flags,
                            const struct sockaddr *dest, socklen_t dest_len) {
    ssize_t sent;

    do {
        sent = sendto(sockfd, buf, len, flags, dest, dest_len);
    } while (sent < 0 && errno == EINTR);

    return sent;
}
