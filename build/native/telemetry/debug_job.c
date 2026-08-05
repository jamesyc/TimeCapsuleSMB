#include "telemetry.h"

static int write_download(const char *path, const unsigned char *data, size_t len) {
    size_t off = 0;
    int result = 1;
    /* O_EXCL refuses existing files and symlinks. A partial download is never
     * executable. Only this parent writes files; curl only writes to a pipe. */
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (fd < 0) {
        fprintf(stderr, "telemetry: cannot create %s: %s\n", path, strerror(errno));
        return 1;
    }
    while (off < len && !telemetry_stop) {
        ssize_t n = write(fd, data + off, len - off);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) {
            fprintf(stderr, "telemetry: cannot write %s: %s\n", path, strerror(errno));
            goto done;
        }
        off += (size_t)n;
    }
    if (off == len) result = 0;
done:
    if (close(fd)) {
        fprintf(stderr, "telemetry: cannot close %s: %s\n", path, strerror(errno));
        result = 1;
    }
    return result;
}

int telemetry_debug_job(const char *reason, int lock_fd) {
    unsigned char *binary = NULL, *signature = NULL;
    size_t binary_len = 0, signature_len = 0;
    int result = 1, status, verified;
    pid_t child;

    if (telemetry_http(TC_DEBUG_BASE_URL TC_TELEMETRY_LANE TC_DEBUG_QUERY, NULL,
                       &binary, &binary_len, TC_DEBUG_MAX)) goto done;
    if (telemetry_stop || !binary_len || write_download(TC_DEBUG_PATH, binary, binary_len)) goto done;
    if (telemetry_http(TC_DEBUG_BASE_URL TC_TELEMETRY_LANE ".sig" TC_DEBUG_QUERY, NULL,
                       &signature, &signature_len, TC_SIGNATURE_MAX)) goto done;
    if (telemetry_stop || write_download(TC_DEBUG_SIGNATURE_PATH, signature, signature_len)) goto done;
    verified = telemetry_verify(binary, binary_len, signature, signature_len);
    /* The signature is no longer needed, even if verification failed. Do not
     * silently proceed to execution if its removal fails. */
    if (telemetry_remove_file(TC_DEBUG_SIGNATURE_PATH)) goto done;
    if (!verified) {
        fputs("telemetry: debug executable signature invalid\n", stderr);
        goto done;
    }
    free(binary); binary = NULL;
    free(signature); signature = NULL;
    if (telemetry_stop) goto done;
    if (chmod(TC_DEBUG_PATH, 0700)) {
        perror("telemetry: chmod debug");
        goto done;
    }
    child = fork();
    if (child < 0) { perror("telemetry: fork debug"); goto done; }
    if (child == 0) {
        char descriptor[32];
        int flags = fcntl(lock_fd, F_GETFD);
        /* Keep ownership across exec and parent death. The debug program must
         * retain this FD (and pass it to any detached worker) until work ends.
         * FD flags are per-process; curl still sees CLOEXEC in the parent. */
        if (flags < 0 || fcntl(lock_fd, F_SETFD, flags & ~FD_CLOEXEC)) _exit(126);
        snprintf(descriptor, sizeof(descriptor), "%d", lock_fd);
        if (setenv("TC_DEBUG_LOCK_FD", descriptor, 1)) _exit(126);
        signal(SIGTERM, SIG_DFL); signal(SIGINT, SIG_DFL); signal(SIGPIPE, SIG_DFL);
        execl(TC_DEBUG_PATH, TC_DEBUG_PATH, reason, (char *)NULL);
        _exit(127);
    }
    /* TERM stops future scheduling but deliberately lets this child finish. */
    do { result = waitpid(child, &status, 0) < 0 ? -1 : 0; } while (result < 0 && errno == EINTR);
    result = result == 0 && WIFEXITED(status) && WEXITSTATUS(status) == 0 ? 0 : 1;
done:
    free(binary); free(signature);
    /* main closes its lock reference and reacquires a fresh lock before
     * cleanup. A surviving descendant may still own the inherited lock. */
    return result;
}
