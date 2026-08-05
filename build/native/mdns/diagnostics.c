#include "mdns.h"
TC_LOCAL void log_mdns_counters(const char *reason);
TC_LOCAL void remember_logged_mdns_counters(long long now_ms);
TC_LOCAL int mdns_counters_changed_since_log(void);
TC_LOCAL void mdns_transport_requirements_from_links(const struct link_context_set *desired_links,
                                                   struct mdns_transport_requirements *requirements);
TC_LOCAL const char *mdns_transport_health_label(const struct mdns_transport_status *status);
TC_LOCAL void mdns_first_active_ipv4(char *out, size_t out_len, const struct link_context_set *active_links);
TC_LOCAL void mdns_first_active_ipv6(char *out, size_t out_len, const struct link_context_set *active_links);
void log_startup_config(const struct config *cfg) {
    fprintf(stderr,
            "mdns startup: mode=%s instance=%s host=%s ipv4=%s service=%s afp=%s adisk=%s device_model=%s airport=%s advertise=%s\n",
            "exclusive",
            cfg->instance_name[0] != '\0' ? cfg->instance_name : "(empty)",
            cfg->host_label[0] != '\0' ? cfg->host_label : "(empty)",
            "auto",
            cfg->service_type[0] != '\0' ? cfg->service_type : "(empty)",
            afp_enabled(cfg) ? "enabled" : "disabled",
            adisk_enabled(cfg) ? "enabled" : "disabled",
            cfg->device_model[0] != '\0' ? cfg->device_model : "(empty)",
            is_airport_enabled(cfg) ? "enabled" : "disabled",
            cfg->diskless ? "diskless" : "diskful");
    if (is_riousbprint_enabled(cfg)) {
        fprintf(stderr,
                "mdns startup: USB printer instance=%s mfg=%s mdl=%s cmd=%s riousbprint_port=%u pdl_datastream_port=%u\n",
                cfg->riousbprint_instance_name,
                cfg->riousbprint_mfg[0] != '\0' ? cfg->riousbprint_mfg : "(empty)",
                cfg->riousbprint_mdl[0] != '\0' ? cfg->riousbprint_mdl : "(empty)",
                cfg->riousbprint_cmd[0] != '\0' ? cfg->riousbprint_cmd : "(none)",
                (unsigned int)cfg->riousbprint_port,
                (unsigned int)cfg->pdl_datastream_port);
    }
}

void log_send_failure(const char *stage, const struct sockaddr_in *dest, const char *detail) {
    char dest_ip[INET_ADDRSTRLEN];

    fprintf(stderr,
            "mdns send failure: stage=%s dest=%s:%u detail=%s\n",
            stage,
            ipv4_to_string(dest->sin_addr.s_addr, dest_ip, sizeof(dest_ip)),
            (unsigned int)ntohs(dest->sin_port),
            detail);
    fprintf(stderr,
            "mdns send failure: listener remains active; discovery may still work via received queries even though unsolicited announcements failed\n");
}

void remember_last_send_failure(const char *stage, int saved_errno) {
    int written;

    g_mdns_counters.send_failures++;
    written = snprintf(g_mdns_counters.last_send_failure,
                       sizeof(g_mdns_counters.last_send_failure),
                       "%s errno=%d (%s)",
                       stage,
                       saved_errno,
                       strerror(saved_errno));
    if (written < 0 || (size_t)written >= sizeof(g_mdns_counters.last_send_failure)) {
        g_mdns_counters.last_send_failure[sizeof(g_mdns_counters.last_send_failure) - 1] = '\0';
    }
}

TC_LOCAL void log_mdns_counters(const char *reason) {
    fprintf(stderr,
            "mdns counters: reason=%s ipv4_rx=%lu ipv6_rx=%lu query_matches=%lu responses_sent=%lu send_failures=%lu last_send_failure=%s\n",
            reason,
            g_mdns_counters.ipv4_packets_received,
            g_mdns_counters.ipv6_packets_received,
            g_mdns_counters.query_packets_matched,
            g_mdns_counters.responses_sent,
            g_mdns_counters.send_failures,
            g_mdns_counters.last_send_failure[0] != '\0' ? g_mdns_counters.last_send_failure : "(none)");
}

TC_LOCAL void remember_logged_mdns_counters(long long now_ms) {
    g_mdns_counter_log_state.ipv4_packets_received = g_mdns_counters.ipv4_packets_received;
    g_mdns_counter_log_state.ipv6_packets_received = g_mdns_counters.ipv6_packets_received;
    g_mdns_counter_log_state.query_packets_matched = g_mdns_counters.query_packets_matched;
    g_mdns_counter_log_state.responses_sent = g_mdns_counters.responses_sent;
    g_mdns_counter_log_state.send_failures = g_mdns_counters.send_failures;
    g_mdns_counter_log_state.last_log_ms = now_ms;
}

TC_LOCAL int mdns_counters_changed_since_log(void) {
    return g_mdns_counter_log_state.ipv4_packets_received != g_mdns_counters.ipv4_packets_received ||
           g_mdns_counter_log_state.ipv6_packets_received != g_mdns_counters.ipv6_packets_received ||
           g_mdns_counter_log_state.query_packets_matched != g_mdns_counters.query_packets_matched ||
           g_mdns_counter_log_state.responses_sent != g_mdns_counters.responses_sent ||
           g_mdns_counter_log_state.send_failures != g_mdns_counters.send_failures;
}

void log_mdns_counters_force(const char *reason) {
    long long now_ms = monotonic_millis();

    log_mdns_counters(reason);
    remember_logged_mdns_counters(now_ms);
}

void maybe_log_mdns_counters(const char *reason, long long now_ms) {
    if (!g_debug_logging) {
        return;
    }
    if (!mdns_counters_changed_since_log()) {
        return;
    }
    if (g_mdns_counter_log_state.last_log_ms > 0 &&
        now_ms - g_mdns_counter_log_state.last_log_ms < MDNS_COUNTER_LOG_INTERVAL_MS) {
        return;
    }
    log_mdns_counters(reason);
    remember_logged_mdns_counters(now_ms);
}

int note_mdns_ipv4_packet_received(void) {
    g_mdns_counters.ipv4_packets_received++;
    if (!g_mdns_counter_log_state.logged_ipv4_packet) {
        g_mdns_counter_log_state.logged_ipv4_packet = 1;
        return 1;
    }
    return 0;
}

int note_mdns_ipv6_packet_received(void) {
    g_mdns_counters.ipv6_packets_received++;
    if (!g_mdns_counter_log_state.logged_ipv6_packet) {
        g_mdns_counter_log_state.logged_ipv6_packet = 1;
        return 1;
    }
    return 0;
}

void log_mdns_receive_counters(const char *first_packet_reason,
                                      int first_packet,
                                      unsigned long query_matches_before,
                                      long long now_ms) {
    if (g_mdns_counters.query_packets_matched > query_matches_before &&
        !g_mdns_counter_log_state.logged_query_match) {
        g_mdns_counter_log_state.logged_query_match = 1;
        log_mdns_counters_force("first_query_match");
        return;
    }
    if (first_packet) {
        log_mdns_counters_force(first_packet_reason);
        return;
    }
    maybe_log_mdns_counters("traffic_summary", now_ms);
}

TC_LOCAL void mdns_transport_requirements_from_links(const struct link_context_set *desired_links,
                                                   struct mdns_transport_requirements *requirements) {
    int wants_ipv4 = link_contexts_need_ipv4_socket(desired_links);
    int wants_ipv6 = link_contexts_need_ipv6_socket(desired_links);

    memset(requirements, 0, sizeof(*requirements));
    requirements->ipv4_required = wants_ipv4;
    requirements->ipv6_required = wants_ipv6;
}

void mdns_transport_status_from_links(const struct link_context_set *desired_links,
                                             const struct link_context_set *active_links,
                                             const struct mdns_socket_pair *sockets,
                                             struct mdns_transport_status *status) {
    struct mdns_transport_requirements requirements;

    mdns_transport_requirements_from_links(desired_links, &requirements);
    memset(status, 0, sizeof(*status));
    status->required_ipv4 = requirements.ipv4_required;
    status->required_ipv6 = requirements.ipv6_required;
    status->active_ipv4 = sockets->ipv4_fd >= 0 && link_contexts_need_ipv4_socket(active_links);
    status->active_ipv6 = sockets->ipv6_fd >= 0 && link_contexts_need_ipv6_socket(active_links);
    status->missing_required_ipv4 = status->required_ipv4 && !status->active_ipv4;
    status->missing_required_ipv6 = status->required_ipv6 && !status->active_ipv6;
    status->last_ipv4_errno = g_last_ipv4_socket_errno;
    status->last_ipv6_errno = g_last_ipv6_socket_errno;
}

int mdns_transport_has_active_socket(const struct mdns_transport_status *status) {
    return status->active_ipv4 || status->active_ipv6;
}

int mdns_transport_missing_required(const struct mdns_transport_status *status) {
    return status->missing_required_ipv4 || status->missing_required_ipv6;
}

int mdns_transport_is_healthy(const struct mdns_transport_status *status) {
    return mdns_transport_has_active_socket(status) && !mdns_transport_missing_required(status);
}

TC_LOCAL const char *mdns_transport_health_label(const struct mdns_transport_status *status) {
    if (mdns_transport_is_healthy(status)) {
        return "healthy";
    }
    if (mdns_transport_has_active_socket(status)) {
        return "degraded";
    }
    return "down";
}

TC_LOCAL void mdns_first_active_ipv4(char *out, size_t out_len, const struct link_context_set *active_links) {
    size_t i;

    for (i = 0; i < active_links->count; i++) {
        uint32_t ipv4_addr;
        if (!link_context_has_mdns_ipv4_transport(&active_links->links[i])) {
            continue;
        }
        ipv4_addr = link_preferred_ipv4_source(&active_links->links[i]);
        if (ipv4_addr != 0) {
            (void)ipv4_to_string(ipv4_addr, out, out_len);
            return;
        }
    }
    strncpy(out, "off", out_len - 1);
    out[out_len - 1] = '\0';
}

TC_LOCAL void mdns_first_active_ipv6(char *out, size_t out_len, const struct link_context_set *active_links) {
    size_t i;

    for (i = 0; i < active_links->count; i++) {
        if (!link_context_has_mdns_ipv6_transport(&active_links->links[i])) {
            continue;
        }
        snprintf(out, out_len, "%s", active_links->links[i].name);
        return;
    }
    strncpy(out, "off", out_len - 1);
    out[out_len - 1] = '\0';
}

void log_mdns_transport_status(const char *reason,
                                      const struct link_context_set *active_links,
                                      const struct mdns_transport_status *status) {
    char ipv4_buf[INET_ADDRSTRLEN];
    char ipv6_buf[IFNAMSIZ + 1];

    mdns_first_active_ipv4(ipv4_buf, sizeof(ipv4_buf), active_links);
    mdns_first_active_ipv6(ipv6_buf, sizeof(ipv6_buf), active_links);
    fprintf(stderr,
            "mdns transport active: reason=%s status=%s ipv4=%s ipv6=%s required_ipv4=%d required_ipv6=%d missing_required_ipv4=%d missing_required_ipv6=%d last_ipv4_errno=%d last_ipv6_errno=%d\n",
            reason,
            mdns_transport_health_label(status),
            status->active_ipv4 ? ipv4_buf : "off",
            status->active_ipv6 ? ipv6_buf : "off",
            status->required_ipv4,
            status->required_ipv6,
            status->missing_required_ipv4,
            status->missing_required_ipv6,
            status->last_ipv4_errno,
            status->last_ipv6_errno);
}

void log_served_records(const struct config *cfg) {
    fprintf(stderr, "serving summary: source=generated\n");
    if (smb_enabled(cfg)) {
        fprintf(stderr, "serving service: type=%s instance=%s port=%u host=%s\n",
                cfg->service_type, cfg->instance_name, (unsigned int)cfg->port, cfg->host_fqdn);
    }
    if (afp_enabled(cfg)) {
        fprintf(stderr, "serving service: type=%s instance=%s port=%u host=%s\n",
                cfg->afp_service_type, cfg->instance_name, (unsigned int)cfg->afp_port, cfg->host_fqdn);
    }
    if (cfg->device_model[0] != '\0') {
        fprintf(stderr, "serving service: type=%s instance=%s model=%s\n",
                cfg->device_info_service_type, cfg->instance_name, cfg->device_model);
    }
    if (adisk_enabled(cfg)) {
        size_t i;
        for (i = 0; i < cfg->adisk_disks.count; i++) {
            fprintf(stderr, "serving service: type=%s instance=%s share=%s disk_key=%s uuid=%s\n",
                    cfg->adisk_service_type, cfg->instance_name, cfg->adisk_disks.disks[i].share_name,
                    cfg->adisk_disks.disks[i].disk_key, cfg->adisk_disks.disks[i].uuid);
        }
    }
    if (is_airport_enabled(cfg)) {
        fprintf(stderr, "serving service: type=%s instance=%s syAP=%s syVs=%s srcv=%s\n",
                cfg->airport_service_type, cfg->instance_name,
                cfg->airport_syap[0] != '\0' ? cfg->airport_syap : "(none)",
                cfg->airport_syvs[0] != '\0' ? cfg->airport_syvs : "(none)",
                cfg->airport_srcv[0] != '\0' ? cfg->airport_srcv : "(none)");
    }
    if (is_riousbprint_enabled(cfg)) {
        fprintf(stderr, "serving service: type=%s instance=%s port=%u host=%s cmd=%s\n",
                RIOUSBPRINT_SERVICE_TYPE,
                cfg->riousbprint_instance_name,
                (unsigned int)cfg->riousbprint_port,
                cfg->host_fqdn,
                cfg->riousbprint_cmd[0] != '\0' ? cfg->riousbprint_cmd : "(none)");
    }
    if (is_pdl_datastream_enabled(cfg)) {
        fprintf(stderr, "serving service: type=%s instance=%s port=%u host=%s cmd=%s\n",
                PDL_DATASTREAM_SERVICE_TYPE,
                cfg->riousbprint_instance_name,
                (unsigned int)cfg->pdl_datastream_port,
                cfg->host_fqdn,
                cfg->riousbprint_cmd[0] != '\0' ? cfg->riousbprint_cmd : "(none)");
    }
}


struct mdns_runtime_counters g_mdns_counters;

struct mdns_counter_log_state g_mdns_counter_log_state;

int g_debug_logging = 0;
