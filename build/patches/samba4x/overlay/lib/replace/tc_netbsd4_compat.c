/* Included by replace.c (patch 0002) for TC_SAMBA4X_NETBSD4_COMPAT. */

/*
 * NetBSD4 compatibility layer for symbols Samba can reference.
 *
 * NetBSD 6/7 do not compile this block; they use native libc support. For
 * NetBSD4, path-aware source3 VFS fallbacks handle normal SMB file I/O. These
 * libc-level shims are intentionally conservative: AT_FDCWD and absolute paths
 * can safely use older syscalls, but arbitrary relative dirfd operations do not
 * have enough information here to reconstruct a pathname safely.
 */
static int rep_at_path_is_direct(int dirfd, const char *path)
{
	return dirfd == AT_FDCWD || (path != NULL && path[0] == '/');
}

int openat(int dirfd, const char *path, int flags, ...)
{
	mode_t mode = 0;
	if ((flags & O_CREAT) != 0) {
		va_list ap;
		va_start(ap, flags);
		mode = va_arg(ap, mode_t);
		va_end(ap);
	}
	if (!rep_at_path_is_direct(dirfd, path)) {
		errno = ENOSYS;
		return -1;
	}
	return open(path, flags, mode);
}

int mkdirat(int dirfd, const char *path, mode_t mode)
{
	if (!rep_at_path_is_direct(dirfd, path)) {
		errno = ENOSYS;
		return -1;
	}
	return mkdir(path, mode);
}

int unlinkat(int dirfd, const char *path, int flags)
{
	if (!rep_at_path_is_direct(dirfd, path)) {
		errno = ENOSYS;
		return -1;
	}
	if ((flags & AT_REMOVEDIR) != 0) {
		return rmdir(path);
	}
	return unlink(path);
}

int symlinkat(const char *target, int newdirfd, const char *linkpath)
{
	if (!rep_at_path_is_direct(newdirfd, linkpath)) {
		errno = ENOSYS;
		return -1;
	}
	return symlink(target, linkpath);
}

ssize_t readlinkat(int dirfd, const char *path, char *buf, size_t bufsiz)
{
	if (!rep_at_path_is_direct(dirfd, path)) {
		errno = ENOSYS;
		return -1;
	}
	return readlink(path, buf, bufsiz);
}

int linkat(int olddirfd, const char *oldpath, int newdirfd, const char *newpath, int flags)
{
	if (flags != 0 ||
	    !rep_at_path_is_direct(olddirfd, oldpath) ||
	    !rep_at_path_is_direct(newdirfd, newpath)) {
		errno = ENOSYS;
		return -1;
	}
	return link(oldpath, newpath);
}

int fstatat(int dirfd, const char *path, struct stat *buf, int flags)
{
	if (!rep_at_path_is_direct(dirfd, path)) {
		errno = ENOSYS;
		return -1;
	}
	if ((flags & ~AT_SYMLINK_NOFOLLOW) != 0) {
		errno = ENOSYS;
		return -1;
	}
	if ((flags & AT_SYMLINK_NOFOLLOW) != 0) {
		return lstat(path, buf);
	}
	return stat(path, buf);
}

static void rep_timespecs_to_timevals(const struct timespec times[2], struct timeval tv[2])
{
	tv[0].tv_sec = times[0].tv_sec;
	tv[0].tv_usec = times[0].tv_nsec / 1000;
	tv[1].tv_sec = times[1].tv_sec;
	tv[1].tv_usec = times[1].tv_nsec / 1000;
}

int futimens(int fd, const struct timespec times[2])
{
#ifdef HAVE_FUTIMES
	struct timeval tv[2];
	struct timeval *tvp = NULL;
	if (times != NULL) {
		rep_timespecs_to_timevals(times, tv);
		tvp = tv;
	}
	return futimes(fd, tvp);
#else
	errno = ENOSYS;
	return -1;
#endif
}

int utimensat(int dirfd, const char *path, const struct timespec times[2], int flags)
{
	struct timeval tv[2];
	struct timeval *tvp = NULL;
	if (!rep_at_path_is_direct(dirfd, path)) {
		errno = ENOSYS;
		return -1;
	}
	if ((flags & ~AT_SYMLINK_NOFOLLOW) != 0) {
		errno = ENOSYS;
		return -1;
	}
	if (times != NULL) {
		rep_timespecs_to_timevals(times, tv);
		tvp = tv;
	}
	if ((flags & AT_SYMLINK_NOFOLLOW) != 0) {
#ifdef HAVE_LUTIMES
		return lutimes(path, tvp);
#else
		errno = ENOSYS;
		return -1;
#endif
	}
	return utimes(path, tvp);
}

void arc4random_buf(void *buf, size_t n)
{
	unsigned char *p = buf;
	size_t done = 0;
	int fd = open("/dev/urandom", O_RDONLY);
	if (fd != -1) {
		while (done < n) {
			ssize_t ret = read(fd, p + done, n - done);
			if (ret == -1 && errno == EINTR) {
				continue;
			}
			if (ret <= 0) {
				break;
			}
			done += ret;
		}
		close(fd);
		if (done == n) {
			return;
		}
	}

	/*
	 * Samba/GnuTLS may use this for security-sensitive randomness. A weak
	 * random() fallback would hide a serious platform failure, so fail hard.
	 */
	abort();
}

ssize_t getline(char **lineptr, size_t *n, FILE *stream)
{
	int c = 0;
	size_t pos = 0;
	char *new_line = NULL;
	size_t new_size = 0;

	if (lineptr == NULL || n == NULL || stream == NULL) {
		errno = EINVAL;
		return -1;
	}
	if (*lineptr == NULL || *n == 0) {
		*n = 128;
		*lineptr = malloc(*n);
		if (*lineptr == NULL) {
			return -1;
		}
	}

	while ((c = fgetc(stream)) != EOF) {
		if (pos + 1 >= *n) {
			new_size = *n * 2;
			if (new_size <= *n) {
				errno = ENOMEM;
				return -1;
			}
			new_line = realloc(*lineptr, new_size);
			if (new_line == NULL) {
				return -1;
			}
			*lineptr = new_line;
			*n = new_size;
		}
		(*lineptr)[pos++] = (char)c;
		if (c == '\n') {
			break;
		}
	}
	if (pos == 0 && c == EOF) {
		return -1;
	}
	(*lineptr)[pos] = '\0';
	return (ssize_t)pos;
}
