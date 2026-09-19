#ifndef TC_SAMBA_RUNTIME_H
#define TC_SAMBA_RUNTIME_H

#include "../common/plan.h"
#include "../storage/shares.h"

#ifndef TC_SAMBA_RAM_ROOT
#define TC_SAMBA_RAM_ROOT "/mnt/Memory/samba4"
#endif
#define TC_SAMBA_BIN TC_SAMBA_RAM_ROOT "/sbin/smbd"
#define TC_RSYNC_BIN TC_SAMBA_RAM_ROOT "/sbin/rsync"
#define TC_SAMBA_CONF TC_SAMBA_RAM_ROOT "/private/smb.conf"

struct tc_runtime_config {
    char payload_dir[256];
    int telemetry;
    int nbns;
    int rsync;
    int internal_root;
    int browse_compatibility;
    int any_protocol;
    int require_encryption;
    int disable_security;
    int netatalk;
    int aio_fork;
    int debug;
    int advertise_afp;
};

int tc_runtime_config_load(struct tc_runtime_config *config);
int tc_samba_prepare(const struct tc_runtime_config *config,
                     const struct device_plan *plan,
                     const struct tc_share_set *shares);
int tc_samba_listener_ready(int attempts);

#endif
