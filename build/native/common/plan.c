#include "plan.h"

static void set_status(struct plan_status *status, int validated, const char *reason) {
    status->validated = validated;
    strncpy(status->reason, reason, sizeof(status->reason) - 1);
    status->reason[sizeof(status->reason) - 1] = '\0';
}

/* Group the interface table into links with their addresses. Every link
 * from RTM_IFINFO appears (unnamed ones included); addresses whose owner
 * index matches no link get a nameless `synthetic` link. The kernel never
 * reports an address for an interface it does not list, so that is a gap
 * in our IFINFO parsing, not a network state: the link still takes roles
 * by ownership like any other (the evidence is the address, never the
 * name) and is flagged so the gap is visible (review finding 9, C.11). */
static void build_links(struct device_plan *plan, const struct device_facts *facts) {
    size_t i;

    plan->link_count = 0;
    for (i = 0; i < facts->ifs.link_count && plan->link_count < TC_MAX_LINKS; i++) {
        struct link_plan *link = &plan->links[plan->link_count++];
        memset(link, 0, sizeof(*link));
        link->link = facts->ifs.links[i];
        link->role = LINK_ROLE_ISOLATED;
    }
    for (i = 0; i < facts->ifs.addr_count; i++) {
        const struct if_addr *addr = &facts->ifs.addrs[i];
        struct link_plan *link = (struct link_plan *)device_plan_find_link(plan, addr->owner_index);
        if (link == NULL) {
            if (plan->link_count >= TC_MAX_LINKS) {
                continue;
            }
            link = &plan->links[plan->link_count++];
            memset(link, 0, sizeof(*link));
            link->link.index = addr->owner_index;
            link->role = LINK_ROLE_ISOLATED;
            link->synthetic = 1;
        }
        if (link->addr_count < TC_MAX_ADDRS_PER_LINK) {
            link->addrs[link->addr_count++] = *addr;
        } else {
            plan->addrs_truncated = 1;
        }
    }
}

static const struct link_plan *unchanged_link(const struct device_plan *previous, const struct link_plan *link) {
    const struct link_plan *old;
    if (previous == NULL || !previous->status.validated) {
        return NULL;
    }
    old = device_plan_find_link(previous, link->link.index);
    if (old == NULL || strcmp(old->link.name, link->link.name) != 0) {
        return NULL;
    }
    return old;
}

int device_plan_build(struct device_plan *out, const struct device_facts *facts,
                      const struct device_plan *previous, const struct plan_options *options, long long now_ms) {
    const char *reason = "";
    int coherent;
    size_t i;
    int have_previous = previous != NULL && previous->status.validated;

    memset(out, 0, sizeof(*out));
    out->options = *options;
    out->config = facts->config;
    out->mode = router_mode_from_facts(facts);
    out->raNA = acp_bool(&facts->acp[ACP_KEY_raNA]);
    out->raDS = acp_bool(&facts->acp[ACP_KEY_raDS]);
    out->waNM = acp_bool(&facts->acp[ACP_KEY_waNM]);
    out->laIP = acp_ipv4(&facts->acp[ACP_KEY_laIP]);
    out->waIP = acp_ipv4(&facts->acp[ACP_KEY_waIP]);
    out->waLL = acp_ipv4(&facts->acp[ACP_KEY_waLL]);
    out->gnRo = acp_ipv4(&facts->acp[ACP_KEY_gnRo]);
    identity_derive(&out->id, facts);
    if (previous != NULL && previous->id.instance[0] != '\0') {
        /* Domain (c) is optional for validation, but an aborted read is not
         * a new name: keep the previous instance name / waMA rather than
         * fall back to the hostname and re-register under it (review). */
        if (facts->acp[ACP_KEY_syNm].status == ACP_ABORT) {
            memcpy(out->id.instance, previous->id.instance, sizeof(out->id.instance));
            out->id.retained = 1;
        }
        if (facts->acp[ACP_KEY_waMA].status == ACP_ABORT && previous->id.wama[0] != '\0') {
            memcpy(out->id.wama, previous->id.wama, sizeof(out->id.wama));
            out->id.retained = 1;
        }
    }
    build_links(out, facts);
    out->status.cold_start = !have_previous;

    coherent = topology_ownership_coherent(facts, &reason);
    if (coherent && out->addrs_truncated) {
        /* B.4: a bind set that cannot be represented is never published. */
        coherent = 0;
        reason = "addrs";
    }
    out->usbF = acp_u32(&facts->acp[ACP_KEY_usbF]);
    out->wan_disks_allowed = out->usbF.available && (out->usbF.value & 0x8) ? 1 : 0;
    if (coherent && out->mode == ROUTER_MODE_NAT && !out->usbF.available) {
        coherent = 0;
        reason = "usbF";
    }
    if (coherent) {
        topology_assign_roles(out, facts);
        policy_assign_masks(out, facts);
        set_status(&out->status, 1, "");
        out->validated_at_ms = now_ms;
    } else {
        set_status(&out->status, 0, reason);
        if (have_previous) {
            /* Failed kernel enumeration is not an empty network. Preserve
             * the previous addresses without adding any from partial data. */
            if (!facts->ifs_ok || facts->ifs.truncated || out->addrs_truncated) {
                memcpy(out->links, previous->links, sizeof(out->links));
                out->link_count = previous->link_count;
            }
            out->mode = previous->mode;
            out->wan_disks_allowed = previous->wan_disks_allowed;
            for (i = 0; i < out->link_count; i++) {
                struct link_plan *link = &out->links[i];
                const struct link_plan *old = unchanged_link(previous, link);
                link->role = old != NULL ? old->role : LINK_ROLE_ISOLATED;
                link->retained = old != NULL;
                link->mask = old != NULL && (old->role == LINK_ROLE_LAN ||
                    ((old->role == LINK_ROLE_WAN || old->role == LINK_ROLE_GUEST) &&
                     previous->mode == ROUTER_MODE_NAT && previous->wan_disks_allowed))
                    ? policy_lan_mask(facts, options) : 0;
            }
        }
        /* Without history, build_links left every role isolated and every
         * mask zero. Bridge names and partial ACP answers grant nothing. */
    }

    /* Rules 4/5: a role without a service address publishes nothing. */
    for (i = 0; i < out->link_count; i++) {
        if (!link_plan_has_service_address(&out->links[i])) {
            out->links[i].role = LINK_ROLE_ISOLATED;
            out->links[i].mask = 0;
        }
    }
    if (!out->status.validated && have_previous && now_ms >= previous->validated_at_ms) {
        out->status.stale_seconds = (unsigned long)((now_ms - previous->validated_at_ms) / 1000);
        out->validated_at_ms = previous->validated_at_ms;
    }
    return 0;
}

/* A complete observation that loses a link also retires its old grant.
 * Otherwise a later reused name/index could resurrect a stale permission.
 * The caller keeps the original validation time and raw policy inputs. */
void device_plan_prune_history(struct device_plan *previous, const struct device_plan *current) {
    size_t i, count = 0;
    for (i = 0; i < previous->link_count; i++) {
        const struct link_plan *live = device_plan_find_link(current, previous->links[i].link.index);
        if (live != NULL && !strcmp(live->link.name, previous->links[i].link.name)) {
            enum link_role role = previous->links[i].role;
            unsigned mask = previous->links[i].mask;
            /* Addresses are current kernel observations, not retained ACP
             * permission. A later kernel-read failure must not restore an
             * obsolete DHCP address from the original validated snapshot. */
            previous->links[count] = *live;
            previous->links[count].role = role;
            previous->links[count].mask = mask;
            count++;
        }
    }
    previous->link_count = count;
}

int device_plan_collect(struct device_plan *out, const struct device_plan *previous, const struct plan_options *options) {
    struct device_facts facts;
    collect_device_facts(&facts);
    return device_plan_build(out, &facts, previous, options, acp_monotonic_ms());
}

#ifdef TC_NATIVE_TEST
int device_plan_collect_from_file(struct device_plan *out, const char *facts_path,
                                  const struct device_plan *previous, const struct plan_options *options) {
    struct device_facts facts;
    FILE *fp = fopen(facts_path, "r");
    int rc;
    if (fp == NULL) {
        return -1;
    }
    rc = device_facts_parse_file(&facts, fp);
    (void)fclose(fp);
    if (rc != 0) {
        return -1;
    }
    return device_plan_build(out, &facts, previous, options, acp_monotonic_ms());
}

#endif

static void print_mask(FILE *stream, unsigned mask) {
    const char *sep = "";
    if (mask == 0) {
        fputs("none", stream);
        return;
    }
    if (mask & SVC_SMB) { fprintf(stream, "%ssmb", sep); sep = ","; }
    if (mask & SVC_AFP) { fprintf(stream, "%safp", sep); sep = ","; }
    if (mask & SVC_ADISK) { fprintf(stream, "%sadisk", sep); }
}

static void print_bool(FILE *stream, const char *name, struct acp_bool value) {
    if (value.available) {
        fprintf(stream, " %s=%d", name, value.value);
    } else {
        fprintf(stream, " %s=unavailable", name);
    }
}

static void print_ipv4(FILE *stream, const char *name, struct acp_ipv4 value) {
    if (value.available) {
        struct in_addr addr;
        char text[INET_ADDRSTRLEN];
        addr.s_addr = value.addr;
        fprintf(stream, " %s=%s", name, inet_ntop(AF_INET, &addr, text, sizeof(text)) ? text : "invalid");
    } else {
        fprintf(stream, " %s=unavailable", name);
    }
}

/* C.2 wire format: a quoted string escapes `"` and `\` with a backslash
 * (the shlex/POSIX double-quote grammar the doctor parses with), so every
 * accepted instance name round-trips (review 2, R9). */
static void print_quoted_body(FILE *stream, const char *text) {
    for (; *text; text++) {
        if (*text == '"' || *text == '\\') {
            fputc('\\', stream);
        }
        fputc(*text, stream);
    }
}

/* One fact per line, stable and machine-readable; doctor and tests parse it. */
void device_plan_print(FILE *stream, const struct device_plan *plan) {
    size_t i, j;
    char text[INET6_ADDRSTRLEN];
    char tokens[TC_BIND_TOKENS_MAX];

    fprintf(stream, "plan: status=%s", plan->status.validated ? "validated" : plan->status.cold_start ? "cold-start" : "incomplete");
    if (!plan->status.validated && plan->status.reason[0] != '\0') {
        fprintf(stream, " reason=%s", plan->status.reason);
    }
    fprintf(stream, " mode=%s stale_seconds=%lu diskless=%d\n", router_mode_name(plan->mode), plan->status.stale_seconds, plan->options.diskless);
    fputs("acp:", stream);
    print_bool(stream, "raNA", plan->raNA);
    print_bool(stream, "raDS", plan->raDS);
    print_bool(stream, "waNM", plan->waNM);
    if (plan->usbF.available) {
        fprintf(stream, " usbF=0x%x", plan->usbF.value);
    } else {
        fputs(" usbF=unavailable", stream);
    }
    print_ipv4(stream, "laIP", plan->laIP);
    print_ipv4(stream, "waIP", plan->waIP);
    print_ipv4(stream, "waLL", plan->waLL);
    print_ipv4(stream, "gnRo", plan->gnRo);
    fputc('\n', stream);
    fputs("identity: instance=\"", stream);
    print_quoted_body(stream, plan->id.instance);
    fprintf(stream, "\" netbios=%s wama=%s%s\n", plan->id.netbios,
            plan->id.wama[0] ? plan->id.wama : "unavailable", plan->id.retained ? " retained=1" : "");
    for (i = 0; i < plan->link_count; i++) {
        const struct link_plan *link = &plan->links[i];
        fprintf(stream, "link: name=%s index=%u role=%s mask=", link->link.name, link->link.index, link_role_name(link->role));
        print_mask(stream, link->mask);
        if (link->retained) {
            fputs(" retained=1", stream);
        }
        if (link->synthetic) {
            fputs(" synthetic=1", stream);
        }
        fputc('\n', stream);
        for (j = 0; j < link->addr_count; j++) {
            const struct if_addr *addr = &link->addrs[j];
            if (addr->family == AF_INET6) {
                fprintf(stream, "addr: link=%u family=inet6 addr=%s scope=%u prefix=%u\n", link->link.index,
                        addr_text(addr, text, sizeof(text)), addr->scope, addr->prefix);
            } else {
                fprintf(stream, "addr: link=%u family=inet addr=%s prefix=%u\n", link->link.index,
                        addr_text(addr, text, sizeof(text)), addr->prefix);
            }
        }
    }
    if (device_plan_bind_tokens(plan, tokens, sizeof(tokens)) == 0) {
        fprintf(stream, "bind: %s\n", tokens);
    } else {
        fputs("bind: overflow\n", stream);
    }
}

/* B.4: on every link whose mask has SVC_SMB, 127.0.0.1/8 ::1/128 plus every
 * service address (IPv4 with prefix incl. 169.254, fe80 embedded-scope,
 * GUA/ULA with prefix). Links without SVC_SMB contribute nothing. */
int device_plan_bind_tokens(const struct device_plan *plan, char *out, size_t out_len) {
    size_t used;
    size_t i, j;

    if (out_len < 24) {
        return -1;
    }
    strcpy(out, "127.0.0.1/8 ::1/128");
    used = strlen(out);
    for (i = 0; i < plan->link_count; i++) {
        const struct link_plan *link = &plan->links[i];
        if (!(link->mask & SVC_SMB)) {
            continue;
        }
        for (j = 0; j < link->addr_count; j++) {
            char token[INET6_ADDRSTRLEN + 8];
            size_t token_len;
            const struct if_addr *addr = &link->addrs[j];
            if (!addr_is_service_address(addr)) {
                continue;
            }
            if (addr->family == AF_INET ? bind_token_ipv4(token, sizeof(token), addr) != 0
                                        : bind_token_ipv6(token, sizeof(token), addr) != 0) {
                continue;
            }
            token_len = strlen(token);
            if (used + 1 + token_len >= out_len) {
                return -1;
            }
            out[used++] = ' ';
            memcpy(out + used, token, token_len + 1);
            used += token_len;
        }
    }
    return 0;
}
