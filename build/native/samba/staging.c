#include "staging.h"
#include "../common/worker.h"
#include <sys/stat.h>
#include <dirent.h>
#include <sys/statfs.h>
#include <string.h>

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

/* For swap-backed tmpfs (mount_mfs), remount to clear swap-held mutexes */
static int remount_tmpfs_if_mounted(const char *path) {
    struct statfs st;
    if (statfs(path, &st) == 0 && (st.f_flags & MNT_LOCAL) &&
        strstr(st.f_fstypename, "mfs")) {
        if (unmount(path, 0) == 0) {
            mkdir(path, 0755);
        }
    }
    return 0;
}

/* Counted from the files rather than statvfs(): a NetBSD tmpfs's free count
 * also shrinks with free memory, and st_blocks is the space a file takes on
 * tmpfs and on NetBSD 4's MFS alike. A file gone between readdir() and lstat()
 * takes none. */
static long long directory_bytes(const char *path, unsigned depth) {
    DIR *dir = opendir(path);
    struct dirent *entry;
    struct stat st;
    long long total = 0;
    if (!dir)
        return -1;
    while ((entry = readdir(dir))) {
        char child[1024];
        if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..") ||
            snprintf(child, sizeof(child), "%s/%s", path, entry->d_name) >= (int)sizeof(child) || lstat(child, &st))
            continue;
        total += (long long)st.st_blocks * 512;
        if (S_ISDIR(st.st_mode) && depth == 0) {
            long long inner = directory_bytes(child, 1);
            if (inner > 0)
                total += inner;
        }
    }
    closedir(dir);
    return total;
}
long long tc_samba_locks_bytes(void) {
    return directory_bytes(TC_LOCKS_ROOT, 0);
}
int tc_samba_clear_locks(void) {
    if (clear_directory(TC_LOCKS_ROOT, 0))
        return -1;
    if (unlink(TC_RAM_ROOT "/var/smbd.pid") && errno != ENOENT)
        return -1;
    remount_tmpfs_if_mounted(TC_LOCKS_ROOT);
    remount_tmpfs_if_mounted(TC_RAM_ROOT "/var");
    return 0;
}
