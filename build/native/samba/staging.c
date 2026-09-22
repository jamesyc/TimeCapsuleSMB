#include "staging.h"
#include "../common/worker.h"
#include <sys/stat.h>
#include <dirent.h>

static int clear_directory(const char *path, unsigned depth) {
    DIR *dir;
    struct dirent *entry;
    struct stat st;
    int result = 0;
    if (lstat(path, &st))
        return errno == ENOENT ? 0 : -1;
    if (!S_ISDIR(st.st_mode) || depth > 8)
        return -1;
    dir = opendir(path);
    if (!dir)
        return -1;
    for (;;) {
        char child[1024];
        errno = 0;
        entry = readdir(dir);
        if (!entry) {
            if (errno)
                result = -1;
            break;
        }
        if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, ".."))
            continue;
        if (tc_worker_cancelled() ||
            snprintf(child, sizeof(child), "%s/%s", path, entry->d_name) >= (int)sizeof(child) ||
            lstat(child, &st)) {
            result = -1;
            break;
        }
        if (S_ISDIR(st.st_mode)) {
            if (clear_directory(child, depth + 1) || rmdir(child)) {
                result = -1;
                break;
            }
        } else if (unlink(child)) {
            result = -1;
            break;
        }
    }
    closedir(dir);
    return result;
}
int tc_samba_clear_locks(void) {
    if (clear_directory(TC_LOCKS_ROOT, 0))
        return -1;
    if (unlink(TC_RAM_ROOT "/var/smbd.pid") && errno != ENOENT)
        return -1;
    return 0;
}

static const char *prepared[] = {TC_SMBD_CONF, TC_RAM_ROOT "/private/smbpasswd",
                                 TC_RAM_ROOT "/private/username.map", TC_RSYNC_CONF};

int tc_samba_settings_read(struct tc_samba_settings *settings) {
    memset(settings, 0, sizeof(*settings));
    if (tc_runtime_config_load(&settings->config)) {
        fputs("settings: invalid or unavailable runtime configuration\n", stderr);
        return -1;
    }
    if (tc_samba_identity_read(&settings->identity)) {
        fputs("settings: device identity unavailable\n", stderr);
        return -1;
    }
    if (device_nt_hash(settings->nt_hash)) {
        fputs("settings: device authentication unavailable\n", stderr);
        return -1;
    }
    return 0;
}

static FILE *prepare_file(const char *path) {
    char next[512];
    int fd;
    snprintf(next, sizeof(next), "%s.next", path);
    if (unlink(next) && errno != ENOENT) {
        fprintf(stderr, "prepare configuration failed: %s: %s\n", next, strerror(errno));
        return NULL;
    }
    fd = open(next, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (fd < 0) {
        fprintf(stderr, "create configuration failed: %s: %s\n", next, strerror(errno));
        return NULL;
    }
    FILE *file = fdopen(fd, "w");
    if (!file)
        close(fd);
    return file;
}
static int finish(FILE *file) {
    int failed = ferror(file) || fflush(file) || fsync(fileno(file));
    if (fclose(file))
        failed = 1;
    return failed ? -1 : 0;
}
static int stage_rsync_config(const char *payload, const char *root) {
    char source[352], line[2048];
    int found = 0, failed = 0;
    FILE *input, *output;
    snprintf(source, sizeof(source), "%s/rsyncd.conf", payload);
    input = fopen(source, "r");
    if (!input)
        return -1;
    output = prepare_file(TC_RSYNC_CONF);
    if (!output) {
        fclose(input);
        return -1;
    }
    while (fgets(line, sizeof(line), input)) {
        char *p = line;
        if (!strchr(line, '\n') && !feof(input)) {
            failed = 1;
            break;
        }
        while (isspace((unsigned char)*p))
            p++;
        if (!strncmp(p, "path", 4)) {
            p += 4;
            while (isspace((unsigned char)*p))
                p++;
            if (*p == '=') {
                fprintf(output, "path = %s/ShareRoot\n", root);
                found = 1;
                continue;
            }
        }
        fputs(line, output);
    }
    if (ferror(input))
        failed = 1;
    fclose(input);
    if (finish(output))
        failed = 1;
    return !found || failed ? -1 : 0;
}

void tc_samba_discard(void) {
    size_t i;
    char path[512];
    for (i = 0; i < sizeof(prepared) / sizeof(prepared[0]); i++) {
        snprintf(path, sizeof(path), "%s.next", prepared[i]);
        unlink(path);
    }
}
int tc_samba_publish(int rsync) {
    size_t i;
    char path[512];
    for (i = 0; i < (rsync ? 4u : 3u); i++) {
        snprintf(path, sizeof(path), "%s.next", prepared[i]);
        if (rename(path, prepared[i])) {
            fprintf(stderr, "publish configuration failed: %s: %s\n", prepared[i], strerror(errno));
            return -1;
        }
    }
    return 0;
}

int tc_samba_stage(const struct tc_storage_snapshot *storage, const struct tc_samba_settings *settings,
                   int copy_smbd, int copy_rsync) {
    const struct tc_runtime_config *config = &settings->config;
    const struct tc_volume *volume;
    char path[512], source[352];
    int guard = -1, failed = 1;
    FILE *file;
    static const char *directories[] = {TC_RAM_ROOT, TC_RAM_ROOT "/sbin", TC_RAM_ROOT "/etc",
                                        TC_RAM_ROOT "/var", TC_RAM_ROOT "/private"};
    size_t i;
    if (storage->payload_index < 0 || (size_t)storage->payload_index >= storage->inventory.count ||
        strlen(settings->nt_hash) != 32 || strspn(settings->nt_hash, "0123456789ABCDEF") != 32)
        return -1;
    volume = &storage->inventory.volumes[storage->payload_index];
    guard = tc_storage_guard(volume);
    if (guard < 0)
        return -1;
    for (i = 0; i < sizeof(directories) / sizeof(directories[0]); i++)
        if (tc_make_dir(directories[i], i == 4 ? 0700 : 0755))
            goto out;
    snprintf(path, sizeof(path), "%s/logs", storage->payload);
    if (tc_make_dir(path, 0755))
        goto out;
    snprintf(path, sizeof(path), "%s/logs/cores", storage->payload);
    if (tc_make_dir(path, 0700))
        goto out;
    snprintf(path, sizeof(path), "%s/logs/cores/smbd", storage->payload);
    if (tc_make_dir(path, 0700))
        goto out;
    if (config->netbsd4) {
        snprintf(path, sizeof(path), "%s/cache", storage->payload);
        if (tc_make_dir(path, 0700))
            goto out;
    }
    /* A 15 MiB RAM disk cannot hold two smbd images. The manager must stop
     * the old generation before asking for replacement; there is no rollback
     * generation. A failed job is retried from the payload after cleanup. */
    if (copy_smbd && tc_copy_file(storage->smbd_source, TC_SMBD_BIN, 0755))
        goto out;
    if (config->rsync) {
        snprintf(path, sizeof(path), "%s/ShareRoot", volume->root);
        if (tc_make_dir(path, 0755))
            goto out;
        snprintf(source, sizeof(source), "%s/rsync", storage->payload);
        if (copy_rsync && tc_copy_file(source, TC_RSYNC_BIN, 0755))
            goto out;
        if (stage_rsync_config(storage->payload, volume->root))
            goto out;
    }
    file = prepare_file(TC_SMBD_CONF);
    if (!file)
        goto out;
    int render_failed =
        tc_samba_render(file, config, &settings->identity, storage->payload, &storage->shares);
    if (finish(file) || render_failed)
        goto out;
    file = prepare_file(TC_RAM_ROOT "/private/smbpasswd");
    if (!file)
        goto out;
    fprintf(file, "root:0:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX:%s:[U          ]:LCT-%08lX:\n", settings->nt_hash,
            (unsigned long)time(NULL));
    if (finish(file))
        goto out;
    file = prepare_file(TC_RAM_ROOT "/private/username.map");
    if (!file)
        goto out;
    fputs("!root = root\nroot = *\n", file);
    if (finish(file) || !tc_storage_guard_valid(volume, guard))
        goto out;
    failed = 0;
out:
    close(guard);
    if (failed) {
        tc_samba_discard();
        /* Only images whose generation was already stopped may be removed. */
        if (copy_smbd)
            unlink(TC_SMBD_BIN);
        if (copy_rsync)
            unlink(TC_RSYNC_BIN);
    }
    return failed ? -1 : 0;
}
