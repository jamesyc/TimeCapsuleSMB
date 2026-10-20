#ifndef TC_SYSCTL_STUB_H
#define TC_SYSCTL_STUB_H
#include "common/platform.h"

/* Only NetBSD's declarations and the syscall are replaced. kernel.c's
 * discovery, caching, reads, writes and platform policy remain production code.
 * Deliberately distinct node numbers catch wrong MIB construction. */
#define TC_SYSCTL_NATIVE_TEST 1
#define CTL_KERN 1
#define CTL_NET 4
#define CTL_QUERY (-2)
#define KERN_SBMAX 101
#define KERN_MAXFILES 103
#define KERN_MAXPROC 107
#define SYSCTL_VERSION 0x01000000u
struct sysctlnode {
    unsigned sysctl_flags;
    int sysctl_num;
    char sysctl_name[32];
};
int tc_test_sysctl(const int *, unsigned, void *, size_t *, const void *, size_t);
#define sysctl tc_test_sysctl
#endif
