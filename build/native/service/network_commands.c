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

int print_link_plan(FILE *stream, const struct device_plan *plan) {
    device_plan_print(stream, plan);
    return ferror(stream) || fflush(stream) != 0 ? -1 : 0;
}
