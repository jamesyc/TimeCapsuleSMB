#include <arpa/inet.h>
#include <string.h>
#include "mdns/mdns.h"
#undef EXIT_USAGE
#include "service/service.h"

struct fake_auto_ip_plan {
    int mode;
};

static int fake_collect_contexts(struct link_context_set *out, void *userdata) {
    struct fake_auto_ip_plan *plan = (struct fake_auto_ip_plan *)userdata;
    memset(out, 0, sizeof(*out));
    if (plan->mode == 1) {
        return -1;
    }
    if (plan->mode == 2) {
        append_link_ipv4(out, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
    }
    if (plan->mode == 3) {
        append_link_ipv4(out, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
        out->truncated = 1;
    }
    if (plan->mode == 4) {
        struct in6_addr addr6;
        if (inet_pton(AF_INET6, "fdbb:1111:2222:3333::40", &addr6) != 1) {
            return -1;
        }
        append_link_ipv6(out, "bridge0", &addr6, 64, 7, IFF_UP | IFF_RUNNING);
    }
    return 0;
}

int main(void) {
    struct fake_auto_ip_plan plan;

    memset(&plan, 0, sizeof(plan));
    plan.mode = 2;
    if (print_auto_ip_cidrs_with_provider(stdout, fake_collect_contexts, &plan) != EXIT_OK) {
        return 1;
    }
    plan.mode = 0;
    if (print_auto_ip_cidrs_with_provider(stdout, fake_collect_contexts, &plan) != EXIT_AUTO_IP_UNAVAILABLE) {
        return 2;
    }
    plan.mode = 1;
    if (print_auto_ip_cidrs_with_provider(stdout, fake_collect_contexts, &plan) != EXIT_AUTO_IP_PROBE_FAILED) {
        return 3;
    }
    if (print_auto_ip_cidrs_with_provider(stdout, NULL, &plan) != EXIT_AUTO_IP_PROBE_FAILED) {
        return 4;
    }
    plan.mode = 3;
    if (print_auto_ip_cidrs_with_provider(stdout, fake_collect_contexts, &plan) != EXIT_AUTO_IP_PROBE_FAILED) {
        return 5;
    }
    plan.mode = 4;
    if (print_auto_ip_cidrs_with_provider(stdout, fake_collect_contexts, &plan) != EXIT_AUTO_IP_UNAVAILABLE) {
        return 6;
    }
    return 0;
}
