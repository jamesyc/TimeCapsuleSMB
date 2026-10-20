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

int tc_samba_settings_read(struct tc_samba_settings *settings, const char *hostname) {
    memset(settings, 0, sizeof(*settings));
    if (tc_runtime_config_load(&settings->config)) {
        fputs("settings: invalid or unavailable runtime configuration\n", stderr);
        return -1;
    }
    if (tc_samba_identity_read(&settings->identity, hostname)) {
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
    if (!input) {
        fprintf(stderr, "stage: %s unreadable: %s\n", source, strerror(errno));
        return -1;
    }
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
    /* Each failure names its cause: rsync is left out of the run for it. */
    if (failed)
        fprintf(stderr, "stage: %s could not be staged (a line too long, or a read or write failed)\n", source);
    else if (!found)
        fprintf(stderr, "stage: %s has no path line\n", source);
    return !found || failed ? -1 : 0;
}

/* Each program's files, published apart: Samba's three and rsync's one. */
static const char *const samba_files[] = {TC_SMBD_CONF, TC_RAM_ROOT "/private/smbpasswd",
                                          TC_RAM_ROOT "/private/username.map"};
static const char *const rsync_files[] = {TC_RSYNC_CONF};

static void discard(const char *const *files, size_t count) {
    size_t i;
    char path[512];
    for (i = 0; i < count; i++) {
        snprintf(path, sizeof(path), "%s.next", files[i]);
        unlink(path);
    }
}
static int publish(const char *const *files, size_t count) {
    size_t i;
    char path[512];
    for (i = 0; i < count; i++) {
        snprintf(path, sizeof(path), "%s.next", files[i]);
        if (rename(path, files[i])) {
            fprintf(stderr, "publish configuration failed: %s: %s\n", files[i], strerror(errno));
            return -1;
        }
    }
    return 0;
}
void tc_samba_discard(void) {
    discard(samba_files, sizeof(samba_files) / sizeof(samba_files[0]));
}
int tc_samba_publish(void) {
    return publish(samba_files, sizeof(samba_files) / sizeof(samba_files[0]));
}
void tc_rsync_discard(void) {
    discard(rsync_files, 1);
}
int tc_rsync_publish(void) {
    return publish(rsync_files, 1);
}

/* Sets result->crc32 to the CRC-32 the payload's checksums file records for
 * name. Deploy writes it with the binaries, one "NAME XXXXXXXX" line each, so
 * a payload carries the checksums of its own files. -1 without the file (a
 * payload from before checksums: nothing to check). Returns 0;
 * TC_COPY_DISK_DAMAGED when the disk cannot read the file or it has no valid
 * line for name, since that is damage too (logged unless the last run saw
 * it); or -1 for another failure to open it. */
static int payload_checksum(const char *payload, const char *name, const struct tc_stage_copy *how,
                            struct tc_stage_result *result) {
    char path[352], text[256], *line, *save = NULL;
    size_t length, n = strlen(name);
    int failed, unreadable = 0;
    FILE *file;
    result->crc32 = -1;
    snprintf(path, sizeof(path), "%s/checksums", payload);
    file = fopen(path, "r");
    if (!file && errno == ENOENT)
        return 0;
    if (!file && errno != EIO) {
        fprintf(stderr, "stage: %s unreadable: %s\n", path, strerror(errno));
        return -1;
    }
    if (!file)
        unreadable = errno;
    else {
        length = fread(text, 1, sizeof(text) - 1, file);
        if (ferror(file))
            unreadable = errno ? errno : EIO;
        failed = unreadable || fgetc(file) != EOF;
        fclose(file);
        text[length] = 0;
        for (line = failed ? NULL : strtok_r(text, "\n", &save); line; line = strtok_r(NULL, "\n", &save))
            if (!strncmp(line, name, n) && line[n] == ' ' && strlen(line + n + 1) == 8 &&
                strspn(line + n + 1, "0123456789abcdef") == 8) {
                result->crc32 = (long long)strtoul(line + n + 1, NULL, 16);
                return 0;
            }
    }
    result->read = TC_STAGE_BAD_CHECKSUMS;
    if (how->seen != TC_STAGE_BAD_CHECKSUMS) {
        if (unreadable)
            fprintf(stderr, "stage: %s unreadable: %s\n", path, strerror(unreadable));
        else
            fprintf(stderr, "stage: %s is damaged: no checksum for %s\n", path, name);
    }
    return TC_COPY_DISK_DAMAGED;
}

/* Which of the files a run needs the payload lacks: result->missing, a bit
 * for each in order (0 for none), returned. Each is logged unless the last
 * run found it missing too: it lasts until a reinstall. */
static int payload_missing(const char *const *paths, size_t count, const struct tc_stage_copy *how,
                           struct tc_stage_result *result) {
    struct stat st;
    size_t i;
    result->missing = 0;
    for (i = 0; i < count; i++) {
#ifdef TC_NATIVE_TEST
        /* Host tests take the payload's disk away once the run has looked
         * for the file TC_TEST_UNMOUNT_AT names. */
        const char *at = getenv("TC_TEST_UNMOUNT_AT"), *mounts = getenv("TC_TEST_MOUNTS");
        if (at && mounts && !strcmp(at, paths[i]))
            (void)truncate(mounts, 0);
#endif
        if (stat(paths[i], &st) && errno == ENOENT) {
            if (!(how->missing & 1 << i))
                fprintf(stderr, "stage: %s is missing\n", paths[i]);
            result->missing |= 1 << i;
        }
    }
    return result->missing;
}

/* A run's hold on the payload's volume: the guard says whether it is still
 * the disk the run started on (tc_storage_guard_valid). */
struct stage_run {
    const struct tc_volume *volume;
    int guard;
};
/* Starts a run: nothing found yet, and the payload's volume held. -1 when
 * there is no payload or it is not mounted. */
static int stage_open(struct stage_run *run, const struct tc_storage_snapshot *storage, struct tc_stage_result *result) {
    result->copy = result->missing = -1;
    result->crc32 = result->read = -1;
    if (storage->payload_index < 0 || (size_t)storage->payload_index >= storage->inventory.count)
        return -1;
    run->volume = &storage->inventory.volumes[storage->payload_index];
    run->guard = tc_storage_guard(run->volume);
    return run->guard < 0 ? -1 : 0;
}
/* Ends a run: one that got through still needs its disk to be the one it
 * started on. A failed run leaves none of its files, and the binary only
 * when it did not copy it. Whether the payload lacks a file, or what its
 * damage read, counts only from a disk that stayed: a disk going away reads
 * as both. */
static int stage_close(struct stage_run *run, int failed, const struct tc_stage_copy *how,
                       struct tc_stage_result *result, void (*discard)(void), const char *binary) {
    if (!tc_storage_guard_valid(run->volume, run->guard)) {
        failed = 1;
        result->missing = -1;
        if (result->copy == TC_COPY_DISK_DAMAGED) {
            result->copy = -1;
            result->read = -1;
        }
    }
    close(run->guard);
    if (failed) {
        discard();
        if (how->copy)
            unlink(binary);
    }
    return failed ? -1 : 0;
}

/* Copies a program's binary to RAM, checked against its payload's checksum.
 * After damage (how->probe), the disk copy is first checked where it is: one
 * that has not changed is neither copied into RAM again nor logged again. */
static int copy_binary(const char *payload, const char *name, const char *source, const char *destination,
                       const struct tc_stage_copy *how, struct tc_stage_result *result) {
    uint32_t crc = 0;
    int checked = payload_checksum(payload, name, how, result);
    if (checked)
        return result->copy = checked;
    if (how->probe) {
        if (tc_file_crc32(source, &crc)) {
            if (errno != EIO) {
                fprintf(stderr, "copy open source failed: %s: %s\n", source, strerror(errno));
                return result->copy = -1;
            }
            result->read = TC_COPY_UNREADABLE;
            if (how->seen != TC_COPY_UNREADABLE)
                fprintf(stderr, "copy verify failed: %s: %s\n", source, strerror(EIO));
            return result->copy = TC_COPY_DISK_DAMAGED;
        }
        if (result->crc32 >= 0 && crc != (uint32_t)result->crc32) {
            result->read = crc;
            if (how->seen != (long long)crc)
                fprintf(stderr, "copy verify failed: %s: CRC-32 read %08x, deployed %08x\n", source, (unsigned)crc,
                        (unsigned)result->crc32);
            return result->copy = TC_COPY_DISK_DAMAGED;
        }
    }
    return result->copy = tc_copy_file(source, destination, 0755, result->crc32, &result->read);
}

int tc_samba_stage(const struct tc_storage_snapshot *storage, const struct tc_samba_settings *settings,
                   const struct tc_stage_copy *how, struct tc_stage_result *result) {
    const struct tc_runtime_config *config = &settings->config;
    struct stage_run run;
    char path[512];
    int failed = 1;
    FILE *file;
    static const char *directories[] = {TC_RAM_ROOT, TC_RAM_ROOT "/sbin", TC_RAM_ROOT "/etc",
                                        TC_RAM_ROOT "/var", TC_RAM_ROOT "/private"};
    size_t i;
    if (stage_open(&run, storage, result))
        return -1;
    if (strlen(settings->nt_hash) != 32 || strspn(settings->nt_hash, "0123456789ABCDEF") != 32)
        goto out;
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
    if (how->copy) {
        const char *needed[] = {storage->smbd_source};
        if (payload_missing(needed, 1, how, result) ||
            copy_binary(storage->payload, "smbd", storage->smbd_source, TC_SMBD_BIN, how, result))
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
    if (finish(file))
        goto out;
    failed = 0;
out:
    /* Only an image whose generation was already stopped may be removed. */
    return stage_close(&run, failed, how, result, tc_samba_discard, TC_SMBD_BIN);
}

int tc_rsync_stage(const struct tc_storage_snapshot *storage, const struct tc_stage_copy *how,
                   struct tc_stage_result *result) {
    struct stage_run run;
    char path[512], conf[352], source[352];
    int failed = 1;
    static const char *directories[] = {TC_RAM_ROOT, TC_RAM_ROOT "/sbin", TC_RAM_ROOT "/etc"};
    const char *needed[] = {conf, source};
    size_t i;
    if (stage_open(&run, storage, result))
        return -1;
    for (i = 0; i < sizeof(directories) / sizeof(directories[0]); i++)
        if (tc_make_dir(directories[i], 0755))
            goto out;
    snprintf(path, sizeof(path), "%s/ShareRoot", run.volume->root);
    if (tc_make_dir(path, 0755))
        goto out;
    /* What rsync needs from the payload, which only a reinstall puts back
     * (the binary only for a copy); then the small configuration, so a
     * payload that cannot give it costs no copy of the binary. */
    snprintf(conf, sizeof(conf), "%s/rsyncd.conf", storage->payload);
    snprintf(source, sizeof(source), "%s/rsync", storage->payload);
    if (payload_missing(needed, how->copy ? 2 : 1, how, result) || stage_rsync_config(storage->payload, run.volume->root))
        goto out;
    if (how->copy && copy_binary(storage->payload, "rsync", source, TC_RSYNC_BIN, how, result))
        goto out;
    failed = 0;
out:
    return stage_close(&run, failed, how, result, tc_rsync_discard, TC_RSYNC_BIN);
}
