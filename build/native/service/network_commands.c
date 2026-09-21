#include "service.h"

int service_collect_plan(struct device_plan *plan, const char *facts_file) {
    struct plan_options options;
    memset(&options, 0, sizeof(options));
#ifndef TC_NATIVE_TEST
    (void)facts_file;
#endif
    return
#ifdef TC_NATIVE_TEST
        facts_file != NULL ? device_plan_collect_from_file(plan, facts_file, NULL, &options) :
#endif
        device_plan_collect(plan, NULL, &options);
}

/* One-shot diagnostic output: Samba `interfaces =` tokens (B.4), then whether
 * this observation validated. The manager does not consume this command; its
 * plan loop owns retained history in memory. */
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
