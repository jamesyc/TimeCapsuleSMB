#ifndef TC_CONFIG_H
#define TC_CONFIG_H
#include "platform.h"

/* Literal reader for /mnt/Flash/tcapsulesmb.conf. The file is written by
 * deploy.py's _render_flash_config_assignment(): integers bare, strings via
 * shlex.quote() (bare when safe, otherwise single-quoted with apostrophes
 * emitted as the '"'"' fragment sequence). This decoder handles exactly that
 * syntax plus double-quoted fragments; it never executes anything. */

#ifndef TC_FLASH_CONFIG_PATH
#define TC_FLASH_CONFIG_PATH "/mnt/Flash/tcapsulesmb.conf"
#endif
#define TC_CONFIG_VALUE_MAX 256

/* Returns 0 when decoded, 1 for a missing key, -1 for an unreadable file
 * or invalid value. Missing keys can use defaults; failed reads are unknown. */
int config_read_value(const char *path, const char *key, char *out, size_t out_len);
int config_decode_assignment_value(const char *text, char *out, size_t out_len);
/* 1/0 for a recognised boolean, otherwise fallback. */
int config_bool_value(const char *text, int fallback);
#endif
