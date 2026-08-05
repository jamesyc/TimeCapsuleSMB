#include <arpa/inet.h>
#include <string.h>
#include "mdns/mdns.h"
#undef EXIT_USAGE
#include "service/service.h"

struct fake_lan_plan {
    int mode;
};

static int fake_collect_links(struct link_context_set *out, void *userdata) {
    struct fake_lan_plan *plan = (struct fake_lan_plan *)userdata;
    struct in6_addr bridge_ula;
    struct in6_addr wan_ula;

    memset(out, 0, sizeof(*out));
    if (inet_pton(AF_INET6, "fdbb:1111:2222:3333::40", &bridge_ula) != 1 ||
        inet_pton(AF_INET6, "fdbb:aaaa:bbbb:cccc::217", &wan_ula) != 1) {
        return -1;
    }
    if (plan->mode == 1) {
        append_link_ipv4(out, "bridge0", inet_addr("10.0.1.1"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
        append_link_ipv4(out, "bridge0", inet_addr("169.254.1.9"), inet_addr("255.255.0.0"), IFF_UP | IFF_RUNNING);
        append_link_ipv6(out, "bridge0", &bridge_ula, 64, 7, IFF_UP | IFF_RUNNING);
    }
    if (plan->mode == 1 || plan->mode == 2) {
        append_link_ipv4(out, "bcmeth1", inet_addr("192.168.1.217"), inet_addr("255.255.255.0"), IFF_UP | IFF_RUNNING);
        append_link_ipv4(out, "bcmeth1", inet_addr("169.254.155.207"), inet_addr("255.255.0.0"), IFF_UP | IFF_RUNNING);
        append_link_ipv6(out, "bcmeth1", &wan_ula, 64, 8, IFF_UP | IFF_RUNNING);
    }
    return 0;
}

int main(void) {
    struct fake_lan_plan plan;

    memset(&plan, 0, sizeof(plan));
    plan.mode = 1;
    if (print_smb_bind_interfaces_with_provider(stdout, fake_collect_links, &plan) != EXIT_OK) {
        return 1;
    }
    if (print_smb_bind_interfaces_lan_with_provider(stdout, fake_collect_links, &plan) != EXIT_OK) {
        return 2;
    }
    plan.mode = 2;
    if (print_smb_bind_interfaces_lan_with_provider(stdout, fake_collect_links, &plan) != EXIT_AUTO_IP_UNAVAILABLE) {
        return 3;
    }
    return 0;
}
