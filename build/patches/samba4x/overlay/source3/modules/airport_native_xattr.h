/* Apple AirPort firmware adds Darwin-style xattr syscalls to its NetBSD 4
 * and NetBSD 6 kernels without exporting matching libc entry points. Keep
 * these private ABI calls behind an appliance-only build define.
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 3 of the License, or
 * (at your option) any later version.
 */
#ifndef _AIRPORT_NATIVE_XATTR_H
#define _AIRPORT_NATIVE_XATTR_H

#define TC_AIRPORT_SYS_FSETXATTR 377
#define TC_AIRPORT_SYS_FGETXATTR 380
#define TC_AIRPORT_SYS_FLISTXATTR 383
#define TC_AIRPORT_SYS_FREMOVEXATTR 386
/* Path variants that act on a symlink itself (NetBSD l*xattr numbering).
 * Apple's AFP server keeps provenance and Finder metadata on the link. */
#define TC_AIRPORT_SYS_LSETXATTR 376
#define TC_AIRPORT_SYS_LGETXATTR 379
#define TC_AIRPORT_SYS_LLISTXATTR 382
#define TC_AIRPORT_SYS_LREMOVEXATTR 385

#ifndef TC_AIRPORT_XATTR_SYSCALL
#define TC_AIRPORT_XATTR_SYSCALL syscall
#endif
#ifndef TC_AIRPORT_XATTR_FLOCK
#define TC_AIRPORT_XATTR_FLOCK flock
#endif

static inline int tc_airport_xattr_flock(int fd, int operation)
{
	int ret;

	do {
		ret = TC_AIRPORT_XATTR_FLOCK(fd, operation);
	} while (ret != 0 && errno == EINTR);
	return ret;
}

static inline ssize_t tc_airport_fgetxattr(int fd,
					   const char *name,
					   void *value,
					   size_t size)
{
#ifdef TC_AIRPORT_NATIVE_XATTR_SYSCALLS
	return (ssize_t)TC_AIRPORT_XATTR_SYSCALL(
		TC_AIRPORT_SYS_FGETXATTR, fd, name, value, size);
#else
	errno = ENOSYS;
	return -1;
#endif
}

static inline int tc_airport_fsetxattr(int fd,
				       const char *name,
				       const void *value,
				       size_t size,
				       int flags)
{
#ifdef TC_AIRPORT_NATIVE_XATTR_SYSCALLS
	ssize_t existing;
	long ret;
	int result = -1;
	int operation_error = 0;

	if ((flags & ~(XATTR_CREATE | XATTR_REPLACE)) != 0 ||
	    (flags & (XATTR_CREATE | XATTR_REPLACE)) ==
		(XATTR_CREATE | XATTR_REPLACE))
	{
		errno = EINVAL;
		return -1;
	}
	if (tc_airport_xattr_flock(fd, LOCK_EX) != 0) {
		return -1;
	}
	if (flags != 0) {
		existing = tc_airport_fgetxattr(fd, name, NULL, 0);
		if (existing >= 0 && (flags & XATTR_CREATE)) {
			errno = EEXIST;
			goto out;
		}
		if (existing < 0) {
			if (errno != ENOATTR && errno != ENODATA && errno != ENOENT) {
				goto out;
			}
			if (flags & XATTR_REPLACE) {
				errno = ENOATTR;
				goto out;
			}
		}
	}
	ret = TC_AIRPORT_XATTR_SYSCALL(
		TC_AIRPORT_SYS_FSETXATTR, fd, name, value, size, 0);
	result = ret < 0 ? -1 : 0;

	/* NetBSD 4 returns bytes written and ignores flags, while NetBSD 6
	 * returns zero and rejects XATTR_CREATE for a missing HFS attribute.
	 * Serialize every native mutation so the existence check is atomic
	 * relative to other smbd children using this wrapper. */
out:
	operation_error = errno;
	if (tc_airport_xattr_flock(fd, LOCK_UN) != 0 && result == 0) {
		return -1;
	}
	if (result != 0) {
		errno = operation_error;
	}
	return result;
#else
	errno = ENOSYS;
	return -1;
#endif
}

static inline bool tc_airport_xattr_list_contains(const char *list,
						   size_t list_size,
						   const char *name,
						   size_t name_size)
{
	size_t offset = 0;

	while (offset < list_size) {
		size_t entry_size = strnlen(list + offset, list_size - offset) + 1;

		if (entry_size > list_size - offset) {
			return false;
		}
		if (entry_size == name_size &&
		    memcmp(list + offset, name, name_size) == 0)
		{
			return true;
		}
		offset += entry_size;
	}
	return false;
}

static inline ssize_t tc_airport_flistxattr(int fd, char *list, size_t size)
{
#ifdef TC_AIRPORT_NATIVE_XATTR_SYSCALLS
	char *raw = NULL;
	ssize_t raw_size;
	ssize_t ret;
	size_t in_offset = 0;
	size_t out_size = 0;

	raw_size = (ssize_t)TC_AIRPORT_XATTR_SYSCALL(
		TC_AIRPORT_SYS_FLISTXATTR, fd, NULL, 0);
	if (raw_size <= 0) {
		return raw_size;
	}
	raw = malloc(raw_size);
	if (raw == NULL) {
		errno = ENOMEM;
		return -1;
	}
	ret = (ssize_t)TC_AIRPORT_XATTR_SYSCALL(
		TC_AIRPORT_SYS_FLISTXATTR, fd, raw, raw_size);
	if (ret < 0) {
		int error = errno;

		free(raw);
		errno = error;
		return -1;
	}
	while (in_offset < (size_t)ret) {
		size_t entry_size = strnlen(
			raw + in_offset, (size_t)ret - in_offset) + 1;

		if (entry_size > (size_t)ret - in_offset) {
			free(raw);
			errno = EIO;
			return -1;
		}
		if (!tc_airport_xattr_list_contains(
				raw, out_size, raw + in_offset, entry_size))
		{
			memmove(raw + out_size, raw + in_offset, entry_size);
			out_size += entry_size;
		}
		in_offset += entry_size;
	}
	if (list == NULL || size == 0) {
		free(raw);
		return out_size;
	}
	if (size < out_size) {
		free(raw);
		errno = ERANGE;
		return -1;
	}
	memcpy(list, raw, out_size);
	free(raw);
	return out_size;
#else
	errno = ENOSYS;
	return -1;
#endif
}

static inline int tc_airport_fremovexattr(int fd, const char *name)
{
#ifdef TC_AIRPORT_NATIVE_XATTR_SYSCALLS
	long ret;
	int operation_error;

	if (tc_airport_xattr_flock(fd, LOCK_EX) != 0) {
		return -1;
	}
	ret = TC_AIRPORT_XATTR_SYSCALL(
		TC_AIRPORT_SYS_FREMOVEXATTR, fd, name);
	operation_error = errno;
	if (tc_airport_xattr_flock(fd, LOCK_UN) != 0 && ret >= 0) {
		return -1;
	}
	if (ret < 0) {
		errno = operation_error;
	}

	return ret < 0 ? -1 : 0;
#else
	errno = ENOSYS;
	return -1;
#endif
}

/* A symlink has no descriptor: these operate on the link by path and never
 * follow it. No flock() is possible, so XATTR_CREATE/REPLACE are checked
 * without the cross-process serialization the fd variant provides. */
static inline ssize_t tc_airport_lgetxattr(const char *path,
					   const char *name,
					   void *value,
					   size_t size)
{
#ifdef TC_AIRPORT_NATIVE_XATTR_SYSCALLS
	return (ssize_t)TC_AIRPORT_XATTR_SYSCALL(
		TC_AIRPORT_SYS_LGETXATTR, path, name, value, size);
#else
	errno = ENOSYS;
	return -1;
#endif
}

static inline int tc_airport_lsetxattr(const char *path,
				       const char *name,
				       const void *value,
				       size_t size,
				       int flags)
{
#ifdef TC_AIRPORT_NATIVE_XATTR_SYSCALLS
	ssize_t existing;
	long ret;

	if ((flags & ~(XATTR_CREATE | XATTR_REPLACE)) != 0 ||
	    (flags & (XATTR_CREATE | XATTR_REPLACE)) ==
		(XATTR_CREATE | XATTR_REPLACE))
	{
		errno = EINVAL;
		return -1;
	}
	if (flags != 0) {
		existing = tc_airport_lgetxattr(path, name, NULL, 0);
		if (existing >= 0 && (flags & XATTR_CREATE)) {
			errno = EEXIST;
			return -1;
		}
		if (existing < 0) {
			if (errno != ENOATTR && errno != ENODATA && errno != ENOENT) {
				return -1;
			}
			if (flags & XATTR_REPLACE) {
				errno = ENOATTR;
				return -1;
			}
		}
	}
	/* NetBSD 4 returns bytes written, NetBSD 6 returns zero. */
	ret = TC_AIRPORT_XATTR_SYSCALL(
		TC_AIRPORT_SYS_LSETXATTR, path, name, value, size, 0);
	return ret < 0 ? -1 : 0;
#else
	errno = ENOSYS;
	return -1;
#endif
}

static inline ssize_t tc_airport_llistxattr(const char *path,
					    char *list,
					    size_t size)
{
#ifdef TC_AIRPORT_NATIVE_XATTR_SYSCALLS
	char *raw = NULL;
	ssize_t raw_size;
	ssize_t ret;
	size_t in_offset = 0;
	size_t out_size = 0;

	raw_size = (ssize_t)TC_AIRPORT_XATTR_SYSCALL(
		TC_AIRPORT_SYS_LLISTXATTR, path, NULL, 0);
	if (raw_size <= 0) {
		return raw_size;
	}
	raw = malloc(raw_size);
	if (raw == NULL) {
		errno = ENOMEM;
		return -1;
	}
	ret = (ssize_t)TC_AIRPORT_XATTR_SYSCALL(
		TC_AIRPORT_SYS_LLISTXATTR, path, raw, raw_size);
	if (ret < 0) {
		int error = errno;

		free(raw);
		errno = error;
		return -1;
	}
	/* NetBSD 6 can list an HFS attribute twice; see tc_airport_flistxattr. */
	while (in_offset < (size_t)ret) {
		size_t entry_size = strnlen(
			raw + in_offset, (size_t)ret - in_offset) + 1;

		if (entry_size > (size_t)ret - in_offset) {
			free(raw);
			errno = EIO;
			return -1;
		}
		if (!tc_airport_xattr_list_contains(
				raw, out_size, raw + in_offset, entry_size))
		{
			memmove(raw + out_size, raw + in_offset, entry_size);
			out_size += entry_size;
		}
		in_offset += entry_size;
	}
	if (list == NULL || size == 0) {
		free(raw);
		return out_size;
	}
	if (size < out_size) {
		free(raw);
		errno = ERANGE;
		return -1;
	}
	memcpy(list, raw, out_size);
	free(raw);
	return out_size;
#else
	errno = ENOSYS;
	return -1;
#endif
}

static inline int tc_airport_lremovexattr(const char *path, const char *name)
{
#ifdef TC_AIRPORT_NATIVE_XATTR_SYSCALLS
	return TC_AIRPORT_XATTR_SYSCALL(
		TC_AIRPORT_SYS_LREMOVEXATTR, path, name) < 0 ? -1 : 0;
#else
	errno = ENOSYS;
	return -1;
#endif
}

static inline bool tc_airport_path_is_hfs(const char *path)
{
#ifdef TC_AIRPORT_NATIVE_XATTR_SYSCALLS
#ifdef TC_AIRPORT_PATH_IS_HFS
	return TC_AIRPORT_PATH_IS_HFS(path);
#else
	struct statvfs statvfs_buf;

	if (statvfs(path, &statvfs_buf) != 0) {
		return false;
	}
	return strcmp(statvfs_buf.f_fstypename, "hfs") == 0;
#endif
#else
	return false;
#endif
}

#endif
