#ifndef TC_LOG_H
#define TC_LOG_H
#include "platform.h"
int timestamped_fprintf(FILE *stream, const char *format, ...);
void timestamped_perror(const char *message);
int tc_log_trim(const char *path);
#endif
