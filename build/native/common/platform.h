#ifndef TC_PLATFORM_H
#define TC_PLATFORM_H
#if defined(__linux__) && !defined(_GNU_SOURCE)
#define _GNU_SOURCE
#endif

#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <net/if.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/uio.h>
#if defined(__NetBSD__)
#include <dev/usb/usb.h>
#endif
#include <time.h>
#include <unistd.h>

#ifdef TC_NATIVE_TEST
/* Host tests stand in for a full process table: fork() fails with EAGAIN
 * while the file named by TC_TEST_FORK_FAIL exists. */
#define TC_TEST_FORK_FAILS() (getenv("TC_TEST_FORK_FAIL") && !access(getenv("TC_TEST_FORK_FAIL"), F_OK))
#endif

#endif
