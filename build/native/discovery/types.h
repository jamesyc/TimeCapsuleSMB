#ifndef TC_DISCOVERY_TYPES_H
#define TC_DISCOVERY_TYPES_H
#include "../common/plan.h"
#include "../common/log.h"
#include "../common/exit_codes.h"
/* The adisk TXT vocabulary (byte-identical items, golden-tested) and the
 * service ports discovery registers. Apple's mDNSResponder owns the wire;
 * we register through its dns_sd IPC. */
#define MAX_NAME 256
#define MAX_LABEL 63
#define MAX_TXT_STRING 255
#define ADISK_SYS_ADVF "0x1010"
#define ADISK_MAX_DISKS 16
#define ADISK_DISK_UUID_LEN 36
#define ADISK_SYS_TXT_PREFIX "sys=waMA="
#define ADISK_SYS_TXT_SUFFIX ",adVF=" ADISK_SYS_ADVF
#define ADISK_DISK_TXT_ADVF_PREFIX "=adVF="
#define ADISK_DISK_TXT_ADVN_MID ",adVN="
#define ADISK_DISK_TXT_SUFFIX ",adVU="
#define SMB_REGTYPE "_smb._tcp"
#define SMB_PORT 445
#define ADISK_REGTYPE "_adisk._tcp,_airport"
#define ADISK_PORT 9
#define AFP_REGTYPE "_afpovertcp._tcp"
#define AFP_PORT 548

struct adisk_disk {
    char share_name[MAX_NAME];
    char disk_key[MAX_LABEL + 1];
    char disk_advf[16];
    char uuid[ADISK_DISK_UUID_LEN + 1];
};

struct adisk_disk_set {
    struct adisk_disk disks[ADISK_MAX_DISKS];
    size_t count;
};

/* What the CLI feeds the registrant. */
struct config {
    struct adisk_disk_set adisk_disks;
    int diskless;
    int debug_logging;
};

extern volatile sig_atomic_t g_stop;
#endif
