#include <stdlib.h>
/* Only hashing and signature verification are linked by our programs. If a future
 * caller accidentally invokes a signing/key-generation API, fail rather than use
 * the old all-zero randombytes stub. */
void randombytes(unsigned char *out, unsigned long long n) {
    (void)out; (void)n; abort();
}
