#ifndef TC_TEST_DEVICE_FAULTS_H
#define TC_TEST_DEVICE_FAULTS_H
#include "device.h"

int test_pipe(int fds[2]);
pid_t test_fork(void);
int test_fcntl(int fd, int command, ...);
int test_setpgid(pid_t pid, pid_t group);
int test_clock_gettime(clockid_t clock, struct timespec *value);
int test_select(int count, fd_set *readable, fd_set *writable, fd_set *errors, struct timeval *timeout);
ssize_t test_read(int fd, void *buffer, size_t count);
pid_t test_waitpid(pid_t child, int *status, int options);

/* Only the separately compiled production device.c gets these substitutions.
 * The driver delegates to real syscalls and injects one selected failure. */
#ifdef TC_TEST_DEVICE_FAULTS
#define pipe test_pipe
#define fork test_fork
#define fcntl test_fcntl
#define setpgid test_setpgid
#define clock_gettime test_clock_gettime
#define select test_select
#define read test_read
#define waitpid test_waitpid
#endif
#endif
