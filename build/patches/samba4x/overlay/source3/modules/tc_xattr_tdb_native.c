/*
 * Included by vfs_xattr_tdb.c (patch 0038) on a native HFS share. Mac xattrs
 * live in native HFS attributes (overlay airport_native_xattr.h): a
 * com.apple.* stream is its bare native attribute, and FinderInfo and
 * resource forks belong to fruit.
 */

#define TC_APPLE_STREAM_XATTR_PREFIX "user.DosStream."
#define TC_APPLE_STREAM_XATTR_SUFFIX ":$DATA"

static char *tc_apple_stream_native_name(TALLOC_CTX *mem_ctx,
					 const char *name)
{
	size_t prefix_len = strlen(TC_APPLE_STREAM_XATTR_PREFIX);
	size_t suffix_len = strlen(TC_APPLE_STREAM_XATTR_SUFFIX);
	size_t name_len = strlen(name);

	if (name_len <= prefix_len + suffix_len ||
	    strncmp(name, TC_APPLE_STREAM_XATTR_PREFIX "com.apple.",
		    strlen(TC_APPLE_STREAM_XATTR_PREFIX "com.apple.")) != 0 ||
	    strcmp(name + name_len - suffix_len,
		   TC_APPLE_STREAM_XATTR_SUFFIX) != 0)
	{
		return NULL;
	}
	return talloc_strndup(
		mem_ctx, name + prefix_len, name_len - prefix_len - suffix_len);
}

static bool tc_fruit_owned_native_xattr(const char *name)
{
	return strcmp(name, TC_FINDERINFO_XATTR) == 0 ||
		strcmp(name, TC_RESOURCEFORK_XATTR) == 0;
}

/*
 * A native symlink opened as itself (Time Capsule native links, patch 0059)
 * has no descriptor. Its attributes live on the link, as Apple's AFP server
 * stores them, and are reached by path with the no-follow syscalls. Any
 * other descriptor-less handle keeps failing with EBADF.
 *
 * The handle keeps only the link's name, and an AFP or SSH client can rename
 * the link between two SMB requests and put another object there. So check
 * that the name still holds the link this handle opened, by the file id taken
 * at open (vfs_stat_fsp() re-lstats the name and would follow a replacement),
 * and fail with ENOENT otherwise. This is the practical fix, not an atomic
 * one: NetBSD cannot open a symlink itself (no O_PATH or O_SYMLINK), so
 * nothing binds the syscall to the inode, and the name can still change in
 * the microseconds between the lstat() and the syscall. vfs_default's own
 * path-based pathref calls ("This is no longer a handle based call") have
 * the same gap with no check at all; it is tolerable.
 *
 * Returns 0 with *_path NULL for a handle that has a descriptor.
 */
static int tc_native_link_path(files_struct *fsp, char **_path)
{
	struct stat st;
	char *path = NULL;

	*_path = NULL;
	if (fsp_get_pathref_fd(fsp) != -1 || fsp->fsp_name == NULL ||
	    !S_ISLNK(fsp->fsp_name->st.st_ex_mode))
	{
		return 0;
	}
	path = talloc_asprintf(talloc_tos(), "%s/%s",
			       fsp->conn->connectpath,
			       fsp->fsp_name->base_name);
	if (path == NULL) {
		errno = ENOMEM;
		return -1;
	}
	if (lstat(path, &st) != 0 || !S_ISLNK(st.st_mode) ||
	    (uint64_t)st.st_dev != fsp->file_id.devid ||
	    (uint64_t)st.st_ino != fsp->file_id.inode)
	{
		DBG_NOTICE("%s is no longer the link this handle opened\n",
			   path);
		TALLOC_FREE(path);
		errno = ENOENT;
		return -1;
	}
	*_path = path;
	return 0;
}

static ssize_t tc_native_fgetxattr(files_struct *fsp, const char *name,
				   void *value, size_t size)
{
	char *path = NULL;
	ssize_t ret;
	int err;

	if (tc_native_link_path(fsp, &path) != 0) {
		return -1;
	}
	if (path == NULL) {
		return tc_airport_fgetxattr(
			fsp_get_pathref_fd(fsp), name, value, size);
	}
	ret = tc_airport_lgetxattr(path, name, value, size);
	err = errno;
	TALLOC_FREE(path);
	errno = err;
	return ret;
}

static int tc_native_fsetxattr(files_struct *fsp, const char *name,
			       const void *value, size_t size, int flags)
{
	char *path = NULL;
	int ret, err;

	if (tc_native_link_path(fsp, &path) != 0) {
		return -1;
	}
	if (path == NULL) {
		return tc_airport_fsetxattr(
			fsp_get_pathref_fd(fsp), name, value, size, flags);
	}
	ret = tc_airport_lsetxattr(path, name, value, size, flags);
	err = errno;
	TALLOC_FREE(path);
	errno = err;
	return ret;
}

static ssize_t tc_native_flistxattr(files_struct *fsp, char *list, size_t size)
{
	char *path = NULL;
	ssize_t ret;
	int err;

	if (tc_native_link_path(fsp, &path) != 0) {
		return -1;
	}
	if (path == NULL) {
		return tc_airport_flistxattr(fsp_get_pathref_fd(fsp), list, size);
	}
	ret = tc_airport_llistxattr(path, list, size);
	err = errno;
	TALLOC_FREE(path);
	errno = err;
	return ret;
}

static int tc_native_fremovexattr(files_struct *fsp, const char *name)
{
	char *path = NULL;
	int ret, err;

	if (tc_native_link_path(fsp, &path) != 0) {
		return -1;
	}
	if (path == NULL) {
		return tc_airport_fremovexattr(fsp_get_pathref_fd(fsp), name);
	}
	ret = tc_airport_lremovexattr(path, name);
	err = errno;
	TALLOC_FREE(path);
	errno = err;
	return ret;
}

static ssize_t tc_native_apple_stream_get(files_struct *fsp,
					   const char *native_name,
					   void *value,
					   size_t size)
{
	ssize_t raw_size;
	ssize_t ret;

	raw_size = tc_native_fgetxattr(fsp, native_name, NULL, 0);
	if (raw_size < 0) {
		return -1;
	}
	if (raw_size >= SSIZE_MAX) {
		errno = EOVERFLOW;
		return -1;
	}
	if (size == 0) {
		return raw_size + 1;
	}
	if ((size_t)raw_size + 1 > size) {
		errno = ERANGE;
		return -1;
	}
	ret = tc_native_fgetxattr(fsp, native_name, value, raw_size);
	if (ret < 0) {
		return -1;
	}
	if (ret > raw_size) {
		errno = EIO;
		return -1;
	}
	((uint8_t *)value)[ret] = 0;
	return ret + 1;
}

static int tc_native_apple_stream_set(files_struct *fsp,
				      const char *native_name,
				      const void *value,
				      size_t size,
				      int flags)
{
	if (size == 0) {
		errno = EINVAL;
		return -1;
	}
	if (((const uint8_t *)value)[size - 1] != 0) {
		/* HFS must expose one complete canonical xattr to AFP. A
		 * streams_xattr extent count here would expose only its anchor. */
		errno = E2BIG;
		return -1;
	}
	return tc_native_fsetxattr(fsp, native_name, value, size - 1, flags);
}

/* Native attributes as xattr_tdb lists them: fruit's own two are left out,
 * and each com.apple.* attribute also appears as its stream name. */
static ssize_t tc_xattr_tdb_native_list(files_struct *fsp, char *list,
					size_t size)
{
	ssize_t raw_size;
	ssize_t ret;
	char *raw = NULL;
	size_t offset = 0;
	size_t required = 0;
	size_t written_size = 0;

	raw_size = tc_native_flistxattr(fsp, NULL, 0);
	if (raw_size <= 0) {
		return raw_size;
	}
	raw = talloc_array(talloc_tos(), char, raw_size);
	if (raw == NULL) {
		errno = ENOMEM;
		return -1;
	}
	ret = tc_native_flistxattr(fsp, raw, raw_size);
	if (ret < 0) {
		TALLOC_FREE(raw);
		return -1;
	}
	while (offset < (size_t)ret) {
		const char *entry = raw + offset;
		size_t entry_size = strlen(entry) + 1;

		if (tc_fruit_owned_native_xattr(entry)) {
			offset += entry_size;
			continue;
		}
		required += entry_size;
		if (strncmp(entry, "com.apple.", strlen("com.apple.")) == 0) {
			required += strlen(TC_APPLE_STREAM_XATTR_PREFIX) +
				entry_size - 1 +
				strlen(TC_APPLE_STREAM_XATTR_SUFFIX) + 1;
		}
		offset += entry_size;
	}
	if (list == NULL || size == 0) {
		TALLOC_FREE(raw);
		return required;
	}
	if (size < required) {
		TALLOC_FREE(raw);
		errno = ERANGE;
		return -1;
	}
	offset = 0;
	while (offset < (size_t)ret) {
		const char *entry = raw + offset;
		size_t entry_size = strlen(entry) + 1;

		if (tc_fruit_owned_native_xattr(entry)) {
			offset += entry_size;
			continue;
		}
		memcpy(list + written_size, entry, entry_size);
		written_size += entry_size;
		if (strncmp(entry, "com.apple.", strlen("com.apple.")) == 0) {
			int stream_written = snprintf(
				list + written_size,
				size - written_size,
				"%s%s%s",
				TC_APPLE_STREAM_XATTR_PREFIX,
				entry,
				TC_APPLE_STREAM_XATTR_SUFFIX);

			if (stream_written < 0) {
				TALLOC_FREE(raw);
				errno = EIO;
				return -1;
			}
			written_size += stream_written + 1;
		}
		offset += entry_size;
	}
	TALLOC_FREE(raw);
	return written_size;
}

/*
 * Native get, set and remove for a name xattr_tdb receives: a com.apple.*
 * stream name maps to its native attribute, and neither form may reach
 * fruit's FinderInfo or resource fork.
 */
static ssize_t tc_xattr_tdb_native_get(files_struct *fsp, const char *name,
				       void *value, size_t size)
{
	char *native_name = tc_apple_stream_native_name(talloc_tos(), name);
	ssize_t ret;

	if (native_name != NULL) {
		if (tc_fruit_owned_native_xattr(native_name)) {
			TALLOC_FREE(native_name);
			errno = ENOATTR;
			return -1;
		}
		ret = tc_native_apple_stream_get(fsp, native_name, value, size);
		TALLOC_FREE(native_name);
		return ret;
	}
	if (strcmp(name, TC_RESOURCEFORK_XATTR) == 0) {
		errno = ENOTSUP;
		return -1;
	}
	return tc_native_fgetxattr(fsp, name, value, size);
}

static int tc_xattr_tdb_native_set(files_struct *fsp, const char *name,
				   const void *value, size_t size, int flags)
{
	char *native_name = tc_apple_stream_native_name(talloc_tos(), name);
	int ret;

	if (native_name != NULL) {
		if (tc_fruit_owned_native_xattr(native_name)) {
			TALLOC_FREE(native_name);
			errno = ENOTSUP;
			return -1;
		}
		ret = tc_native_apple_stream_set(
			fsp, native_name, value, size, flags);
		TALLOC_FREE(native_name);
		return ret;
	}
	if (strcmp(name, TC_RESOURCEFORK_XATTR) == 0) {
		errno = ENOTSUP;
		return -1;
	}
	return tc_native_fsetxattr(fsp, name, value, size, flags);
}

static int tc_xattr_tdb_native_remove(files_struct *fsp, const char *name)
{
	char *native_name = tc_apple_stream_native_name(talloc_tos(), name);
	int ret;

	if (native_name != NULL) {
		if (tc_fruit_owned_native_xattr(native_name)) {
			TALLOC_FREE(native_name);
			errno = ENOATTR;
			return -1;
		}
		ret = tc_native_fremovexattr(fsp, native_name);
		TALLOC_FREE(native_name);
		return ret;
	}
	if (strcmp(name, TC_RESOURCEFORK_XATTR) == 0) {
		errno = ENOTSUP;
		return -1;
	}
	return tc_native_fremovexattr(fsp, name);
}
