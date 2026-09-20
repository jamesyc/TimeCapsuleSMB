#ifndef TC_LOG_H
#define TC_LOG_H
#include "platform.h"
#ifdef TC_NATIVE_TEST
void log_timestamp_prefix(FILE *stream);
#endif
#ifdef TC_NATIVE_TEST
int timestamped_write_message(FILE *stream, const char *message);
#endif
#ifdef TC_NATIVE_TEST
int timestamped_vfprintf(FILE *stream, const char *format, va_list ap);
#endif
int timestamped_fprintf(FILE *stream, const char *format, ...);
void timestamped_perror(const char *message);
int tc_log_trim(const char *path);
#endif
