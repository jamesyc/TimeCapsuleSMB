/* Host tests for build/patches/samba4x/overlay/lib/replace/tc_fork_repair.c,
 * compiled with TC_FORK_REPAIR_TEST: the registry the mmap-family wrappers
 * keep, the repair of ranges these tests map themselves, and the fork()
 * wrapper. The kernel bug itself only exists on the NetBSD 6 device; the
 * Samba driver tests/samba/tc_fork_repair_test.c covers that. */
#include <sys/types.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

pid_t tc_fork_repair_fork(pid_t (*real_fork)(void));
int tc_fork_repair_lookup(const void *address, int *prot);
void tc_fork_repair_note_map(void *address, size_t length, int prot, int flags);
void tc_fork_repair_note_unmap(void *address, size_t length);
void tc_fork_repair_note_remap(void *old_address, size_t old_length, void *new_address, size_t new_length);
void tc_fork_repair_note_protect(void *address, size_t length, int prot);
int tc_fork_repair_prepare(void);
void tc_fork_repair_test_reset(unsigned capacity);
void tc_fork_repair_test_segments(void *data_start, void *static_end, void *data_end, void *stack_top);
void tc_fork_repair_test_lock_fails(int error);
unsigned tc_fork_repair_test_advice(uintptr_t (*out)[3], unsigned max);
unsigned tc_fork_repair_test_ranges(uintptr_t (*out)[3], unsigned max);
int tc_fork_repair_test_overflowed(void);
extern unsigned long tc_fork_repair_fallbacks, tc_fork_repair_lock_failures, tc_fork_repair_cycles;
extern unsigned long tc_fork_repair_test_unlocks;

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: CHECK(%s) failed\n", __FILE__, __LINE__, #x); exit(1); } } while (0)

/*
 * The repair walks the real stack from its own frame up to the top these
 * tests give it, taken from a local. ASan can move an instrumented function's
 * locals to a separate "fake stack" (Linux CI does), which would put that top
 * outside the stack the repair walks. Like tc_fork_repair_prepare(), the
 * tests that pass a stack top are left uninstrumented.
 */
#if defined(__has_feature)
#if __has_feature(address_sanitizer)
#define REAL_STACK_LOCALS __attribute__((no_sanitize_address))
#endif
#endif
#if !defined(REAL_STACK_LOCALS) && defined(__SANITIZE_ADDRESS__)
#define REAL_STACK_LOCALS __attribute__((no_sanitize_address))
#endif
#ifndef REAL_STACK_LOCALS
#define REAL_STACK_LOCALS
#endif

static uintptr_t pg;
static uintptr_t got[16][3];

/* The registry as "start-end:prot" pages relative to base, e.g. "0-2:3 4-5:1". */
static void expect(uintptr_t base, const char *want)
{
	char text[512] = "";
	unsigned n = tc_fork_repair_test_ranges(got, 16), i;

	for (i = 0; i < n && i < 16; i++) {
		char one[64];
		snprintf(one, sizeof(one), "%s%ld-%ld:%lu", i ? " " : "", (long)((got[i][0] - base) / pg),
			 (long)((got[i][1] - base) / pg), (unsigned long)got[i][2]);
		strcat(text, one);
	}
	if (strcmp(text, want) != 0) {
		fprintf(stderr, "registry is \"%s\", want \"%s\"\n", text, want);
		exit(1);
	}
}

static void *at(uintptr_t base, int page) { return (void *)(base + (uintptr_t)page * pg); }

/* Mapping calls record private mappings with their protection and nothing else. */
static void map_records_private_only(uintptr_t b)
{
	tc_fork_repair_note_map(at(b, 0), 2 * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	tc_fork_repair_note_map(at(b, 4), pg, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANON);	/* shared by design */
	tc_fork_repair_note_map(at(b, 6), pg, PROT_NONE, MAP_PRIVATE | MAP_ANON);		/* nothing mapped */
	tc_fork_repair_note_map(MAP_FAILED, pg, PROT_READ, MAP_PRIVATE | MAP_ANON);		/* failed call */
	tc_fork_repair_note_map(at(b, 8), pg - 100, PROT_READ, MAP_PRIVATE);			/* a file, rounded up */
	expect(b, "0-2:3 8-9:1");
}

/* MAP_FIXED over recorded pages replaces them, whatever the new mapping is. */
static void map_fixed_replaces(uintptr_t b)
{
	tc_fork_repair_note_map(at(b, 0), 6 * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	tc_fork_repair_note_map(at(b, 2), pg, PROT_READ, MAP_PRIVATE | MAP_ANON | MAP_FIXED);
	tc_fork_repair_note_map(at(b, 4), pg, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANON | MAP_FIXED);
	expect(b, "0-2:3 2-3:1 3-4:3 5-6:3");
}

/* munmap() of the middle splits a range; one call can trim and drop several;
 * unaligned calls cover whole pages. */
static void unmap_splits_and_trims(uintptr_t b)
{
	tc_fork_repair_note_map(at(b, 0), 4 * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	tc_fork_repair_note_map(at(b, 6), 2 * pg, PROT_READ, MAP_PRIVATE | MAP_ANON);
	tc_fork_repair_note_map(at(b, 9), 2 * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	tc_fork_repair_note_map(at(b, 13), 2 * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	tc_fork_repair_note_unmap(at(b, 1), pg);
	expect(b, "0-1:3 2-4:3 6-8:1 9-11:3 13-15:3");
	tc_fork_repair_note_unmap((char *)at(b, 7) + 10, 3 * pg);	/* pages 7-10 */
	expect(b, "0-1:3 2-4:3 6-7:1 13-15:3");
	tc_fork_repair_note_unmap(at(b, 0), 20 * pg);
	expect(b, "");
}

/* Adjacent ranges with the same protection become one; a gap filled joins three. */
static void merge_adjacent(uintptr_t b)
{
	tc_fork_repair_note_map(at(b, 0), pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	tc_fork_repair_note_map(at(b, 2), pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	tc_fork_repair_note_map(at(b, 3), pg, PROT_READ, MAP_PRIVATE | MAP_ANON);
	expect(b, "0-1:3 2-3:3 3-4:1");
	tc_fork_repair_note_map(at(b, 1), pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	expect(b, "0-3:3 3-4:1");
	tc_fork_repair_note_map(at(b, 4), pg, PROT_READ, MAP_PRIVATE | MAP_ANON);
	expect(b, "0-3:3 3-5:1");
}

/* mremap() moves a recorded range with its protection; unrecorded ones stay out. */
static void remap_moves(uintptr_t b)
{
	tc_fork_repair_note_map(at(b, 0), 2 * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	tc_fork_repair_note_remap(at(b, 0), 2 * pg, at(b, 10), 3 * pg);
	expect(b, "10-13:3");
	tc_fork_repair_note_remap(at(b, 10), 3 * pg, MAP_FAILED, 5 * pg);	/* failed: unchanged */
	expect(b, "10-13:3");
	tc_fork_repair_note_remap(at(b, 20), pg, at(b, 30), pg);		/* shared or unknown */
	expect(b, "10-13:3");
	tc_fork_repair_note_remap(at(b, 10), 3 * pg, at(b, 10), 5 * pg);	/* grown in place */
	expect(b, "10-15:3");
	tc_fork_repair_note_remap(at(b, 10), 5 * pg, at(b, 10), 2 * pg);	/* shrunk in place */
	expect(b, "10-12:3");
}

/* An unrecorded mapping moved onto recorded pages replaces them: the registry
 * must not keep them (NetBSD never does this, so they could only be stale). */
static void remap_drops_replaced(uintptr_t b)
{
	tc_fork_repair_note_map(at(b, 0), 4 * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	tc_fork_repair_note_remap(at(b, 20), pg, at(b, 1), 2 * pg);
	expect(b, "0-1:3 3-4:3");
	tc_fork_repair_note_remap(at(b, 20), pg, MAP_FAILED, 2 * pg);	/* failed: unchanged */
	expect(b, "0-1:3 3-4:3");
}

/* mprotect() changes only recorded parts; PROT_NONE drops them. */
static void protect_updates(uintptr_t b)
{
	tc_fork_repair_note_map(at(b, 0), 4 * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	tc_fork_repair_note_protect(at(b, 1), 2 * pg, PROT_READ);
	expect(b, "0-1:3 1-3:1 3-4:3");
	tc_fork_repair_note_protect(at(b, 3), 4 * pg, PROT_NONE);	/* runs past the recorded end */
	expect(b, "0-1:3 1-3:1");
	tc_fork_repair_note_protect(at(b, 0), 3 * pg, PROT_READ | PROT_WRITE);
	expect(b, "0-3:3");
	tc_fork_repair_note_protect(at(b, 8), pg, PROT_READ);		/* not recorded: nothing */
	expect(b, "0-3:3");
}

static pid_t fake_pid;
static int fake_errno;
static pid_t fake_fork(void)
{
	if (fake_pid < 0) {
		errno = fake_errno;
	}
	return fake_pid;
}

/* Capture what fn writes to fd 2. */
static void stderr_of(void (*fn)(void), char *text, size_t size)
{
	int fds[2], saved;
	ssize_t n;

	CHECK(pipe(fds) == 0);
	saved = dup(2);
	CHECK(saved >= 0 && dup2(fds[1], 2) == 2);
	close(fds[1]);
	fn();
	CHECK(dup2(saved, 2) == 2);
	close(saved);
	n = read(fds[0], text, size - 1);
	text[n > 0 ? n : 0] = '\0';
	close(fds[0]);
}

static void fork_twice(void)
{
	CHECK(tc_fork_repair_fork(fake_fork) == 4242);
	CHECK(tc_fork_repair_fork(fake_fork) == 4242);
}

static const char locked_message[] = "tc_fork_repair: private mappings unknown; fork() with memory locked\n";
static const char unlocked_message[] =
	"tc_fork_repair: private mappings unknown and mlockall() failed; fork() unprotected\n";

/* A full registry must not lose track silently: the flag sticks, and fork()
 * then locks memory instead of repairing (once logged, always counted). */
static void overflow_falls_back(uintptr_t b)
{
	char text[512];

	tc_fork_repair_test_reset(2);
	tc_fork_repair_note_map(at(b, 0), pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	tc_fork_repair_note_map(at(b, 2), pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	CHECK(!tc_fork_repair_test_overflowed());
	tc_fork_repair_note_map(at(b, 4), pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	CHECK(tc_fork_repair_test_overflowed());
	CHECK(tc_fork_repair_prepare() == -1);
	fake_pid = 4242;
	stderr_of(fork_twice, text, sizeof(text));
	CHECK(strcmp(text, locked_message) == 0);
	CHECK(tc_fork_repair_fallbacks == 2 && tc_fork_repair_lock_failures == 0);
	CHECK(tc_fork_repair_test_unlocks == 2);
	/* Removing entries does not bring back the mapping that was missed. */
	tc_fork_repair_note_unmap(at(b, 0), 3 * pg);
	CHECK(tc_fork_repair_test_overflowed());

	/* Splitting a range needs a slot too. */
	tc_fork_repair_test_reset(1);
	tc_fork_repair_note_map(at(b, 0), 3 * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	CHECK(!tc_fork_repair_test_overflowed());
	tc_fork_repair_note_unmap(at(b, 1), pg);
	CHECK(tc_fork_repair_test_overflowed());
}

/* When the fallback cannot lock memory either, fork() still happens, nothing
 * is unlocked, and the log says it was unprotected, not locked. A later
 * fallback that can lock says so once. */
static void fallback_lock_failure(uintptr_t b)
{
	char text[512];

	tc_fork_repair_test_reset(1);
	tc_fork_repair_note_map(at(b, 0), pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	tc_fork_repair_note_map(at(b, 2), pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	CHECK(tc_fork_repair_test_overflowed());
	tc_fork_repair_test_lock_fails(EAGAIN);
	fake_pid = 4242;
	stderr_of(fork_twice, text, sizeof(text));
	CHECK(strcmp(text, unlocked_message) == 0);
	CHECK(tc_fork_repair_fallbacks == 2 && tc_fork_repair_lock_failures == 2);
	CHECK(tc_fork_repair_test_unlocks == 0);

	tc_fork_repair_test_lock_fails(0);
	stderr_of(fork_twice, text, sizeof(text));
	CHECK(strcmp(text, locked_message) == 0);
	CHECK(tc_fork_repair_fallbacks == 4 && tc_fork_repair_lock_failures == 2);
	CHECK(tc_fork_repair_test_unlocks == 2);

	/* A failed fork() on the fallback path unlocks too; errno is fork()'s. */
	fake_pid = -1;
	fake_errno = ENOMEM;
	errno = 0;
	CHECK(tc_fork_repair_fork(fake_fork) == -1);
	CHECK(errno == ENOMEM && tc_fork_repair_test_unlocks == 3);
}

/* Repair leaves every byte and every protection as it was. */
REAL_STACK_LOCALS static void repair_keeps_contents(uintptr_t b)
{
	char *rw = mmap(NULL, 3 * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
	char *ro = mmap(NULL, pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
	char *data = mmap(NULL, 2 * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
	volatile char canary[64];
	int status, i;
	pid_t pid;

	(void)b;
	CHECK(rw != MAP_FAILED && ro != MAP_FAILED && data != MAP_FAILED);
	for (i = 0; i < 3 * (int)pg; i++) rw[i] = (char)(i * 7);
	memset(ro, 'r', pg);
	memset(data, 'd', 2 * pg);
	memset((char *)canary, 'c', sizeof(canary));
	CHECK(mprotect(ro, pg, PROT_READ) == 0);
	tc_fork_repair_note_map(rw, 3 * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	tc_fork_repair_note_map(ro, pg, PROT_READ, MAP_PRIVATE | MAP_ANON);
	/* The "data segment" and a stack top above this frame, inside the live stack. */
	tc_fork_repair_test_segments(data, data + pg, data + 2 * pg, (void *)(((uintptr_t)&canary[63] + pg) & ~(pg - 1)));
	CHECK(tc_fork_repair_prepare() == 0);
	CHECK(tc_fork_repair_cycles == 1);	/* rw only: ro cannot be written */
	for (i = 0; i < 3 * (int)pg; i++) CHECK(rw[i] == (char)(i * 7));
	for (i = 0; i < (int)pg; i++) CHECK(ro[i] == 'r');
	for (i = 0; i < 2 * (int)pg; i++) CHECK(data[i] == 'd');
	for (i = 0; i < 64; i++) CHECK(canary[i] == 'c');
	rw[0] = 1;
	data[0] = 1;
	/* Still read-only: a write kills the writer. */
	pid = fork();
	if (pid == 0) {
		/* Under ASan its handler would catch the fault and exit(1) instead. */
		signal(SIGSEGV, SIG_DFL);
		signal(SIGBUS, SIG_DFL);
		ro[0] = 'x';
		_exit(0);
	}
	CHECK(waitpid(pid, &status, 0) == pid);
	CHECK(WIFSIGNALED(status) && (WTERMSIG(status) == SIGSEGV || WTERMSIG(status) == SIGBUS));
	munmap(rw, 3 * pg);
	munmap(ro, pg);
	munmap(data, 2 * pg);
}

/* The wrapper restores the caller's signal mask and keeps fork()'s errno. */
static void fork_mask_and_errno(uintptr_t b)
{
	sigset_t block, now;
	int go[2], status;
	char c;
	pid_t pid;
	char *m = mmap(NULL, pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);

	(void)b;
	CHECK(m != MAP_FAILED);
	memset(m, 'a', pg);
	tc_fork_repair_note_map(m, pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	sigemptyset(&block);
	sigaddset(&block, SIGUSR1);
	CHECK(sigprocmask(SIG_BLOCK, &block, NULL) == 0);

	fake_pid = -1;
	fake_errno = EAGAIN;
	errno = 0;
	CHECK(tc_fork_repair_fork(fake_fork) == -1);
	CHECK(errno == EAGAIN);
	CHECK(sigprocmask(SIG_BLOCK, NULL, &now) == 0);
	CHECK(sigismember(&now, SIGUSR1) && !sigismember(&now, SIGUSR2));

	/* A real fork: both sides get the caller's mask back, and the child
	 * keeps the fork-time contents while the parent writes. */
	CHECK(pipe(go) == 0);
	pid = tc_fork_repair_fork(fork);
	CHECK(pid >= 0);
	if (pid == 0) {
		sigset_t mine;
		close(go[1]);
		if (read(go[0], &c, 1) != 1) _exit(2);
		sigprocmask(SIG_BLOCK, NULL, &mine);
		_exit(m[0] == 'a' && sigismember(&mine, SIGUSR1) && !sigismember(&mine, SIGUSR2) ? 0 : 1);
	}
	close(go[0]);
	CHECK(sigprocmask(SIG_BLOCK, NULL, &now) == 0);
	CHECK(sigismember(&now, SIGUSR1) && !sigismember(&now, SIGUSR2));
	m[0] = 'b';
	CHECK(write(go[1], "x", 1) == 1);
	CHECK(waitpid(pid, &status, 0) == pid);
	CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0);
	CHECK(tc_fork_repair_fallbacks == 0);
	munmap(m, pg);
}

static void check_advice(const uintptr_t *entry, const void *start, const void *end, int advice)
{
	if (entry[0] != (uintptr_t)start || entry[1] != (uintptr_t)end || entry[2] != (uintptr_t)advice) {
		fprintf(stderr, "advice %#lx-%#lx:%lu, want %p-%p:%d\n", (unsigned long)entry[0],
			(unsigned long)entry[1], (unsigned long)entry[2], start, end, advice);
		exit(1);
	}
}

/* MADV_RANDOM covers what the repair touched until the fork system call; right
 * after fork() the parent and the child put normal advice back on all of it
 * but the static data and .bss, where patch 0046 keeps MADV_RANDOM. */
REAL_STACK_LOCALS static void advice_restored(uintptr_t b)
{
	char *rw = mmap(NULL, 2 * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
	char *data = mmap(NULL, 4 * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
	volatile char frame[64];
	uintptr_t log[64][3], top = ((uintptr_t)&frame[63] + pg) & ~(pg - 1);
	int status;
	pid_t pid;

	CHECK(rw != MAP_FAILED && data != MAP_FAILED);
	frame[0] = 1;
	tc_fork_repair_note_map(rw, 2 * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	/* .bss ends 100 bytes into the second page: the heap starts on the third. */
	tc_fork_repair_test_segments(data, data + pg + 100, data + 4 * pg, (void *)top);
	fake_pid = 4242;
	CHECK(tc_fork_repair_fork(fake_fork) == 4242);
	CHECK(tc_fork_repair_test_advice(log, 64) == 6);
	check_advice(log[0], rw, rw + 2 * pg, MADV_RANDOM);
	check_advice(log[1], data, data + 4 * pg, MADV_RANDOM);
	/* The stack from the repair's own frame, below this one, to the top. */
	CHECK(log[2][0] < (uintptr_t)&frame[0] && log[2][1] == top && log[2][2] == MADV_RANDOM);
	check_advice(log[3], rw, rw + 2 * pg, MADV_NORMAL);
	check_advice(log[4], data + 2 * pg, data + 4 * pg, MADV_NORMAL);
	check_advice(log[5], (void *)log[2][0], (void *)top, MADV_NORMAL);
	/* Undone once: another fork() repairs and undoes again. */
	CHECK(tc_fork_repair_fork(fake_fork) == 4242);
	CHECK(tc_fork_repair_test_advice(log, 64) == 12);

	/* A real fork(): the child undoes the advice in its own address space. */
	tc_fork_repair_test_reset(0);
	tc_fork_repair_note_map(rw, 2 * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	tc_fork_repair_test_segments(data, data + pg + 100, data + 4 * pg, (void *)top);
	pid = tc_fork_repair_fork(fork);
	CHECK(pid >= 0);
	if (pid == 0) {
		_exit(tc_fork_repair_test_advice(log, 64) == 6 && log[3][2] == MADV_NORMAL &&
		      log[4][0] == (uintptr_t)(data + 2 * pg) && log[5][2] == MADV_NORMAL ? 0 : 1);
	}
	CHECK(waitpid(pid, &status, 0) == pid);
	CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0);
	CHECK(tc_fork_repair_test_advice(log, 64) == 6);

	/* Without data bounds for the heap (.bss end unknown), none of the data
	 * segment gets normal advice back. */
	tc_fork_repair_test_reset(0);
	tc_fork_repair_test_segments(data, NULL, data + 4 * pg, NULL);
	CHECK(tc_fork_repair_fork(fake_fork) == 4242);
	CHECK(tc_fork_repair_test_advice(log, 64) == 1);
	check_advice(log[0], data, data + 4 * pg, MADV_RANDOM);

	/* A fallback that repaired nothing advised nothing and undoes nothing. */
	tc_fork_repair_test_reset(1);
	tc_fork_repair_note_map(at(b, 0), pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	tc_fork_repair_note_map(at(b, 2), pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	CHECK(tc_fork_repair_fork(fake_fork) == 4242);
	CHECK(tc_fork_repair_test_advice(log, 64) == 0);
	munmap(rw, 2 * pg);
	munmap(data, 4 * pg);
}

/* A repair that fails part way falls back, and still undoes the advice it
 * gave the ranges it did repair. */
static void advice_partial_repair(uintptr_t b)
{
	char *pages = mmap(NULL, 3 * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
	uintptr_t log[64][3];

	(void)b;
	CHECK(pages != MAP_FAILED);
	/* Two recorded ranges with a gap, so they stay two. */
	tc_fork_repair_note_map(pages, pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	tc_fork_repair_note_map(pages + 2 * pg, pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	CHECK(munmap(pages + pg, 2 * pg) == 0);	/* behind the registry's back: the second cycle fails */
	fake_pid = 4242;
	CHECK(tc_fork_repair_fork(fake_fork) == 4242);
	CHECK(tc_fork_repair_fallbacks == 1 && tc_fork_repair_test_unlocks == 1);
	CHECK(tc_fork_repair_test_advice(log, 64) == 3);
	check_advice(log[0], pages, pages + pg, MADV_RANDOM);
	check_advice(log[1], pages, pages + pg, MADV_NORMAL);
	check_advice(log[2], pages + 2 * pg, pages + 3 * pg, MADV_NORMAL);
	munmap(pages, pg);
}

/* Registry updates leave the signal mask and errno as they were. */
/* A read-only range stays recorded but is not repaired or advised; once
 * mprotect() makes it writable, the next repair covers it. */
static void read_only_ranges_skipped(uintptr_t b)
{
	/* One mapping: the read-only range, an unrecorded gap page, so the two
	 * ranges never merge, and the writable range. */
	char *pages = mmap(NULL, 4 * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
	char *ro = pages, *rw = pages + 3 * pg;
	uintptr_t log[64][3];
	int prot;

	(void)b;
	CHECK(pages != MAP_FAILED);
	memset(ro, 'r', 2 * pg);
	CHECK(mprotect(ro, 2 * pg, PROT_READ) == 0);
	tc_fork_repair_note_map(ro, 2 * pg, PROT_READ, MAP_PRIVATE | MAP_ANON);
	tc_fork_repair_note_map(rw, pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	fake_pid = 4242;
	CHECK(tc_fork_repair_fork(fake_fork) == 4242);
	CHECK(tc_fork_repair_cycles == 1 && tc_fork_repair_fallbacks == 0);
	CHECK(tc_fork_repair_test_advice(log, 64) == 2);
	check_advice(log[0], rw, rw + pg, MADV_RANDOM);
	check_advice(log[1], rw, rw + pg, MADV_NORMAL);
	CHECK(tc_fork_repair_lookup(ro, &prot) && prot == PROT_READ);
	CHECK(ro[0] == 'r' && ro[2 * pg - 1] == 'r');

	/* Made writable, it is repaired from the next fork() on. */
	CHECK(mprotect(ro, 2 * pg, PROT_READ | PROT_WRITE) == 0);
	tc_fork_repair_note_protect(ro, 2 * pg, PROT_READ | PROT_WRITE);
	CHECK(tc_fork_repair_fork(fake_fork) == 4242);
	CHECK(tc_fork_repair_cycles == 3);
	CHECK(tc_fork_repair_test_advice(log, 64) == 6);
	munmap(pages, 4 * pg);
}

static void updates_keep_mask_and_errno(uintptr_t b)
{
	sigset_t before, after;

	sigprocmask(SIG_BLOCK, NULL, &before);
	errno = ENOSPC;
	tc_fork_repair_note_map(at(b, 0), pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON);
	tc_fork_repair_note_protect(at(b, 0), pg, PROT_READ);
	tc_fork_repair_note_remap(at(b, 0), pg, at(b, 4), pg);
	tc_fork_repair_note_unmap(at(b, 4), pg);
	CHECK(errno == ENOSPC);
	sigprocmask(SIG_BLOCK, NULL, &after);
	CHECK(memcmp(&before, &after, sizeof(before)) == 0);
}

int main(int argc, char **argv)
{
	/* An address range nobody maps: only the registry sees these numbers. */
	uintptr_t base;
	static const struct { const char *name; void (*fn)(uintptr_t); } cases[] = {
		{"map_records_private_only", map_records_private_only},
		{"map_fixed_replaces", map_fixed_replaces},
		{"unmap_splits_and_trims", unmap_splits_and_trims},
		{"merge_adjacent", merge_adjacent},
		{"remap_moves", remap_moves},
		{"remap_drops_replaced", remap_drops_replaced},
		{"protect_updates", protect_updates},
		{"overflow_falls_back", overflow_falls_back},
		{"fallback_lock_failure", fallback_lock_failure},
		{"repair_keeps_contents", repair_keeps_contents},
		{"fork_mask_and_errno", fork_mask_and_errno},
		{"advice_restored", advice_restored},
		{"advice_partial_repair", advice_partial_repair},
		{"read_only_ranges_skipped", read_only_ranges_skipped},
		{"updates_keep_mask_and_errno", updates_keep_mask_and_errno},
	};
	unsigned i;

	pg = (uintptr_t)getpagesize();
	base = (uintptr_t)1 << 30;
	for (i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
		if (argc < 2 || strcmp(argv[1], cases[i].name) == 0) {
			tc_fork_repair_test_reset(0);
			cases[i].fn(base);
			printf("PASS %s\n", cases[i].name);
		}
	}
	return 0;
}
