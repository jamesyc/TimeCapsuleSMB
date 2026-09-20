#include "runtime.h"
#include "../common/acp.h"
#include "../common/worker.h"
#include <sys/stat.h>
#include <sys/statvfs.h>
#if defined(__NetBSD__)
#include <sys/mount.h>
#endif

int tc_volume_mounted(const struct tc_volume *volume, int *writable) {
    *writable = 0;
#ifdef TC_NATIVE_TEST
    {
        const char *fixture = getenv("TC_TEST_MOUNTS");
        if (fixture) {
            FILE *f = fopen(fixture, "r");
            char line[768];
            int found = 0;
            if (!f)
                return -1;
            while (fgets(line, sizeof(line), f)) {
                char root[256], device[16];
                int rw;
                if (sscanf(line, "%255s %15s %d", root, device, &rw) != 3) {
                    fclose(f);
                    return -1;
                }
                if (!strcmp(root, volume->root) && !strcmp(device, volume->device)) {
                    found = 1;
                    *writable = rw != 0;
                }
            }
            if (ferror(f))
                found = -1;
            fclose(f);
            return found;
        }
    }
#endif
#if defined(__NetBSD__)
    {
        struct statvfs *mounts;
        int count = getmntinfo(&mounts, MNT_NOWAIT), i;
        char device[32];
        if (count <= 0)
            return -1;
        snprintf(device, sizeof(device), "/dev/%s", volume->device);
        for (i = 0; i < count; i++) {
            if (strcmp(mounts[i].f_mntonname, volume->root))
                continue;
            if (strcmp(mounts[i].f_mntfromname, device) || strcmp(mounts[i].f_fstypename, "hfs"))
                return 0;
            *writable = (mounts[i].f_flag & MNT_RDONLY) == 0;
            return 1;
        }
        return 0;
    }
#else
    /* Host builds exercise injected mounts; never claim host volumes. */
    (void)volume;
    return -1;
#endif
}

int tc_storage_guard(const struct tc_volume *volume) {
    int writable, fd;
    struct stat st;
    if (tc_volume_mounted(volume, &writable) != 1 || !writable)
        return -1;
    fd = open(volume->root, O_RDONLY | O_NOFOLLOW);
    if (fd < 0)
        return -1;
    if (fstat(fd, &st) || !S_ISDIR(st.st_mode)) {
        close(fd);
        return -1;
    }
    return fd;
}
int tc_storage_guard_valid(const struct tc_volume *volume, int guard) {
    struct stat held, current;
    int writable;
    /* Apple can remount a bumped USB disk with identical dkN/dev/inode/fsid.
     * A retained old vnode still fails fstat (measured EBADF on NetBSD 4).
     * This descriptor is held only during the operation, not while idle. */
    return guard >= 0 && !tc_worker_cancelled() && !fstat(guard, &held) && !lstat(volume->root, &current) &&
           held.st_dev == current.st_dev && held.st_ino == current.st_ino &&
           tc_volume_mounted(volume, &writable) == 1 && writable;
}

static int previously_active(const struct tc_storage_snapshot *previous, const struct tc_volume *volume) {
    size_t i;
    if (!previous)
        return 0;
    for (i = 0; i < previous->inventory.count; i++) {
        const struct tc_volume *old = &previous->inventory.volumes[i];
        if ((previous->available & (1u << i)) && !strcmp(old->uuid, volume->uuid) &&
            !strcmp(old->device, volume->device) && !strcmp(old->disk, volume->disk))
            return 1;
    }
    return 0;
}
static int activate(const struct tc_volume *volume, const struct tc_runtime_config *config) {
    char argument[288];
    unsigned attempt;
    char *argv[] = {TC_ACP_PATH, "rpc", "diskd.useVolume", argument, NULL};
    if (snprintf(argument, sizeof(argument), "path:s:%s", volume->root) >= (int)sizeof(argument))
        return -1;
    /* Preserve the established product's diskd activation operation. Do not
     * substitute mount_hfs or experimental ACP notification/write commands. */
    for (attempt = 0; attempt < config->mount_attempts; attempt++) {
        long long started = acp_monotonic_ms(), deadline = started + (long long)config->mount_timeout * 1000;
        int writable;
        fprintf(stderr, "storage: claim %s attempt=%u/%u\n", volume->root, attempt + 1,
                config->mount_attempts);
        if (tc_command_run(argv, 30) == 0) {
            for (;;) {
                long long now = acp_monotonic_ms(), next;
                if (tc_volume_mounted(volume, &writable) == 1)
                    return writable ? 0 : -1;
                if (now >= deadline || tc_worker_cancelled())
                    break;
                next = now + (long long)config->mount_poll * 1000;
                if (next > deadline)
                    next = deadline;
                while (acp_monotonic_ms() < next && !tc_worker_cancelled())
                    usleep(100000);
            }
        }
        if (tc_worker_cancelled())
            return -1;
    }
    return -1;
}

static int prepare_share(const struct tc_volume *volume, int internal_root, int guard) {
    char path[288], marker[352];
    struct stat st;
    int fd;
    if (snprintf(path, sizeof(path), "%s%s", volume->root,
                 volume->builtin && !internal_root ? "/ShareRoot" : "") >= (int)sizeof(path))
        return -1;
    if (!tc_storage_guard_valid(volume, guard) || tc_make_dir(path, 0755))
        return -1;
    snprintf(marker, sizeof(marker), "%s/.com.apple.timemachine.supported", path);
    if (lstat(marker, &st) == 0)
        return S_ISREG(st.st_mode) ? 0 : -1;
    if (errno != ENOENT || !tc_storage_guard_valid(volume, guard))
        return -1;
    fd = open(marker, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0644);
    if (fd < 0)
        return -1;
    if (close(fd))
        return -1;
    return tc_storage_guard_valid(volume, guard) ? 0 : -1;
}

static int payload_at(struct tc_storage_snapshot *snapshot, size_t index,
                      const struct tc_runtime_config *config) {
    const struct tc_volume *volume = &snapshot->inventory.volumes[index];
    char payload[288], source[320], path[320];
    struct stat st;
    int guard = tc_storage_guard(volume), result = -1;
    if (guard < 0)
        return -1;
    snprintf(payload, sizeof(payload), "%s/.samba4", volume->root);
    snprintf(source, sizeof(source), "%s/smbd", payload);
    if (access(source, X_OK))
        snprintf(source, sizeof(source), "%s/sbin/smbd", payload);
    if (stat(source, &st) || !S_ISREG(st.st_mode) || access(source, X_OK))
        goto out;
    snprintf(path, sizeof(path), "%s/private", payload);
    if (lstat(path, &st) || !S_ISDIR(st.st_mode))
        goto out;
    if (config->rsync) {
        snprintf(path, sizeof(path), "%s/rsync", payload);
        if (access(path, X_OK))
            goto out;
        snprintf(path, sizeof(path), "%s/rsyncd.conf", payload);
        if (access(path, R_OK))
            goto out;
    }
    if (!tc_storage_guard_valid(volume, guard))
        goto out;
    strcpy(snapshot->payload, payload);
    strcpy(snapshot->smbd_source, source);
    snapshot->payload_index = (int)index;
    result = 0;
out:
    close(guard);
    return result;
}

static void tune_disk(const struct tc_volume *volume, const struct tc_runtime_config *config) {
    char device[40], idle[24];
    char *argv[] = {"/sbin/atactl", device, "setidle", idle, NULL};
    if (!volume->builtin || strncmp(volume->disk, "wd", 2) || !isdigit((unsigned char)volume->disk[2]))
        return;
    snprintf(device, sizeof(device), "/dev/%s", volume->disk);
    snprintf(idle, sizeof(idle), "%u", config->ata_idle);
    (void)tc_command_run(argv, 20);
    if (*config->ata_standby) {
        argv[2] = "setstandby";
        argv[3] = (char *)config->ata_standby;
        (void)tc_command_run(argv, 20);
    }
}

int tc_storage_prepare(struct tc_storage_snapshot *snapshot, const struct tc_inventory *inventory,
                       const struct tc_storage_snapshot *previous, const struct tc_runtime_config *config,
                       int tune_ata) {
    size_t i, j;
    int pass;
    memset(snapshot, 0, sizeof(*snapshot));
    snapshot->inventory = *inventory;
    snapshot->payload_index = -1;
    for (i = 0; i < inventory->count; i++) {
        const struct tc_volume *volume = &inventory->volumes[i];
        int writable, active = previously_active(previous, volume),
                      mounted = tc_volume_mounted(volume, &writable), guard;
        if (tc_worker_cancelled() || mounted < 0)
            return -1;
        if ((mounted != 1 || volume->users == 0 || !active) && activate(volume, config))
            continue;
        guard = tc_storage_guard(volume);
        if (guard < 0)
            continue;
        if (prepare_share(volume, config->internal_root, guard) == 0 && tc_storage_guard_valid(volume, guard))
            snapshot->available |= 1u << i;
        close(guard);
        if (!(snapshot->available & (1u << i)))
            continue;
        if (tune_ata || !active) {
            for (j = 0; j < i; j++)
                if ((snapshot->available & (1u << j)) && !strcmp(inventory->volumes[j].disk, volume->disk))
                    break;
            if (j == i)
                tune_disk(volume, config);
        }
    }
    if (tc_shares_build(&snapshot->shares, inventory, snapshot->available, config->internal_root,
                        config->advertise_afp))
        return -1;
    /* Keep a valid selection within its priority class. Internal payloads
     * retain preference, but a reordered MaSt array alone does not move home. */
    for (pass = 1; pass >= 0; pass--) {
        if (previous && previous->payload_index >= 0) {
            const struct tc_volume *old = &previous->inventory.volumes[previous->payload_index];
            for (i = 0; i < inventory->count; i++)
                if (inventory->volumes[i].builtin == pass && (snapshot->available & (1u << i)) &&
                    !strcmp(old->uuid, inventory->volumes[i].uuid) && !payload_at(snapshot, i, config))
                    return 0;
        }
        for (i = 0; i < inventory->count; i++)
            if (inventory->volumes[i].builtin == pass && (snapshot->available & (1u << i)) &&
                !payload_at(snapshot, i, config))
                return 0;
    }
    return tc_worker_cancelled() ? -1 : 0;
}
