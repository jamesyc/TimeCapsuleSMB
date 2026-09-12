#include "mdns.h"
TC_LOCAL int known_answer_ttl_is_fresh(uint32_t known_ttl, uint32_t advertised_ttl);
TC_LOCAL int planned_rr_rdata_equals(const struct planned_rr *rr, const uint8_t *rdata, uint16_t rdlength);
TC_LOCAL int planned_rr_add_raw(struct planned_rr_set *set,
                              int routes,
                              const char *owner,
                              uint16_t type,
                              uint16_t rrclass,
                              uint32_t ttl,
                              const uint8_t *rdata,
                              uint16_t rdlength);
TC_LOCAL int planned_rr_add_a(struct planned_rr_set *set, int routes, const char *owner, uint32_t ipv4_addr, uint32_t ttl);
TC_LOCAL int planned_rr_add_aaaa(struct planned_rr_set *set,
                               int routes,
                               const char *owner,
                               const struct in6_addr *ipv6_addr,
                               uint32_t ttl);
TC_LOCAL int planned_set_has_route(const struct planned_rr_set *set, int route);
TC_LOCAL int planned_set_has_any_route(const struct planned_rr_set *set);
TC_LOCAL int planned_rr_matches_known_answer(const struct planned_rr *rr,
                                           const char *owner,
                                           uint16_t type,
                                           uint16_t rrclass,
                                           const uint8_t *packet,
                                           size_t packet_len,
                                           size_t rdata_cursor,
                                           uint16_t rdlength);
TC_LOCAL void suppress_planned_known_answers(const uint8_t *packet,
                                           size_t packet_len,
                                           size_t cursor,
                                           uint16_t answer_count,
                                           struct planned_rr_set *planned);
TC_LOCAL uint16_t sockaddr_port_host(const struct sockaddr *addr);
TC_LOCAL int source_can_receive_unicast_response(const struct sockaddr *source,
                                               const struct link_context *response_link,
                                               unsigned int ingress_ifindex);
TC_LOCAL int plan_question_answers(struct planned_rr_set *planned,
                                 int route,
                                 const char *qname,
                                 uint16_t qtype,
                                 const struct config *cfg,
                                 const struct link_context *response_link,
                                 const char *instance_fqdn,
                                 const char *afp_instance_fqdn,
                                 const char *adisk_instance_fqdn,
                                 const char *device_info_instance_fqdn,
                                 const char *airport_instance_fqdn,
                                 const char *riousbprint_instance_fqdn,
                                 const char *pdl_datastream_instance_fqdn);
TC_LOCAL int add_planned_rr_to_packet(uint8_t *reply,
                                    size_t *off,
                                    size_t reply_cap,
                                    const struct planned_rr *rr,
                                    int legacy_unicast);
TC_LOCAL int build_planned_response_packet(uint8_t *reply,
                                         size_t reply_cap,
                                         size_t *reply_len,
                                         int *answer_count,
                                         uint16_t response_id,
                                         int route,
                                         int legacy_unicast,
                                         const struct response_question_section *questions,
                                         const struct planned_rr_set *planned);
TC_LOCAL void stored_question_section_as_response(const struct stored_question_section *stored,
                                                struct response_question_section *out);
TC_LOCAL int send_planned_response_route(int sockfd,
                                       const struct planned_rr_set *planned,
                                       int route,
                                       uint16_t response_id,
                                       const struct response_question_section *questions,
                                       const struct sockaddr *dest,
                                       socklen_t dest_len,
                                       int delay_multicast);
TC_LOCAL void clear_deferred_response(void);
TC_LOCAL int sockaddr_endpoint_equal(const struct sockaddr *a, socklen_t a_len,
                                   const struct sockaddr *b, socklen_t b_len);
TC_LOCAL int deferred_response_matches_source(int sockfd, const struct sockaddr *source, socklen_t source_len);
TC_LOCAL int copy_sockaddr_storage(struct sockaddr_storage *out,
                                 socklen_t *out_len,
                                 const struct sockaddr *src,
                                 socklen_t src_len);
TC_LOCAL int flush_deferred_response_now(void);
TC_LOCAL int defer_planned_response(int sockfd,
                                  uint16_t response_id,
                                  const struct sockaddr *multicast_dest,
                                  socklen_t multicast_dest_len,
                                  const struct sockaddr *source,
                                  socklen_t source_len,
                                  const struct response_question_section *questions,
                                  const struct planned_rr_set *planned);
TC_LOCAL int TC_UNUSED handle_query(int sockfd, const uint8_t *packet, size_t packet_len,
                                  const struct sockaddr_in *multicast_dest, const struct sockaddr_in *source,
                                  const struct config *cfg, const struct link_context *response_link);
TC_LOCAL int known_answer_ttl_is_fresh(uint32_t known_ttl, uint32_t advertised_ttl) {
    return known_ttl > advertised_ttl / 2;
}

TC_LOCAL int planned_rr_rdata_equals(const struct planned_rr *rr, const uint8_t *rdata, uint16_t rdlength) {
    return rr->rdlength == rdlength && memcmp(rr->rdata, rdata, rdlength) == 0;
}

TC_LOCAL int planned_rr_add_raw(struct planned_rr_set *set,
                              int routes,
                              const char *owner,
                              uint16_t type,
                              uint16_t rrclass,
                              uint32_t ttl,
                              const uint8_t *rdata,
                              uint16_t rdlength) {
    size_t i;

    if (routes == 0 || owner == NULL || owner[0] == '\0') {
        return 0;
    }
    if (rdlength > PLANNED_RDATA_MAX) {
        set->truncated = 1;
        return -1;
    }
    for (i = 0; i < set->count; i++) {
        if (set->records[i].type == type &&
            set->records[i].rrclass == rrclass &&
            name_equals(set->records[i].owner, owner) &&
            planned_rr_rdata_equals(&set->records[i], rdata, rdlength)) {
            set->records[i].routes |= routes;
            return 0;
        }
    }
    if (set->count >= PLANNED_RR_MAX) {
        set->truncated = 1;
        return -1;
    }
    strncpy(set->records[set->count].owner, owner, sizeof(set->records[set->count].owner) - 1);
    set->records[set->count].owner[sizeof(set->records[set->count].owner) - 1] = '\0';
    set->records[set->count].type = type;
    set->records[set->count].rrclass = rrclass;
    set->records[set->count].ttl = ttl;
    memcpy(set->records[set->count].rdata, rdata, rdlength);
    set->records[set->count].rdlength = rdlength;
    set->records[set->count].routes = routes;
    set->count++;
    return 0;
}

int planned_rr_add_name(struct planned_rr_set *set,
                               int routes,
                               const char *owner,
                               uint16_t type,
                               uint16_t rrclass,
                               uint32_t ttl,
                               const char *target) {
    uint8_t rdata[PLANNED_RDATA_MAX];
    size_t off = 0;

    if (encode_name(rdata, &off, sizeof(rdata), target) != 0) {
        return -1;
    }
    return planned_rr_add_raw(set, routes, owner, type, rrclass, ttl, rdata, (uint16_t)off);
}

int planned_rr_add_srv(struct planned_rr_set *set,
                              int routes,
                              const char *owner,
                              const char *target,
                              uint16_t port,
                              uint32_t ttl) {
    uint8_t rdata[PLANNED_RDATA_MAX];
    size_t off = 0;

    if (append_u16(rdata, &off, sizeof(rdata), 0) != 0 ||
        append_u16(rdata, &off, sizeof(rdata), 0) != 0 ||
        append_u16(rdata, &off, sizeof(rdata), port) != 0 ||
        encode_name(rdata, &off, sizeof(rdata), target) != 0) {
        return -1;
    }
    return planned_rr_add_raw(set, routes, owner, DNS_TYPE_SRV, DNS_CLASS_IN_UNIQUE, ttl, rdata, (uint16_t)off);
}

int planned_rr_add_txt_items(struct planned_rr_set *set,
                                    int routes,
                                    const char *owner,
                                    const char **strings,
                                    const uint8_t *lengths,
                                    size_t string_count,
                                    uint32_t ttl) {
    uint8_t rdata[PLANNED_RDATA_MAX];
    size_t off = 0;
    size_t i;

    if (string_count == 0) {
        uint8_t zero = 0;
        return planned_rr_add_raw(set, routes, owner, DNS_TYPE_TXT, DNS_CLASS_IN_UNIQUE, ttl, &zero, 1);
    }
    for (i = 0; i < string_count; i++) {
        size_t slen = lengths != NULL ? lengths[i] : strlen(strings[i]);
        uint8_t len;
        if (slen > 255) {
            return -1;
        }
        len = (uint8_t)slen;
        if (append_bytes(rdata, &off, sizeof(rdata), &len, 1) != 0 ||
            append_bytes(rdata, &off, sizeof(rdata), strings[i], slen) != 0) {
            return -1;
        }
    }
    return planned_rr_add_raw(set, routes, owner, DNS_TYPE_TXT, DNS_CLASS_IN_UNIQUE, ttl, rdata, (uint16_t)off);
}

int planned_rr_add_txt_empty(struct planned_rr_set *set, int routes, const char *owner, uint32_t ttl) {
    return planned_rr_add_txt_items(set, routes, owner, NULL, NULL, 0, ttl);
}

TC_LOCAL int planned_rr_add_a(struct planned_rr_set *set, int routes, const char *owner, uint32_t ipv4_addr, uint32_t ttl) {
    return planned_rr_add_raw(set, routes, owner, DNS_TYPE_A, DNS_CLASS_IN_UNIQUE, ttl,
                              (const uint8_t *)&ipv4_addr, 4);
}

TC_LOCAL int planned_rr_add_aaaa(struct planned_rr_set *set,
                               int routes,
                               const char *owner,
                               const struct in6_addr *ipv6_addr,
                               uint32_t ttl) {
    return planned_rr_add_raw(set, routes, owner, DNS_TYPE_AAAA, DNS_CLASS_IN_UNIQUE, ttl,
                              ipv6_addr->s6_addr, 16);
}

int planned_rr_add_link_addresses(struct planned_rr_set *set,
                                         int routes,
                                         const char *owner,
                                         const struct link_context *link,
                                         int include_a,
                                         int include_aaaa,
                                         uint32_t ttl) {
    size_t i;

    if (owner == NULL || owner[0] == '\0' || link == NULL) {
        return 0;
    }
    if (include_a) {
        for (i = 0; i < link->ipv4_count; i++) {
            if (planned_rr_add_a(set, routes, owner, link->ipv4[i].addr, ttl) != 0) {
                return -1;
            }
        }
    }
    if (include_aaaa) {
        for (i = 0; i < link->ipv6_count; i++) {
            struct in6_addr canonical;
            if (!link_ipv6_addr_is_samba_bindable(&link->ipv6[i])) {
                continue;
            }
            if (link->ipv6[i].link_local && link->ifindex == 0) {
                continue;
            }
            ipv6_canonicalize_scoped_address(&canonical, &link->ipv6[i].addr);
            if (planned_rr_add_aaaa(set, routes, owner, &canonical, ttl) != 0) {
                return -1;
            }
        }
    }
    return 0;
}

TC_LOCAL int planned_set_has_route(const struct planned_rr_set *set, int route) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if ((set->records[i].routes & route) != 0) {
            return 1;
        }
    }
    return 0;
}

TC_LOCAL int planned_set_has_any_route(const struct planned_rr_set *set) {
    size_t i;

    for (i = 0; i < set->count; i++) {
        if (set->records[i].routes != 0) {
            return 1;
        }
    }
    return 0;
}

TC_LOCAL int planned_rr_matches_known_answer(const struct planned_rr *rr,
                                           const char *owner,
                                           uint16_t type,
                                           uint16_t rrclass,
                                           const uint8_t *packet,
                                           size_t packet_len,
                                           size_t rdata_cursor,
                                           uint16_t rdlength) {
    if (rr->type != type ||
        (rr->rrclass & 0x7FFF) != (rrclass & 0x7FFF) ||
        !name_equals(rr->owner, owner)) {
        return 0;
    }
    if (type == DNS_TYPE_PTR) {
        char known_name[MAX_NAME];
        char planned_name[MAX_NAME];
        size_t rdata_end = rdata_cursor + rdlength;
        size_t planned_cursor = 0;
        if (decode_name(packet, packet_len, &rdata_cursor, known_name, sizeof(known_name)) != 0 ||
            decode_name(rr->rdata, rr->rdlength, &planned_cursor, planned_name, sizeof(planned_name)) != 0) {
            return 0;
        }
        if (rdata_cursor != rdata_end || planned_cursor != rr->rdlength) {
            return 0;
        }
        return name_equals(known_name, planned_name);
    }
    if (type == DNS_TYPE_SRV) {
        char known_target[MAX_NAME];
        char planned_target[MAX_NAME];
        size_t rdata_end = rdata_cursor + rdlength;
        size_t known_cursor = rdata_cursor + 6;
        size_t planned_cursor = 6;
        if (rdlength < 6 || rr->rdlength < 6 || rdata_cursor + rdlength > packet_len ||
            memcmp(packet + rdata_cursor, rr->rdata, 6) != 0 ||
            decode_name(packet, packet_len, &known_cursor, known_target, sizeof(known_target)) != 0 ||
            decode_name(rr->rdata, rr->rdlength, &planned_cursor, planned_target, sizeof(planned_target)) != 0) {
            return 0;
        }
        if (known_cursor != rdata_end || planned_cursor != rr->rdlength) {
            return 0;
        }
        return name_equals(known_target, planned_target);
    }
    if (rdata_cursor + rdlength > packet_len) {
        return 0;
    }
    if (rr->rdlength != rdlength) {
        return 0;
    }
    return memcmp(packet + rdata_cursor, rr->rdata, rdlength) == 0;
}

TC_LOCAL void suppress_planned_known_answers(const uint8_t *packet,
                                           size_t packet_len,
                                           size_t cursor,
                                           uint16_t answer_count,
                                           struct planned_rr_set *planned) {
    uint16_t i;

    for (i = 0; i < answer_count; i++) {
        char owner[MAX_NAME];
        uint16_t type;
        uint16_t rrclass;
        uint32_t ttl;
        uint16_t rdlength;
        size_t rdata_cursor;
        size_t j;

        if (decode_name(packet, packet_len, &cursor, owner, sizeof(owner)) != 0 || cursor + 10 > packet_len) {
            return;
        }
        memcpy(&type, packet + cursor, 2);
        memcpy(&rrclass, packet + cursor + 2, 2);
        memcpy(&ttl, packet + cursor + 4, 4);
        memcpy(&rdlength, packet + cursor + 8, 2);
        cursor += 10;
        type = ntohs(type);
        rrclass = ntohs(rrclass);
        ttl = ntohl(ttl);
        rdlength = ntohs(rdlength);
        if (cursor + rdlength > packet_len) {
            return;
        }
        rdata_cursor = cursor;
        cursor += rdlength;
        if ((rrclass & 0x7FFF) != DNS_CLASS_IN) {
            continue;
        }
        for (j = 0; j < planned->count; j++) {
            if (planned->records[j].routes == 0 ||
                !known_answer_ttl_is_fresh(ttl, planned->records[j].ttl)) {
                continue;
            }
            if (planned_rr_matches_known_answer(&planned->records[j],
                                                owner,
                                                type,
                                                rrclass,
                                                packet,
                                                packet_len,
                                                rdata_cursor,
                                                rdlength)) {
                planned->records[j].routes = 0;
            }
        }
    }
}

TC_LOCAL uint16_t sockaddr_port_host(const struct sockaddr *addr) {
    if (addr == NULL) {
        return 0;
    }
    if (addr->sa_family == AF_INET) {
        const struct sockaddr_in *sin = (const struct sockaddr_in *)addr;
        return ntohs(sin->sin_port);
    }
    if (addr->sa_family == AF_INET6) {
        const struct sockaddr_in6 *sin6 = (const struct sockaddr_in6 *)addr;
        return ntohs(sin6->sin6_port);
    }
    return 0;
}

TC_LOCAL int source_can_receive_unicast_response(const struct sockaddr *source,
                                               const struct link_context *response_link,
                                               unsigned int ingress_ifindex) {
    if (source == NULL || response_link == NULL) {
        return 0;
    }
    if (source->sa_family == AF_INET) {
        const struct sockaddr_in *sin = (const struct sockaddr_in *)source;
        return source_matches_link_ipv4_subnet(sin->sin_addr.s_addr, response_link);
    }
    if (source->sa_family == AF_INET6) {
        const struct sockaddr_in6 *sin6 = (const struct sockaddr_in6 *)source;
        unsigned int source_ifindex = ingress_ifindex != 0
                                          ? ingress_ifindex
                                          : ipv6_sockaddr_effective_ifindex(sin6);
        size_t i;

        if (source_ifindex != 0 && source_ifindex == response_link->ifindex) {
            return 1;
        }
        for (i = 0; i < response_link->ipv6_count; i++) {
            if (response_link->ipv6[i].link_local) {
                continue;
            }
            if (ipv6_prefix_matches(&sin6->sin6_addr,
                                    &response_link->ipv6[i].addr,
                                    response_link->ipv6[i].prefix_len)) {
                return 1;
            }
        }
    }
    return 0;
}

TC_LOCAL int plan_question_answers(struct planned_rr_set *planned,
                                 int route,
                                 const char *qname,
                                 uint16_t qtype,
                                 const struct config *cfg,
                                 const struct link_context *response_link,
                                 const char *instance_fqdn,
                                 const char *afp_instance_fqdn,
                                 const char *adisk_instance_fqdn,
                                 const char *device_info_instance_fqdn,
                                 const char *airport_instance_fqdn,
                                 const char *riousbprint_instance_fqdn,
                                 const char *pdl_datastream_instance_fqdn) {
    int planned_generated_apple_service_type = 0;

    if (name_equals(qname, DNS_SD_SERVICE_ENUMERATION_NAME) &&
        (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
        return plan_service_type_enumeration_records(planned, route, cfg);
    }
    if (smb_enabled(cfg) && name_equals(qname, cfg->service_type) &&
        (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
        return plan_smb_records(planned, route, cfg, instance_fqdn, response_link, 1, 1, 1, 1, 1);
    }
    if (afp_enabled(cfg) && name_equals(qname, cfg->afp_service_type) &&
        (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
        return plan_afp_records(planned, route, cfg, afp_instance_fqdn, response_link, 1, 1, 1, 1, 1);
    }
    if (adisk_enabled(cfg) && name_equals(qname, cfg->adisk_service_type) &&
        (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
        return plan_adisk_records(planned, route, cfg, adisk_instance_fqdn, response_link, 1, 1, 1, 1, 1);
    }
    if (cfg->device_model[0] != '\0' && name_equals(qname, cfg->device_info_service_type) &&
        (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
        return plan_device_info_records(planned, route, cfg, device_info_instance_fqdn, response_link, 1, 1, 1, 1, 1);
    }
    if (is_airport_enabled(cfg) && name_equals(qname, cfg->airport_service_type) &&
        (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
        if (plan_airport_records(planned, route, cfg, airport_instance_fqdn, response_link, 1, 1, 1, 1, 1) != 0) {
            return -1;
        }
        planned_generated_apple_service_type = 1;
    }
    if (is_riousbprint_enabled(cfg) && name_equals(qname, RIOUSBPRINT_SERVICE_TYPE) &&
        (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
        if (plan_riousbprint_records(planned, route, cfg, riousbprint_instance_fqdn, response_link, 1, 1, 1, 1, 1) != 0) {
            return -1;
        }
        planned_generated_apple_service_type = 1;
    }
    if (is_pdl_datastream_enabled(cfg) && name_equals(qname, PDL_DATASTREAM_SERVICE_TYPE) &&
        (qtype == DNS_TYPE_PTR || qtype == DNS_TYPE_ANY)) {
        if (plan_pdl_datastream_records(planned, route, cfg, pdl_datastream_instance_fqdn, response_link, 1, 1, 1, 1, 1) != 0) {
            return -1;
        }
        planned_generated_apple_service_type = 1;
    }
    if (planned_generated_apple_service_type) {
        return 0;
    }
    if (smb_enabled(cfg) && name_equals(qname, instance_fqdn)) {
        return plan_smb_records(planned, route, cfg, instance_fqdn, response_link,
                                0,
                                qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY,
                                qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY);
    }
    if (afp_enabled(cfg) && name_equals(qname, afp_instance_fqdn)) {
        return plan_afp_records(planned, route, cfg, afp_instance_fqdn, response_link,
                                0,
                                qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY,
                                qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY);
    }
    if (adisk_enabled(cfg) && name_equals(qname, adisk_instance_fqdn)) {
        return plan_adisk_records(planned, route, cfg, adisk_instance_fqdn, response_link,
                                  0,
                                  qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                  qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY,
                                  qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                  qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY);
    }
    if (cfg->device_model[0] != '\0' && name_equals(qname, device_info_instance_fqdn)) {
        return plan_device_info_records(planned, route, cfg, device_info_instance_fqdn, response_link,
                                        0,
                                        qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                        qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY,
                                        qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                        qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY);
    }
    if (is_airport_enabled(cfg) && name_equals(qname, airport_instance_fqdn)) {
        return plan_airport_records(planned, route, cfg, airport_instance_fqdn, response_link,
                                    0,
                                    qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                    qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY,
                                    qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                    qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY);
    }
    if (is_riousbprint_enabled(cfg) && name_equals(qname, riousbprint_instance_fqdn)) {
        return plan_riousbprint_records(planned, route, cfg, riousbprint_instance_fqdn, response_link,
                                        0,
                                        qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                        qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY,
                                        qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                        qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY);
    }
    if (is_pdl_datastream_enabled(cfg) && name_equals(qname, pdl_datastream_instance_fqdn)) {
        return plan_pdl_datastream_records(planned, route, cfg, pdl_datastream_instance_fqdn, response_link,
                                           0,
                                           qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                           qtype == DNS_TYPE_TXT || qtype == DNS_TYPE_ANY,
                                           qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY,
                                           qtype == DNS_TYPE_SRV || qtype == DNS_TYPE_ANY);
    }
    if (name_equals(qname, cfg->host_fqdn)) {
        return planned_rr_add_link_addresses(planned,
                                             route,
                                             cfg->host_fqdn,
                                             response_link,
                                             qtype == DNS_TYPE_A || qtype == DNS_TYPE_ANY,
                                             qtype == DNS_TYPE_AAAA || qtype == DNS_TYPE_ANY,
                                             cfg->ttl);
    }
    return 0;
}

TC_LOCAL int add_planned_rr_to_packet(uint8_t *reply,
                                    size_t *off,
                                    size_t reply_cap,
                                    const struct planned_rr *rr,
                                    int legacy_unicast) {
    uint16_t rrclass = rr->rrclass;
    uint32_t ttl = rr->ttl;

    if (legacy_unicast) {
        rrclass = (uint16_t)(rrclass & 0x7FFF);
        if (ttl > LEGACY_UNICAST_TTL_MAX) {
            ttl = LEGACY_UNICAST_TTL_MAX;
        }
    }
    return encode_name(reply, off, reply_cap, rr->owner) != 0 ||
           append_u16(reply, off, reply_cap, rr->type) != 0 ||
           append_u16(reply, off, reply_cap, rrclass) != 0 ||
           append_u32(reply, off, reply_cap, ttl) != 0 ||
           append_u16(reply, off, reply_cap, rr->rdlength) != 0 ||
           append_bytes(reply, off, reply_cap, rr->rdata, rr->rdlength) != 0
               ? -1
               : 0;
}

TC_LOCAL int build_planned_response_packet(uint8_t *reply,
                                         size_t reply_cap,
                                         size_t *reply_len,
                                         int *answer_count,
                                         uint16_t response_id,
                                         int route,
                                         int legacy_unicast,
                                         const struct response_question_section *questions,
                                         const struct planned_rr_set *planned) {
    struct dns_header hdr;
    size_t off = sizeof(struct dns_header);
    int answers = 0;
    size_t i;

    memset(&hdr, 0, sizeof(hdr));
    hdr.id = response_id;
    hdr.flags = htons(DNS_FLAG_QR | DNS_FLAG_AA);
    if (legacy_unicast && questions != NULL && questions->count > 0) {
        if (questions->bytes == NULL || questions->len == 0 ||
            append_bytes(reply, &off, reply_cap, questions->bytes, questions->len) != 0) {
            return -1;
        }
        hdr.qdcount = htons(questions->count);
    }
    for (i = 0; i < planned->count; i++) {
        if ((planned->records[i].routes & route) == 0) {
            continue;
        }
        if (add_planned_rr_to_packet(reply, &off, reply_cap, &planned->records[i], legacy_unicast) != 0) {
            return -1;
        }
        answers++;
    }
    hdr.ancount = htons((uint16_t)answers);
    memcpy(reply, &hdr, sizeof(hdr));
    *reply_len = off;
    *answer_count = answers;
    return 0;
}

TC_LOCAL void stored_question_section_as_response(const struct stored_question_section *stored,
                                                struct response_question_section *out) {
    out->bytes = stored->bytes;
    out->len = stored->len;
    out->count = stored->count;
}

TC_LOCAL int send_planned_response_route(int sockfd,
                                       const struct planned_rr_set *planned,
                                       int route,
                                       uint16_t response_id,
                                       const struct response_question_section *questions,
                                       const struct sockaddr *dest,
                                       socklen_t dest_len,
                                       int delay_multicast) {
    uint8_t reply[BUF_SIZE];
    size_t reply_len;
    int answers;
    int legacy_unicast = route == MDNS_REPLY_LEGACY_UNICAST;

    if (build_planned_response_packet(reply,
                                      sizeof(reply),
                                      &reply_len,
                                      &answers,
                                      response_id,
                                      route,
                                      legacy_unicast,
                                      questions,
                                      planned) != 0) {
        return -1;
    }
    if (answers <= 0) {
        return 0;
    }
    if (delay_multicast && route == MDNS_REPLY_MULTICAST) {
        delay_multicast_query_response();
    }
    return send_dns_packet_any("query_response",
                               sockfd,
                               reply,
                               reply_len,
                               dest,
                               dest_len,
                               answers);
}

TC_LOCAL void clear_deferred_response(void) {
    memset(&g_deferred_response, 0, sizeof(g_deferred_response));
}

void clear_deferred_response_for_sockfd(int sockfd) {
    if (g_deferred_response.active && g_deferred_response.sockfd == sockfd) {
        clear_deferred_response();
    }
}

TC_LOCAL int sockaddr_endpoint_equal(const struct sockaddr *a, socklen_t a_len,
                                   const struct sockaddr *b, socklen_t b_len) {
    if (a == NULL || b == NULL || a->sa_family != b->sa_family) {
        return 0;
    }
    if (a->sa_family == AF_INET) {
        const struct sockaddr_in *sin_a = (const struct sockaddr_in *)a;
        const struct sockaddr_in *sin_b = (const struct sockaddr_in *)b;
        if (a_len < (socklen_t)sizeof(*sin_a) || b_len < (socklen_t)sizeof(*sin_b)) {
            return 0;
        }
        return sin_a->sin_port == sin_b->sin_port &&
               sin_a->sin_addr.s_addr == sin_b->sin_addr.s_addr;
    }
    if (a->sa_family == AF_INET6) {
        const struct sockaddr_in6 *sin6_a = (const struct sockaddr_in6 *)a;
        const struct sockaddr_in6 *sin6_b = (const struct sockaddr_in6 *)b;
        if (a_len < (socklen_t)sizeof(*sin6_a) || b_len < (socklen_t)sizeof(*sin6_b)) {
            return 0;
        }
        return sin6_a->sin6_port == sin6_b->sin6_port &&
               sin6_a->sin6_scope_id == sin6_b->sin6_scope_id &&
               memcmp(&sin6_a->sin6_addr, &sin6_b->sin6_addr, sizeof(sin6_a->sin6_addr)) == 0;
    }
    return 0;
}

TC_LOCAL int deferred_response_matches_source(int sockfd, const struct sockaddr *source, socklen_t source_len) {
    if (!g_deferred_response.active || g_deferred_response.sockfd != sockfd) {
        return 0;
    }
    return sockaddr_endpoint_equal((const struct sockaddr *)&g_deferred_response.source,
                                   g_deferred_response.source_len,
                                   source,
                                   source_len);
}

TC_LOCAL int copy_sockaddr_storage(struct sockaddr_storage *out,
                                 socklen_t *out_len,
                                 const struct sockaddr *src,
                                 socklen_t src_len) {
    if (src == NULL || src_len > (socklen_t)sizeof(*out)) {
        return -1;
    }
    memset(out, 0, sizeof(*out));
    memcpy(out, src, src_len);
    *out_len = src_len;
    return 0;
}

TC_LOCAL int flush_deferred_response_now(void) {
    int status = 0;
    struct response_question_section questions;

    if (!g_deferred_response.active) {
        return 0;
    }
    stored_question_section_as_response(&g_deferred_response.questions, &questions);
    if (planned_set_has_route(&g_deferred_response.planned, MDNS_REPLY_LEGACY_UNICAST)) {
        if (send_planned_response_route(g_deferred_response.sockfd,
                                        &g_deferred_response.planned,
                                        MDNS_REPLY_LEGACY_UNICAST,
                                        g_deferred_response.response_id,
                                        &questions,
                                        (const struct sockaddr *)&g_deferred_response.source,
                                        g_deferred_response.source_len,
                                        0) != 0) {
            status = -1;
        }
    }
    if (planned_set_has_route(&g_deferred_response.planned, MDNS_REPLY_UNICAST)) {
        if (send_planned_response_route(g_deferred_response.sockfd,
                                        &g_deferred_response.planned,
                                        MDNS_REPLY_UNICAST,
                                        g_deferred_response.response_id,
                                        &questions,
                                        (const struct sockaddr *)&g_deferred_response.source,
                                        g_deferred_response.source_len,
                                        0) != 0) {
            status = -1;
        }
    }
    if (planned_set_has_route(&g_deferred_response.planned, MDNS_REPLY_MULTICAST)) {
        if (send_planned_response_route(g_deferred_response.sockfd,
                                        &g_deferred_response.planned,
                                        MDNS_REPLY_MULTICAST,
                                        0,
                                        &questions,
                                        (const struct sockaddr *)&g_deferred_response.multicast_dest,
                                        g_deferred_response.multicast_dest_len,
                                        0) != 0) {
            status = -1;
        }
    }
    clear_deferred_response();
    return status;
}

int flush_deferred_response_if_due(long long now_ms) {
    if (!g_deferred_response.active || now_ms < g_deferred_response.due_ms) {
        return 0;
    }
    return flush_deferred_response_now();
}

long long deferred_response_adjust_wait_ms(long long now_ms, long long wait_ms) {
    long long deferred_wait;

    if (!g_deferred_response.active) {
        return wait_ms;
    }
    deferred_wait = g_deferred_response.due_ms - now_ms;
    if (deferred_wait < 0) {
        deferred_wait = 0;
    }
    return deferred_wait < wait_ms ? deferred_wait : wait_ms;
}

TC_LOCAL int defer_planned_response(int sockfd,
                                  uint16_t response_id,
                                  const struct sockaddr *multicast_dest,
                                  socklen_t multicast_dest_len,
                                  const struct sockaddr *source,
                                  socklen_t source_len,
                                  const struct response_question_section *questions,
                                  const struct planned_rr_set *planned) {
    if (!planned_set_has_any_route(planned)) {
        clear_deferred_response();
        return 0;
    }
    clear_deferred_response();
    g_deferred_response.active = 1;
    g_deferred_response.sockfd = sockfd;
    g_deferred_response.due_ms = monotonic_millis() + TC_KNOWN_ANSWER_DEFER_MS;
    g_deferred_response.response_id = response_id;
    g_deferred_response.planned = *planned;
    if (questions != NULL && questions->count > 0) {
        if (questions->bytes == NULL || questions->len > sizeof(g_deferred_response.questions.bytes)) {
            clear_deferred_response();
            return -1;
        }
        memcpy(g_deferred_response.questions.bytes, questions->bytes, questions->len);
        g_deferred_response.questions.len = questions->len;
        g_deferred_response.questions.count = questions->count;
    }
    if (copy_sockaddr_storage(&g_deferred_response.multicast_dest,
                              &g_deferred_response.multicast_dest_len,
                              multicast_dest,
                              multicast_dest_len) != 0 ||
        copy_sockaddr_storage(&g_deferred_response.source,
                              &g_deferred_response.source_len,
                              source,
                              source_len) != 0) {
        clear_deferred_response();
        return -1;
    }
    return 0;
}

int handle_query_any_scoped(int sockfd,
                                   const uint8_t *packet,
                                   size_t packet_len,
                                   const struct sockaddr *multicast_dest,
                                   socklen_t multicast_dest_len,
                                   const struct sockaddr *source,
                                   socklen_t source_len,
                                   unsigned int ingress_ifindex,
                                   const struct config *cfg,
                                   const struct link_context *response_link,
                                   enum mdns_service_scope scope) {
    struct dns_header hdr;
    size_t cursor = sizeof(struct dns_header);
    uint16_t qdcount;
    uint16_t ancount;
    uint16_t query_id;
    uint16_t flags;
    char instance_fqdn[MAX_NAME];
    char afp_instance_fqdn[MAX_NAME];
    char adisk_instance_fqdn[MAX_NAME];
    char device_info_instance_fqdn[MAX_NAME];
    char airport_instance_fqdn[MAX_NAME];
    char riousbprint_instance_fqdn[MAX_NAME];
    char pdl_datastream_instance_fqdn[MAX_NAME];
    uint16_t i;
    int status = 0;
    int source_port;
    int legacy_unicast_query;
    int source_allows_unicast;
    size_t question_section_start = sizeof(struct dns_header);
    struct response_question_section questions;
    struct config scoped_cfg;
    static struct planned_rr_set planned;

    cfg = mdns_config_for_scope(&scoped_cfg, cfg, scope);
    memset(&planned, 0, sizeof(planned));
    memset(&questions, 0, sizeof(questions));
    instance_fqdn[0] = '\0';
    afp_instance_fqdn[0] = '\0';
    adisk_instance_fqdn[0] = '\0';
    device_info_instance_fqdn[0] = '\0';
    airport_instance_fqdn[0] = '\0';
    riousbprint_instance_fqdn[0] = '\0';
    pdl_datastream_instance_fqdn[0] = '\0';

    if (packet_len < sizeof(struct dns_header)) {
        return 0;
    }
    memcpy(&hdr, packet, sizeof(hdr));
    flags = ntohs(hdr.flags);
    if (flags & DNS_FLAG_QR) {
        return 0;
    }

    qdcount = ntohs(hdr.qdcount);
    ancount = ntohs(hdr.ancount);
    query_id = hdr.id;
    source_port = sockaddr_port_host(source);
    legacy_unicast_query = source_port != 0 && source_port != MDNS_PORT;
    source_allows_unicast = source_can_receive_unicast_response(source, response_link, ingress_ifindex);
    if (smb_enabled(cfg) &&
        build_instance_fqdn(instance_fqdn, sizeof(instance_fqdn), cfg->instance_name, cfg->service_type) != 0) {
        log_packet_build_failure("query_response", "build_instance_fqdn", sizeof(struct dns_header), 0);
        return 0;
    }
    if (afp_enabled(cfg) &&
        build_instance_fqdn(afp_instance_fqdn, sizeof(afp_instance_fqdn), cfg->instance_name, cfg->afp_service_type) != 0) {
        log_packet_build_failure("query_response", "build_afp_instance_fqdn", sizeof(struct dns_header), 0);
        return 0;
    }
    if (adisk_enabled(cfg) &&
        build_instance_fqdn(adisk_instance_fqdn, sizeof(adisk_instance_fqdn), cfg->instance_name, cfg->adisk_service_type) != 0) {
        log_packet_build_failure("query_response", "build_adisk_instance_fqdn", sizeof(struct dns_header), 0);
        return 0;
    }
    if (cfg->device_model[0] != '\0' &&
        build_instance_fqdn(device_info_instance_fqdn, sizeof(device_info_instance_fqdn), cfg->instance_name, cfg->device_info_service_type) != 0) {
        log_packet_build_failure("query_response", "build_device_info_instance_fqdn", sizeof(struct dns_header), 0);
        return 0;
    }
    if (is_airport_enabled(cfg) &&
        build_instance_fqdn(airport_instance_fqdn, sizeof(airport_instance_fqdn), cfg->instance_name, cfg->airport_service_type) != 0) {
        log_packet_build_failure("query_response", "build_airport_instance_fqdn", sizeof(struct dns_header), 0);
        return 0;
    }
    if (is_riousbprint_enabled(cfg) &&
        build_instance_fqdn(riousbprint_instance_fqdn, sizeof(riousbprint_instance_fqdn), cfg->riousbprint_instance_name, RIOUSBPRINT_SERVICE_TYPE) != 0) {
        log_packet_build_failure("query_response", "build_riousbprint_instance_fqdn", sizeof(struct dns_header), 0);
        return 0;
    }
    if (is_pdl_datastream_enabled(cfg) &&
        build_instance_fqdn(pdl_datastream_instance_fqdn, sizeof(pdl_datastream_instance_fqdn), cfg->riousbprint_instance_name, PDL_DATASTREAM_SERVICE_TYPE) != 0) {
        log_packet_build_failure("query_response", "build_pdl_datastream_instance_fqdn", sizeof(struct dns_header), 0);
        return 0;
    }

    if (qdcount == 0) {
        if (deferred_response_matches_source(sockfd, source, source_len)) {
            suppress_planned_known_answers(packet, packet_len, cursor, ancount, &g_deferred_response.planned);
            if ((flags & DNS_FLAG_TC) == 0) {
                return flush_deferred_response_now();
            }
        }
        return 0;
    }

    if (deferred_response_matches_source(sockfd, source, source_len) && (flags & DNS_FLAG_TC) == 0) {
        clear_deferred_response();
    }

    for (i = 0; i < qdcount; i++) {
        char qname[MAX_NAME];
        uint16_t qtype;
        uint16_t qclass;
        uint16_t qclass_raw;
        uint16_t qclass_base;
        int reply_route;

        if (decode_name(packet, packet_len, &cursor, qname, sizeof(qname)) != 0 ||
            cursor + 4 > packet_len) {
            return 0;
        }
        memcpy(&qtype, packet + cursor, 2);
        memcpy(&qclass, packet + cursor + 2, 2);
        cursor += 4;
        qtype = ntohs(qtype);
        qclass_raw = ntohs(qclass);
        qclass_base = (uint16_t)(qclass_raw & 0x7FFF);
        if (qclass_base != DNS_CLASS_IN && qclass_base != DNS_CLASS_ANY) {
            continue;
        }
        if (legacy_unicast_query && source_allows_unicast) {
            reply_route = MDNS_REPLY_LEGACY_UNICAST;
        } else if ((qclass_raw & DNS_CLASS_QU) && source_allows_unicast) {
            reply_route = MDNS_REPLY_UNICAST;
            if (source_port == MDNS_PORT) {
                reply_route |= MDNS_REPLY_MULTICAST;
            }
        } else {
            reply_route = MDNS_REPLY_MULTICAST;
        }
        if (plan_question_answers(&planned,
                                  reply_route,
                                  qname,
                                  qtype,
                                  cfg,
                                  response_link,
                                  instance_fqdn,
                                  afp_instance_fqdn,
                                  adisk_instance_fqdn,
                                  device_info_instance_fqdn,
                                  airport_instance_fqdn,
                                  riousbprint_instance_fqdn,
                                  pdl_datastream_instance_fqdn) != 0) {
            log_packet_build_failure("query_response", "plan_question_answers", cursor, 0);
            return -1;
        }
    }
    questions.bytes = packet + question_section_start;
    questions.len = cursor - question_section_start;
    questions.count = qdcount;

    suppress_planned_known_answers(packet, packet_len, cursor, ancount, &planned);
    if (planned.count > 0) {
        g_mdns_counters.query_packets_matched++;
    }

    if (flags & DNS_FLAG_TC) {
        if (defer_planned_response(sockfd,
                                   query_id,
                                   multicast_dest,
                                   multicast_dest_len,
                                   source,
                                   source_len,
                                   &questions,
                                   &planned) != 0) {
            return -1;
        }
        return 0;
    }

    if (planned_set_has_route(&planned, MDNS_REPLY_LEGACY_UNICAST)) {
        if (send_planned_response_route(sockfd,
                                        &planned,
                                        MDNS_REPLY_LEGACY_UNICAST,
                                        query_id,
                                        &questions,
                                        source,
                                        source_len,
                                        0) != 0) {
            status = -1;
        }
    }

    if (planned_set_has_route(&planned, MDNS_REPLY_UNICAST)) {
        if (send_planned_response_route(sockfd,
                                        &planned,
                                        MDNS_REPLY_UNICAST,
                                        query_id,
                                        &questions,
                                        source,
                                        source_len,
                                        0) != 0) {
            status = -1;
        }
    }

    if (planned_set_has_route(&planned, MDNS_REPLY_MULTICAST)) {
        if (send_planned_response_route(sockfd,
                                        &planned,
                                        MDNS_REPLY_MULTICAST,
                                        0,
                                        &questions,
                                        multicast_dest,
                                        multicast_dest_len,
                                        1) != 0) {
            status = -1;
        }
    }

    return status;
}

int handle_query_scoped(int sockfd, const uint8_t *packet, size_t packet_len,
                               const struct sockaddr_in *multicast_dest, const struct sockaddr_in *source,
                               const struct config *cfg, const struct link_context *response_link,
                               enum mdns_service_scope scope) {
    return handle_query_any_scoped(sockfd,
                                   packet,
                                   packet_len,
                                   (const struct sockaddr *)multicast_dest,
                                   sizeof(*multicast_dest),
                                   (const struct sockaddr *)source,
                                   sizeof(*source),
                                   0,
                                   cfg,
                                   response_link,
                                   scope);
}

TC_LOCAL int TC_UNUSED handle_query(int sockfd, const uint8_t *packet, size_t packet_len,
                                  const struct sockaddr_in *multicast_dest, const struct sockaddr_in *source,
                                  const struct config *cfg, const struct link_context *response_link) {
    return handle_query_scoped(sockfd,
                               packet,
                               packet_len,
                               multicast_dest,
                               source,
                               cfg,
                               response_link,
                               MDNS_SERVICE_SCOPE_LAN);
}


struct deferred_response g_deferred_response;
