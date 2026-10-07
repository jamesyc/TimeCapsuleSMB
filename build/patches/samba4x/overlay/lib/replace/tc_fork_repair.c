/*
 * Time Capsule appliance builds for Apple's NetBSD 6 kernel (patch 0070; the
 * native service and rsync compile this same file). Nothing here is meant for
 * upstream Samba.
 *
 * The NetBSD 6 kernel can show a child the writes its parent makes after
 * fork(). The ARM pmap marks a page unreferenced by making its PTE invalid
 * while keeping the rest of the PTE, and the page daemon does that under
 * memory pressure (so does madvise(MADV_DONTNEED)). fork() write-protects the
 * parent's pages with pmap_protect(), which skips invalid PTEs, so the page's
 * pv entry keeps PVF_WRITE. The parent's next write then goes through the
 * pmap's "modified" emulation, which makes the page writable again without a
 * copy-on-write fault, and the parent writes into the page it now shares with
 * the child. NetBSD 4's pmap_protect() treats any non-zero PTE as present and
 * is not affected. Proven on the device (2026-09-29):
 * a connection child spun forever freeing an smbd parent context whose child
 * list came half from before and half from after the fork.
 *
 * Right before each fork() this file removes the parent's mappings of its own
 * private memory, so the kernel's stale page state goes with them: every
 * private writable mapping is protected PROT_NONE and straight back (the
 * pmap_remove() that mprotect(PROT_NONE) does drops the pv entries), then one
 * word per page of the stack in use is written back unchanged. fork() then
 * write-protects every page the parent still maps, and copy-on-write works. The
 * process cannot list its own mappings (this kernel has no vm.proc.map sysctl),
 * so linker wrappers (--wrap=mmap,_mmap,munmap,mremap,mprotect) keep a registry
 * of the private ones, read-only ones too: the parent cannot write to those
 * after fork(), so the repair skips them, but mprotect() can make one writable
 * and only a recorded range follows that. The data segment runs from
 * __preinit_array_start to sbrk(0), and the stack from the current frame to
 * __ps_strings. The repaired ranges also get MADV_RANDOM: libc's fork() runs
 * its atfork handlers (jemalloc writes its locks) between the repair and the
 * system call, and UVM fault-ahead would enter the faulting page's neighbours
 * unreferenced, which is the leaking state again (seen on the device in .bss).
 * Only that window needs it: once fork() has write-protected the pages,
 * whatever fault-ahead enters later is removed by the next fork()'s repair
 * before it matters. So the parent and the child put normal advice back as soon
 * as fork() returns, except on the static data and .bss, where patch 0046 keeps
 * MADV_RANDOM for its own reason (the service and rsync do the same). If the
 * registry ever misses a mapping (it is full), fork() falls back to locking all
 * memory across the call: the kernel then gives the child private copies at
 * once (slower, and the child's memory grows by the parent's).
 *
 * Two parts, chosen by define:
 * - TC_FORK_REPAIR: the registry, the repair and tc_fork_repair_fork().
 *   libreplace includes this part (Samba also links a shared libreplace).
 * - TC_FORK_REPAIR_WRAPPERS: the linker wrappers. Their __real_ references
 *   must be strong (a weak one is not resolved from libc's archive, and libc's
 *   own TLS setup then got a failed mmap), so they only link where --wrap is
 *   given: Samba's build compiles this part into a separate object for its
 *   static links only; the service and rsync compile both parts.
 */

#if defined(TC_FORK_REPAIR) || defined(TC_FORK_REPAIR_WRAPPERS)
#include <sys/types.h>
#include <sys/mman.h>
#include <unistd.h>
#endif

#ifdef TC_FORK_REPAIR

#include <sys/types.h>
#include <sys/mman.h>
#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#if defined(__NetBSD__) && !defined(TC_FORK_REPAIR_TEST)
#include <sys/exec.h>
/*
 * Set by csu to the process's ps_strings, at the very top of its stack; and
 * the first part of the writable segment that is ever written (only .eh_frame
 * precedes it), as patch 0046's constructor uses it. Both exist only in an
 * executable. Weak, so the shared libreplace Samba also links (with
 * -no-undefined) still links; there they are zero and fork() uses the
 * fallback. "end" closes .bss, where the sbrk() heap starts; patch 0046, the
 * service and rsync keep MADV_RANDOM up to there.
 */
extern struct ps_strings *__ps_strings __attribute__((weak));
extern char tc_fork_repair_data_first[] __asm__("__preinit_array_start") __attribute__((weak));
extern char tc_fork_repair_static_end[] __asm__("end") __attribute__((weak));
#endif

#ifndef TC_FORK_REPAIR_SLOTS
#define TC_FORK_REPAIR_SLOTS 512
#endif

/*
 * Only the host tests build with AddressSanitizer (TC_NATIVE_SANITIZERS).
 * The repair reads and rewrites whole stack pages from its own frame up,
 * which ASan reports, and ASan can move the local it starts from onto a
 * separate "fake stack", so the pages it walked were not the stack at all.
 * An uninstrumented function keeps its locals on the real stack. Empty in
 * every build for the devices, so their code is unchanged.
 */
#if defined(__has_feature)
#if __has_feature(address_sanitizer)
#define TC_FORK_REPAIR_NO_ASAN __attribute__((no_sanitize_address))
#endif
#endif
#if !defined(TC_FORK_REPAIR_NO_ASAN) && defined(__SANITIZE_ADDRESS__)
#define TC_FORK_REPAIR_NO_ASAN __attribute__((no_sanitize_address))
#endif
#ifndef TC_FORK_REPAIR_NO_ASAN
#define TC_FORK_REPAIR_NO_ASAN
#endif

struct tc_fork_repair_range {
	uintptr_t start;
	uintptr_t end;
	int prot;
};

/* Sorted by start, never overlapping; adjacent ranges with the same
 * protection are merged. */
static struct tc_fork_repair_range tc_fork_repair_map[TC_FORK_REPAIR_SLOTS];
static unsigned tc_fork_repair_count;
static unsigned tc_fork_repair_capacity = TC_FORK_REPAIR_SLOTS;
/* Sticky: once one mapping went unrecorded the registry is incomplete. */
static int tc_fork_repair_overflow;
/* How many fork() calls used the locked-memory fallback, and how many of those
 * could not lock memory. */
unsigned long tc_fork_repair_fallbacks;
unsigned long tc_fork_repair_lock_failures;
/* What the last repair gave MADV_RANDOM beyond the registry: the sbrk() heap
 * part of the data segment and the stack; the fork() caller undoes it. */
static int tc_fork_repair_advised;
static uintptr_t tc_fork_repair_heap[2], tc_fork_repair_stack[2];

pid_t tc_fork_repair_fork(pid_t (*real_fork)(void));
int tc_fork_repair_lookup(const void *address, int *prot);
void tc_fork_repair_note_map(void *address, size_t length, int prot, int flags);
void tc_fork_repair_note_unmap(void *address, size_t length);
void tc_fork_repair_note_remap(void *old_address, size_t old_length, void *new_address, size_t new_length);
void tc_fork_repair_note_protect(void *address, size_t length, int prot);
int tc_fork_repair_prepare(void);
int tc_fork_repair_bounds(uintptr_t *data_start, uintptr_t *data_end, uintptr_t *stack_top);

static uintptr_t tc_fork_repair_page(void)
{
	static uintptr_t page;

	if (page == 0) {
		page = (uintptr_t)getpagesize();
	}
	return page;
}

static uintptr_t tc_fork_repair_down(uintptr_t address)
{
	return address & ~(tc_fork_repair_page() - 1);
}

static uintptr_t tc_fork_repair_up(uintptr_t address)
{
	return (address + tc_fork_repair_page() - 1) & ~(tc_fork_repair_page() - 1);
}

/* Drop [start, end) from the registry, splitting a range that covers it. */
static void tc_fork_repair_remove(uintptr_t start, uintptr_t end)
{
	unsigned i = 0;

	while (i < tc_fork_repair_count) {
		struct tc_fork_repair_range *r = &tc_fork_repair_map[i];

		if (r->end <= start || r->start >= end) {
			i++;
			continue;
		}
		if (r->start < start && r->end > end) {
			/* The removed part is inside: keep both sides. */
			if (tc_fork_repair_count == tc_fork_repair_capacity) {
				tc_fork_repair_overflow = 1;
				return;
			}
			memmove(r + 2, r + 1, (tc_fork_repair_count - i - 1) * sizeof(*r));
			r[1].start = end;
			r[1].end = r->end;
			r[1].prot = r->prot;
			r->end = start;
			tc_fork_repair_count++;
			return;
		}
		if (r->start < start) {
			r->end = start;
			i++;
		} else if (r->end > end) {
			r->start = end;
			i++;
		} else {
			memmove(r, r + 1, (tc_fork_repair_count - i - 1) * sizeof(*r));
			tc_fork_repair_count--;
		}
	}
}

/* Record [start, end) with prot; the caller removed any overlap first. */
static void tc_fork_repair_insert(uintptr_t start, uintptr_t end, int prot)
{
	unsigned i = 0;
	struct tc_fork_repair_range *r;

	if (start >= end || prot == PROT_NONE) {
		return;
	}
	while (i < tc_fork_repair_count && tc_fork_repair_map[i].start < start) {
		i++;
	}
	if (i > 0 && tc_fork_repair_map[i - 1].end == start && tc_fork_repair_map[i - 1].prot == prot) {
		r = &tc_fork_repair_map[i - 1];
		r->end = end;
		if (i < tc_fork_repair_count && tc_fork_repair_map[i].start == end &&
		    tc_fork_repair_map[i].prot == prot) {
			r->end = tc_fork_repair_map[i].end;
			memmove(&tc_fork_repair_map[i], &tc_fork_repair_map[i + 1],
				(tc_fork_repair_count - i - 1) * sizeof(*r));
			tc_fork_repair_count--;
		}
		return;
	}
	if (i < tc_fork_repair_count && tc_fork_repair_map[i].start == end && tc_fork_repair_map[i].prot == prot) {
		tc_fork_repair_map[i].start = start;
		return;
	}
	if (tc_fork_repair_count == tc_fork_repair_capacity) {
		tc_fork_repair_overflow = 1;
		return;
	}
	r = &tc_fork_repair_map[i];
	memmove(r + 1, r, (tc_fork_repair_count - i) * sizeof(*r));
	r->start = start;
	r->end = end;
	r->prot = prot;
	tc_fork_repair_count++;
}

/*
 * The registry is changed from the mmap family's wrappers and read by fork()'s;
 * block signals while it changes, so a handler that maps memory cannot see or
 * leave it half updated.
 */
#define TC_FORK_REPAIR_UPDATE(statement) do { \
	sigset_t tc_all_, tc_old_; \
	int tc_errno_ = errno; \
	sigfillset(&tc_all_); \
	sigprocmask(SIG_BLOCK, &tc_all_, &tc_old_); \
	statement; \
	sigprocmask(SIG_SETMASK, &tc_old_, NULL); \
	errno = tc_errno_; \
} while (0)

void tc_fork_repair_note_map(void *address, size_t length, int prot, int flags)
{
	uintptr_t start = (uintptr_t)address, end = tc_fork_repair_up(start + length);

	if (address == MAP_FAILED) {
		return;
	}
	/* A MAP_FIXED mapping replaces whatever was there. Shared mappings are
	 * shared with the child by design and need no repair. */
	TC_FORK_REPAIR_UPDATE(
		tc_fork_repair_remove(start, end);
		if ((flags & MAP_SHARED) == 0) {
			tc_fork_repair_insert(start, end, prot);
		});
}

void tc_fork_repair_note_unmap(void *address, size_t length)
{
	uintptr_t start = tc_fork_repair_down((uintptr_t)address);
	uintptr_t end = tc_fork_repair_up((uintptr_t)address + length);

	TC_FORK_REPAIR_UPDATE(tc_fork_repair_remove(start, end));
}

void tc_fork_repair_note_remap(void *old_address, size_t old_length, void *new_address, size_t new_length)
{
	uintptr_t old_start = (uintptr_t)old_address, old_end = tc_fork_repair_up(old_start + old_length);
	uintptr_t new_start = (uintptr_t)new_address, new_end = tc_fork_repair_up(new_start + new_length);
	int prot, recorded;

	if (new_address == MAP_FAILED) {
		return;
	}
	/* jemalloc's realloc() moves and grows its huge blocks with mremap().
	 * Whatever the registry held at the new range is gone even when the old
	 * one was not recorded. NetBSD's mremap() never moves onto mapped pages
	 * (uvm_map_reserve() refuses them), so that only drops stale entries. */
	TC_FORK_REPAIR_UPDATE(
		recorded = tc_fork_repair_lookup(old_address, &prot);
		if (recorded) {
			tc_fork_repair_remove(old_start, old_end);
		}
		tc_fork_repair_remove(new_start, new_end);
		if (recorded) {
			tc_fork_repair_insert(new_start, new_end, prot);
		});
}

void tc_fork_repair_note_protect(void *address, size_t length, int prot)
{
	uintptr_t start = tc_fork_repair_down((uintptr_t)address);
	uintptr_t end = tc_fork_repair_up((uintptr_t)address + length);
	uintptr_t spans[8][2];
	unsigned spans_count = 0, i;

	/* Only the recorded (private) parts change protection. */
	TC_FORK_REPAIR_UPDATE(
		for (i = 0; i < tc_fork_repair_count; i++) {
			struct tc_fork_repair_range *r = &tc_fork_repair_map[i];
			if (r->end <= start || r->start >= end) {
				continue;
			}
			if (spans_count == 8) {
				tc_fork_repair_overflow = 1;
				break;
			}
			spans[spans_count][0] = r->start > start ? r->start : start;
			spans[spans_count][1] = r->end < end ? r->end : end;
			spans_count++;
		}
		for (i = 0; i < spans_count; i++) {
			tc_fork_repair_remove(spans[i][0], spans[i][1]);
			tc_fork_repair_insert(spans[i][0], spans[i][1], prot);
		});
}

/* 1 and the recorded protection if address lies in a recorded range. */
int tc_fork_repair_lookup(const void *address, int *prot)
{
	uintptr_t a = (uintptr_t)address;
	unsigned i;

	for (i = 0; i < tc_fork_repair_count; i++) {
		if (a >= tc_fork_repair_map[i].start && a < tc_fork_repair_map[i].end) {
			*prot = tc_fork_repair_map[i].prot;
			return 1;
		}
	}
	return 0;
}

/*
 * Protect [start, start + length) PROT_NONE and back to prot. Returns 0, or the
 * errno of a PROT_NONE step that failed (the range is then unchanged). While
 * the range is PROT_NONE nothing may touch it; on the appliance it can hold
 * errno and __stack_chk_guard, and a fault with signals blocked spins in the
 * kernel forever. So the device version is two raw system calls that use only
 * registers and the stack (which is never protected here), and it exits if it
 * cannot restore the range: nothing could run safely afterwards.
 */
#if defined(__arm__) && defined(__NetBSD__) && !defined(TC_FORK_REPAIR_TEST)
int tc_fork_repair_cycle(uintptr_t start, size_t length, int prot);
/* NetBSD/arm: svc 0xa00000 | SYS_x, arguments in r0-r2, carry set on error
 * with the errno in r0 (as libc's mprotect and _exit stubs). SYS_mprotect is
 * 74, SYS_exit 1. */
__asm__(
	"	.text\n"
	"	.arm\n"
	"	.align	2\n"
	"	.type	tc_fork_repair_cycle, %function\n"
	"tc_fork_repair_cycle:\n"
	"	push	{r4, r5, r6, r7, lr}\n"
	"	mov	r4, r0\n"
	"	mov	r5, r1\n"
	"	mov	r6, r2\n"
	"	mov	r2, #0\n"
	"	svc	0x00a0004a\n"
	"	bcs	2f\n"
	"	mov	r7, #16\n"
	"1:	mov	r0, r4\n"
	"	mov	r1, r5\n"
	"	mov	r2, r6\n"
	"	svc	0x00a0004a\n"
	"	bcc	3f\n"
	"	subs	r7, r7, #1\n"
	"	bne	1b\n"
	"	mov	r0, #127\n"
	"	svc	0x00a00001\n"
	"3:	mov	r0, #0\n"
	"2:	pop	{r4, r5, r6, r7, pc}\n"
	"	.size	tc_fork_repair_cycle, .-tc_fork_repair_cycle\n");
#else
/* Host tests only ever pass ranges they mapped themselves. */
static int tc_fork_repair_cycle(uintptr_t start, size_t length, int prot)
{
	if (mprotect((void *)start, length, PROT_NONE) != 0) {
		return errno;
	}
	while (mprotect((void *)start, length, prot) != 0) {
	}
	return 0;
}
#endif

#ifdef TC_FORK_REPAIR_TEST
/* Host tests supply the data segment and stack ranges (or none) and may
 * shrink the registry; they never repair the test process's own memory. They
 * also read back every madvise() call and can make mlockall() fail. */
static uintptr_t tc_fork_repair_test_data[3], tc_fork_repair_test_stack_top;
static int tc_fork_repair_test_touch_stack, tc_fork_repair_test_lock_errno;
static uintptr_t tc_fork_repair_test_advice_log[64][3];
static unsigned tc_fork_repair_test_advice_count;
unsigned long tc_fork_repair_cycles, tc_fork_repair_test_unlocks;

void tc_fork_repair_test_reset(unsigned capacity)
{
	tc_fork_repair_count = 0;
	tc_fork_repair_overflow = 0;
	tc_fork_repair_fallbacks = 0;
	tc_fork_repair_lock_failures = 0;
	tc_fork_repair_advised = 0;
	tc_fork_repair_cycles = 0;
	tc_fork_repair_test_unlocks = 0;
	tc_fork_repair_capacity = capacity ? capacity : TC_FORK_REPAIR_SLOTS;
	tc_fork_repair_test_data[0] = tc_fork_repair_test_data[1] = tc_fork_repair_test_data[2] = 0;
	tc_fork_repair_test_stack_top = 0;
	tc_fork_repair_test_touch_stack = 0;
	tc_fork_repair_test_lock_errno = 0;
	tc_fork_repair_test_advice_count = 0;
}

/* The data segment runs from data_start to data_end; its static part (.data
 * and .bss) ends at static_end, and the sbrk() heap follows. */
void tc_fork_repair_test_segments(void *data_start, void *static_end, void *data_end, void *stack_top)
{
	tc_fork_repair_test_data[0] = (uintptr_t)data_start;
	tc_fork_repair_test_data[1] = (uintptr_t)static_end;
	tc_fork_repair_test_data[2] = (uintptr_t)data_end;
	tc_fork_repair_test_stack_top = (uintptr_t)stack_top;
	tc_fork_repair_test_touch_stack = stack_top != NULL;
}

void tc_fork_repair_test_lock_fails(int error)
{
	tc_fork_repair_test_lock_errno = error;
}

unsigned tc_fork_repair_test_advice(uintptr_t (*out)[3], unsigned max)
{
	unsigned i;

	for (i = 0; i < tc_fork_repair_test_advice_count && i < max; i++) {
		memcpy(out[i], tc_fork_repair_test_advice_log[i], sizeof(out[i]));
	}
	return tc_fork_repair_test_advice_count;
}

unsigned tc_fork_repair_test_ranges(uintptr_t (*out)[3], unsigned max)
{
	unsigned i;

	for (i = 0; i < tc_fork_repair_count && i < max; i++) {
		out[i][0] = tc_fork_repair_map[i].start;
		out[i][1] = tc_fork_repair_map[i].end;
		out[i][2] = (uintptr_t)tc_fork_repair_map[i].prot;
	}
	return tc_fork_repair_count;
}

int tc_fork_repair_test_overflowed(void)
{
	return tc_fork_repair_overflow;
}
#endif

static void tc_fork_repair_advise(uintptr_t start, uintptr_t end, int advice)
{
	if (end <= start) {
		return;
	}
#ifdef TC_FORK_REPAIR_TEST
	if (tc_fork_repair_test_advice_count < 64) {
		uintptr_t *entry = tc_fork_repair_test_advice_log[tc_fork_repair_test_advice_count++];
		entry[0] = start;
		entry[1] = end;
		entry[2] = (uintptr_t)advice;
	}
#endif
	(void)madvise((void *)start, end - start, advice);
}

/* mlockall(MCL_CURRENT) and munlockall(); host tests only pretend. */
static int tc_fork_repair_lock(void)
{
#ifdef TC_FORK_REPAIR_TEST
	if (tc_fork_repair_test_lock_errno != 0) {
		errno = tc_fork_repair_test_lock_errno;
		return -1;
	}
	return 0;
#else
	return mlockall(MCL_CURRENT);
#endif
}

static void tc_fork_repair_unlock(void)
{
#ifdef TC_FORK_REPAIR_TEST
	tc_fork_repair_test_unlocks++;
#else
	(void)munlockall();
#endif
}

/* The writable static data and sbrk() heap: one run of private mappings. */
static int tc_fork_repair_data(uintptr_t *start, uintptr_t *end)
{
#ifdef TC_FORK_REPAIR_TEST
	*start = tc_fork_repair_test_data[0];
	*end = tc_fork_repair_test_data[2];
	return *start != 0;
#elif defined(__NetBSD__)
	if (tc_fork_repair_data_first == NULL) {
		return 0;
	}
	*start = tc_fork_repair_down((uintptr_t)tc_fork_repair_data_first);
	*end = tc_fork_repair_up((uintptr_t)sbrk(0));
	return *end > *start;
#else
	return 0;
#endif
}

/* The top of the stack: the process's ps_strings sits at its very end. */
static uintptr_t tc_fork_repair_stack_top(void)
{
#ifdef TC_FORK_REPAIR_TEST
	return tc_fork_repair_test_stack_top;
#elif defined(__NetBSD__)
	if (&__ps_strings == NULL || __ps_strings == NULL) {
		return 0;
	}
	return tc_fork_repair_up((uintptr_t)__ps_strings + sizeof(*__ps_strings));
#else
	return 0;
#endif
}

/* The data segment and stack top the repair uses (for the regression driver). */
int tc_fork_repair_bounds(uintptr_t *data_start, uintptr_t *data_end, uintptr_t *stack_top)
{
	*stack_top = tc_fork_repair_stack_top();
	if (!tc_fork_repair_data(data_start, data_end)) {
		*data_start = *data_end = 0;
	}
	return *stack_top != 0 && *data_start != 0;
}

/*
 * Where the sbrk() heap starts in the data segment: after .bss, rounded up as
 * patch 0046 rounds it. data_end when that is unknown, so no part of the
 * static data is ever taken for heap.
 */
static uintptr_t tc_fork_repair_heap_start(uintptr_t data_start, uintptr_t data_end)
{
	uintptr_t start;

#ifdef TC_FORK_REPAIR_TEST
	start = tc_fork_repair_test_data[1];
#elif defined(__NetBSD__)
	start = (uintptr_t)tc_fork_repair_static_end;
#else
	start = 0;
#endif
	start = tc_fork_repair_up(start);
	if (start == 0 || start > data_end) {
		return data_end;
	}
	return start < data_start ? data_start : start;
}

/*
 * Remove the parent's mappings of its private memory; see the top of this
 * file. 0, or -1 when fork() must use the fallback instead. Signals are
 * blocked by the caller.
 */
TC_FORK_REPAIR_NO_ASAN int tc_fork_repair_prepare(void)
{
	uintptr_t data_start, data_end, top, page, low;
	volatile char here;
	int have_data;
	unsigned i;

	/* Every precondition first: a fallback never follows a partial repair. */
	if (tc_fork_repair_overflow) {
		return -1;
	}
	top = tc_fork_repair_stack_top();
	have_data = tc_fork_repair_data(&data_start, &data_end);
#ifdef TC_FORK_REPAIR_TEST
	if (tc_fork_repair_test_touch_stack && top == 0) {
		return -1;
	}
#else
	if (top == 0 || !have_data) {
		return -1;
	}
#endif
	/* From here on some ranges may have MADV_RANDOM; fork() undoes it. */
	tc_fork_repair_advised = 1;
	tc_fork_repair_heap[0] = tc_fork_repair_heap[1] = 0;
	tc_fork_repair_stack[0] = tc_fork_repair_stack[1] = 0;
	/* Each call restores its range before returning, so this loop may keep
	 * reading the registry, which lives in the data segment. That segment
	 * goes last. */
	for (i = 0; i < tc_fork_repair_count; i++) {
		struct tc_fork_repair_range *r = &tc_fork_repair_map[i];
		if ((r->prot & PROT_WRITE) == 0) {
			continue;	/* read-only: nothing the parent writes can leak */
		}
		if (tc_fork_repair_cycle(r->start, r->end - r->start, r->prot) != 0) {
			return -1;
		}
		tc_fork_repair_advise(r->start, r->end, MADV_RANDOM);
#ifdef TC_FORK_REPAIR_TEST
		tc_fork_repair_cycles++;
#endif
	}
	if (have_data) {
		if (tc_fork_repair_cycle(data_start, data_end - data_start, PROT_READ | PROT_WRITE) != 0) {
			return -1;
		}
		tc_fork_repair_advise(data_start, data_end, MADV_RANDOM);
		tc_fork_repair_heap[0] = tc_fork_repair_heap_start(data_start, data_end);
		tc_fork_repair_heap[1] = data_end;
	}
	/*
	 * The stack cannot be protected while in use: write one word of each
	 * page from this frame up back unchanged, so each page is mapped
	 * writable and fork() write-protects it. Pages below this frame hold no
	 * live data; the child writes them before reading them.
	 */
#ifdef TC_FORK_REPAIR_TEST
	if (!tc_fork_repair_test_touch_stack) {
		return 0;
	}
#endif
	page = tc_fork_repair_page();
	low = tc_fork_repair_down((uintptr_t)&here);
	tc_fork_repair_advise(low, top, MADV_RANDOM);
	tc_fork_repair_stack[0] = low;
	tc_fork_repair_stack[1] = top;
	for (i = 0; ; i++) {
		volatile char *p = (volatile char *)(low + i * page);
		char c;

		if ((uintptr_t)p >= top) {
			break;
		}
		c = *p;
		*p = c;
	}
	return 0;
}

/*
 * After fork(), in the parent and in the child: normal advice again on what
 * the repair gave MADV_RANDOM, except the static data and .bss (patch 0046).
 * Nothing had other advice before: only those constructors set any.
 */
static void tc_fork_repair_unadvise(void)
{
	unsigned i;

	if (!tc_fork_repair_advised) {
		return;
	}
	tc_fork_repair_advised = 0;
	for (i = 0; i < tc_fork_repair_count; i++) {
		if ((tc_fork_repair_map[i].prot & PROT_WRITE) != 0) {
			tc_fork_repair_advise(tc_fork_repair_map[i].start, tc_fork_repair_map[i].end, MADV_NORMAL);
		}
	}
	tc_fork_repair_advise(tc_fork_repair_heap[0], tc_fork_repair_heap[1], MADV_NORMAL);
	tc_fork_repair_advise(tc_fork_repair_stack[0], tc_fork_repair_stack[1], MADV_NORMAL);
}

/*
 * fork() for the appliance: repair, then fork. Falls back to locking all
 * memory across the call when the registry is incomplete; the child then gets
 * private copies of the parent's memory at once, which the kernel handles
 * correctly (uvmspace_fork's amap_cow_now()). If memory cannot be locked
 * either (a memory-lock limit), fork() goes ahead unprotected and says so.
 */
pid_t tc_fork_repair_fork(pid_t (*real_fork)(void))
{
	static const char locked[] =
		"tc_fork_repair: private mappings unknown; fork() with memory locked\n";
	static const char unlocked[] =
		"tc_fork_repair: private mappings unknown and mlockall() failed; fork() unprotected\n";
	sigset_t all, old;
	pid_t pid;
	int fallback, lock_failed = 0, saved;

	sigfillset(&all);
	sigprocmask(SIG_BLOCK, &all, &old);
	fallback = tc_fork_repair_prepare() != 0;
	if (fallback) {
		lock_failed = tc_fork_repair_lock() != 0;
	}
	pid = real_fork();
	saved = errno;
	tc_fork_repair_unadvise();
	/* A child does not inherit memory locks; it has nothing to undo. */
	if (fallback && pid != 0) {
		tc_fork_repair_fallbacks++;
		if (lock_failed) {
			if (tc_fork_repair_lock_failures++ == 0) {
				(void)write(2, unlocked, sizeof(unlocked) - 1);
			}
		} else {
			tc_fork_repair_unlock();
			if (tc_fork_repair_fallbacks - tc_fork_repair_lock_failures == 1) {
				(void)write(2, locked, sizeof(locked) - 1);
			}
		}
	}
	sigprocmask(SIG_SETMASK, &old, NULL);
	errno = saved;
	return pid;
}

#endif /* TC_FORK_REPAIR */

/*
 * Linker wrappers (-Wl,--wrap=...), only on the NetBSD 6 appliance lanes and
 * only in links that use --wrap (see the top of this file).
 */
#if defined(TC_FORK_REPAIR_WRAPPERS) && defined(__NetBSD__)
pid_t tc_fork_repair_fork(pid_t (*real_fork)(void));
void tc_fork_repair_note_map(void *address, size_t length, int prot, int flags);
void tc_fork_repair_note_unmap(void *address, size_t length);
void tc_fork_repair_note_remap(void *old_address, size_t old_length, void *new_address, size_t new_length);
void tc_fork_repair_note_protect(void *address, size_t length, int prot);
pid_t __real_fork(void);
pid_t __real__fork(void);
void *__real_mmap(void *, size_t, int, int, int, off_t);
void *__real__mmap(void *, size_t, int, int, int, off_t);
int __real_munmap(void *, size_t);
void *__real_mremap(void *, size_t, void *, size_t, int);
int __real_mprotect(void *, size_t, int);
pid_t __wrap_fork(void);
pid_t __wrap__fork(void);
void *__wrap_mmap(void *, size_t, int, int, int, off_t);
void *__wrap__mmap(void *, size_t, int, int, int, off_t);
int __wrap_munmap(void *, size_t);
void *__wrap_mremap(void *, size_t, void *, size_t, int);
int __wrap_mprotect(void *, size_t, int);

pid_t __wrap_fork(void)
{
	return tc_fork_repair_fork(__real_fork);
}

/* libc's daemon(), wordexp(), rcmd() and utmpx call _fork directly. */
pid_t __wrap__fork(void)
{
	return tc_fork_repair_fork(__real__fork);
}

void *__wrap_mmap(void *address, size_t length, int prot, int flags, int fd, off_t offset)
{
	void *result = __real_mmap(address, length, prot, flags, fd, offset);

	tc_fork_repair_note_map(result, length, prot, flags);
	return result;
}

/* libc's jemalloc, arc4random, catopen, cdbr and citrus map through _mmap. */
void *__wrap__mmap(void *address, size_t length, int prot, int flags, int fd, off_t offset)
{
	void *result = __real__mmap(address, length, prot, flags, fd, offset);

	tc_fork_repair_note_map(result, length, prot, flags);
	return result;
}

int __wrap_munmap(void *address, size_t length)
{
	int result = __real_munmap(address, length);

	if (result == 0) {
		tc_fork_repair_note_unmap(address, length);
	}
	return result;
}

void *__wrap_mremap(void *old_address, size_t old_length, void *new_address, size_t new_length, int flags)
{
	void *result = __real_mremap(old_address, old_length, new_address, new_length, flags);

	tc_fork_repair_note_remap(old_address, old_length, result, new_length);
	return result;
}

int __wrap_mprotect(void *address, size_t length, int prot)
{
	int result = __real_mprotect(address, length, prot);

	if (result == 0) {
		tc_fork_repair_note_protect(address, length, prot);
	}
	return result;
}
#endif /* TC_FORK_REPAIR_WRAPPERS */

#if !defined(TC_FORK_REPAIR) && !defined(TC_FORK_REPAIR_WRAPPERS)
/* ISO C wants a translation unit to declare something. */
typedef int tc_fork_repair_unused;
#endif
