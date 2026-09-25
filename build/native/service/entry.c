#include "service.h"

int tc_manager_main(int argc, char **argv);
int tc_service_helper_main(int argc, char **argv);
int tc_discovery_main(int argc, char **argv);
int tc_telemetry_main(int argc, char **argv);

#if defined(__NetBSD__)
#include <sys/mman.h>

/* Apple's NetBSD 4 and NetBSD 6 kernels lose writes to a static binary's
 * initialized data: handling the first write to a .data page, UVM fault-ahead
 * maps the neighbouring pages (4 below, 3 above) from the executable again,
 * dropping this process's changes to them, and a forked child's first writes
 * drop its parent's. MADV_RANDOM turns fault-ahead off for the writable
 * segment; every role and child inherits it. .preinit_array opens the part of
 * the segment that is ever written and "end" closes .bss (both from the linker
 * script; "end" is what libc's sbrk() starts from, and is right where _end is
 * not). Samba's talloc does the same for smbd (Samba patch 0046). Running it as a
 * constructor, first among them where GCC 4.3+ has priorities (the NetBSD 6
 * lane), leaves only libc's own startup writes before it. The build fails if the
 * binary lacks this call (build/_data_segment_check.sh). */
extern char data_first[] __asm__("__preinit_array_start");
extern char data_end[] __asm__("end");

#if __GNUC__ > 4 || (__GNUC__ == 4 && __GNUC_MINOR__ >= 3)
__attribute__((constructor(101)))
#else
__attribute__((constructor))
#endif
static void disable_data_faultahead(void) {
    uintptr_t page = (uintptr_t)getpagesize();
    uintptr_t start = (uintptr_t)data_first & ~(page - 1);
    uintptr_t end = ((uintptr_t)data_end + page - 1) & ~(page - 1);
    if (end > start) (void)madvise((void *)start, end - start, MADV_RANDOM);
}
#endif

/* One static image, independent processes. Each daemon still owns its own
 * collector/history; this dispatcher does not introduce a plan IPC protocol. */
int main(int argc, char **argv) {
    if (argc > 1 && !strcmp(argv[1], "manager"))
        return tc_manager_main(argc - 1, argv + 1);
    if (argc > 1 && !strcmp(argv[1], "discovery"))
        return tc_discovery_main(argc - 1, argv + 1);
    if (argc > 1 && !strcmp(argv[1], "telemetry"))
        return tc_telemetry_main(argc - 1, argv + 1);
    return tc_service_helper_main(argc, argv);
}
