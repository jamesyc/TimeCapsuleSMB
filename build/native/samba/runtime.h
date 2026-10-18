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
/* Open files for the whole system (kern.maxfiles; Apple's kernels allow
 * 1024). A Time Machine sparse bundle keeps a hundred or more band files open
 * in its smbd, each taking more than one entry, so several Macs at once fill
 * Apple's table; every open then fails and the Macs drop their disk images.
 * The kernel allocates entries as files open, so the limit costs nothing
 * until it is used. */
#define TC_MAXFILES 4096u
/* Processes for the whole system (kern.maxproc). Apple's kernels allow 84
 * (NetBSD's 20 + 16 * maxusers with maxusers 4). Each client connection adds
 * its smbd and up to two aio_fork helpers per share it uses, and a full table
 * fails every fork on the device, Apple's daemons' included. The table cannot
 * grow much past 84: the ARM pmap shares each L1 page table among 15 address
 * spaces and allocates ceil(maxproc / 15) of them once, at boot, so these
 * kernels hold 90 user address spaces whatever kern.maxproc says later, and
 * the fork() that needs a 91st takes a NULL L1 and panics. fork() refuses at
 * kern.maxproc, root included, so the limit is safe at 90 plus the processes
 * that never take an address space. Every NetBSD 6 process but proc0 has its
 * own: 90. NetBSD 4's kernel threads are processes on the kernel's pmap, and
 * each NetBSD 4 build runs on Time Capsules and on AirPort Extremes, which
 * lack the Time Capsule's fan thread and so set the build's limit. Counting
 * only the processes that exist from boot (the physical-I/O worker starts on
 * first use), the little-endian Extremes run 13: 103. The big-endian ones
 * run 9 (proc0, four core kernel threads, three USB, one flash): 99. */
#if defined(__ARMEB__)
#define TC_NETBSD4_MAXPROC 99u
#else
#define TC_NETBSD4_MAXPROC 103u
#endif
#define TC_MAXPROC(netbsd4) ((netbsd4) ? TC_NETBSD4_MAXPROC : 90u)

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
