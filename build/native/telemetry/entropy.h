#ifndef TC_TELEMETRY_ENTROPY_H
#define TC_TELEMETRY_ENTROPY_H
#include "../common/platform.h"

/* Unpredictable bytes for the debug nonce and the DNS query ids, with no
 * descriptor per call: a heartbeat sent while the device is out of
 * descriptors still gets them. The first call reads a 32-byte seed from
 * /dev/urandom (each call retries until one read works); every 32 bytes
 * after that are the first half of SHA-512 of the seed, and the second half
 * replaces the seed, so earlier output cannot be recovered from the state.
 * 0, or -1 with errno set while no seed could be read. */
int tc_random_bytes(unsigned char *out, size_t n);
/* n (at most 64) bytes for an identifier no other call returns: the
 * heartbeat ids and the manager's report tokens. tc_random_bytes' output,
 * or, while no seed can be read, SHA-512 of the monotonic clock, the time,
 * the pid and a call counter: unique within this process, and across
 * processes unless all of those repeat. The daemon and the report child
 * are exec'd, never forked from a seeded process, so no two processes share
 * a seed. */
void tc_unique_bytes(unsigned char *out, size_t n);
#ifdef TC_NATIVE_TEST
/* Forgets the seed, as a new process starts. */
void tc_entropy_restart(void);
#endif
#endif
