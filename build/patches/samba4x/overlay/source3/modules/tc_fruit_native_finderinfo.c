/*
 * Included by vfs_fruit.c (patch 0055) on native HFS shares: the
 * AFP_AfpInfo stream is backed by the native com.apple.FinderInfo attribute
 * that AFP serves (overlay airport_native_xattr.h), instead of a stream or a
 * netatalk metadata xattr.
 */

/* Defined later in vfs_fruit.c; fruit_open_meta_native() installs it. */
static void fio_destroy_fn(void *p_data);

static bool tc_fruit_backend_missing(int error)
{
	return error == ENOATTR || error == ENOENT;
}

/* Return 1 for a 32-byte native FinderInfo value, 0 when absent, -1 on
 * a real error. The lower native-HFS xattr backend owns the private ABI. */
static int tc_native_finderinfo_read_fsp(files_struct *fsp,
					 uint8_t finderinfo[AFP_FinderSize])
{
	ssize_t size;
	ssize_t ret;
	int fd = fsp_get_pathref_fd(fsp);

	/* Named AFP streams on symlinks are not a native-HFS fallback case. In
	 * particular, never read or mutate FinderInfo on the symlink target. */
	if (S_ISLNK(fsp->fsp_name->st.st_ex_mode)) {
		errno = ENOENT;
		return 0;
	}
	if (fd == -1) {
		errno = EBADF;
		return -1;
	}

	size = SMB_VFS_FGETXATTR(
		fsp, TC_FINDERINFO_XATTR, NULL, 0);
	if (size < 0) {
		return tc_fruit_backend_missing(errno) ? 0 : -1;
	}
	if (size != AFP_FinderSize) {
		errno = EIO;
		return -1;
	}

	ret = SMB_VFS_FGETXATTR(
		fsp, TC_FINDERINFO_XATTR, finderinfo, AFP_FinderSize);
	if (ret != AFP_FinderSize) {
		if (ret < 0 && tc_fruit_backend_missing(errno)) {
			return 0;
		}
		if (ret >= 0) {
			errno = EIO;
		}
		return -1;
	}
	return 1;
}

/* Resolve the base file of smb_fname: its own fsp (the base of a stream
 * fsp), else a pathref of the base name opened under dirfsp. Return 1 with
 * *_base_fsp set, 0 when the base is absent or a nested pathref open is in
 * progress, -1 on a real error. A pathref is returned in *_base_name, which
 * the caller frees; otherwise *_base_name is NULL. */
static int tc_native_finderinfo_base_fsp(
	vfs_handle_struct *handle,
	const struct files_struct *dirfsp,
	const struct smb_filename *smb_fname,
	struct smb_filename **_base_name,
	files_struct **_base_fsp)
{
	struct fruit_config_data *config = NULL;
	struct smb_filename *base_name = NULL;
	files_struct *base_fsp = NULL;
	NTSTATUS status;

	*_base_name = NULL;
	if (smb_fname->fsp != NULL) {
		base_fsp = smb_fname->fsp;
		if (fsp_is_alternate_stream(base_fsp)) {
			base_fsp = base_fsp->base_fsp;
		}
		*_base_fsp = base_fsp;
		return 1;
	}

	SMB_VFS_HANDLE_GET_DATA(handle, config,
				struct fruit_config_data, return -1);
	if (config->in_openat_pathref_fsp) {
		errno = ENOENT;
		return 0;
	}

	base_name = cp_smb_filename_nostream(talloc_tos(), smb_fname);
	if (base_name == NULL) {
		errno = ENOMEM;
		return -1;
	}

	config->in_openat_pathref_fsp = true;
	status = openat_pathref_fsp_lcomp(
		discard_const_p(struct files_struct, dirfsp), base_name, 0);
	config->in_openat_pathref_fsp = false;
	if (!NT_STATUS_IS_OK(status)) {
		int error = map_errno_from_nt_status(status);

		TALLOC_FREE(base_name);
		errno = error;
		return tc_fruit_backend_missing(error) ? 0 : -1;
	}

	*_base_name = base_name;
	*_base_fsp = base_name->fsp;
	return 1;
}

static int tc_native_finderinfo_read_at(
	vfs_handle_struct *handle,
	const struct files_struct *dirfsp,
	const struct smb_filename *smb_fname,
	uint8_t finderinfo[AFP_FinderSize])
{
	struct smb_filename *base_name = NULL;
	files_struct *base_fsp = NULL;
	int ret;

	ret = tc_native_finderinfo_base_fsp(
		handle, dirfsp, smb_fname, &base_name, &base_fsp);
	if (ret != 1) {
		return ret;
	}

	ret = tc_native_finderinfo_read_fsp(base_fsp, finderinfo);
	{
		int error = errno;

		TALLOC_FREE(base_name);
		errno = error;
	}
	return ret;
}

static int tc_native_finderinfo_write_fsp(
	files_struct *base_fsp,
	const uint8_t finderinfo[AFP_FinderSize])
{
	int ret;

	if (S_ISLNK(base_fsp->fsp_name->st.st_ex_mode)) {
		errno = EACCES;
		return -1;
	}
	if (all_zero(finderinfo, AFP_FinderSize)) {
		ret = SMB_VFS_FREMOVEXATTR(
			base_fsp, TC_FINDERINFO_XATTR);
		if (ret == 0 || tc_fruit_backend_missing(errno)) {
			return 0;
		}
	} else {
		ret = SMB_VFS_FSETXATTR(
			base_fsp,
			TC_FINDERINFO_XATTR,
			finderinfo,
			AFP_FinderSize,
			0);
		if (ret == 0) {
			return 0;
		}
	}
	return -1;
}

/* Return 1 when removed, 0 when absent, -1 on a real error. */
static int tc_native_finderinfo_remove_at(
	vfs_handle_struct *handle,
	const struct files_struct *dirfsp,
	const struct smb_filename *smb_fname)
{
	struct smb_filename *base_name = NULL;
	files_struct *base_fsp = NULL;
	int ret;

	ret = tc_native_finderinfo_base_fsp(
		handle, dirfsp, smb_fname, &base_name, &base_fsp);
	if (ret != 1) {
		return ret;
	}
	if (S_ISLNK(base_fsp->fsp_name->st.st_ex_mode)) {
		TALLOC_FREE(base_name);
		errno = ENOENT;
		return 0;
	}

	ret = SMB_VFS_FREMOVEXATTR(base_fsp, TC_FINDERINFO_XATTR);
	{
		int error = errno;

		TALLOC_FREE(base_name);
		errno = error;
	}
	if (ret == 0) {
		return 1;
	}
	return tc_fruit_backend_missing(errno) ? 0 : -1;
}

static int fruit_open_meta_native(vfs_handle_struct *handle,
				  files_struct *fsp,
				  int flags,
				  mode_t mode)
{
	struct fruit_config_data *config = NULL;
	struct fio *fio = NULL;
	uint8_t finderinfo[AFP_FinderSize];
	int native_state;
	int fd;

	SMB_VFS_HANDLE_GET_DATA(handle, config,
				struct fruit_config_data, return -1);
	SMB_ASSERT(fsp_is_alternate_stream(fsp));
	native_state = tc_native_finderinfo_read_fsp(
		fsp->base_fsp, finderinfo);
	if (native_state < 0) {
		return -1;
	}
	if (native_state == 1 && all_zero(finderinfo, AFP_FinderSize)) {
		native_state = 0;
	}
	if (native_state == 0 && !(flags & O_CREAT)) {
		errno = ENOENT;
		return -1;
	}
	if ((flags & O_TRUNC) && native_state == 1) {
		if (SMB_VFS_FREMOVEXATTR(
				fsp->base_fsp,
				TC_FINDERINFO_XATTR) != 0 &&
		    !tc_fruit_backend_missing(errno))
		{
			return -1;
		}
	}

	fd = vfs_fake_fd();
	if (fd == -1) {
		return -1;
	}
	fio = VFS_ADD_FSP_EXTENSION(handle, fsp, struct fio, fio_destroy_fn);
	if (fio == NULL) {
		vfs_fake_fd_close(fd);
		errno = ENOMEM;
		return -1;
	}
	fio->handle = handle;
	fio->fsp = fsp;
	fio->type = ADOUBLE_META;
	fio->config = config;
	fio->fake_fd = true;
	fio->flags = flags;
	fio->mode = mode;

	return fd;
}

/* A native metadata handle is always a fake fd, so an absent value reads as
 * an empty AfpInfo record, like upstream's fake_fd fallback. */
static ssize_t fruit_pread_meta_native(files_struct *fsp,
				       void *data,
				       size_t n)
{
	AfpInfo *ai = NULL;
	uint8_t native_finderinfo[AFP_FinderSize];
	char afpinfo_buf[AFP_INFO_SIZE];
	ssize_t to_return = MIN(n, AFP_INFO_SIZE);
	ssize_t nread;
	int native_state;

	native_state = tc_native_finderinfo_read_fsp(
		fsp->base_fsp, native_finderinfo);
	if (native_state < 0) {
		return -1;
	}
	ai = afpinfo_new(talloc_tos());
	if (ai == NULL) {
		return -1;
	}
	if (native_state == 1) {
		memcpy(ai->afpi_FinderInfo,
		       native_finderinfo,
		       AFP_FinderSize);
	}
	nread = afpinfo_pack(ai, afpinfo_buf);
	TALLOC_FREE(ai);
	if (nread != AFP_INFO_SIZE) {
		return -1;
	}
	memcpy(data, afpinfo_buf, to_return);
	return to_return;
}

static int fruit_fstatat_meta_native(struct vfs_handle_struct *handle,
				     const struct files_struct *dirfsp,
				     const struct smb_filename *smb_relname,
				     SMB_STRUCT_STAT *sbuf,
				     ino_t ino)
{
	uint8_t native_finderinfo[AFP_FinderSize];
	int native_state;

	native_state = tc_native_finderinfo_read_at(
		handle, dirfsp, smb_relname, native_finderinfo);
	if (native_state != 1 ||
	    all_zero(native_finderinfo, AFP_FinderSize))
	{
		/* All-zero FinderInfo is absent too, as in the stream list. */
		if (native_state >= 0) {
			errno = ENOENT;
		}
		return -1;
	}
	sbuf->st_ex_ino = ino;
	sbuf->st_ex_size = AFP_INFO_SIZE;
	sbuf->st_ex_mode &= ~S_IFMT;
	sbuf->st_ex_mode |= S_IFREG;
	sbuf->st_ex_blocks = sbuf->st_ex_size / STAT_ST_BLOCKSIZE + 1;
	return 0;
}

static NTSTATUS fruit_streaminfo_meta_native(
	vfs_handle_struct *handle,
	const struct smb_filename *smb_fname,
	TALLOC_CTX *mem_ctx,
	unsigned int *pnum_streams,
	struct stream_struct **pstreams)
{
	uint8_t native_finderinfo[AFP_FinderSize];
	int native_state;

	if (!del_fruit_stream(mem_ctx, pnum_streams, pstreams,
			      AFPINFO_STREAM) ||
	    !del_fruit_stream(mem_ctx, pnum_streams, pstreams,
			      ":" NETATALK_META_XATTR ":$DATA"))
	{
		return NT_STATUS_NO_MEMORY;
	}
	native_state = tc_native_finderinfo_read_at(
		handle,
		handle->conn->cwd_fsp,
		smb_fname,
		native_finderinfo);
	if (native_state < 0) {
		return map_nt_error_from_unix(errno);
	}
	if (native_state == 0 ||
	    all_zero(native_finderinfo, AFP_FinderSize))
	{
		return NT_STATUS_OK;
	}
	if (!add_fruit_stream(mem_ctx, pnum_streams, pstreams,
			      AFPINFO_STREAM_NAME, AFP_INFO_SIZE,
			      smb_roundup(handle->conn, AFP_INFO_SIZE)))
	{
		return NT_STATUS_NO_MEMORY;
	}
	return NT_STATUS_OK;
}
