#include "entropy.h"
#include "../vendor/tweetnacl.h"

static unsigned char seed[32];
static int seeded;

static int read_seed(void) {
    size_t off = 0;
    int fd = open("/dev/urandom", O_RDONLY), error = 0;
    if (fd < 0) return -1;
    while (off < sizeof(seed)) {
        ssize_t got = read(fd, seed + off, sizeof(seed) - off);
        if (got < 0 && errno == EINTR) continue;
        if (got <= 0) { error = got < 0 ? errno : EIO; break; }
        off += (size_t)got;
    }
    close(fd);
    if (error) {
        errno = error;
        return -1;
    }
    seeded = 1;
    return 0;
}

int tc_random_bytes(unsigned char *out, size_t n) {
    unsigned char digest[64];
    if (!seeded && read_seed()) return -1;
    while (n) {
        size_t take = n < 32 ? n : 32;
        crypto_hash(digest, seed, sizeof(seed));
        memcpy(out, digest, take);
        memcpy(seed, digest + 32, sizeof(seed));
        out += take;
        n -= take;
    }
    return 0;
}
