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
/* crc32 is the CRC-32 (zlib's) the source's bytes must have, or -1 to skip
 * the check. Returns 0, -1, or TC_COPY_DISK_DAMAGED when the bytes read do not
 * have it or the disk could not read them (EIO): the file on the disk, or
 * what the disk returns, is damaged. read_out, when not NULL, gets what the
 * damage read: the CRC-32 of the bytes when they did not match,
 * TC_COPY_UNREADABLE when the disk could not read them, else -1. */
#define TC_COPY_DISK_DAMAGED -2
#define TC_COPY_UNREADABLE -2
int tc_copy_file(const char *source, const char *destination, mode_t mode, long long crc32, long long *read_out);
/* The CRC-32 (zlib's) of the file's bytes; -1 with errno when unreadable or
 * when the job reading it was stopped (ECANCELED). */
int tc_file_crc32(const char *path, uint32_t *crc);
#endif
