#include "common/log.h"
#include <assert.h>
#include <sys/stat.h>
int main(void) {
    int fd = open("runtime.log", O_RDWR | O_CREAT | O_APPEND, 0600);
    char bytes[1000];
    struct stat before, after;
    unsigned i;
    assert(fd >= 0);
    memset(bytes, 'a', sizeof(bytes));
    for (i = 0; i < 40; i++)
        assert(write(fd, bytes, sizeof(bytes)) == sizeof(bytes));
    assert(write(fd, "last line\n", 10) == 10 && !fstat(fd, &before));
    assert(!tc_log_trim("runtime.log") && !stat("runtime.log", &after));
    assert(before.st_ino == after.st_ino && after.st_size == 16384);
    assert(write(fd, "still open\n", 11) == 11);
    memset(bytes, 0, sizeof(bytes));
    assert(pread(fd, bytes, 21, 16374) == 21 && !strcmp(bytes, "last line\nstill open\n"));
    assert(!symlink("runtime.log", "link") && tc_log_trim("link") < 0);
    close(fd);
    return 0;
}
