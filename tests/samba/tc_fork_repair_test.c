/*
 * Regression driver for the fork() repair on Apple's NetBSD 6 kernel (Samba
 * patch 0070, lib/replace/tc_fork_repair.c; host unit tests are in
 * tests/native/test_fork_repair.py). That kernel shows a child the writes its
 * parent makes after fork() to every page whose reference bit was cleared
 * before it (the page daemon does that under memory pressure; madvise
 * MADV_DONTNEED does it on demand). These cases clear references on pages in
 * every kind of private memory an appliance binary has, fork through the
 * linker-wrapped fork(), write every page in the parent, and check the child
 * still sees its fork-time copy.
 *
 * Only the NetBSD 6 lane links the repair. On the NetBSD 4 lanes "kernel"
 * checks the assumption that lets them ship without it; the other cases skip.
 * On Linux, MADV_DONTNEED discards private pages, so every case skips there.
 */
#include "replace.h"
#include <sys/types.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: CHECK(%s) failed\n", __FILE__, __LINE__, #x); exit(1); } } while (0)
#define PAGES 4

#ifdef TC_FORK_REPAIR
pid_t __real_fork(void);	/* the NetBSD 6 lane links with --wrap=fork */
int tc_fork_repair_lookup(const void *address, int *prot);
int tc_fork_repair_bounds(uintptr_t *data_start, uintptr_t *data_end, uintptr_t *stack_top);
extern unsigned long tc_fork_repair_fallbacks, tc_fork_repair_lock_failures;
#endif

static size_t pg;
/* Page-aligned buffers in .data and .bss. */
static char data_pages[(PAGES + 1) * 16384] = { 1 };
static char bss_pages[(PAGES + 1) * 16384];

struct region {
	const char *name;
	char *start;	/* page aligned */
};

static char *align(char *p)
{
	return (char *)(((uintptr_t)p + pg - 1) & ~(uintptr_t)(pg - 1));
}

static void fill(struct region *r, int n, char value)
{
	int i;
	size_t j;

	for (i = 0; i < n; i++) {
		for (j = 0; j < PAGES; j++) {
			r[i].start[j * pg] = value;
			r[i].start[j * pg + pg - 1] = value;
		}
	}
}

/*
 * Clear the references of every region's pages, fork with fork_fn, write
 * every page in the parent, and return how many pages the child saw change.
 */
static int leaked(struct region *r, int n, pid_t (*fork_fn)(void))
{
	int go[2], status, i;
	pid_t pid;

	fill(r, n, 'a');
	for (i = 0; i < n; i++) {
		CHECK(madvise(r[i].start, PAGES * pg, MADV_DONTNEED) == 0);
	}
	CHECK(pipe(go) == 0);
	pid = fork_fn();
	CHECK(pid >= 0);
	if (pid == 0) {
		char c;
		int bad = 0;
		size_t j;

		close(go[1]);
		if (read(go[0], &c, 1) != 1) {
			_exit(255);
		}
		for (i = 0; i < n; i++) {
			for (j = 0; j < PAGES; j++) {
				if (r[i].start[j * pg] != 'a' || r[i].start[j * pg + pg - 1] != 'a') {
					fprintf(stderr, "child sees the parent's write: %s page %zu\n", r[i].name, j);
					bad++;
				}
			}
		}
		_exit(bad);
	}
	close(go[0]);
	fill(r, n, 'b');
	CHECK(write(go[1], "x", 1) == 1);
	close(go[1]);
	CHECK(waitpid(pid, &status, 0) == pid);
	CHECK(WIFEXITED(status));
	return WEXITSTATUS(status);
}

/* Every kind of private memory: static data, bss, the sbrk heap, jemalloc
 * small blocks and a huge block, a private mapping, and the stack. */
static int regions(struct region *r, char *stack_pages)
{
	char *heap = malloc(64 * 1024), *huge = malloc(8 << 20), *brk_pages;
	char *mapped = mmap(NULL, (PAGES + 1) * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);

	brk_pages = sbrk((PAGES + 1) * pg);
	CHECK(heap && huge && mapped != MAP_FAILED && brk_pages != (void *)-1);
	r[0] = (struct region){ "data", align(data_pages) };
	r[1] = (struct region){ "bss", align(bss_pages) };
	r[2] = (struct region){ "sbrk", align(brk_pages) };
	r[3] = (struct region){ "heap", align(heap) };
	r[4] = (struct region){ "huge", align(huge) };
	r[5] = (struct region){ "mmap", mapped };
	r[6] = (struct region){ "stack", align(stack_pages) };
	return 7;
}

#if defined(__NetBSD__)
/* The kernel's behaviour with a plain fork(). NetBSD 4 lanes ship without the
 * repair because their kernel does not leak; check that here. */
static void case_kernel(void)
{
	char stack[(PAGES + 1) * 16384];
	struct region r[7];
	int n = regions(r, stack), leaks;

#ifdef TC_FORK_REPAIR
	leaks = leaked(r, n, __real_fork);
	printf("kernel: a plain fork() leaked %d of %d pages (expected on NetBSD 6)\n", leaks, n * PAGES);
#else
	leaks = leaked(r, n, fork);
	CHECK(leaks == 0);
#endif
}
#endif

#if defined(__NetBSD__) && defined(TC_FORK_REPAIR)
/* The wrapped fork() keeps every region's fork-time copy, three times in a row. */
static void case_regions(void)
{
	char stack[(PAGES + 1) * 16384];
	struct region r[7];
	int n = regions(r, stack), round;

	for (round = 0; round < 3; round++) {
		CHECK(leaked(r, n, fork) == 0);
	}
	CHECK(tc_fork_repair_fallbacks == 0);
}

/* Pages the parent only read after an earlier repaired fork, then lost their
 * references again, are repaired too. */
static void case_read_after_repair(void)
{
	char stack[(PAGES + 1) * 16384];
	struct region r[7];
	int n = regions(r, stack), i, status;
	volatile char sink = 0;
	size_t j;
	pid_t pid = fork();

	CHECK(pid >= 0);
	if (pid == 0) {
		_exit(0);
	}
	CHECK(waitpid(pid, &status, 0) == pid);
	for (i = 0; i < n; i++) {
		for (j = 0; j < PAGES; j++) {
			sink += r[i].start[j * pg];
		}
	}
	(void)sink;
	CHECK(leaked(r, n, fork) == 0);
}

/* A child that forks again protects its own child the same way. */
static void case_nested(void)
{
	char stack[(PAGES + 1) * 16384];
	struct region r[7];
	int n = regions(r, stack), status;
	pid_t pid = fork();

	CHECK(pid >= 0);
	if (pid == 0) {
		_exit(leaked(r, n, fork) == 0 ? 0 : 1);
	}
	CHECK(waitpid(pid, &status, 0) == pid);
	CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0);
}

/* Covered by the repair: in the registry, or in the data segment run that
 * ends at sbrk(0) (jemalloc also grows with sbrk()). */
static int covered(const void *p)
{
	uintptr_t start, end, top;
	int prot;

	return tc_fork_repair_lookup(p, &prot) ||
	       (tc_fork_repair_bounds(&start, &end, &top) && (uintptr_t)p >= start && (uintptr_t)p < end);
}

/* The data segment and the stack top the repair uses match the real mappings. */
static void case_bounds(void)
{
	uintptr_t start, end, top;
	char vec[2], *all;

	CHECK(tc_fork_repair_bounds(&start, &end, &top));
	CHECK(start <= (uintptr_t)data_pages && (uintptr_t)data_pages < end);
	CHECK(start <= (uintptr_t)bss_pages && (uintptr_t)bss_pages < end);
	CHECK((uintptr_t)sbrk(0) <= end);
	all = malloc((end - start) / pg + 1);
	CHECK(all != NULL);
	CHECK(mincore((void *)start, end - start, all) == 0);				/* no hole */
	free(all);
	CHECK(mincore((void *)(top - pg), pg, vec) == 0);				/* last stack page */
	CHECK(mincore((void *)top, pg, vec) == -1 && errno == ENOMEM);		/* nothing above */
	CHECK((uintptr_t)&start < top && (uintptr_t)&start > top - 8 * 1024 * 1024);
	printf("bounds: data %#lx-%#lx, stack top %#lx\n", (unsigned long)start, (unsigned long)end,
	       (unsigned long)top);
}

/* The wrappers keep the registry: private writable mappings in, shared ones
 * out, protection changes followed, unmapped ranges dropped. */
static void case_registry(void)
{
	char *huge = malloc(8 << 20);
	char *priv = mmap(NULL, 4 * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
	char *shared = mmap(NULL, pg, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANON, -1, 0);
	int prot;

	CHECK(huge && priv != MAP_FAILED && shared != MAP_FAILED);
	CHECK(covered(huge));
	CHECK(tc_fork_repair_lookup(priv + 3 * pg, &prot) && prot == (PROT_READ | PROT_WRITE));
	CHECK(!tc_fork_repair_lookup(shared, &prot));
	CHECK(mprotect(priv + pg, pg, PROT_READ) == 0);
	CHECK(tc_fork_repair_lookup(priv + pg, &prot) && prot == PROT_READ);
	CHECK(tc_fork_repair_lookup(priv + 2 * pg, &prot) && prot == (PROT_READ | PROT_WRITE));
	CHECK(munmap(priv, 4 * pg) == 0);
	CHECK(!tc_fork_repair_lookup(priv, &prot) && !tc_fork_repair_lookup(priv + 3 * pg, &prot));
	/* A realloc() that moves a huge block (jemalloc uses mremap()). */
	huge = realloc(huge, 32 << 20);
	CHECK(huge && covered(huge) && covered(huge + (31 << 20)));
	free(huge);
	munmap(shared, pg);
}

/* With more mappings than the registry holds, fork() locks memory instead
 * (root may lock it all), and the child still keeps its copy. */
static void case_fallback(void)
{
	char stack[(PAGES + 1) * 16384];
	struct region r[7];
	int n, i;
	size_t count = 1200;
	char *many = mmap(NULL, count * pg, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);

	CHECK(many != MAP_FAILED);
	/* Every other page PROT_NONE: 600 separate private ranges. */
	for (i = 1; i < (int)count; i += 2) {
		CHECK(mprotect(many + i * pg, pg, PROT_NONE) == 0);
	}
	n = regions(r, stack);
	CHECK(leaked(r, n, fork) == 0);
	CHECK(tc_fork_repair_fallbacks == 1 && tc_fork_repair_lock_failures == 0);
}
#endif

int main(int argc, char **argv)
{
	const char *name = argc > 1 ? argv[1] : "";

	pg = (size_t)getpagesize();
	CHECK(pg <= 16384);
#if defined(__NetBSD__)
	if (strcmp(name, "kernel") == 0) {
		case_kernel();
		return 0;
	}
#endif
#if defined(__NetBSD__) && defined(TC_FORK_REPAIR)
	if (strcmp(name, "regions") == 0) case_regions();
	else if (strcmp(name, "read_after_repair") == 0) case_read_after_repair();
	else if (strcmp(name, "nested") == 0) case_nested();
	else if (strcmp(name, "bounds") == 0) case_bounds();
	else if (strcmp(name, "registry") == 0) case_registry();
	else if (strcmp(name, "fallback") == 0) case_fallback();
	else {
		fprintf(stderr, "unknown case %s\n", name);
		return 2;
	}
	printf("PASS %s\n", name);
#else
	printf("SKIP %s: this build does not link the fork() repair\n", name);
#endif
	return 0;
}
