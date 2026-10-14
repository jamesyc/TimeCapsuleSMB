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
/* Each SMB connection's TCP receive buffer (smb.conf socket options; stock
 * NetBSD gives 32 KiB). smbd reads nothing while it waits for the disk, so
 * once the client's data in flight fills this buffer the TCP window shuts.
 * macOS gives up on a send after 5 s, then either reconnects or sends a short
 * WRITE (Samba patch 0072). NetBSD 6 takes 2 MiB, about what a Mac's Time
 * Machine writes keep in flight. NetBSD 4's Ethernet driver lends the stack
 * its own 2048 2 KiB receive buffers per port and drops packets once all are
 * lent, so NetBSD 4 takes 512 KiB: enough for the Mac's writes to stream, not
 * enough to hold them all through a stall. */
#define TC_SMBD_RCVBUF(netbsd4) ((netbsd4) ? 512u * 1024 : 2048u * 1024)
/* NetBSD refuses a socket buffer above kern.sbmax * MCLBYTES / (MSIZE +
 * MCLBYTES): 8/9 of kern.sbmax with ARM's 256-byte mbufs. Apple's kernels
 * start at 256 KiB. */
#define TC_SMBD_SBMAX(netbsd4) (TC_SMBD_RCVBUF(netbsd4) / 8 * 9)

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
