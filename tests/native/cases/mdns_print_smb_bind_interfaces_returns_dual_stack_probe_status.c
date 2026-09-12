#include <arpa/inet.h>
#include <string.h>
#include "mdns/mdns.h"
#undef EXIT_USAGE
#include "service/service.h"

struct fake_bind_plan {
    int mode;
};

static int fake_collect_links(struct link_context_set *out, void *userdata) {
    struct fake_bind_plan *plan = (struct fake_bind_plan *)userdata;
    struct in6_addr ula;
    struct in6_addr ll;

    memset(out, 0, sizeof(*out));
    if (plan->mode == 1) {
        return -1;
    }
    if (plan->mode == 2) {
        inet_pton(AF_INET6, "fdbb:1111:2222:3333::40", &ula);
        inet_pton(AF_INET6, "fe80::40", &ll);
        append_link_ipv4(out, "bridge0", inet_addr("169.254.1.9"), inet_addr("255.255.0.0"), IFF_UP | IFF_RUNNING);
        append_link_ipv6(out, "bridge0", &ula, 64, 7, IFF_UP | IFF_RUNNING);
        append_link_ipv6(out, "bridge0", &ll, 64, 7, IFF_UP | IFF_RUNNING);
    }
    if (plan->mode == 3) {
        inet_pton(AF_INET6, "fe80::40", &ll);
        append_link_ipv6(out, "bridge0", &ll, 64, 7, IFF_UP | IFF_RUNNING);
    }
    if (plan->mode == 4) {
        append_link_ipv4(out, "bridge0", inet_addr("169.254.1.9"), inet_addr("255.255.0.0"), IFF_UP | IFF_RUNNING);
        out->truncated = 1;
    }
    if (plan->mode == 5) {
        inet_pton(AF_INET6, "fe80::40", &ll);
        append_link_ipv4(out, "bridge0", inet_addr("169.254.1.9"), inet_addr("255.255.0.0"), IFF_UP | IFF_RUNNING);
        append_link_ipv6(out, "bridge0", &ll, 64, 7, IFF_UP | IFF_RUNNING);
    }
    return 0;
}

int main(void) {
    struct fake_bind_plan plan;

    memset(&plan, 0, sizeof(plan));
    plan.mode = 2;
    if (print_smb_bind_interfaces_with_provider(stdout, fake_collect_links, &plan) != EXIT_OK) {
        return 1;
    }
    plan.mode = 0;
    if (print_smb_bind_interfaces_with_provider(stdout, fake_collect_links, &plan) != EXIT_AUTO_IP_UNAVAILABLE) {
        return 2;
    }
    plan.mode = 1;
    if (print_smb_bind_interfaces_with_provider(stdout, fake_collect_links, &plan) != EXIT_AUTO_IP_PROBE_FAILED) {
        return 3;
    }
    plan.mode = 3;
    if (print_smb_bind_interfaces_with_provider(stdout, fake_collect_links, &plan) != EXIT_OK) {
        return 4;
    }
    if (print_smb_bind_interfaces_with_provider(stdout, NULL, &plan) != EXIT_AUTO_IP_PROBE_FAILED) {
        return 5;
    }
    plan.mode = 4;
    if (print_smb_bind_interfaces_with_provider(stdout, fake_collect_links, &plan) != EXIT_AUTO_IP_PROBE_FAILED) {
        return 6;
    }
    plan.mode = 5;
    if (print_smb_bind_interfaces_with_provider(stdout, fake_collect_links, &plan) != EXIT_OK) {
        return 7;
    }
    return 0;
}
