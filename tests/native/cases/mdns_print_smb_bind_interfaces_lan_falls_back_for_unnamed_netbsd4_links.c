#include <arpa/inet.h>
#include <string.h>
#include "mdns/mdns.h"
#undef EXIT_USAGE
#include "service/service.h"

struct fake_unnamed_lan_plan {
    int mode;
};

static int fake_collect_links(struct link_context_set *out, void *userdata) {
    struct fake_unnamed_lan_plan *plan = (struct fake_unnamed_lan_plan *)userdata;
    struct in6_addr ula;
    struct in6_addr public_addr;

    memset(out, 0, sizeof(*out));
    if (inet_pton(AF_INET6, "fdbb:5737:6e53:9bf7::40", &ula) != 1 ||
        inet_pton(AF_INET6, "2001:db8:5737:6e53::40", &public_addr) != 1) {
        return -1;
    }
    if (plan->mode == 1) {
        append_link_ipv4(out, "ip4-0a000101", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
        append_link_ipv4(out, "ip4-a9fea3e0", inet_addr("169.254.163.224"), inet_addr("255.255.0.0"), IFF_UP | IFF_RUNNING);
        append_link_ipv6(out, "ipv6", &ula, 64, 0, IFF_UP | IFF_RUNNING);
        append_link_ipv6(out, "ipv6-if9", &public_addr, 64, 0, IFF_UP | IFF_RUNNING);
    }
    if (plan->mode == 2) {
        append_link_ipv6(out, "ipv6", &ula, 64, 0, IFF_UP | IFF_RUNNING);
    }
    if (plan->mode == 3) {
        append_link_ipv6(out, "ipv6-if9", &public_addr, 64, 0, IFF_UP | IFF_RUNNING);
    }
    return 0;
}

int main(void) {
    struct fake_unnamed_lan_plan plan;

    memset(&plan, 0, sizeof(plan));
    plan.mode = 1;
    if (print_smb_bind_interfaces_with_provider(stdout, fake_collect_links, &plan) != EXIT_OK) {
        return 1;
    }
    if (print_smb_bind_interfaces_lan_with_provider(stdout, fake_collect_links, &plan) != EXIT_OK) {
        return 2;
    }
    plan.mode = 2;
    if (print_smb_bind_interfaces_lan_with_provider(stdout, fake_collect_links, &plan) != EXIT_OK) {
        return 3;
    }
    plan.mode = 3;
    if (print_smb_bind_interfaces_lan_with_provider(stdout, fake_collect_links, &plan) != EXIT_AUTO_IP_UNAVAILABLE) {
        return 4;
    }
    return 0;
}
