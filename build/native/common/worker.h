#ifndef TC_WORKER_H
#define TC_WORKER_H
#include "platform.h"

/* Slow setup runs in a short-lived manager child. The daemon event loop stays
 * responsive to topology changes and stop requests while these operations run. */
void tc_worker_begin(const char *operation);
int tc_worker_cancelled(void);
int tc_command_run(char *const argv[], unsigned timeout_seconds);
int tc_command_capture(char *const argv[], char *out, size_t capacity, unsigned timeout_seconds);
int tc_worker_result(const void *data, size_t length);
int tc_make_dir(const char *path, mode_t mode);
int tc_copy_file(const char *source, const char *destination, mode_t mode);
#endif
