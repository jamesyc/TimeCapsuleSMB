/* Execute the appliance's *at emulation (lib/replace/tc_at_emulation.c, Samba
 * patch 0002) against a real directory: every emulated call, relative to a
 * directory descriptor, absolute and AT_FDCWD; its errno contract; that the
 * working directory is always restored and no descriptor leaks; that names
 * resolve from the descriptor's directory after it is renamed; renames within
 * and across directories, also past PATH_MAX; utimensat/futimens with UTIME_NOW and UTIME_OMIT;
 * fdopendir over a large directory, and listings of a directory that changes
 * while another descriptor on it is closed. Appliance builds link the library's
 * emulation (NetBSD 4 also its fdopendir); host builds compile the same file
 * into this driver, where the kernel's own O_DIRECTORY and fdopendir are
 * used. futimens is emulated everywhere: NetBSD 6's own ignores the call when
 * atime is UTIME_OMIT and sets the mtime to -1 when mtime is. */
#ifndef TC_SAMBA4X_AT_EMULATION
#define TC_SAMBA4X_AT_EMULATION 1
#define TC_AT_EMULATION_IN_DRIVER 1
#endif
#include "replace.h"
#include "system/filesys.h"
#include "system/dir.h"
#include "system/time.h"
#ifdef TC_AT_EMULATION_IN_DRIVER
#include "../../lib/replace/tc_at_emulation.c"
#endif

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: %s errno=%d (%s)\n", __FILE__, __LINE__, #x, errno, strerror(errno)); exit(90); } } while (0)
/* A call that must fail with this errno and leave the working directory alone. */
#define FAILS(call, err) do { errno = 0; CHECK((call) == -1); CHECK(errno == (err)); check_cwd(); } while (0)

static char workdir[PATH_MAX];
static struct stat cwd_st;

/* The working directory is where the case started: every call restores it. */
static void check_cwd(void)
{
	struct stat st;
	CHECK(stat(".", &st) == 0);
	CHECK(st.st_dev == cwd_st.st_dev && st.st_ino == cwd_st.st_ino);
}

static void enter_case(const char *name)
{
	char dir[PATH_MAX];
	CHECK(snprintf(dir, sizeof(dir), "%s/%s", workdir, name) > 0);
	CHECK(mkdir(dir, 0755) == 0 && chdir(dir) == 0);
	CHECK(stat(".", &cwd_st) == 0);
}

static int open_dir(const char *path)
{
	int fd = open(path, O_RDONLY);
	CHECK(fd >= 0);
	return fd;
}

static void make_file(int dirfd, const char *name, const char *data)
{
	int fd = openat(dirfd, name, O_CREAT | O_EXCL | O_WRONLY, 0644);
	CHECK(fd >= 0);
	check_cwd();
	CHECK(write(fd, data, strlen(data)) == (ssize_t)strlen(data));
	CHECK(close(fd) == 0);
}

static bool has_content(const char *path, const char *data)
{
	char buf[256];
	ssize_t n;
	int fd = open(path, O_RDONLY);
	if (fd < 0) return false;
	n = read(fd, buf, sizeof(buf) - 1);
	close(fd);
	if (n < 0) return false;
	buf[n] = '\0';
	return strcmp(buf, data) == 0;
}

static bool exists(const char *path)
{
	struct stat st;
	return lstat(path, &st) == 0;
}

static int count_open_fds(void)
{
	int fd, n = 0;
	for (fd = 0; fd < 256; fd++) {
		if (fcntl(fd, F_GETFD) != -1) n++;
	}
	return n;
}

/* Every emulated call, relative to a directory descriptor other than the cwd. */
static void test_calls(void)
{
	struct stat st;
	char buf[64];
	ssize_t n;
	int d, fd;

	enter_case("calls");
	CHECK(mkdir("d", 0755) == 0);
	d = open_dir("d");

	make_file(d, "f", "hello");
	CHECK(has_content("d/f", "hello"));
	FAILS(openat(d, "f", O_CREAT | O_EXCL | O_WRONLY, 0644), EEXIST);
	fd = openat(d, "f", O_RDONLY);
	CHECK(fd >= 0 && close(fd) == 0);
	check_cwd();

	CHECK(fstatat(d, "f", &st, 0) == 0 && S_ISREG(st.st_mode) && st.st_size == 5);
	check_cwd();
	CHECK(symlinkat("f", d, "l") == 0);
	check_cwd();
	CHECK(fstatat(d, "l", &st, AT_SYMLINK_NOFOLLOW) == 0 && S_ISLNK(st.st_mode));
	CHECK(fstatat(d, "l", &st, 0) == 0 && S_ISREG(st.st_mode));
	n = readlinkat(d, "l", buf, sizeof(buf));
	CHECK(n == 1 && buf[0] == 'f');
	check_cwd();

	CHECK(mkdirat(d, "sub", 0755) == 0);
	check_cwd();
	CHECK(fstatat(d, "sub", &st, 0) == 0 && S_ISDIR(st.st_mode));
	CHECK(unlinkat(d, "sub", AT_REMOVEDIR) == 0);
	check_cwd();
	CHECK(!exists("d/sub"));

	CHECK(linkat(d, "f", d, "h", 0) == 0);
	check_cwd();
	CHECK(fstatat(d, "h", &st, 0) == 0 && st.st_nlink == 2);
	CHECK(unlinkat(d, "f", 0) == 0);
	check_cwd();
	CHECK(!exists("d/f") && has_content("d/h", "hello"));

	/* FIFOs are optional on HFS; either one is made or a clean refusal. */
	errno = 0;
	if (mknodat(d, "p", S_IFIFO | 0600, 0) == 0) {
		CHECK(fstatat(d, "p", &st, AT_SYMLINK_NOFOLLOW) == 0 && S_ISFIFO(st.st_mode));
		CHECK(unlinkat(d, "p", 0) == 0);
	} else {
		CHECK(errno == EPERM || errno == EOPNOTSUPP || errno == EINVAL || errno == ENOTSUP);
	}
	check_cwd();

	CHECK(close(d) == 0);
	CHECK(chdir(workdir) == 0);
}

/* An absolute name ignores the descriptor, even an invalid one; AT_FDCWD is
 * the working directory. */
static void test_absolute(void)
{
	char abs_dir[PATH_MAX], path[PATH_MAX], other[PATH_MAX];
	struct stat st;
	char buf[PATH_MAX];
	int fd;

	enter_case("absolute");
	CHECK(getcwd(abs_dir, sizeof(abs_dir)) != NULL);
	CHECK(snprintf(path, sizeof(path), "%s/a", abs_dir) > 0);
	CHECK(snprintf(other, sizeof(other), "%s/b", abs_dir) > 0);

	fd = openat(-1, path, O_CREAT | O_EXCL | O_WRONLY, 0644);
	CHECK(fd >= 0 && close(fd) == 0);
	check_cwd();
	CHECK(fstatat(-1, path, &st, 0) == 0 && S_ISREG(st.st_mode));
	CHECK(renameat(-1, path, -1, other) == 0);
	check_cwd();
	CHECK(!exists("a") && exists("b"));
	CHECK(linkat(-1, other, -1, path, 0) == 0);
	CHECK(unlinkat(-1, other, 0) == 0);
	CHECK(symlinkat(path, -1, other) == 0);
	CHECK(readlinkat(-1, other, buf, sizeof(buf)) == (ssize_t)strlen(path));
	CHECK(utimensat(-1, other, NULL, AT_SYMLINK_NOFOLLOW) == 0);
	CHECK(unlinkat(-1, other, 0) == 0);
	CHECK(snprintf(other, sizeof(other), "%s/dir", abs_dir) > 0);
	CHECK(mkdirat(-1, other, 0755) == 0);
	CHECK(unlinkat(-1, other, AT_REMOVEDIR) == 0);
	check_cwd();

	/* AT_FDCWD: relative to the working directory. */
	fd = openat(AT_FDCWD, "c", O_CREAT | O_EXCL | O_WRONLY, 0644);
	CHECK(fd >= 0 && close(fd) == 0);
	CHECK(exists("c"));
	CHECK(renameat(AT_FDCWD, "c", AT_FDCWD, "c2") == 0);
	CHECK(exists("c2") && !exists("c"));
	CHECK(fstatat(AT_FDCWD, "c2", &st, 0) == 0);
	CHECK(unlinkat(AT_FDCWD, "c2", 0) == 0);
	check_cwd();
	CHECK(chdir(workdir) == 0);
}

/* The errno of each failure is the real call's, and the cwd is restored. */
static void test_errors(void)
{
	struct stat st;
	struct timespec ts[2] = { { 0, 0 }, { 0, 0 } };
	char buf[16];
	int d, f;

	enter_case("errors");
	CHECK(mkdir("d", 0755) == 0);
	d = open_dir("d");
	make_file(d, "file", "x");
	f = open("d/file", O_RDONLY);
	CHECK(f >= 0);

	/* A relative name needs a valid directory descriptor. */
	FAILS(openat(-1, "x", O_RDONLY), EBADF);
	FAILS(fstatat(-1, "x", &st, 0), EBADF);
	FAILS(mkdirat(-1, "x", 0755), EBADF);
	/* A descriptor that is not a directory. */
	FAILS(openat(f, "x", O_RDONLY), ENOTDIR);
	FAILS(unlinkat(f, "x", 0), ENOTDIR);
	/* Missing names. */
	FAILS(openat(d, "missing", O_RDONLY), ENOENT);
	FAILS(fstatat(d, "missing", &st, 0), ENOENT);
	FAILS(readlinkat(d, "missing", buf, sizeof(buf)), ENOENT);
	FAILS(renameat(d, "missing", d, "other"), ENOENT);
	FAILS(linkat(d, "missing", d, "other", 0), ENOENT);
	FAILS(utimensat(d, "missing", ts, 0), ENOENT);
	FAILS(utimensat(d, "missing", NULL, 0), ENOENT);
	/* Existing and non-empty. */
	FAILS(mkdirat(d, "file", 0755), EEXIST);
	FAILS(symlinkat("t", d, "file"), EEXIST);
	CHECK(mkdirat(d, "full", 0755) == 0);
	make_file(d, "full/x", "x");
	errno = 0;
	CHECK(unlinkat(d, "full", AT_REMOVEDIR) == -1);
	CHECK(errno == ENOTEMPTY || errno == EEXIST);
	check_cwd();
	/* Not a symlink, flags the emulation does not support. */
	FAILS(readlinkat(d, "file", buf, sizeof(buf)), EINVAL);
	FAILS(fstatat(d, "file", &st, 0x40000000), EINVAL);
	FAILS(unlinkat(d, "file", 0x40000000), EINVAL);
	FAILS(linkat(d, "file", d, "l2", 0x40000000), EINVAL);
	FAILS(utimensat(d, "file", NULL, 0x40000000), EINVAL);
	/* A NULL name. */
	FAILS(openat(d, NULL, O_RDONLY), EFAULT);

	CHECK(close(f) == 0 && close(d) == 0);
	CHECK(chdir(workdir) == 0);
}

/* NetBSD reports O_NOFOLLOW on a symlink as EFTYPE; the emulation says ELOOP.
 * O_DIRECTORY refuses a file (emulated on NetBSD 4, whose kernel ignores it). */
static void test_flags(void)
{
	struct stat st;
	int d, fd;
	mode_t mask;

	enter_case("flags");
	CHECK(mkdir("d", 0755) == 0);
	CHECK(mkdir("d/sub", 0755) == 0);
	d = open_dir("d");
	make_file(d, "file", "x");
	CHECK(symlinkat("file", d, "link") == 0);
	CHECK(symlinkat("sub", d, "dirlink") == 0);

	FAILS(openat(d, "link", O_RDONLY | O_NOFOLLOW), ELOOP);
	FAILS(openat(d, "file", O_RDONLY | O_DIRECTORY), ENOTDIR);
	FAILS(openat(d, "link", O_RDONLY | O_DIRECTORY), ENOTDIR);
	errno = 0;
	CHECK(openat(d, "dirlink", O_RDONLY | O_DIRECTORY | O_NOFOLLOW) == -1);
	CHECK(errno == ELOOP || errno == ENOTDIR);
	check_cwd();
	fd = openat(d, "sub", O_RDONLY | O_DIRECTORY);
	CHECK(fd >= 0);
	CHECK(fstat(fd, &st) == 0 && S_ISDIR(st.st_mode) && close(fd) == 0);
	fd = openat(d, "dirlink", O_RDONLY | O_DIRECTORY);
	CHECK(fd >= 0 && close(fd) == 0);
	fd = openat(d, "file", O_RDONLY | O_NOFOLLOW);
	CHECK(fd >= 0 && close(fd) == 0);
	/* A directory opens for reading, as open(2) allows. */
	fd = openat(d, "sub", O_RDONLY);
	CHECK(fd >= 0 && close(fd) == 0);
	FAILS(openat(d, "sub", O_RDWR), EISDIR);

	/* The creation mode reaches open(2). */
	mask = umask(022);
	fd = openat(d, "mode", O_CREAT | O_EXCL | O_WRONLY, 0640);
	umask(mask);
	CHECK(fd >= 0 && close(fd) == 0);
	CHECK(fstatat(d, "mode", &st, 0) == 0 && (st.st_mode & 0777) == 0640);
	check_cwd();
	CHECK(close(d) == 0);
	CHECK(chdir(workdir) == 0);
}

/* A descriptor keeps naming its directory after that directory is renamed and
 * another takes its name, as with the real calls. */
static void test_renamed(void)
{
	char buf[16];
	int d;

	enter_case("renamed");
	CHECK(mkdir("orig", 0755) == 0);
	d = open_dir("orig");
	CHECK(rename("orig", "moved") == 0);
	CHECK(mkdir("orig", 0755) == 0);

	make_file(d, "new", "via-fd");
	CHECK(has_content("moved/new", "via-fd") && !exists("orig/new"));
	CHECK(mkdirat(d, "sub", 0755) == 0 && exists("moved/sub"));
	CHECK(symlinkat("new", d, "lnk") == 0);
	CHECK(readlinkat(d, "lnk", buf, sizeof(buf)) == 3);
	CHECK(renameat(d, "new", d, "renamed") == 0);
	CHECK(exists("moved/renamed") && !exists("orig/renamed"));
	CHECK(unlinkat(d, "sub", AT_REMOVEDIR) == 0 && !exists("moved/sub"));
	check_cwd();
	CHECK(close(d) == 0);
	CHECK(chdir(workdir) == 0);
}

/* Renames within a directory, through two descriptors of one directory,
 * across directories, onto an existing name, and mixed with AT_FDCWD. */
static void test_rename(void)
{
	int a, a2, b;

	enter_case("rename");
	CHECK(mkdir("a", 0755) == 0 && mkdir("b", 0755) == 0);
	a = open_dir("a");
	a2 = open_dir("a");
	b = open_dir("b");

	make_file(a, "one", "1");
	CHECK(renameat(a, "one", a, "two") == 0);
	CHECK(has_content("a/two", "1") && !exists("a/one"));
	CHECK(renameat(a, "two", a2, "three: with colon") == 0);
	CHECK(has_content("a/three: with colon", "1"));
	CHECK(renameat(a2, "three: with colon", b, "four") == 0);
	CHECK(has_content("b/four", "1") && !exists("a/three: with colon"));
	make_file(a, "five", "5");
	CHECK(renameat(a, "five", b, "four") == 0); /* replaces */
	CHECK(has_content("b/four", "5") && !exists("a/five"));
	CHECK(renameat(b, "four", AT_FDCWD, "six") == 0);
	CHECK(has_content("six", "5"));
	CHECK(renameat(AT_FDCWD, "six", a, "seven") == 0);
	CHECK(has_content("a/seven", "5") && !exists("six"));
	/* Directories move too. */
	CHECK(mkdirat(a, "dir", 0755) == 0);
	make_file(a, "dir/inner", "in");
	CHECK(renameat(a, "dir", b, "dir") == 0);
	CHECK(has_content("b/dir/inner", "in"));
	check_cwd();
	CHECK(close(a) == 0 && close(a2) == 0 && close(b) == 0);
	CHECK(chdir(workdir) == 0);
}

/* A directory whose absolute name is longer than PATH_MAX: calls relative to
 * its descriptor never build an absolute name, so they all work, including a
 * rename between two descriptors of that same directory. */
static void test_long_paths(void)
{
	char name[201];
	struct stat st;
	int i, deep, deep2, fd;

	enter_case("long_paths");
	memset(name, 'n', 200);
	name[200] = '\0';
	for (i = 0; i < 7; i++) {
		CHECK(mkdir(name, 0755) == 0 && chdir(name) == 0);
	}
	deep = open_dir(".");
	deep2 = open_dir(".");
	CHECK(chdir(workdir) == 0 && chdir("long_paths") == 0);

	make_file(deep, "x", "deep");
	CHECK(fstatat(deep, "x", &st, 0) == 0 && st.st_size == 4);
	CHECK(mkdirat(deep, "d", 0755) == 0);
	CHECK(renameat(deep, "x", deep2, "y") == 0);
	CHECK(renameat(deep2, "y", deep, "d/y") == 0);
	fd = openat(deep, "d/y", O_RDONLY);
	CHECK(fd >= 0 && close(fd) == 0);
	CHECK(unlinkat(deep, "d/y", 0) == 0);
	CHECK(unlinkat(deep, "d", AT_REMOVEDIR) == 0);
	check_cwd();
	CHECK(close(deep) == 0 && close(deep2) == 0);
	CHECK(chdir(workdir) == 0);
}

/* The kernels' getcwd() limit (NetBSD: MAXPATHLEN * 4; Linux: a page). */
#define GETCWD_MAX 4096

/* Create n nested 200-byte directories under dirfd; return the deepest. */
static int make_chain(int dirfd, const char *top, int n)
{
	char name[201];
	int fd, next, i;

	memset(name, 'n', 200);
	name[200] = '\0';
	CHECK(mkdirat(dirfd, top, 0755) == 0);
	fd = openat(dirfd, top, O_RDONLY);
	CHECK(fd >= 0);
	for (i = 0; i < n; i++) {
		CHECK(mkdirat(fd, name, 0755) == 0);
		next = openat(fd, name, O_RDONLY);
		CHECK(next >= 0 && close(fd) == 0);
		fd = next;
	}
	return fd;
}

static bool has_content_at(int dirfd, const char *name, const char *data)
{
	char buf[64];
	ssize_t n;
	int fd = openat(dirfd, name, O_RDONLY);
	if (fd < 0) return false;
	n = read(fd, buf, sizeof(buf) - 1);
	close(fd);
	if (n < 0) return false;
	buf[n] = '\0';
	return strcmp(buf, data) == 0;
}

static bool exists_at(int dirfd, const char *name)
{
	struct stat st;
	return fstatat(dirfd, name, &st, AT_SYMLINK_NOFOLLOW) == 0;
}

/* Create top under dirfd (whose absolute name is base), then nested
 * directories until the deepest one's absolute name is exactly target bytes;
 * return the deepest. */
static int make_chain_to(int dirfd, const char *base, const char *top, size_t target)
{
	char name[256];
	size_t len = strlen(base) + 1 + strlen(top), r, n;
	int fd, next;

	CHECK(mkdirat(dirfd, top, 0755) == 0);
	fd = openat(dirfd, top, O_RDONLY);
	CHECK(fd >= 0 && len + 2 <= target);
	while (len < target) {
		/* Whole 200-byte names while more than one name is left, then
		 * one name that ends exactly at target (at most 255 bytes). */
		r = target - len;
		n = r > 256 ? 200 : r - 1;
		memset(name, 'e', n);
		name[n] = '\0';
		CHECK(mkdirat(fd, name, 0755) == 0);
		next = openat(fd, name, O_RDONLY);
		CHECK(next >= 0 && close(fd) == 0);
		fd = next;
		len += 1 + n;
	}
	return fd;
}

/* A rename or link the emulation may be unable to make (its limits, see
 * rep_at_two_names()): it either takes effect, or fails with ENAMETOOLONG
 * (never getcwd()'s ERANGE) and leaves both names as they were, and the
 * working directory is restored either way. */
static void moved_or_refused(int ret, int olddir, const char *oldname,
			     int newdir, const char *newname, bool link)
{
	int err = errno;

	check_cwd();
	if (ret == 0) {
		CHECK(exists_at(newdir, newname));
		CHECK(exists_at(olddir, oldname) == link);
		if (link) {
			CHECK(unlinkat(newdir, newname, 0) == 0);
		}
	} else {
		CHECK(ret == -1 && err == ENAMETOOLONG);
		CHECK(exists_at(olddir, oldname) && !exists_at(newdir, newname));
	}
}

/* Renames and links between two different directories: the emulation works
 * from one of them and names the other relative to it ("../" up to the
 * common ancestor, then down), from whichever side is shorter, so moves
 * between directories deeper than PATH_MAX work as long as getcwd() can
 * name both (the appliance: PATH_MAX 1,024, getcwd up to 4,095 bytes).
 * Where PATH_MAX is getcwd's limit too (Linux) the chain stays shorter and
 * only the relative names are tested. Past those limits the emulation
 * refuses what a real renameat() would do; that part is checked for a clean
 * refusal (ENAMETOOLONG, never ERANGE, nothing moved), not pinned, so an
 * emulation that handles it still passes. */
static void test_cross_directory(void)
{
	char abs_dir[PATH_MAX], path[PATH_MAX], deep[GETCWD_MAX + 1];
	struct stat st;
	int top, trunk, p, c, s, t, a, ab, far1, far2, edge, over, gone, file;
	int before, levels, i;

	enter_case("cross_directory");
	top = open_dir(".");
	before = count_open_fds();
	/* trunk/p/c and trunk/s/t, with trunk past PATH_MAX where possible. */
	levels = PATH_MAX < GETCWD_MAX ? PATH_MAX / 201 + 1 : 3;
	trunk = make_chain(top, "trunk", levels);
	CHECK(mkdirat(trunk, "p", 0755) == 0 && mkdirat(trunk, "p/c", 0755) == 0);
	CHECK(mkdirat(trunk, "s", 0755) == 0 && mkdirat(trunk, "s/t", 0755) == 0);
	p = openat(trunk, "p", O_RDONLY);
	c = openat(trunk, "p/c", O_RDONLY);
	s = openat(trunk, "s", O_RDONLY);
	t = openat(trunk, "s/t", O_RDONLY);
	CHECK(p >= 0 && c >= 0 && s >= 0 && t >= 0);

	/* Up, down, to a sibling and a cousin. */
	make_file(c, "f", "cross");
	CHECK(renameat(c, "f", p, "up") == 0);
	check_cwd();
	CHECK(has_content_at(p, "up", "cross") && !exists_at(c, "f"));
	CHECK(renameat(p, "up", c, "down") == 0);
	CHECK(has_content_at(c, "down", "cross") && !exists_at(p, "up"));
	CHECK(renameat(p, "c/down", s, "sibling") == 0);
	CHECK(has_content_at(s, "sibling", "cross"));
	CHECK(renameat(s, "sibling", c, "cousin") == 0);
	CHECK(renameat(c, "cousin", t, "cousin") == 0);
	CHECK(has_content_at(t, "cousin", "cross") && !exists_at(c, "cousin"));
	/* From deep to the shallow case directory and back: only the name from
	 * the deep side ("../" repeated) fits. */
	CHECK(renameat(t, "cousin", top, "shallow") == 0);
	CHECK(has_content("shallow", "cross") && !exists_at(t, "cousin"));
	CHECK(renameat(top, "shallow", c, "deep") == 0);
	CHECK(has_content_at(c, "deep", "cross") && !exists("shallow"));
	check_cwd();
	/* A directory with its contents, and replacing an existing file. */
	CHECK(mkdirat(c, "dir", 0755) == 0);
	make_file(c, "dir/inner", "in");
	CHECK(renameat(c, "dir", top, "dir") == 0);
	CHECK(has_content("dir/inner", "in") && !exists_at(c, "dir"));
	make_file(t, "old", "replaced");
	CHECK(renameat(c, "deep", t, "old") == 0);
	CHECK(has_content_at(t, "old", "cross") && !exists_at(c, "deep"));
	/* Hard links across directories. */
	CHECK(linkat(t, "old", p, "link", 0) == 0);
	CHECK(fstatat(p, "link", &st, 0) == 0 && st.st_nlink == 2);
	CHECK(linkat(p, "link", top, "link", 0) == 0);
	CHECK(fstatat(top, "link", &st, 0) == 0 && st.st_nlink == 3);
	CHECK(unlinkat(p, "link", 0) == 0 && unlinkat(top, "link", 0) == 0);
	check_cwd();

	/* One absolute name: no directory names are needed. */
	CHECK(getcwd(abs_dir, sizeof(abs_dir)) != NULL);
	CHECK(snprintf(path, sizeof(path), "%s/absolute", abs_dir) > 0);
	CHECK(renameat(t, "old", -1, path) == 0);
	CHECK(has_content("absolute", "cross") && !exists_at(t, "old"));
	CHECK(renameat(-1, path, c, "back") == 0);
	CHECK(has_content_at(c, "back", "cross") && !exists("absolute"));

	/* Names that share a prefix but not a component ("a" and "ab"). */
	CHECK(mkdirat(top, "a", 0755) == 0 && mkdirat(top, "ab", 0755) == 0);
	a = open_dir("a");
	ab = open_dir("ab");
	make_file(ab, "g", "prefix");
	CHECK(renameat(ab, "g", a, "g") == 0);
	CHECK(has_content("a/g", "prefix") && !exists("ab/g"));
	CHECK(renameat(a, "g", ab, "g") == 0);
	CHECK(has_content("ab/g", "prefix") && !exists("a/g"));

	/* Two branches too far apart for a name either way. */
	/* The deepest directory getcwd() can name: renames in and out work. */
	edge = make_chain_to(top, abs_dir, "edge", GETCWD_MAX - 1);
	CHECK(fchdir(edge) == 0 && getcwd(deep, sizeof(deep)) != NULL);
	CHECK(strlen(deep) == GETCWD_MAX - 1 && fchdir(top) == 0);
	make_file(edge, "x", "edge");
	CHECK(renameat(edge, "x", top, "from_edge") == 0 && has_content("from_edge", "edge"));
	CHECK(renameat(top, "from_edge", edge, "x") == 0 && has_content_at(edge, "x", "edge"));
	CHECK(linkat(edge, "x", top, "edge_link", 0) == 0 && unlinkat(top, "edge_link", 0) == 0);
	check_cwd();

	/* The emulation's limits, not the contract: where it cannot name the
	 * other directory relative to one of them, a real renameat() still
	 * succeeds, so these accept a move as well as a clean refusal. Two
	 * branches too far apart for a relative name either way: */
	far1 = make_chain(top, "far1", PATH_MAX / 201 + 1);
	far2 = make_chain(top, "far2", PATH_MAX / 201 + 1);
	make_file(far1, "stay", "put");
	moved_or_refused(linkat(far1, "stay", far2, "linked", 0), far1, "stay", far2, "linked", true);
	moved_or_refused(renameat(far1, "stay", far2, "moved"), far1, "stay", far2, "moved", false);
	/* and a directory past what getcwd() can name. */
	CHECK(mkdirat(edge, "o", 0755) == 0);
	over = openat(edge, "o", O_RDONLY);
	CHECK(over >= 0);
	make_file(over, "y", "over");
	moved_or_refused(renameat(over, "y", top, "from_over"), over, "y", top, "from_over", false);
	make_file(top, "to_over", "top");
	moved_or_refused(renameat(top, "to_over", over, "to_over"), top, "to_over", over, "to_over", false);
	/* Renames within it name nothing else and always work. */
	make_file(over, "z", "within");
	CHECK(renameat(over, "z", over, "z2") == 0 && has_content_at(over, "z2", "within"));

	/* Errors are the real call's. */
	CHECK(mkdirat(top, "gone", 0755) == 0);
	gone = open_dir("gone");
	CHECK(unlinkat(top, "gone", AT_REMOVEDIR) == 0);
	FAILS(renameat(gone, "f", c, "f"), ENOENT);
	FAILS(renameat(-1, "f", c, "f"), EBADF);
	FAILS(renameat(c, "back", -1, "f"), EBADF);
	file = openat(c, "back", O_RDONLY);
	CHECK(file >= 0);
	FAILS(renameat(file, "f", c, "f"), ENOTDIR);
	FAILS(renameat(c, "missing", t, "f"), ENOENT);
	FAILS(renameat(c, "back", t, NULL), EFAULT);

	for (i = 0; i < 20; i++) {
		CHECK(renameat(c, "back", t, "forth") == 0);
		CHECK(renameat(t, "forth", c, "back") == 0);
		/* Fails however the emulation handles far-apart directories. */
		CHECK(renameat(far1, "missing", far2, "moved") == -1);
	}
	CHECK(close(file) == 0 && close(gone) == 0 && close(over) == 0 && close(edge) == 0);
	CHECK(close(far1) == 0 && close(far2) == 0 && close(a) == 0 && close(ab) == 0);
	CHECK(close(p) == 0 && close(c) == 0 && close(s) == 0 && close(t) == 0);
	CHECK(close(trunk) == 0);
	CHECK(count_open_fds() == before);
	check_cwd();
	CHECK(close(top) == 0);
	CHECK(chdir(workdir) == 0);
}

#define LISTING_FILES 600
#define LISTING_BATCH 64

/* Open and close another descriptor on the directory, as smbd does for every
 * create and delete in it: NetBSD 4's HFS drops a listing's place on any such
 * close, so without rep_fdopendir()'s full read the listing resumes by
 * counting entries. */
static void touch_close(int dirfd)
{
	int fd = openat(dirfd, ".", O_RDONLY);
	CHECK(fd >= 0 && close(fd) == 0);
}

static int listed_index(const char *name, char prefix)
{
	int i;
	char end[8];
	if (name[0] != prefix || sscanf(name + 1, "%4d%7s", &i, end) != 2 || strcmp(end, ".txt") != 0) {
		return -1;
	}
	return i;
}

static void make_listing(int d)
{
	char name[32];
	int i;
	for (i = 0; i < LISTING_FILES; i++) {
		snprintf(name, sizeof(name), "t%04d.txt", i);
		make_file(d, name, "x");
	}
}

/* A directory that changes while an fdopendir() stream lists it, with another
 * descriptor on it closed between batches: deletions skip nothing, creations
 * repeat nothing, reading after the end repeats nothing, and rewinddir() shows
 * the directory as it is now and can be repeated. */
static void test_listing_changes(void)
{
	static char seen[LISTING_FILES];
	static char batch[LISTING_BATCH][NAME_MAX + 1];
	char name[32];
	struct dirent *e;
	DIR *dir;
	int d, fd, i, n, idx, before, entries, created = 0;
	bool saw_new, saw_deleted, saw_kept;

	enter_case("listing_changes");
	CHECK(mkdir("d", 0755) == 0);
	d = open_dir("d");
	before = count_open_fds();

	/* Delete each batch as it is read. */
	make_listing(d);
	memset(seen, 0, sizeof(seen));
	fd = open_dir("d");
	dir = fdopendir(fd);
	CHECK(dir != NULL);
	n = 0;
	while ((e = readdir(dir)) != NULL) {
		if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0) {
			continue;
		}
		idx = listed_index(e->d_name, 't');
		CHECK(idx >= 0 && idx < LISTING_FILES && !seen[idx]);
		seen[idx] = 1;
		snprintf(batch[n++], sizeof(batch[0]), "%s", e->d_name);
		if (n == LISTING_BATCH) {
			for (i = 0; i < n; i++) {
				CHECK(unlinkat(d, batch[i], 0) == 0);
			}
			touch_close(d);
			n = 0;
		}
	}
	for (i = 0; i < n; i++) {
		CHECK(unlinkat(d, batch[i], 0) == 0);
	}
	CHECK(closedir(dir) == 0);
	for (i = 0; i < LISTING_FILES; i++) {
		CHECK(seen[i]);
	}
	fd = open_dir("d");
	dir = fdopendir(fd);
	CHECK(dir != NULL);
	entries = 0;
	while ((e = readdir(dir)) != NULL) {
		entries += strcmp(e->d_name, ".") != 0 && strcmp(e->d_name, "..") != 0;
	}
	CHECK(entries == 0 && closedir(dir) == 0);
	check_cwd();

	/* Create names that sort before the position while listing. */
	make_listing(d);
	memset(seen, 0, sizeof(seen));
	fd = open_dir("d");
	dir = fdopendir(fd);
	CHECK(dir != NULL);
	n = entries = 0;
	while ((e = readdir(dir)) != NULL) {
		if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0) {
			continue;
		}
		/* A listing that repeats entries runs on; bound it. */
		CHECK(++entries <= LISTING_FILES + 5 * LISTING_BATCH);
		idx = listed_index(e->d_name, 't');
		if (idx >= 0) {
			CHECK(idx < LISTING_FILES && !seen[idx]);
			seen[idx] = 1;
		} else {
			CHECK(listed_index(e->d_name, 'a') >= 0);
		}
		if (++n == LISTING_BATCH) {
			for (i = 0; i < LISTING_BATCH && created < 5 * LISTING_BATCH; i++) {
				snprintf(name, sizeof(name), "a%04d.txt", created++);
				make_file(d, name, "a");
			}
			touch_close(d);
			n = 0;
		}
	}
	for (i = 0; i < LISTING_FILES; i++) {
		CHECK(seen[i]);
	}

	/* After the end, readdir() never returns an entry again, even when files
	 * were created before the listing's position (HFS repeats the last
	 * entries there: NetBSD 6 keeps reporting the end until a rewind). A new
	 * name may appear; an old one may not. */
	for (i = 0; i < 20; i++) {
		snprintf(name, sizeof(name), "e%04d.txt", i);
		make_file(d, name, "e");
	}
	while ((e = readdir(dir)) != NULL) {
		CHECK(listed_index(e->d_name, 'e') >= 0);
	}
	CHECK(readdir(dir) == NULL);

	/* rewinddir() reads the directory again, and a rewind clears the end. */
	make_file(d, "z_new.txt", "z");
	CHECK(unlinkat(d, "t0000.txt", 0) == 0);
	rewinddir(dir);
	saw_new = saw_deleted = saw_kept = false;
	n = 0;
	while ((e = readdir(dir)) != NULL) {
		saw_new |= strcmp(e->d_name, "z_new.txt") == 0;
		saw_deleted |= strcmp(e->d_name, "t0000.txt") == 0;
		saw_kept |= strcmp(e->d_name, "t0001.txt") == 0;
		n++;
	}
	CHECK(saw_new && !saw_deleted && saw_kept);
	CHECK(readdir(dir) == NULL);
	rewinddir(dir);
	entries = 0;
	while ((e = readdir(dir)) != NULL) {
		entries++;
	}
	CHECK(entries == n);
	CHECK(closedir(dir) == 0);
	CHECK(count_open_fds() == before);
	check_cwd();
	CHECK(close(d) == 0);
	CHECK(chdir(workdir) == 0);
}

/* No descriptor survives any call, whether it succeeds or fails. */
static void test_fds(void)
{
	struct stat st;
	char buf[16];
	int before, i, d, fd;

	enter_case("fds");
	CHECK(mkdir("d", 0755) == 0 && mkdir("e", 0755) == 0);
	d = open_dir("d");
	before = count_open_fds();
	for (i = 0; i < 200; i++) {
		fd = openat(d, "f", O_CREAT | O_WRONLY, 0644);
		CHECK(fd >= 0 && close(fd) == 0);
		CHECK(openat(d, "missing", O_RDONLY) == -1);
		CHECK(fstatat(d, "f", &st, 0) == 0);
		CHECK(fstatat(d, "missing", &st, 0) == -1);
		CHECK(symlinkat("f", d, "l") == 0);
		CHECK(readlinkat(d, "l", buf, sizeof(buf)) == 1);
		CHECK(unlinkat(d, "l", 0) == 0);
		CHECK(mkdirat(d, "s", 0755) == 0);
		CHECK(mkdirat(d, "s", 0755) == -1);
		CHECK(unlinkat(d, "s", AT_REMOVEDIR) == 0);
		CHECK(renameat(d, "f", d, "g") == 0);
		CHECK(renameat(d, "g", AT_FDCWD, "e/g") == 0);
		CHECK(renameat(AT_FDCWD, "e/g", d, "f") == 0);
		CHECK(linkat(d, "f", d, "h", 0) == 0 && unlinkat(d, "h", 0) == 0);
		CHECK(utimensat(d, "f", NULL, 0) == 0);
		CHECK(utimensat(d, "missing", NULL, 0) == -1);
	}
	CHECK(count_open_fds() == before);
	check_cwd();
	CHECK(close(d) == 0);
	CHECK(chdir(workdir) == 0);
}

static struct timespec file_time(const struct stat *st, int which)
{
#if defined(__NetBSD__)
	return which == 0 ? st->st_atimespec : st->st_mtimespec;
#else
	return which == 0 ? st->st_atim : st->st_mtim;
#endif
}

/* Same second, and the same microsecond where the filesystem keeps them
 * (HFS keeps whole seconds). */
static bool same_time(struct timespec a, struct timespec b)
{
	return a.tv_sec == b.tv_sec &&
	       (a.tv_nsec == 0 || b.tv_nsec == 0 || a.tv_nsec / 1000 == b.tv_nsec / 1000);
}

/* utimensat and futimens: explicit times, now, UTIME_NOW and UTIME_OMIT, and
 * AT_SYMLINK_NOFOLLOW on a link changes the link, not its target. */
static void test_times(void)
{
	struct timespec set[2], got_a, got_m;
	struct stat before, st, target_before;
	time_t now;
	int d, fd;

	enter_case("times");
	CHECK(mkdir("d", 0755) == 0);
	d = open_dir("d");
	make_file(d, "f", "t");

	set[0].tv_sec = 981173106; set[0].tv_nsec = 123456000;
	set[1].tv_sec = 981000000; set[1].tv_nsec = 654321000;
	CHECK(utimensat(d, "f", set, 0) == 0);
	check_cwd();
	CHECK(fstatat(d, "f", &st, 0) == 0);
	CHECK(same_time(file_time(&st, 0), set[0]) && same_time(file_time(&st, 1), set[1]));

	/* UTIME_OMIT keeps that time exactly; UTIME_NOW takes the clock. */
	before = st;
	set[0].tv_sec = 0; set[0].tv_nsec = UTIME_OMIT;
	set[1].tv_sec = 990000000; set[1].tv_nsec = 0;
	CHECK(utimensat(d, "f", set, 0) == 0);
	CHECK(fstatat(d, "f", &st, 0) == 0);
	got_a = file_time(&st, 0);
	CHECK(same_time(got_a, file_time(&before, 0)) && file_time(&st, 1).tv_sec == 990000000);
	now = time(NULL);
	set[0].tv_nsec = UTIME_NOW;
	set[1].tv_nsec = UTIME_OMIT;
	CHECK(utimensat(d, "f", set, 0) == 0);
	CHECK(fstatat(d, "f", &st, 0) == 0);
	CHECK(file_time(&st, 0).tv_sec >= now - 1 && file_time(&st, 0).tv_sec <= now + 5);
	CHECK(file_time(&st, 1).tv_sec == 990000000);
	/* Both omitted changes nothing but still needs the name. */
	set[0].tv_nsec = UTIME_OMIT;
	CHECK(utimensat(d, "f", set, 0) == 0);
	CHECK(fstatat(d, "f", &before, 0) == 0 && file_time(&before, 1).tv_sec == 990000000);
	/* NULL is now. */
	CHECK(utimensat(d, "f", NULL, 0) == 0);
	CHECK(fstatat(d, "f", &st, 0) == 0);
	CHECK(file_time(&st, 1).tv_sec >= now - 1 && file_time(&st, 1).tv_sec <= now + 5);

	/* A link's own times, never its target's. Apple's HFS accepts lutimes()
	 * on a symlink but keeps the link's times (probed on both kernels
	 * 2026-09-28); other filesystems set them. */
	set[0].tv_sec = 981173106; set[0].tv_nsec = 0;
	set[1].tv_sec = 981173106; set[1].tv_nsec = 0;
	CHECK(utimensat(d, "f", set, 0) == 0);
	CHECK(symlinkat("f", d, "l") == 0);
	CHECK(fstatat(d, "f", &target_before, 0) == 0);
	CHECK(fstatat(d, "l", &before, AT_SYMLINK_NOFOLLOW) == 0);
	set[1].tv_sec = 970000000;
	CHECK(utimensat(d, "l", set, AT_SYMLINK_NOFOLLOW) == 0);
	CHECK(fstatat(d, "l", &st, AT_SYMLINK_NOFOLLOW) == 0);
	CHECK(file_time(&st, 1).tv_sec == 970000000 ||
	      file_time(&st, 1).tv_sec == file_time(&before, 1).tv_sec);
	CHECK(fstatat(d, "f", &st, 0) == 0 && file_time(&st, 1).tv_sec == file_time(&target_before, 1).tv_sec);
	check_cwd();

	/* futimens on a descriptor. */
	fd = openat(d, "f", O_RDWR);
	CHECK(fd >= 0);
	set[0].tv_sec = 960000000; set[0].tv_nsec = 0;
	set[1].tv_sec = 961000000; set[1].tv_nsec = 0;
	CHECK(futimens(fd, set) == 0);
	CHECK(fstat(fd, &st) == 0 && file_time(&st, 0).tv_sec == 960000000 && file_time(&st, 1).tv_sec == 961000000);
	set[0].tv_nsec = UTIME_OMIT;
	set[1].tv_sec = 962000000;
	CHECK(futimens(fd, set) == 0);
	CHECK(fstat(fd, &st) == 0 && file_time(&st, 0).tv_sec == 960000000 && file_time(&st, 1).tv_sec == 962000000);
	CHECK(futimens(fd, NULL) == 0);
	CHECK(fstat(fd, &st) == 0 && file_time(&st, 1).tv_sec >= now - 1);
	got_m = file_time(&st, 1);
	(void)got_m;
	CHECK(close(fd) == 0);
	CHECK(close(d) == 0);
	CHECK(chdir(workdir) == 0);
}

static int cmpstr(const void *a, const void *b)
{
	return strcmp(*(char *const *)a, *(char *const *)b);
}

/* Read the whole stream: every e* entry exactly once, no read error. */
static void check_listing(DIR *dir, int expect)
{
	char **names = calloc(expect + 1, sizeof(char *));
	struct dirent *de;
	int n = 0, i;

	CHECK(names != NULL);
	errno = 0;
	while ((de = readdir(dir)) != NULL) {
		if (de->d_name[0] != 'e') continue;
		CHECK(n < expect);
		names[n] = strdup(de->d_name);
		CHECK(names[n] != NULL);
		n++;
	}
	CHECK(errno == 0);
	CHECK(n == expect);
	qsort(names, n, sizeof(char *), cmpstr);
	for (i = 1; i < n; i++) CHECK(strcmp(names[i - 1], names[i]) != 0);
	for (i = 0; i < n; i++) free(names[i]);
	free(names);
}

/* fdopendir over a directory larger than one getdents buffer: it takes the
 * caller's descriptor (the same number; closedir closes it), lists everything
 * once, lists again after rewinddir, after the cwd moves, and after the
 * directory is renamed, and never leaks a descriptor. */
static void test_fdopendir(void)
{
	char path[PATH_MAX];
	const int entries = 2500;
	DIR *dir;
	int before, fd, file, i;

	enter_case("fdopendir");
	CHECK(mkdir("big", 0755) == 0);
	for (i = 0; i < entries; i++) {
		CHECK(snprintf(path, sizeof(path), "big/e%05d-a-longer-name-to-fill-directory-blocks", i) > 0);
		fd = open(path, O_CREAT | O_EXCL | O_WRONLY, 0644);
		CHECK(fd >= 0 && close(fd) == 0);
	}
	before = count_open_fds();

	fd = open_dir("big");
	dir = fdopendir(fd);
	CHECK(dir != NULL);
	check_cwd();
	CHECK(dirfd(dir) == fd);
#ifdef TC_SAMBA4X_NETBSD4_COMPAT
	/* The emulation's stream keeps close-on-exec across dup2(). */
	CHECK((fcntl(fd, F_GETFD) & FD_CLOEXEC) != 0);
#endif
	check_listing(dir, entries);
	rewinddir(dir);
	check_listing(dir, entries);
	CHECK(chdir("/") == 0);
	rewinddir(dir);
	check_listing(dir, entries);
	CHECK(chdir(workdir) == 0 && chdir("fdopendir") == 0);
	CHECK(rename("big", "moved") == 0 && mkdir("big", 0755) == 0);
	rewinddir(dir);
	check_listing(dir, entries);
	/* Opening an entry relative to the stream's descriptor still works. */
	file = openat(dirfd(dir), "e00000-a-longer-name-to-fill-directory-blocks", O_RDONLY);
	CHECK(file >= 0 && close(file) == 0);
	CHECK(closedir(dir) == 0);
	errno = 0;
	CHECK(fcntl(fd, F_GETFD) == -1 && errno == EBADF);
	CHECK(count_open_fds() == before);

	/* A file is refused and its descriptor stays the caller's. */
	file = open("moved/e00001-a-longer-name-to-fill-directory-blocks", O_RDONLY);
	CHECK(file >= 0);
	errno = 0;
	CHECK(fdopendir(file) == NULL && errno == ENOTDIR);
	CHECK(fcntl(file, F_GETFD) != -1 && close(file) == 0);

	for (i = 0; i < 50; i++) {
		fd = open_dir("moved");
		dir = fdopendir(fd);
		CHECK(dir != NULL && closedir(dir) == 0);
	}
	CHECK(count_open_fds() == before);
	check_cwd();
	CHECK(chdir(workdir) == 0);
}

int main(int argc, char **argv)
{
	const char *c;
	bool all;

	CHECK(argc == 2);
	c = argv[1];
	all = strcmp(c, "all") == 0;
	CHECK(snprintf(workdir, sizeof(workdir), "%s/tc-at-emulation.XXXXXX",
		       getenv("TMPDIR") ? getenv("TMPDIR") : ".") > 0);
	CHECK(mkdtemp(workdir) != NULL);
	if (workdir[0] != '/') {
		char abs_dir[PATH_MAX];
		CHECK(realpath(workdir, abs_dir) != NULL);
		CHECK(strlen(abs_dir) < sizeof(workdir));
		strcpy(workdir, abs_dir);
	}
	CHECK(chdir(workdir) == 0);

	if (all || strcmp(c, "calls") == 0) test_calls();
	if (all || strcmp(c, "absolute") == 0) test_absolute();
	if (all || strcmp(c, "errors") == 0) test_errors();
	if (all || strcmp(c, "flags") == 0) test_flags();
	if (all || strcmp(c, "renamed") == 0) test_renamed();
	if (all || strcmp(c, "rename") == 0) test_rename();
	if (all || strcmp(c, "long_paths") == 0) test_long_paths();
	if (all || strcmp(c, "cross_directory") == 0) test_cross_directory();
	if (all || strcmp(c, "listing_changes") == 0) test_listing_changes();
	if (all || strcmp(c, "fds") == 0) test_fds();
	if (all || strcmp(c, "times") == 0) test_times();
	if (all || strcmp(c, "fdopendir") == 0) test_fdopendir();

	/* Leave nothing on the scratch disk. */
	CHECK(chdir("/") == 0);
	{
		char cmd[PATH_MAX + 16];
		CHECK(snprintf(cmd, sizeof(cmd), "rm -rf '%s'", workdir) > 0);
		CHECK(system(cmd) == 0);
	}
	printf("PASS %s\n", c);
	return 0;
}
