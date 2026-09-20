#ifndef TC_PARENT_H
#define TC_PARENT_H
#include "platform.h"

/* A supervisor may provide stdin as an anonymous lifetime pipe, just as
 * foreground smbd supports. No protocol bytes or PID/status file are needed. */
int tc_parent_pipe(void);
int tc_parent_alive(int fd);
void tc_parent_prepare(int fd, fd_set *reads, int *maxfd);
#endif
