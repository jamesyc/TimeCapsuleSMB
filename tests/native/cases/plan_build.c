/* Builds device plans from facts files through the same pure function the
 * helpers use, optionally chaining plans to exercise retention (9.4 B):
 *   plan_build [--diskless] <facts-1> [<facts-2> ...]
 * Every facts file is built with the last *validated* plan as `previous`
 * (exactly what the daemons do) and printed; the clock advances 10 s per
 * step so stale ages are observable. */
#include "common/plan.h"

int main(int argc, char **argv) {
    struct device_plan plans[8];
    struct plan_options options;
    struct device_plan *previous = NULL;
    int i, step = 0;
    long long now = 100000;

    memset(&options, 0, sizeof(options));
    for (i = 1; i < argc; i++) {
        struct device_facts facts;
        FILE *fp;
        if (!strcmp(argv[i], "--diskless")) { options.diskless = 1; continue; }
        if (step >= 8) return 2;
        fp = fopen(argv[i], "r");
        if (fp == NULL || device_facts_parse_file(&facts, fp) != 0) { printf("step %d: facts parse error\n", step); return 1; }
        fclose(fp);
        device_plan_build(&plans[step], &facts, previous, &options, now);
        printf("== step %d\n", step);
        device_plan_print(stdout, &plans[step]);
        if (plans[step].status.validated) previous = &plans[step];
        else if (previous != NULL) device_plan_prune_history(previous, &plans[step]);
        step++;
        now += 10000;
    }
    return 0;
}
