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
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/uio.h>
#if defined(__NetBSD__)
#include <dev/usb/usb.h>
#endif
#include <time.h>
#include <unistd.h>

#ifndef TC_UNUSED
#if defined(__GNUC__)
#define TC_UNUSED __attribute__((unused))
#else
#define TC_UNUSED
#endif
#endif

/* Tests link internal helpers as separate objects; device builds keep them
 * private so NetBSD 4's linker need not garbage-collect unreferenced code. */
#ifdef TC_NATIVE_TEST
#define TC_LOCAL
#else
#define TC_LOCAL static TC_UNUSED
#endif

#endif
