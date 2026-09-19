#include "runtime.h"
#include "../common/config.h"
#include "../service/service.h"

static int bool_item(const struct config_item *item, int fallback) {
    return item->present ? config_bool_value(item->value, -1) : fallback;
}

int tc_runtime_config_load(struct tc_runtime_config *config) {
    struct config_item items[] = {
        {"TC_PAYLOAD_DIR", "", 0}, {"TELEMETRY", "", 0}, {"NBNS_ENABLED", "", 0},
        {"RSYNC_ENABLED", "", 0}, {"INTERNAL_SHARE_USE_DISK_ROOT", "", 0},
        {"SMB_BROWSE_COMPATIBILITY", "", 0}, {"ANY_PROTOCOL", "", 0},
        {"REQUIRE_SMB_ENCRYPTION", "", 0}, {"FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION", "", 0},
        {"FRUIT_METADATA_NETATALK", "", 0}, {"VFS_AIO_FORK_ENABLED", "", 0},
        {"SMBD_DEBUG_LOGGING", "", 0}, {"MDNS_ADVERTISE_AFP", "", 0}
    };
    memset(config, 0, sizeof(*config));
    if (config_read_snapshot(TC_FLASH_CONFIG_PATH, items, sizeof(items) / sizeof(items[0])) != 0) return -1;
    if (items[0].present) strncpy(config->payload_dir, items[0].value, sizeof(config->payload_dir) - 1);
    config->telemetry = bool_item(&items[1], 1); config->nbns = bool_item(&items[2], 1);
    config->rsync = bool_item(&items[3], 0); config->internal_root = bool_item(&items[4], 0);
    config->browse_compatibility = bool_item(&items[5], 0); config->any_protocol = bool_item(&items[6], 0);
    config->require_encryption = bool_item(&items[7], 0); config->disable_security = bool_item(&items[8], 0);
    config->netatalk = bool_item(&items[9], 1); config->aio_fork = bool_item(&items[10], 0);
    config->debug = bool_item(&items[11], 0);
    config->advertise_afp = bool_item(&items[12], 0);
    if ((config->payload_dir[0] && (strncmp(config->payload_dir, "/Volumes/", 9) != 0 ||
         strstr(config->payload_dir, "/../") || strchr(config->payload_dir, '\n') || strchr(config->payload_dir, '\r'))) ||
        config->telemetry < 0 || config->nbns < 0 || config->rsync < 0 || config->internal_root < 0 ||
        config->browse_compatibility < 0 || config->any_protocol < 0 || config->require_encryption < 0 ||
        config->disable_security < 0 || config->netatalk < 0 || config->aio_fork < 0 || config->debug < 0 ||
        config->advertise_afp < 0 ||
        (config->require_encryption && config->disable_security)) return -1;
    return 0;
}

static int make_directory(const char *path, mode_t mode) {
    if (mkdir(path, mode) != 0 && errno != EEXIST) return -1;
    return chmod(path, mode);
}

static int prepare_directories(void) {
    return make_directory(TC_SAMBA_RAM_ROOT, 0755) == 0 &&
        make_directory(TC_SAMBA_RAM_ROOT "/sbin", 0755) == 0 &&
        make_directory(TC_SAMBA_RAM_ROOT "/private", 0700) == 0 &&
        make_directory(TC_SAMBA_RAM_ROOT "/var", 0755) == 0 &&
        make_directory(TC_SAMBA_RAM_ROOT "/locks", 0755) == 0 ? 0 : -1;
}

static int copy_atomic(const char *source, const char *destination, mode_t mode) {
    char temporary[256];
    unsigned char buffer[65536];
    int input = -1, output = -1, result = -1;
    ssize_t got;
    if (snprintf(temporary, sizeof(temporary), "%s.new", destination) >= (int)sizeof(temporary)) return -1;
    unlink(temporary);
    input = open(source, O_RDONLY);
    output = open(temporary, O_WRONLY | O_CREAT | O_EXCL, mode);
    if (input < 0 || output < 0) goto out;
    while ((got = read(input, buffer, sizeof(buffer))) > 0) {
        size_t used = 0;
        while (used < (size_t)got) {
            ssize_t written = write(output, buffer + used, (size_t)got - used);
            if (written < 0 && errno == EINTR) continue;
            if (written <= 0) goto out;
            used += (size_t)written;
        }
    }
    if (got < 0 || fsync(output) != 0 || fchmod(output, mode) != 0 || close(output) != 0) {
        output = -1; goto out;
    }
    output = -1;
    if (rename(temporary, destination) != 0) goto out;
    result = 0;
out:
    if (input >= 0) close(input);
    if (output >= 0) close(output);
    if (result != 0) unlink(temporary);
    return result;
}

static int write_auth(void) {
    char hash[33];
    char temporary[256];
    FILE *file;
    if (device_nt_hash(hash) != 0) return -1;
    snprintf(temporary, sizeof(temporary), "%s.new", TC_SAMBA_RAM_ROOT "/private/smbpasswd");
    file = fopen(temporary, "w");
    if (file == NULL) return -1;
    if (fchmod(fileno(file), 0600) != 0 ||
        fprintf(file, "root:0:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX:%s:[U          ]:LCT-00000000:\n", hash) < 0 ||
        fflush(file) != 0 || fsync(fileno(file)) != 0 || fclose(file) != 0 ||
        rename(temporary, TC_SAMBA_RAM_ROOT "/private/smbpasswd") != 0) {
        unlink(temporary); return -1;
    }
    snprintf(temporary, sizeof(temporary), "%s.new", TC_SAMBA_RAM_ROOT "/private/username.map");
    file = fopen(temporary, "w");
    if (file == NULL) return -1;
    if (fchmod(fileno(file), 0600) != 0 || fputs("!root = root\nroot = *\n", file) == EOF ||
        fflush(file) != 0 || fsync(fileno(file)) != 0 || fclose(file) != 0 ||
        rename(temporary, TC_SAMBA_RAM_ROOT "/private/username.map") != 0) {
        unlink(temporary); return -1;
    }
    return 0;
}

static int render_config(const struct tc_runtime_config *config,
                         const struct device_plan *plan,
                         const struct tc_share_set *shares) {
    char temporary[256], interfaces[TC_BIND_TOKENS_MAX];
    FILE *file;
    size_t i;
    if (device_plan_bind_tokens(plan, interfaces, sizeof(interfaces)) != 0) return -1;
    snprintf(temporary, sizeof(temporary), "%s.new", TC_SAMBA_CONF);
    file = fopen(temporary, "w");
    if (file == NULL || fchmod(fileno(file), 0600) != 0) return -1;
    fprintf(file,
        "[global]\n"
        "    netbios name = %s\n    workgroup = WORKGROUP\n    interfaces = %s\n"
        "    bind interfaces only = yes\n    server string = %s\n    security = user\n"
        "    map to guest = Never\n    restrict anonymous = %d\n    guest account = nobody\n"
        "    null passwords = no\n    ea support = yes\n"
        "    passdb backend = smbpasswd:%s/private/smbpasswd\n"
        "    username map = %s/private/username.map\n    dos charset = ASCII\n",
        plan->id.netbios, interfaces, plan->id.instance,
        config->browse_compatibility ? 0 : 2, TC_SAMBA_RAM_ROOT, TC_SAMBA_RAM_ROOT);
    if (config->disable_security)
        fputs("    server signing = disabled\n    server smb encrypt = off\n", file);
    else if (config->require_encryption)
        fputs("    server smb encrypt = required\n    server min protocol = SMB3_00\n    server max protocol = SMB3\n", file);
    else if (!config->any_protocol)
        fputs("    min protocol = SMB2\n    max protocol = SMB3\n", file);
    fprintf(file,
        "    server multi channel support = no\n    load printers = no\n    disable spoolss = yes\n"
        "    pid directory = %s/var\n    lock directory = %s/locks\n"
        "    state directory = %s/var\n    cache directory = %s/var\n"
        "    private dir = %s/private\n    dbwrap_tdb_max_dead:* = 0\n"
        "    log file = %s/var/log.smbd\n    max log size = 100\n    smb ports = 445\n"
        "    aio read size = %d\n    aio write size = %d\n    deadtime = 720\n"
        "    max open files = 512\n    max smbd processes = 8\n    smb3 directory leases = no\n"
        "    reset on zero vc = yes\n    fruit:aapl = yes\n    fruit:model = MacSamba\n"
        "    fruit:advertise_fullsync = true\n    fruit:nfs_aces = no\n"
        "    fruit:veto_appledouble = yes\n    fruit:wipe_intentionally_left_blank_rfork = yes\n"
        "    fruit:delete_empty_adfiles = yes\n",
        TC_SAMBA_RAM_ROOT, TC_SAMBA_RAM_ROOT, TC_SAMBA_RAM_ROOT,
        TC_SAMBA_RAM_ROOT, TC_SAMBA_RAM_ROOT, TC_SAMBA_RAM_ROOT,
        config->aio_fork ? 1 : 0, config->aio_fork ? 1 : 0);
    for (i = 0; i < shares->count; i++) {
        const struct tc_share *share = &shares->values[i];
        fprintf(file,
            "\n[%s]\n    path = %s\n    browseable = yes\n    read only = no\n"
            "    guest ok = no\n    valid users = root\n    veto files = /.samba4/\n"
            "    vfs objects = catia fruit streams_xattr acl_xattr xattr_tdb%s\n",
            share->name, share->path, config->aio_fork ? " aio_fork" : "");
        if (config->aio_fork) fputs("    aio_fork:max_children = 8\n", file);
        fprintf(file,
            "    acl_xattr:ignore system acls = yes\n    smbd max xattr size = 3802\n"
            "    streams_xattr:max xattrs per stream = 35\n    fruit:resource = file\n"
            "    fruit:metadata = %s\n    fruit:encoding = native\n    fruit:time machine = yes\n"
            "    fruit:posix_rename = yes\n    xattr_tdb:file = %s/private/xattr.tdb\n"
            "    force user = root\n    force group = wheel\n    create mask = 0666\n"
            "    directory mask = 0777\n    force create mode = 0666\n    force directory mode = 0777\n",
            config->netatalk ? "netatalk" : "stream", config->payload_dir);
    }
    if (fflush(file) != 0 || fsync(fileno(file)) != 0 || fclose(file) != 0 ||
        rename(temporary, TC_SAMBA_CONF) != 0) { unlink(temporary); return -1; }
    return 0;
}

int tc_samba_prepare(const struct tc_runtime_config *config,
                     const struct device_plan *plan,
                     const struct tc_share_set *shares) {
    char source[512];
    if (!config->payload_dir[0] || plan->status.cold_start || shares->count == 0 || prepare_directories() != 0) return -1;
    if (snprintf(source, sizeof(source), "%s/smbd", config->payload_dir) >= (int)sizeof(source) ||
        copy_atomic(source, TC_SAMBA_BIN, 0755) != 0 || write_auth() != 0 ||
        render_config(config, plan, shares) != 0) return -1;
    if (config->rsync) {
        if (snprintf(source, sizeof(source), "%s/rsync", config->payload_dir) >= (int)sizeof(source) ||
            copy_atomic(source, TC_RSYNC_BIN, 0755) != 0) return -1;
    }
    return 0;
}

int tc_samba_listener_ready(int attempts) {
    struct sockaddr_in address;
    int attempt;
    memset(&address, 0, sizeof(address)); address.sin_family = AF_INET;
    address.sin_port = htons(445); address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    for (attempt = 0; attempt < attempts; attempt++) {
        int fd = socket(AF_INET, SOCK_STREAM, 0);
        if (fd >= 0 && connect(fd, (struct sockaddr *)&address, sizeof(address)) == 0) { close(fd); return 0; }
        if (fd >= 0) close(fd);
        sleep(1);
    }
    return -1;
}
