#include <arpa/inet.h>
#include <string.h>
#include "mdns/mdns.h"

struct fake_family_plan {
    int mode;
};

static int fake_collect_advertise_links(struct link_context_set *out, void *userdata) {
    struct fake_family_plan *plan = (struct fake_family_plan *)userdata;
    struct in6_addr ll;
    struct in6_addr ula;

    memset(out, 0, sizeof(*out));
    inet_pton(AF_INET6, "fe80::40", &ll);
    inet_pton(AF_INET6, "fdbb:1111:2222:3333::40", &ula);
    if (plan->mode == 1) {
        return -1;
    }
    if (plan->mode == 2) {
        append_link_ipv4(out, "bridge0", inet_addr("192.168.1.40"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
        append_link_ipv6(out, "bridge0", &ll, 64, 7, IFF_UP | IFF_RUNNING);
    }
    if (plan->mode == 3) {
        append_link_ipv6(out, "bridge0", &ula, 64, 7, IFF_UP | IFF_RUNNING);
    }
    if (plan->mode == 4) {
        append_link_ipv6(out, "bridge0", &ll, 64, 7, IFF_UP | IFF_RUNNING);
    }
    if (plan->mode == 5) {
        append_link_ipv4(out, "bridge0", inet_addr("192.168.1.40"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
        append_link_ipv6_with_transport(out, "bridge0", &ll, 64, 7, IFF_UP | IFF_RUNNING, 0);
    }
    return 0;
}

int main(void) {
    struct fake_family_plan plan;

    memset(&plan, 0, sizeof(plan));
    plan.mode = 2;
    if (print_mdns_socket_families_with_provider(stdout, fake_collect_advertise_links, &plan) != EXIT_OK) {
        return 1;
    }
    plan.mode = 3;
    if (print_mdns_socket_families_with_provider(stdout, fake_collect_advertise_links, &plan) != EXIT_OK) {
        return 2;
    }
    plan.mode = 0;
    if (print_mdns_socket_families_with_provider(stdout, fake_collect_advertise_links, &plan) != EXIT_AUTO_IP_UNAVAILABLE) {
        return 3;
    }
    plan.mode = 4;
    if (print_mdns_socket_families_with_provider(stdout, fake_collect_advertise_links, &plan) != EXIT_OK) {
        return 5;
    }
    plan.mode = 5;
    if (print_mdns_socket_families_with_provider(stdout, fake_collect_advertise_links, &plan) != EXIT_OK) {
        return 6;
    }
    plan.mode = 1;
    if (print_mdns_socket_families_with_provider(stdout, fake_collect_advertise_links, &plan) != EXIT_AUTO_IP_PROBE_FAILED) {
        return 4;
    }
    return 0;
}
