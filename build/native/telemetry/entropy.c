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

void tc_unique_bytes(unsigned char *out, size_t n) {
    static unsigned made;
    unsigned char digest[64];
    char text[96];
    struct timespec now;
    if (!tc_random_bytes(out, n)) return;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) now.tv_sec = now.tv_nsec = 0;
    (void)snprintf(text, sizeof(text), "%ld.%09ld %ld %ld %u", (long)now.tv_sec, (long)now.tv_nsec, (long)time(NULL),
                   (long)getpid(), made++);
    crypto_hash(digest, (const unsigned char *)text, (unsigned long long)strlen(text));
    memcpy(out, digest, n);
}

#ifdef TC_NATIVE_TEST
void tc_entropy_restart(void) {
    memset(seed, 0, sizeof(seed));
    seeded = 0;
}
#endif
