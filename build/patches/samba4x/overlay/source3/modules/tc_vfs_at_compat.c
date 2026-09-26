/* Included by vfs_default.c (patch 0003) for TC_SAMBA4X_VFS_AT_PATH_COMPAT. */

/*
 * Time Capsule runtime kernels are older than the NetBSD SDKs used to build
 * Samba. The NetBSD 6 appliance binary is built against NetBSD 7 headers, and
 * NetBSD 4 lacks the same *at syscall family, so source3 can see *at/openat2
 * support at compile time that is missing on-device.
 *
 * Do not emulate relative dirfd operations in lib/replace: a raw fd is not
 * enough context there. The default VFS layer still has Samba's live dirfsp,
 * so emulate the *at contract by temporarily fchdir()ing to that fd and
 * calling the older relative-path syscall. This stays scoped to the static
 * no-pthread appliance build; fchdir() is process-global and would be unsafe
 * in a normal threaded smbd.
 */
static int tc_netbsd_prepare_fd_cwd(int dirfd, int *saved_cwd)
{
	int fd;
	int saved_errno;

	*saved_cwd = -1;

	if (dirfd == AT_FDCWD) {
		return 0;
	}

	if (dirfd == -1) {
		errno = EBADF;
		return -1;
	}

	fd = open(".", O_RDONLY);
	if (fd == -1) {
		return -1;
	}

	if (fchdir(dirfd) == -1) {
		saved_errno = errno;
		close(fd);
		errno = saved_errno;
		return -1;
	}

	*saved_cwd = fd;
	return 0;
}

static int tc_netbsd_restore_cwd(int saved_cwd, int saved_errno)
{
	int restore_errno;

	if (saved_cwd == -1) {
		errno = saved_errno;
		return 0;
	}

	if (fchdir(saved_cwd) == -1) {
		restore_errno = errno;
		close(saved_cwd);
		errno = restore_errno;
		return -1;
	}

	if (close(saved_cwd) == -1) {
		restore_errno = errno;
		errno = restore_errno;
		return -1;
	}

	errno = saved_errno;
	return 0;
}

static int tc_netbsd_prepare_at_cwd(const struct files_struct *dirfsp,
				    const struct smb_filename *smb_fname,
				    const char **path,
				    int *saved_cwd)
{
	const char *base_name = NULL;

	if (smb_fname == NULL ||
	    smb_fname->base_name == NULL)
	{
		errno = EINVAL;
		return -1;
	}

	base_name = smb_fname->base_name;
	*path = base_name;
	*saved_cwd = -1;

	if (base_name[0] == '/') {
		return 0;
	}

	if (dirfsp == NULL) {
		errno = EINVAL;
		return -1;
	}

	return tc_netbsd_prepare_fd_cwd(fsp_get_pathref_fd(dirfsp), saved_cwd);
}

static char *tc_netbsd_getcwd_talloc(TALLOC_CTX *mem_ctx)
{
	size_t size = 256;

	while (size <= 65536) {
		char *cwd = talloc_array(mem_ctx, char, size);
		if (cwd == NULL) {
			errno = ENOMEM;
			return NULL;
		}
		if (getcwd(cwd, size) != NULL) {
			return cwd;
		}
		if (errno != ERANGE) {
			int saved_errno = errno;
			TALLOC_FREE(cwd);
			errno = saved_errno;
			return NULL;
		}
		TALLOC_FREE(cwd);
		size *= 2;
	}

	errno = ERANGE;
	return NULL;
}

static char *tc_netbsd_at_path_from_fd(TALLOC_CTX *mem_ctx,
				       const struct files_struct *dirfsp,
				       const struct smb_filename *smb_fname)
{
	const char *base_name = NULL;
	int saved_cwd = -1;
	int saved_errno = 0;
	char *cwd = NULL;
	char *path = NULL;

	if (smb_fname == NULL ||
	    smb_fname->base_name == NULL)
	{
		errno = EINVAL;
		return NULL;
	}

	base_name = smb_fname->base_name;
	if (base_name[0] == '/') {
		path = talloc_strdup(mem_ctx, base_name);
		if (path == NULL) {
			errno = ENOMEM;
		}
		return path;
	}

	if (dirfsp == NULL) {
		errno = EINVAL;
		return NULL;
	}

	if (tc_netbsd_prepare_fd_cwd(fsp_get_pathref_fd(dirfsp),
				     &saved_cwd) == -1)
	{
		return NULL;
	}

	cwd = tc_netbsd_getcwd_talloc(mem_ctx);
	saved_errno = errno;
	if (tc_netbsd_restore_cwd(saved_cwd, saved_errno) == -1) {
		return NULL;
	}
	if (cwd == NULL) {
		return NULL;
	}

	if (strcmp(cwd, "/") == 0) {
		path = talloc_asprintf(mem_ctx, "/%s", base_name);
	} else {
		path = talloc_asprintf(mem_ctx, "%s/%s", cwd, base_name);
	}
	if (path == NULL) {
		errno = ENOMEM;
	}
	return path;
}

static int tc_netbsd_normalize_open_flags(int flags,
					  bool *want_directory,
					  bool *want_cloexec)
{
	*want_directory = false;
	*want_cloexec = false;

#ifdef O_SEARCH
	/*
	 * NetBSD 7 headers define O_SEARCH but NetBSD 4 headers do not. Samba
	 * can use it for pathref traversal, while the Time Capsule runtime only
	 * has older open(). Always consume it here so callers see one behavior
	 * regardless of which SDK headers built this lane.
	 */
	flags &= ~O_SEARCH;
#endif
#ifdef O_DIRECTORY
	if ((flags & O_DIRECTORY) != 0) {
		*want_directory = true;
		flags &= ~O_DIRECTORY;
	}
#endif
#ifdef O_CLOEXEC
	if ((flags & O_CLOEXEC) != 0) {
		*want_cloexec = true;
		flags &= ~O_CLOEXEC;
	}
#endif
#ifdef O_PATH
	flags &= ~O_PATH;
#endif

	return flags;
}

static int tc_netbsd_set_cloexec(int fd)
{
#ifdef FD_CLOEXEC
	int fd_flags = fcntl(fd, F_GETFD);
	if (fd_flags == -1) {
		return -1;
	}
	return fcntl(fd, F_SETFD, fd_flags | FD_CLOEXEC);
#else
	errno = ENOSYS;
	return -1;
#endif
}

static int tc_netbsd_validate_opened_file(files_struct *fsp,
					  int fd,
					  bool want_directory,
					  bool want_cloexec)
{
	SMB_STRUCT_STAT sbuf;
	int ret;

	if (fd == -1) {
		return fd;
	}

	if (want_cloexec && tc_netbsd_set_cloexec(fd) == -1) {
		int err = errno;
		close(fd);
		errno = err;
		return -1;
	}

	if (!want_directory && !fsp->fsp_flags.is_directory) {
		return fd;
	}

	ret = sys_fstat(fd,
			&sbuf,
			lp_fake_directory_create_times(SNUM(fsp->conn)));
	if (ret != 0) {
		int err = errno;
		close(fd);
		errno = err;
		return -1;
	}
	if (!S_ISDIR(sbuf.st_ex_mode)) {
		close(fd);
		errno = ENOTDIR;
		return -1;
	}
	return fd;
}

static int tc_netbsd_openat_compat(const struct files_struct *dirfsp,
				   const struct smb_filename *smb_fname,
				   files_struct *fsp,
				   int flags,
				   mode_t mode)
{
	const char *path = NULL;
	int saved_cwd = -1;
	int saved_errno = 0;
	bool want_directory = false;
	bool want_cloexec = false;
	int result = -1;

	flags = tc_netbsd_normalize_open_flags(flags,
					       &want_directory,
					       &want_cloexec);

	if (tc_netbsd_prepare_at_cwd(dirfsp,
				     smb_fname,
				     &path,
				     &saved_cwd) == -1)
	{
		return -1;
	}

	result = open(path, flags, mode);
	saved_errno = errno;
#ifdef EFTYPE
	if (result == -1 && saved_errno == EFTYPE && (flags & O_NOFOLLOW)) {
		/*
		 * NetBSD reports O_NOFOLLOW on a symlink as EFTYPE; POSIX, and
		 * Samba's path walk, expect ELOOP. Without O_PATH Samba only spots
		 * a symlink component by that ELOOP (openat_pathref_fsp_nosymlink),
		 * so with EFTYPE a path through a link to a directory fails with
		 * OBJECT_PATH_NOT_FOUND instead of being followed.
		 */
		saved_errno = ELOOP;
	}
#endif
	if (tc_netbsd_restore_cwd(saved_cwd, saved_errno) == -1) {
		int restore_errno = errno;
		if (result != -1) {
			close(result);
		}
		errno = restore_errno;
		return -1;
	}

	return tc_netbsd_validate_opened_file(fsp,
					      result,
					      want_directory,
					      want_cloexec);
}

static int tc_netbsd_mkdirat_compat(const struct files_struct *dirfsp,
				    const struct smb_filename *smb_fname,
				    mode_t mode)
{
	const char *path = NULL;
	int saved_cwd = -1;
	int saved_errno = 0;
	int result = -1;

	if (tc_netbsd_prepare_at_cwd(dirfsp,
				     smb_fname,
				     &path,
				     &saved_cwd) == -1)
	{
		return -1;
	}

	result = mkdir(path, mode);
	saved_errno = errno;
	if (tc_netbsd_restore_cwd(saved_cwd, saved_errno) == -1) {
		return -1;
	}
	return result;
}

static int tc_netbsd_fstatat_compat(
	struct vfs_handle_struct *handle,
	const struct files_struct *dirfsp,
	const struct smb_filename *smb_fname,
	SMB_STRUCT_STAT *sbuf,
	int flags)
{
	const char *path = NULL;
	int saved_cwd = -1;
	int saved_errno = 0;
	int result = -1;

	if ((flags & ~AT_SYMLINK_NOFOLLOW) != 0) {
		errno = ENOSYS;
		return -1;
	}

	if (tc_netbsd_prepare_at_cwd(dirfsp,
				     smb_fname,
				     &path,
				     &saved_cwd) == -1)
	{
		return -1;
	}

	if ((flags & AT_SYMLINK_NOFOLLOW) != 0) {
		result = sys_lstat(
			path,
			sbuf,
			lp_fake_directory_create_times(SNUM(handle->conn)));
	} else {
		result = sys_stat(
			path,
			sbuf,
			lp_fake_directory_create_times(SNUM(handle->conn)));
	}
	saved_errno = errno;
	if (tc_netbsd_restore_cwd(saved_cwd, saved_errno) == -1) {
		return -1;
	}
	return result;
}

static int tc_netbsd_unlinkat_compat(const struct files_struct *dirfsp,
				     const struct smb_filename *smb_fname,
				     int flags)
{
	const char *path = NULL;
	int saved_cwd = -1;
	int saved_errno = 0;
	int result = -1;

	if ((flags & ~AT_REMOVEDIR) != 0) {
		errno = ENOSYS;
		return -1;
	}

	if (tc_netbsd_prepare_at_cwd(dirfsp,
				     smb_fname,
				     &path,
				     &saved_cwd) == -1)
	{
		return -1;
	}

	if ((flags & AT_REMOVEDIR) != 0) {
		result = rmdir(path);
	} else {
		result = unlink(path);
	}
	saved_errno = errno;
	if (tc_netbsd_restore_cwd(saved_cwd, saved_errno) == -1) {
		return -1;
	}
	return result;
}

static int tc_netbsd_symlinkat_compat(const char *target,
				      const struct files_struct *dirfsp,
				      const struct smb_filename *smb_fname)
{
	const char *path = NULL;
	int saved_cwd = -1;
	int saved_errno = 0;
	int result = -1;

	if (target == NULL) {
		errno = EINVAL;
		return -1;
	}

	if (tc_netbsd_prepare_at_cwd(dirfsp,
				     smb_fname,
				     &path,
				     &saved_cwd) == -1)
	{
		return -1;
	}

	result = symlink(target, path);
	saved_errno = errno;
	if (tc_netbsd_restore_cwd(saved_cwd, saved_errno) == -1) {
		return -1;
	}
	return result;
}

static ssize_t tc_netbsd_readlinkat_compat(
	const struct files_struct *dirfsp,
	const struct smb_filename *smb_fname,
	char *buf,
	size_t bufsiz)
{
	const char *path = NULL;
	int saved_cwd = -1;
	int saved_errno = 0;
	ssize_t result = -1;

	if (tc_netbsd_prepare_at_cwd(dirfsp,
				     smb_fname,
				     &path,
				     &saved_cwd) == -1)
	{
		return -1;
	}

	result = readlink(path, buf, bufsiz);
	saved_errno = errno;
	if (tc_netbsd_restore_cwd(saved_cwd, saved_errno) == -1) {
		return -1;
	}
	return result;
}

static int tc_netbsd_mknodat_compat(const struct files_struct *dirfsp,
				    const struct smb_filename *smb_fname,
				    mode_t mode,
				    SMB_DEV_T dev)
{
	const char *path = NULL;
	int saved_cwd = -1;
	int saved_errno = 0;
	int result = -1;

	if (tc_netbsd_prepare_at_cwd(dirfsp,
				     smb_fname,
				     &path,
				     &saved_cwd) == -1)
	{
		return -1;
	}

	result = sys_mknod(path, mode, dev);
	saved_errno = errno;
	if (tc_netbsd_restore_cwd(saved_cwd, saved_errno) == -1) {
		return -1;
	}
	return result;
}

#ifdef TC_SAMBA4X_NETBSD4_COMPAT
static DIR *tc_netbsd_fdopendir_compat(files_struct *fsp)
{
	DIR *result = NULL;
	int dir_fd = -1;
	int fd = fsp_get_io_fd(fsp);
	int saved_errno = 0;

	if (fd == -1 ||
	    fsp->fsp_name == NULL ||
	    fsp->fsp_name->base_name == NULL)
	{
		errno = EINVAL;
		return NULL;
	}

	result = opendir(fsp->fsp_name->base_name);
	if (result != NULL) {
		dir_fd = dirfd(result);
		if (dir_fd == -1) {
			saved_errno = errno;
			closedir(result);
			errno = saved_errno;
			return NULL;
		}
		if (dir_fd != fd && close(fd) == -1) {
			saved_errno = errno;
			closedir(result);
			errno = saved_errno;
			return NULL;
		}
		if (dir_fd != fd) {
			fsp_set_fd(fsp, -1);
			fsp_set_fd(fsp, dir_fd);
		}
	}

	return result;
}
#endif
