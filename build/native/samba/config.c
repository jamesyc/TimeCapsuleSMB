#include "runtime.h"
#include "../common/config.h"
#include <sys/utsname.h>

static int boolean(const struct config_item *item, int fallback) {
    return item->present ? config_bool_value(item->value, -1) : fallback;
}
static unsigned number(const struct config_item *item, unsigned fallback, unsigned minimum,
                       unsigned maximum) {
    char *end;
    unsigned long n;
    if (!item->present || !*item->value || strspn(item->value, "0123456789") != strlen(item->value))
        return fallback;
    errno = 0;
    n = strtoul(item->value, &end, 10);
    return errno || *end || n < minimum || n > maximum ? fallback : (unsigned)n;
}

int tc_runtime_config_load(struct tc_runtime_config *config) {
    struct config_item items[] = {{"TELEMETRY", "", 0},
                                  {"NBNS_ENABLED", "", 0},
                                  {"RSYNC_ENABLED", "", 0},
                                  {"INTERNAL_SHARE_USE_DISK_ROOT", "", 0},
                                  {"SMB_BROWSE_COMPATIBILITY", "", 0},
                                  {"ANY_PROTOCOL", "", 0},
                                  {"REQUIRE_SMB_ENCRYPTION", "", 0},
                                  {"FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION", "", 0},
                                  {"FRUIT_METADATA_NETATALK", "", 0},
                                  {"VFS_AIO_FORK_ENABLED", "", 0},
                                  {"SMBD_DEBUG_LOGGING", "", 0},
                                  {"MDNS_DEBUG_LOGGING", "", 0},
                                  {"MDNS_ADVERTISE_AFP", "", 0},
                                  {"DISKD_USE_VOLUME_ATTEMPTS", "", 0},
                                  {"DISKD_USE_VOLUME_MOUNT_TIMEOUT_SECONDS", "", 0},
                                  {"DISKD_USE_VOLUME_MOUNT_POLL_SECONDS", "", 0},
                                  {"ATA_IDLE_SECONDS", "", 0},
                                  {"ATA_STANDBY", "", 0}};
    struct utsname system;
    memset(config, 0, sizeof(*config));
    if (config_read_snapshot(TC_FLASH_CONFIG_PATH, items, sizeof(items) / sizeof(items[0])))
        return -1;
    config->netbsd4 =
        uname(&system) == 0 && !strncmp(system.sysname, "NetBSD", 6) && system.release[0] == '4';
    config->telemetry = boolean(&items[0], 1);
    config->nbns = boolean(&items[1], 0);
    config->rsync = boolean(&items[2], 0);
    config->internal_root = boolean(&items[3], 0);
    config->browse_compatibility = boolean(&items[4], 0);
    config->any_protocol = boolean(&items[5], 0);
    config->require_encryption = boolean(&items[6], 0);
    config->disable_security = boolean(&items[7], 0);
    config->netatalk = boolean(&items[8], 1);
    config->aio_fork = boolean(&items[9], 0);
    config->debug = boolean(&items[10], 0);
    config->discovery_debug = boolean(&items[11], 0);
    config->advertise_afp = boolean(&items[12], 0);
    config->mount_attempts = number(&items[13], 2, 1, 10);
    config->mount_timeout = number(&items[14], 31, 0, 3600);
    config->mount_poll = number(&items[15], 3, 1, 60);
    config->ata_idle = number(&items[16], 300, 0, 86400);
    if (items[17].present) {
        if (strlen(items[17].value) >= sizeof(config->ata_standby) ||
            strspn(items[17].value, "0123456789") != strlen(items[17].value))
            return -1;
        strcpy(config->ata_standby, items[17].value);
    }
    if (config->telemetry < 0 || config->nbns < 0 || config->rsync < 0 || config->internal_root < 0 ||
        config->browse_compatibility < 0 || config->any_protocol < 0 || config->require_encryption < 0 ||
        config->disable_security < 0 || config->netatalk < 0 || config->aio_fork < 0 || config->debug < 0 ||
        config->discovery_debug < 0 || config->advertise_afp < 0 ||
        (config->require_encryption && config->disable_security))
        return -1;
    return 0;
}

int tc_samba_render(FILE *file, const struct tc_runtime_config *config,
                    const struct tc_samba_identity *identity, const char *payload,
                    const struct tc_share_set *shares) {
    size_t i;
    if (!file || !payload || !*payload || !identity->netbios[0] || !shares->count)
        return -1;
    fprintf(file,
            "[global]\n    netbios name = %s\n    workgroup = WORKGROUP\n"
            "    server string = %s\n"
            "    security = user\n    map to guest = Never\n    restrict anonymous = %d\n"
            "    guest account = nobody\n    null passwords = no\n    ea support = yes\n"
            "    passdb backend = smbpasswd:" TC_RAM_ROOT "/private/smbpasswd\n"
            "    username map = " TC_RAM_ROOT "/private/username.map\n    dos charset = ASCII\n",
            identity->netbios, identity->server, config->browse_compatibility ? 0 : 2);
    if (config->require_encryption)
        fputs("    server smb encrypt = required\n    server min protocol = SMB3_00\n    server max protocol "
              "= SMB3\n",
              file);
    else if (!config->any_protocol)
        fputs("    min protocol = SMB2\n    max protocol = SMB3\n", file);
    if (config->disable_security)
        fputs("    server signing = disabled\n    server smb encrypt = off\n", file);
    fprintf(file,
            "    server multi channel support = no\n    load printers = no\n    disable spoolss = yes\n"
            "    dfree command = /mnt/Flash/dfree.sh\n    pid directory = " TC_RAM_ROOT "/var\n"
            "    lock directory = " TC_LOCKS_ROOT "\n    state directory = " TC_RAM_ROOT "/var\n"
            "    cache directory = %s%s\n    private dir = " TC_RAM_ROOT "/private\n"
            "    dbwrap_tdb_max_dead:* = 0\n    log file = %s/logs/log.smbd\n    max log size = %d\n",
            config->netbsd4 ? payload : TC_RAM_ROOT, config->netbsd4 ? "/cache" : "/var", payload,
            config->debug ? 0 : 128);
    if (config->debug)
        fputs("    log level = 10\n", file);
    fputs("    smb ports = 445\n", file);
    if (config->aio_fork)
        fputs("    smb2 max read = 131072\n    smb2 max write = 131072\n    aio read size = 1\n    aio write "
              "size = 1\n",
              file);
    else
        fputs("    aio read size = 0\n    aio write size = 0\n", file);
    fprintf(file,
            "    deadtime = 720\n    max open files = 512\n    max smbd processes = 8\n"
            "    smb3 directory leases = no\n    reset on zero vc = yes\n    fruit:aapl = yes\n"
            "    fruit:model = %s\n    fruit:advertise_fullsync = true\n    fruit:nfs_aces = no\n"
            "    fruit:veto_appledouble = yes\n    fruit:wipe_intentionally_left_blank_rfork = yes\n"
            "    fruit:delete_empty_adfiles = yes\n",
            identity->model);
    /* Active share definitions can survive a Samba worker's reload. Global
     * mappings are refreshed, so an old tree can detect a removed/replaced
     * Apple volume or changed export root even without real open descriptors.
     * This opaque identity uses the configured path, not a resolved vnode. */
    for (i = 0; i < shares->count; i++)
        fprintf(file, "    tc:volume %s = %s|%s\n", shares->values[i].device,
                shares->values[i].uuid, shares->values[i].path);
    for (i = 0; i < shares->count; i++) {
        const struct tc_share *share = &shares->values[i];
        fprintf(file,
                "\n[%s]\n    path = %s\n    browseable = yes\n    read only = no\n"
                "    guest ok = no\n    valid users = root\n    veto files = /.samba4/\n"
                "    vfs objects = catia fruit streams_xattr acl_xattr xattr_tdb%s\n",
                share->name, share->path, config->aio_fork ? " aio_fork" : "");
        if (config->aio_fork)
            fputs("    aio_fork:max_children = 8\n", file);
        fprintf(file,
                "    acl_xattr:ignore system acls = yes\n    smbd max xattr size = 3802\n"
                "    streams_xattr:max xattrs per stream = 35\n    fruit:resource = file\n"
                "    fruit:metadata = %s\n    fruit:encoding = native\n    fruit:time machine = yes\n"
                "    fruit:posix_rename = yes\n    xattr_tdb:file = %s/private/xattr.tdb\n"
                "    tc:volume uuid = %s\n"
                "    tc:volume device = %s\n"
                "    force user = root\n    force group = wheel\n    create mask = 0666\n"
                "    directory mask = 0777\n    force create mode = 0666\n    force directory mode = 0777\n",
                config->netatalk ? "netatalk" : "stream", payload, share->uuid, share->device);
    }
    return ferror(file) ? -1 : 0;
}
