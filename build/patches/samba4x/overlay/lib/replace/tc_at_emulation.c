/* Included by replace.c (patch 0002) for TC_SAMBA4X_AT_EMULATION. */

/*
 * The *at system calls for Apple's Time Capsule kernels.
 *
 * Neither kernel has any of them: NetBSD 4 and NetBSD 6 both return ENOSYS
 * for openat, fstatat, mkdirat, unlinkat, readlinkat, renameat, linkat,
 * symlinkat, mknodat and utimensat (probed on both devices 2026-09-28), even
 * though the NetBSD 7 SDK that builds the NetBSD 6 lane declares them.
 * NetBSD 4 has no futimens() either, and NetBSD 6's mishandles UTIME_OMIT
 * (an omitted atime makes it change nothing; an omitted mtime sets the mtime
 * to -1), so futimens() is emulated on both with futimes(). NetBSD 4 libc
 * also has no fdopendir(); the NetBSD 7 libc one works on NetBSD 6.
 * system/filesys.h maps each name to the rep_ function here.
 *
 * Each call changes the process's working directory to the directory
 * descriptor with fchdir(), makes the path-based call with the relative
 * name, and changes back. The name is therefore resolved from the
 * descriptor's directory, even after that directory has been renamed, as
 * with the real calls. An absolute name, or AT_FDCWD, needs no change of
 * directory. The working directory belongs to the whole process, so this
 * only works without threads; the appliance builds have none. If the
 * original directory cannot be restored, the process aborts: every later
 * relative call would act on the wrong directory.
 */

#ifdef HAVE_PTHREAD
#error "The fchdir()-based *at emulation needs a process without threads"
#endif

/*
 * Enter dirfd's directory for a call on path. *saved is the descriptor of
 * the original working directory, or -1 when no change was needed.
 */
static int rep_at_enter(int dirfd, const char *path, int *saved)
{
	int err;

	*saved = -1;
	if (path == NULL) {
		errno = EFAULT;
		return -1;
	}
	if (dirfd == AT_FDCWD || path[0] == '/') {
		return 0;
	}
	*saved = open(".", O_RDONLY);
	if (*saved == -1) {
		return -1;
	}
	if (fchdir(dirfd) == -1) {
		err = errno;
		close(*saved);
		*saved = -1;
		errno = err;
		return -1;
	}
	return 0;
}

/* Return to the original working directory, keeping the call's errno. */
static void rep_at_leave(int saved)
{
	int err = errno;

	if (saved == -1) {
		return;
	}
	if (fchdir(saved) == -1) {
		abort();
	}
	close(saved);
	errno = err;
}

int rep_openat(int dirfd, const char *path, int flags, ...)
{
	mode_t mode = 0;
	int saved;
	int fd;
#ifdef TC_AT_EMULATE_O_DIRECTORY
	bool want_directory = (flags & O_DIRECTORY) != 0;
	struct stat st;
	int err;

	/* The NetBSD 4 kernel ignores O_DIRECTORY; check after the open. */
	flags &= ~O_DIRECTORY;
#endif

	if ((flags & O_CREAT) != 0) {
		va_list ap;

		va_start(ap, flags);
		mode = va_arg(ap, mode_t);
		va_end(ap);
	}
#ifdef O_SEARCH
	/*
	 * Samba asks for O_SEARCH on the directories it walks because the
	 * appliance has no O_PATH. Those descriptors are also read and used
	 * for extended attributes, and smbd runs as root, so open them for
	 * reading as before.
	 */
	flags &= ~O_SEARCH;
#endif

	if (rep_at_enter(dirfd, path, &saved) == -1) {
		return -1;
	}
	fd = open(path, flags, mode);
	rep_at_leave(saved);
	if (fd == -1) {
#ifdef EFTYPE
		/*
		 * NetBSD reports O_NOFOLLOW on a symlink as EFTYPE; POSIX and
		 * Samba's path walk (openat_pathref_fsp_nosymlink) expect ELOOP.
		 */
		if (errno == EFTYPE && (flags & O_NOFOLLOW) != 0) {
			errno = ELOOP;
		}
#endif
		return -1;
	}
#ifdef TC_AT_EMULATE_O_DIRECTORY
	if (want_directory) {
		if (fstat(fd, &st) == -1) {
			err = errno;
			close(fd);
			errno = err;
			return -1;
		}
		if (!S_ISDIR(st.st_mode)) {
			close(fd);
			errno = ENOTDIR;
			return -1;
		}
	}
#endif
	return fd;
}

int rep_fstatat(int dirfd, const char *path, struct stat *st, int flags)
{
	int saved;
	int ret;

	if ((flags & ~AT_SYMLINK_NOFOLLOW) != 0) {
		errno = EINVAL;
		return -1;
	}
	if (rep_at_enter(dirfd, path, &saved) == -1) {
		return -1;
	}
	if ((flags & AT_SYMLINK_NOFOLLOW) != 0) {
		ret = lstat(path, st);
	} else {
		ret = stat(path, st);
	}
	rep_at_leave(saved);
	return ret;
}

int rep_mkdirat(int dirfd, const char *path, mode_t mode)
{
	int saved;
	int ret;

	if (rep_at_enter(dirfd, path, &saved) == -1) {
		return -1;
	}
	ret = mkdir(path, mode);
	rep_at_leave(saved);
	return ret;
}

int rep_unlinkat(int dirfd, const char *path, int flags)
{
	int saved;
	int ret;

	if ((flags & ~AT_REMOVEDIR) != 0) {
		errno = EINVAL;
		return -1;
	}
	if (rep_at_enter(dirfd, path, &saved) == -1) {
		return -1;
	}
	if ((flags & AT_REMOVEDIR) != 0) {
		ret = rmdir(path);
	} else {
		ret = unlink(path);
	}
	rep_at_leave(saved);
	return ret;
}

ssize_t rep_readlinkat(int dirfd, const char *path, char *buf, size_t bufsiz)
{
	int saved;
	ssize_t ret;

	if (rep_at_enter(dirfd, path, &saved) == -1) {
		return -1;
	}
	ret = readlink(path, buf, bufsiz);
	rep_at_leave(saved);
	return ret;
}

int rep_symlinkat(const char *target, int dirfd, const char *path)
{
	int saved;
	int ret;

	if (rep_at_enter(dirfd, path, &saved) == -1) {
		return -1;
	}
	ret = symlink(target, path);
	rep_at_leave(saved);
	return ret;
}

int rep_mknodat(int dirfd, const char *path, mode_t mode, dev_t dev)
{
	int saved;
	int ret;

	if (rep_at_enter(dirfd, path, &saved) == -1) {
		return -1;
	}
	ret = mknod(path, mode, dev);
	rep_at_leave(saved);
	return ret;
}

/*
 * The kernels' getcwd() returns names up to 4,095 bytes, whatever the buffer
 * size (NetBSD's sys___getcwd caps the length at MAXPATHLEN * 4; probed on
 * both devices), though any name passed to a call must stay under PATH_MAX
 * (1,024 bytes).
 */
#define REP_AT_DIRNAME_MAX 4096

/*
 * The absolute name of dirfd's directory (the working directory for
 * AT_FDCWD), into buf of REP_AT_DIRNAME_MAX bytes. getcwd() reports a longer
 * name as ERANGE, which smbd would pass to the client as an integer
 * overflow; say ENAMETOOLONG, as the kernel does for a name it cannot take.
 */
static int rep_at_dirname(int dirfd, char *buf)
{
	char *name;
	int saved;

	if (rep_at_enter(dirfd, ".", &saved) == -1) {
		return -1;
	}
	name = getcwd(buf, REP_AT_DIRNAME_MAX);
	rep_at_leave(saved);
	if (name == NULL) {
		if (errno == ERANGE) {
			errno = ENAMETOOLONG;
		}
		return -1;
	}
	return 0;
}

/*
 * Write into buf (PATH_MAX bytes) the name that reaches path in directory
 * "to" from directory "from", both absolute names from getcwd(): "../" for
 * each component of "from" below the two directories' common ancestor, then
 * the rest of "to", then path. Returns its length, or -1 if it does not fit
 * under PATH_MAX, which the kernel would refuse.
 */
static int rep_at_relative(const char *from, const char *to,
			   const char *path, char *buf)
{
	size_t common = 0;
	size_t len = 0;
	size_t i;
	const char *rest;
	int n;

	/* The shared leading components; "/a/b" and "/a/bc" share only "/a". */
	for (i = 0; from[i] != '\0' && from[i] == to[i]; i++) {
		if (from[i] == '/') {
			common = i;
		}
	}
	if ((from[i] == '\0' || from[i] == '/') &&
	    (to[i] == '\0' || to[i] == '/')) {
		common = i;
	}
	/* Up from "from": one "../" per component left in its name. */
	for (i = common; from[i] != '\0'; i++) {
		if (from[i] == '/' && from[i + 1] != '\0') {
			if (len + 3 >= PATH_MAX) {
				return -1;
			}
			memcpy(buf + len, "../", 3);
			len += 3;
		}
	}
	/* Down into "to", then the name itself. */
	rest = to + common;
	if (rest[0] == '/') {
		rest++;
	}
	n = snprintf(buf + len, PATH_MAX - len, "%s%s%s",
		     rest, rest[0] != '\0' ? "/" : "", path);
	if (n < 0 || len + n >= PATH_MAX) {
		return -1;
	}
	return (int)(len + n);
}

/* Whether two directory descriptors (or AT_FDCWD) are the same directory. */
static bool rep_at_same_directory(int fd1, int fd2)
{
	struct stat st1, st2;

	if (fd1 == fd2) {
		return true;
	}
	if (fd1 == AT_FDCWD || fd2 == AT_FDCWD) {
		return false;
	}
	if (fstat(fd1, &st1) == -1 || fstat(fd2, &st2) == -1) {
		return false;
	}
	return st1.st_dev == st2.st_dev && st1.st_ino == st2.st_ino;
}

/*
 * rename() and link() take two names but there is only one working
 * directory. When one name is absolute, work from the other's directory.
 * When both resolve from the same directory, the usual rename within a
 * folder, work from there. Otherwise work from one of the two directories
 * and give the other name relative to it, "../" up to the directories'
 * common ancestor and down from there, from whichever directory makes that
 * name shorter: moving a file up out of a deep folder is then "../../name"
 * however deep the folder is. An absolute name would fail past PATH_MAX,
 * where a real renameat() does not.
 *
 * Limitations, where a real renameat() or linkat() does better:
 * - It needs both directories' names from getcwd(), so it refuses with
 *   ENAMETOOLONG when a directory's name is longer than getcwd() returns
 *   (REP_AT_DIRNAME_MAX) or when the relative name still reaches PATH_MAX,
 *   though the real call would succeed. The refusal is clean: nothing moves.
 * - The kernel resolves the relative name again at the call. A directory on
 *   that path that is renamed, or replaced by a symlink, after the names are
 *   read redirects the call (an absolute name had the same window, over
 *   every directory from the root). The real call resolves nothing by name.
 * - getcwd() needs read access to every directory above the two, which the
 *   real call does not; smbd works as root on the appliance.
 */
static int rep_at_two_names(int olddirfd, const char *oldpath,
			    int newdirfd, const char *newpath,
			    int (*fn)(const char *, const char *))
{
	const char *old = oldpath;
	const char *new = newpath;
	char *names = NULL;
	char *olddir, *newdir, *from_new, *from_old;
	int len_new, len_old;
	int base;
	int saved;
	int ret;
	int err;

	if (oldpath == NULL || newpath == NULL) {
		errno = EFAULT;
		return -1;
	}
	if (oldpath[0] == '/') {
		base = newpath[0] == '/' ? AT_FDCWD : newdirfd;
	} else if (newpath[0] == '/') {
		base = olddirfd;
	} else if (rep_at_same_directory(olddirfd, newdirfd)) {
		base = newdirfd;
	} else {
		names = malloc(2 * REP_AT_DIRNAME_MAX + 2 * PATH_MAX);
		if (names == NULL) {
			errno = ENOMEM;
			return -1;
		}
		olddir = names;
		newdir = olddir + REP_AT_DIRNAME_MAX;
		from_new = newdir + REP_AT_DIRNAME_MAX;
		from_old = from_new + PATH_MAX;
		if (rep_at_dirname(olddirfd, olddir) == -1 ||
		    rep_at_dirname(newdirfd, newdir) == -1) {
			goto fail;
		}
		/* The old name from the new directory, or the reverse. */
		len_new = rep_at_relative(newdir, olddir, oldpath, from_new);
		len_old = rep_at_relative(olddir, newdir, newpath, from_old);
		if (len_new == -1 && len_old == -1) {
			errno = ENAMETOOLONG;
			goto fail;
		}
		if (len_new != -1 && (len_old == -1 || len_new <= len_old)) {
			base = newdirfd;
			old = from_new;
		} else {
			base = olddirfd;
			new = from_old;
		}
	}
	if (rep_at_enter(base, ".", &saved) == -1) {
		goto fail;
	}
	ret = fn(old, new);
	rep_at_leave(saved);
	err = errno;
	free(names);
	errno = err;
	return ret;

fail:
	err = errno;
	free(names);
	errno = err;
	return -1;
}

int rep_renameat(int olddirfd, const char *oldpath,
		 int newdirfd, const char *newpath)
{
	return rep_at_two_names(olddirfd, oldpath, newdirfd, newpath, rename);
}

int rep_linkat(int olddirfd, const char *oldpath,
	       int newdirfd, const char *newpath, int flags)
{
	/* Samba passes no flags; AT_SYMLINK_FOLLOW has no path equivalent. */
	if (flags != 0) {
		errno = EINVAL;
		return -1;
	}
	return rep_at_two_names(olddirfd, oldpath, newdirfd, newpath, link);
}

#if defined(__NetBSD__)
#define REP_AT_TIMESPEC(st, i) ((i) == 0 ? (st)->st_atimespec : (st)->st_mtimespec)
#else
#define REP_AT_TIMESPEC(st, i) ((i) == 0 ? (st)->st_atim : (st)->st_mtim)
#endif

/*
 * utimes() takes microsecond timevals and has no UTIME_NOW or UTIME_OMIT.
 * Convert, taking the current time for UTIME_NOW and the file's own time
 * from *st for UTIME_OMIT (*st is read only when rep_at_times_omit() says so).
 */
static bool rep_at_times_omit(const struct timespec times[2])
{
	return times != NULL &&
	       (times[0].tv_nsec == UTIME_OMIT ||
		times[1].tv_nsec == UTIME_OMIT);
}

/*
 * HFS keeps a time as unsigned 32-bit seconds from 1904-01-01, and both
 * kernels convert without a range check: they store the time plus
 * 2082844800 modulo 2^32, so a time past 2040-02-06 06:28:15 or before 1904
 * lands decades away once the kernel drops its cached copy (2040-02-06
 * 06:28:16 becomes 1970, 1903 becomes 2039; probed on both devices
 * 2026-09-29). Clamp to the nearest time HFS keeps instead. NetBSD 4's
 * time_t is 32 bits: its last is 2038-01-19 03:14:06, one second before
 * INT32_MAX, which Samba reads back as "never" (lib/util/time.h).
 * Apple's own afpserver keeps only
 * 1970 to 2038-01-19 03:14:07 and stores 0 for the rest; keeping every
 * time HFS can hold is deliberately better than that. Times before 1970
 * are stored, but both kernels read them back as 1970 once uncached.
 */
static void rep_at_hfs_range(struct timeval *tv)
{
	const int64_t first = -2082844800LL;	/* 1904-01-01 00:00:00 */
	/* 2040-02-06 06:28:15, or 2038-01-19 03:14:06 with a 32-bit time_t */
	const int64_t last = sizeof(time_t) > 4 ? 2212122495LL : INT32_MAX - 1;
	int64_t sec = tv->tv_sec;

	if (sec < first || sec > last) {
		tv->tv_sec = (time_t)(sec < first ? first : last);
		tv->tv_usec = 0;
	}
}

static void rep_at_timevals(const struct timespec times[2],
			    const struct stat *st, struct timeval tv[2])
{
	struct timeval now;
	struct timespec ts;
	int i;

	gettimeofday(&now, NULL);
	for (i = 0; i < 2; i++) {
		if (times[i].tv_nsec == UTIME_NOW) {
			tv[i] = now;
			continue;
		}
		ts = times[i].tv_nsec == UTIME_OMIT ? REP_AT_TIMESPEC(st, i)
						   : times[i];
		tv[i].tv_sec = ts.tv_sec;
		tv[i].tv_usec = ts.tv_nsec / 1000;
		rep_at_hfs_range(&tv[i]);
	}
	/* Both kernels take -1 in both times as "leave the times alone"
	 * (VNOVAL) and change nothing; one -1 beside another time is stored.
	 * Set 1969-12-31 23:59:58 instead, the nearest time that is kept. */
	if (tv[0].tv_sec == -1 && tv[1].tv_sec == -1) {
		tv[0].tv_sec = tv[1].tv_sec = -2;
		tv[0].tv_usec = tv[1].tv_usec = 0;
	}
}

int rep_utimensat(int dirfd, const char *path,
		  const struct timespec times[2], int flags)
{
	struct timeval tv[2];
	struct timeval *tvp = NULL;
	struct stat st;
	bool nofollow = (flags & AT_SYMLINK_NOFOLLOW) != 0;
	int saved;
	int ret = -1;

	if ((flags & ~AT_SYMLINK_NOFOLLOW) != 0) {
		errno = EINVAL;
		return -1;
	}
	if (times != NULL &&
	    times[0].tv_nsec == UTIME_OMIT && times[1].tv_nsec == UTIME_OMIT) {
		/* Nothing to change, but the name must still exist. */
		return rep_fstatat(dirfd, path, &st, flags);
	}
	if (rep_at_enter(dirfd, path, &saved) == -1) {
		return -1;
	}
	if (rep_at_times_omit(times) &&
	    (nofollow ? lstat(path, &st) : stat(path, &st)) == -1) {
		goto done;
	}
	if (times != NULL) {
		rep_at_timevals(times, &st, tv);
		tvp = tv;
	}
	ret = nofollow ? lutimes(path, tvp) : utimes(path, tvp);
done:
	rep_at_leave(saved);
	return ret;
}

/*
 * Both kernels have futimes() (microseconds). Samba's configure checks lutimes
 * but never futimes, so HAVE_FUTIMES is never defined: do not gate on it, or
 * every handle-based time update fails (SET_INFO on an open data handle,
 * tdb's commit-time mtime).
 */
int rep_futimens(int fd, const struct timespec times[2])
{
	struct timeval tv[2];
	struct timeval *tvp = NULL;
	struct stat st;

	if (times != NULL &&
	    times[0].tv_nsec == UTIME_OMIT && times[1].tv_nsec == UTIME_OMIT) {
		return fstat(fd, &st);
	}
	if (rep_at_times_omit(times) && fstat(fd, &st) == -1) {
		return -1;
	}
	if (times != NULL) {
		rep_at_timevals(times, &st, tv);
		tvp = tv;
	}
	return futimes(fd, tvp);
}

#ifdef TC_SAMBA4X_NETBSD4_COMPAT
/*
 * NetBSD 4's HFS resumes a listing after the name of the entry it returned
 * last only while it still holds a "directory hint" for that listing, and it
 * drops every hint of a directory whenever any descriptor on the directory is
 * closed: hfs_reldirhints() means to release hints older than 45 seconds,
 * but it subtracts the hint's wall-clock time from microuptime() and compares
 * the result as an unsigned 32-bit number, so every hint looks stale
 * (disassembled from the running kernel, 2026-09-28; NetBSD 6 makes the same
 * subtraction in signed 64 bits and keeps the hints). Without a hint the next
 * getdents() resumes by counting entries from the start, which skips entries
 * after deletions before the listing's position and repeats them after
 * creations there. smbd closes descriptors on a directory for every create
 * and delete in it, so a client that deletes a folder page by page left
 * files behind.
 *
 * So a stream from rep_fdopendir() reads the whole directory up front and
 * serves readdir() from memory, as libc already does for NFS and union
 * mounts (__DTF_READALL); rep_rewinddir() reads it again. HFS returns at most
 * 64 KiB per getdents() call, so a large directory takes several calls back
 * to back; if the directory's size or times changed meanwhile, the read is
 * made again. Entries deleted after the read are still listed (Samba skips a
 * name it can no longer stat); entries created after it appear after the next
 * rewind, as SMB allows.
 */
#define REP_DIR_READ_CHUNK 65536
#define REP_DIR_READ_TRIES 4

static bool rep_dir_same(const struct stat *a, const struct stat *b)
{
	return a->st_size == b->st_size &&
	       a->st_mtimespec.tv_sec == b->st_mtimespec.tv_sec &&
	       a->st_mtimespec.tv_nsec == b->st_mtimespec.tv_nsec &&
	       a->st_ctimespec.tv_sec == b->st_ctimespec.tv_sec &&
	       a->st_ctimespec.tv_nsec == b->st_ctimespec.tv_nsec;
}

/* Replace the stream's buffer with every entry of its directory. */
static int rep_dir_read_all(DIR *dir)
{
	struct stat before, after;
	char *buf = NULL, *grown;
	size_t cap = 0, used = 0;
	int tries, n, err;

	for (tries = 0; tries < REP_DIR_READ_TRIES; tries++) {
		if (fstat(dir->dd_fd, &before) == -1 ||
		    lseek(dir->dd_fd, (off_t)0, SEEK_SET) == -1) {
			goto fail;
		}
		used = 0;
		for (;;) {
			if (cap - used < REP_DIR_READ_CHUNK) {
				size_t want = cap == 0 ? 2 * REP_DIR_READ_CHUNK : cap * 2;
				if (want < cap) {
					errno = ENOMEM;
					goto fail;
				}
				grown = realloc(buf, want);
				if (grown == NULL) {
					errno = ENOMEM;
					goto fail;
				}
				buf = grown;
				cap = want;
			}
			n = getdents(dir->dd_fd, buf + used, cap - used);
			if (n == -1) {
				goto fail;
			}
			if (n == 0) {
				break;
			}
			used += (size_t)n;
		}
		if (fstat(dir->dd_fd, &after) == -1) {
			goto fail;
		}
		/* Unchanged while it was read: nothing was skipped or repeated. */
		if (rep_dir_same(&before, &after)) {
			break;
		}
	}
	/* Still changing after every try: keep the last read, as a listing
	 * made while the directory changes may. Keep only what was read: a
	 * listing handle holds this until it is closed. */
	grown = realloc(buf, used > 0 ? used : 1);
	if (grown != NULL) {
		buf = grown;
		cap = used > 0 ? used : 1;
	}
	free(dir->dd_buf);
	dir->dd_buf = buf;
	dir->dd_len = (int)cap;
	dir->dd_size = (long)used;
	dir->dd_loc = 0;
	dir->dd_seek = lseek(dir->dd_fd, (off_t)0, SEEK_CUR);
	dir->dd_flags |= __DTF_READALL;
	return 0;

fail:
	err = errno;
	free(buf);
	errno = err;
	return -1;
}

/*
 * NetBSD 4 libc has no fdopendir(). Open the descriptor's directory as "."
 * from inside it, which is the same directory whatever its name is now, read
 * it (above), and move the new stream onto fd's number: fdopendir() hands fd
 * to the stream, callers keep using fd (Samba opens entries relative to it),
 * and closedir() closes it. The stream then does not depend on the working
 * directory. The read happens before the move, so a failure closes only the
 * stream's own descriptor and leaves fd open, as fdopendir() must.
 */
DIR *rep_fdopendir(int fd)
{
	struct stat st;
	DIR *dir = NULL;
	int saved;
	int dfd;
	int err;

	if (fstat(fd, &st) == -1) {
		return NULL;
	}
	if (!S_ISDIR(st.st_mode)) {
		errno = ENOTDIR;
		return NULL;
	}
	if (rep_at_enter(fd, ".", &saved) == -1) {
		return NULL;
	}
	dir = opendir(".");
	rep_at_leave(saved);
	if (dir == NULL) {
		return NULL;
	}
	if (rep_dir_read_all(dir) == -1) {
		err = errno;
		closedir(dir);
		errno = err;
		return NULL;
	}
	dfd = dirfd(dir);
	if (dfd != fd) {
		if (dup2(dfd, fd) == -1) {
			err = errno;
			closedir(dir);
			errno = err;
			return NULL;
		}
		close(dfd);
		dir->dd_fd = fd;
		/* dup2() clears close-on-exec; opendir() had set it. */
		(void)fcntl(fd, F_SETFD, FD_CLOEXEC);
	}
	return dir;
}

/*
 * libc's rewinddir() only moves back to the start of what was read, which for
 * a stream read in full would repeat the old contents. Read it again; a
 * stream libc reads in pieces rewinds as usual, and so does one whose read
 * fails (it keeps its last contents).
 */
void rep_rewinddir(DIR *dir)
{
	if ((dir->dd_flags & __DTF_READALL) != 0 && rep_dir_read_all(dir) == 0) {
		return;
	}
	(rewinddir)(dir);
}
#elif defined(__NetBSD__)
/*
 * NetBSD 6's HFS (Apple's port, like NetBSD 4's) also resumes a listing by
 * name only while it holds the listing's directory hint, but its
 * hfs_reldirhints() compares the hint's age in signed 64 bits, finds it
 * negative (wall clock against microuptime()) and so never drops a hint as
 * stale: closing other descriptors on the directory keeps the listing's
 * place, and libc's fdopendir() and readdir() are used as they are. This
 * relies on that quirk of the frozen firmware; a kernel that expired hints
 * after 45 seconds would lose a listing's place on any close, as NetBSD 4
 * does (above).
 *
 * What NetBSD 6 still gets wrong is the end: the getdents() that finds no
 * more entries releases the hint on purpose (as Apple's source does), and
 * the stream's offset still counts entries, so the next getdents() resumes
 * by counting and returns the last entries again when files were created
 * before the listing's position (probed on both kernels, 2026-09-28).
 * libc's readdir() calls getdents() again after the end, and smbd reads
 * again for the client's next QUERY_DIRECTORY. So once readdir() has
 * reported the end, rep_readdir() keeps reporting it until rep_rewinddir().
 * A later file then shows up after a rewind, as SMB allows.
 *
 * The mark is a flag bit libc does not use (its bits end at 0x10). It lives
 * and dies with the stream, but libc's rewinddir() rebuilds the stream from
 * the current flags, so rep_rewinddir() clears it first. seekdir() is not
 * covered; nothing in smbd or our tools calls it. A NULL from readdir()
 * without errno is the end; libc also returns that for a corrupt entry,
 * which then ends the listing as well.
 */
#define REP_DIR_AT_END 0x40000000

struct dirent *rep_readdir(DIR *dir)
{
	struct dirent *entry;
	int err = errno;

	if ((dir->dd_flags & REP_DIR_AT_END) != 0) {
		return NULL;
	}
	errno = 0;
	entry = (readdir)(dir);
	if (entry == NULL) {
		if (errno != 0) {
			/* A read error: not the end, and errno is libc's. */
			return NULL;
		}
		dir->dd_flags |= REP_DIR_AT_END;
	}
	/* readdir() leaves errno alone on success and at the end. */
	errno = err;
	return entry;
}

void rep_rewinddir(DIR *dir)
{
	dir->dd_flags &= ~REP_DIR_AT_END;
	(rewinddir)(dir);
}
#endif /* TC_SAMBA4X_NETBSD4_COMPAT */
