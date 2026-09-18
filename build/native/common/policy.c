#include "plan.h"

unsigned policy_lan_mask(const struct device_facts *facts, const struct plan_options *options) {
    unsigned mask;
    if (options->diskless) {
        return 0;
    }
    mask = SVC_SMB | SVC_ADISK;
    if (facts->config.advertise_afp == 1) {
        mask |= SVC_AFP;
    }
    return mask;
}

const struct link_plan *device_plan_find_link(const struct device_plan *plan, unsigned index) {
    size_t i;
    if (plan == NULL) {
        return NULL;
    }
    for (i = 0; i < plan->link_count; i++) {
        if (plan->links[i].link.index == index) {
            return &plan->links[i];
        }
    }
    return NULL;
}

/* Called only for a validated snapshot; incomplete reads retain policy in
 * plan.c, never grant a new LAN/WAN interface from partial facts. */
void policy_assign_masks(struct device_plan *plan, const struct device_facts *facts) {
    unsigned lan_mask = policy_lan_mask(facts, &plan->options);
    size_t i;
    plan->usbF = acp_u32(&facts->acp[ACP_KEY_usbF]);
    plan->wan_disks_allowed = plan->usbF.available && (plan->usbF.value & 0x8) ? 1 : 0;
    for (i = 0; i < plan->link_count; i++) {
        struct link_plan *link = &plan->links[i];
        link->mask = link->role == LINK_ROLE_LAN ||
            ((link->role == LINK_ROLE_WAN || link->role == LINK_ROLE_GUEST) &&
             plan->mode == ROUTER_MODE_NAT && plan->wan_disks_allowed) ? lan_mask : 0;
    }
}
