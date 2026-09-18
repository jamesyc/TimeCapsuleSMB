#include "service.h"

int service_collect_plan(struct device_plan *plan, const char *facts_file, struct device_plan *history) {
    struct plan_options options;
    memset(&options, 0, sizeof(options));
    int rc;
    const struct device_plan *previous = history != NULL && history->status.validated ? history : NULL;
#ifndef TC_NATIVE_TEST
    (void)facts_file;
#endif
    rc =
#ifdef TC_NATIVE_TEST
        facts_file != NULL ? device_plan_collect_from_file(plan, facts_file, previous, &options) :
#endif
        device_plan_collect(plan, previous, &options);
    if (rc == 0 && history != NULL) {
        if (plan->status.validated) *history = *plan;
        else device_plan_prune_history(history, plan);
    }
    return rc;
}

/* Line 1: Samba `interfaces =` tokens (B.4). Line 2: the retention status the
 * manager keys on (B.9). A standalone run has no validated history, so a
 * coherent read is `validated` and anything else is `incomplete`; the
 * manager keeps its own last validated projection across `incomplete`
 * runs. */
int print_smb_bind_interfaces(FILE *stream, const struct device_plan *plan) {
    char tokens[TC_BIND_TOKENS_MAX];

    if (device_plan_bind_tokens(plan, tokens, sizeof(tokens)) != 0) {
        return -1;
    }
    if (fprintf(stream, "%s\n", tokens) < 0) {
        return -1;
    }
    if (plan->status.validated) {
        fputs("status=validated\n", stream);
    } else if (plan->status.reason[0] != '\0') {
        fprintf(stream, "status=incomplete reason=%s\n", plan->status.reason);
    } else {
        fputs("status=incomplete reason=unknown\n", stream);
    }
    return ferror(stream) ? -1 : 0;
}

int print_link_plan(FILE *stream, const struct device_plan *plan) {
    device_plan_print(stream, plan);
    return ferror(stream) ? -1 : 0;
}

/* The manager holds only validated policy in a shell variable and sends it
 * through stdin. No facts/state file: names+indices prevent transferring a
 * grant to a recreated interface. Addresses come from today's kernel table.
 * Kernel-read failure is reported separately; the manager then keeps its
 * existing bind tokens as well as this unchanged policy summary. */
static int policy_number(const char *text, unsigned max, unsigned *out) {
    char *end;
    unsigned long value;
    if (text == NULL || *text < '0' || *text > '9') return -1;
    errno = 0;
    value = strtoul(text, &end, 10);
    if (errno || *end || value > max) return -1;
    *out = (unsigned)value;
    return 0;
}

int service_read_policy(FILE *stream, struct device_plan *history) {
    char line[128];
    unsigned mode, wan;
    memset(history, 0, sizeof(*history));
    if (fgets(line, sizeof(line), stream) == NULL) return -1;
    if (!strcmp(line, "policy none\n")) return fgetc(stream) == EOF && !ferror(stream) ? 0 : -1;
    if (strcmp(strtok(line, " \n") ? line : "", "policy") ||
        policy_number(strtok(NULL, " \n"), ROUTER_MODE_NAT, &mode) || mode == ROUTER_MODE_UNKNOWN ||
        policy_number(strtok(NULL, " \n"), 1, &wan) || strtok(NULL, " \n") != NULL) return -1;
    history->mode = (enum router_mode)mode;
    history->wan_disks_allowed = (int)wan;
    while (fgets(line, sizeof(line), stream) != NULL) {
        unsigned index, role;
        char *name;
        struct link_plan *link;
        if (history->link_count >= TC_MAX_LINKS || strchr(line, '\n') == NULL ||
            policy_number(strtok(line, " \n"), 65535, &index) || index == 0 ||
            policy_number(strtok(NULL, " \n"), LINK_ROLE_ISOLATED, &role)) return -1;
        name = strtok(NULL, " \n");
        if (name == NULL || strlen(name) >= IFNAMSIZ || strtok(NULL, " \n") != NULL ||
            device_plan_find_link(history, index) != NULL) return -1;
        link = &history->links[history->link_count++];
        link->link.index = index;
        if (strcmp(name, "-")) strcpy(link->link.name, name);
        link->role = (enum link_role)role;
    }
    if (ferror(stream)) return -1;
    history->status.validated = 1;
    return 0;
}

int service_print_policy(FILE *stream, const struct device_plan *history) {
    size_t i;
    if (!history->status.validated) {
        fputs("policy none\n", stream);
    } else {
        fprintf(stream, "policy %u %u\n", (unsigned)history->mode, (unsigned)history->wan_disks_allowed);
        for (i = 0; i < history->link_count; i++) {
            const struct link_plan *link = &history->links[i];
            fprintf(stream, "%u %u %s\n", link->link.index, (unsigned)link->role,
                    link->link.name[0] ? link->link.name : "-");
        }
    }
    return ferror(stream) ? -1 : 0;
}
