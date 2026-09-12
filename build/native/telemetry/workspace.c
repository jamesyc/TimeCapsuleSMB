#include "telemetry.h"
#include <sys/file.h>
#include <syslog.h>

void telemetry_workspace_error(const char *operation, const char *path) {
    const char *error = strerror(errno);
    fprintf(stderr, "telemetry: cannot %s %s: %s\n", operation, path, error);
    /* Daemon stderr is discarded by the manager. Use the system logger rather
     * than holding an FD into the manager's replaceable RAM log or adding one. */
    syslog(LOG_DAEMON | LOG_ERR, "telemetry: cannot %s %s: %s", operation, path, error);
}

int telemetry_lock(void) {
    int fd, flags, saved_errno;
    struct stat before, opened;

    /* Lock the existing RAM mount directory, never a created marker file or
     * a directory that Samba's reset path can delete and replace. The mount
     * may be shared; exclusive file creation protects our two reserved names. */
    if (lstat(TC_TELEMETRY_WORK_ROOT, &before)) return -1;
    /* In a shared writable directory, sticky semantics are required so other
     * users cannot replace our verified executable between write and exec. */
    if (!S_ISDIR(before.st_mode) || before.st_uid != geteuid() ||
        ((before.st_mode & 022) && !(before.st_mode & S_ISVTX))) {
        errno = EPERM;
        return -1;
    }
    fd = open(TC_TELEMETRY_WORK_ROOT, O_RDONLY);
    if (fd < 0) return -1;
    if (fstat(fd, &opened)) goto fail;
    if (before.st_dev != opened.st_dev || before.st_ino != opened.st_ino) {
        errno = EAGAIN;
        goto fail;
    }
    if (flock(fd, LOCK_EX | LOCK_NB)) goto fail;
    flags = fcntl(fd, F_GETFD);
    if (flags < 0 || fcntl(fd, F_SETFD, flags | FD_CLOEXEC)) goto fail;
    return fd;
fail:
    saved_errno = errno;
    close(fd);
    errno = saved_errno;
    return -1;
}

int telemetry_remove_file(const char *path) {
    /* unlink removes a symlink itself, never its target. It intentionally
     * refuses directories: telemetry owns files, not the whole RAM tree. */
    if (unlink(path) == 0 || errno == ENOENT) return 0;
    telemetry_workspace_error("remove", path);
    return 1;
}

int telemetry_cleanup_locked(void) {
    int result = telemetry_remove_file(TC_DEBUG_SIGNATURE_PATH);
    if (telemetry_remove_file(TC_DEBUG_PATH)) result = 1;
    return result;
}

int telemetry_recover(void) {
    int fd = telemetry_lock();
    int result;
    if (fd < 0) {
        if (errno == EAGAIN || errno == EWOULDBLOCK) return TC_EXIT_BUSY;
        telemetry_workspace_error("lock", TC_TELEMETRY_WORK_ROOT);
        return 1;
    }
    result = telemetry_cleanup_locked();
    close(fd);
    return result;
}
