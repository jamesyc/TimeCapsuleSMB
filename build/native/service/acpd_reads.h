#ifndef TC_ACPD_READS_H
#define TC_ACPD_READS_H
#include <stddef.h>

/* Timed reads of ACPd's TCP listener, sent as the manager TERMs Apple's
 * diskd, to measure how far behind ACPd's main thread is (README F19).
 *
 * ACPd's main thread both accepts TCP 5009 and handles its posted codes,
 * code 14's synchronous diskd.getVolumeCounts among them. Codes are bytes in
 * a level-triggered pipe and the thread takes one kqueue event per wakeup,
 * so the kernel serves the pipe and the listener in turn: before each accept
 * the thread handles at most one queued code. A read that waits behind a
 * code shows up slower than a lone one; a read stuck behind a code 14 diskd
 * never answers gets no answer at all. Any reply counts, as it proves the
 * accept. Nothing waits on the result, though each read is one more accept
 * for ACPd's main thread between its codes. */

#define TC_ACPD_READS 8
#ifndef TC_ACPD_READ_MS
#define TC_ACPD_READ_MS 15000 /* doctor's ACPD_READ_DEADLINE_SECONDS: a busy ACPd answers within it */
#endif
#ifndef TC_ACPD_PORT
#define TC_ACPD_PORT 5009
#endif

/* Up to TC_ACPD_READS reads of syUT, each sent once the last one answered,
 * to ::1 and then 127.0.0.1 (whichever ACPd listens on), with the admin
 * password keyed in as integrations/acp.py does. Writes "ms=A,B,..." (ms
 * to each first reply byte) and, when one went unanswered, " timeout=K"
 * (no reply within TC_ACPD_READ_MS), " refused=K" (no listener on either
 * address), " reset=K" (the connection was reset before any reply, as a
 * listener that closes resets its queue), " stopped=K" (the job was
 * stopped) or " error=K:ERRNO", K counting from 1. */
void tc_acpd_reads(const char *password, size_t length, char *out, size_t size);
#endif
