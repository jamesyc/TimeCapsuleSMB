#include "parent.h"
#include <sys/stat.h>

int tc_parent_pipe(void) {
    struct stat st;
    int flags;
    if (fstat(STDIN_FILENO, &st) || (!S_ISFIFO(st.st_mode) && !S_ISSOCK(st.st_mode)))
        return -1;
    flags = fcntl(STDIN_FILENO, F_GETFL);
    if (flags < 0 || fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK))
        return -1;
    return STDIN_FILENO;
}
void tc_parent_prepare(int fd, fd_set *reads, int *maxfd) {
    if (fd >= 0) {
        FD_SET(fd, reads);
        if (fd > *maxfd)
            *maxfd = fd;
    }
}
int tc_parent_alive(int fd) {
    char ignored[32];
    ssize_t n;
    if (fd < 0)
        return 1;
    do {
        n = read(fd, ignored, sizeof(ignored));
    } while (n < 0 && errno == EINTR);
    return n > 0 || (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK));
}
