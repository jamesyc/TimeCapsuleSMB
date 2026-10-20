#ifndef TC_WORKER_H
#define TC_WORKER_H
#include "platform.h"

/* Slow setup runs in a short-lived manager child. The daemon event loop stays
 * responsive to topology changes and stop requests while these operations run. */
void tc_worker_begin(const char *operation);
int tc_worker_finish(int result);
int tc_worker_cancelled(void);
int tc_command_run(char *const argv[], unsigned timeout_seconds);
int tc_command_capture(char *const argv[], char *out, size_t capacity, unsigned timeout_seconds);
int tc_worker_result(const void *data, size_t length);
int tc_make_dir(const char *path, mode_t mode);
/* How long reading one payload file may take. A healthy disk returns a 10 MB
 * smbd in seconds; a failing one retries each bad read and returns a chunk
 * now and then, so the read stops here and says so. A run reads a binary at
 * most twice (the check after damage, then the copy), within the manager's
 * staging deadline (STAGE_JOB_MS), and the verdict lands within Activate's
 * and doctor's startup windows (200 s and 180 s from the manager's start,
 * which includes mounting the disk). */
#ifndef TC_PAYLOAD_READ_BUDGET_MS
#define TC_PAYLOAD_READ_BUDGET_MS 90000
#endif
/* crc32 is the CRC-32 (zlib's) the source's bytes must have, or -1 to skip
 * the check. Returns 0, -1, or TC_COPY_DISK_DAMAGED when the bytes read do not
 * have it, or the disk could not read them (EIO) or not within
 * TC_PAYLOAD_READ_BUDGET_MS: the file on the disk, or what the disk returns,
 * is damaged. read_out, when not NULL, gets what the damage read: the CRC-32
 * of the bytes when they did not match, TC_COPY_UNREADABLE when the disk
 * could not read them, TC_COPY_STALLED when it read too slowly, else -1. */
#define TC_COPY_DISK_DAMAGED -2
#define TC_COPY_UNREADABLE -2
#define TC_COPY_STALLED -4
/* elapsed_ms, when not NULL, gets how long a copy that went through took to
 * read the source. */
int tc_copy_file(const char *source, const char *destination, mode_t mode, long long crc32, long long *read_out,
                 long long *elapsed_ms);
/* The CRC-32 (zlib's) of the file's bytes; -1 with errno when unreadable
 * (EIO), past the read budget (ETIMEDOUT), or when the job reading it was
 * stopped (ECANCELED). */
int tc_file_crc32(const char *path, uint32_t *crc);
/* What a payload read that failed with error says about the disk:
 * TC_COPY_UNREADABLE (EIO), TC_COPY_STALLED (ETIMEDOUT, the read budget ran
 * out), else 0, which says nothing about it. */
long long tc_read_damage(int error);
/* Whether host tests make the payload file at path fail with EIO, as a bad
 * sector would: its reads (TC_TEST_EIO), or opening it (TC_TEST_EIO_OPEN,
 * a damaged catalog record). Each holds the path, or "@FILE" for a path FILE
 * holds while it exists. Device builds have no such hook. */
#ifdef TC_NATIVE_TEST
int tc_test_eio(const char *variable, const char *path);
#else
#define tc_test_eio(variable, path) ((void)(variable), (void)(path), 0)
#endif
#endif
