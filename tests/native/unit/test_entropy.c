/* entropy.c: one seed read, then no descriptor per call. */
#include "entropy.h"
#include <assert.h>
#include <sys/resource.h>

static struct rlimit saved;

/* Leaves only stdin, stdout and stderr: open() fails with EMFILE. Only the
 * soft limit, which descriptors_back() restores before exit: a sanitizer
 * build's leak check opens /proc then. */
static void no_descriptors(void) {
    struct rlimit three;
    int fd;
    assert(!getrlimit(RLIMIT_NOFILE, &saved));
    three = saved;
    three.rlim_cur = 3;
    assert(!setrlimit(RLIMIT_NOFILE, &three));
    fd = open("/dev/null", O_RDONLY);
    assert(fd < 0 && errno == EMFILE);
}

static void descriptors_back(void) {
    assert(!setrlimit(RLIMIT_NOFILE, &saved));
}

int main(int argc, char **argv) {
    unsigned char a[48], b[48], none[8];
    size_t same = 0, i;
    if (argc == 2 && !strcmp(argv[1], "unseeded")) {
        /* Out of descriptors before any seed: a failure, not a guess. */
        no_descriptors();
        errno = 0;
        assert(tc_random_bytes(none, sizeof(none)) == -1 && errno == EMFILE);
        descriptors_back();
        return 0;
    }
    assert(!tc_random_bytes(a, sizeof(a)));
    /* Seeded: later calls need no descriptor, and never repeat (a 48-byte
     * request takes two hash blocks, each output differing). */
    no_descriptors();
    assert(!tc_random_bytes(b, sizeof(b)));
    for (i = 0; i < sizeof(a); i++) same += a[i] == b[i];
    assert(same < 8 && memcmp(a, a + 32, 16) && memcmp(b, b + 32, 16));
    descriptors_back();
    return 0;
}
