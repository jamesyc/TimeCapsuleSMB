#ifndef TC_SAMBA_RUNTIME_H
#define TC_SAMBA_RUNTIME_H
#include "../service/service.h"
#include "../storage/shares.h"

#ifndef TC_RAM_ROOT
#define TC_RAM_ROOT "/mnt/Memory/samba4"
#endif
#ifndef TC_LOCKS_ROOT
#define TC_LOCKS_ROOT "/mnt/Locks"
#endif
#ifndef TC_SERVICE_BIN
#define TC_SERVICE_BIN "/mnt/Flash/service"
#endif
#define TC_SMBD_BIN TC_RAM_ROOT "/sbin/smbd"
#define TC_SMBD_CONF TC_RAM_ROOT "/etc/smb.conf"
#define TC_RSYNC_BIN TC_RAM_ROOT "/sbin/rsync"
#define TC_RSYNC_CONF TC_RAM_ROOT "/etc/rsyncd.conf"

struct tc_runtime_config {
    int netbsd4, telemetry, rsync, internal_root, browse_compatibility;
    int any_protocol, require_encryption, disable_security, netatalk, aio_fork;
    int debug, discovery_debug, advertise_afp;
    unsigned mount_attempts, mount_timeout, mount_poll, ata_idle;
    char ata_standby[32];
};
int tc_runtime_config_load(struct tc_runtime_config *);
int tc_samba_render(FILE *, const struct tc_runtime_config *, const struct tc_samba_identity *,
                    const char *payload, const struct tc_share_set *);
#endif
