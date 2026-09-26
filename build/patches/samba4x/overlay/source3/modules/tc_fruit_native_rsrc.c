/*
 * Included by vfs_fruit.c (patch 0056) on native HFS shares: the
 * AFP_Resource stream is the file's native resource fork, opened as
 * <file>/..namedfork/rsrc like AFP serves it, instead of an AppleDouble
 * file, a stream or an xattr.
 */

static struct smb_filename *tc_native_rsrc_name(
	TALLOC_CTX *mem_ctx,
	const struct smb_filename *base_name)
{
	struct smb_filename *native_name = cp_smb_filename_nostream(
		mem_ctx, base_name);
	char *path;

	if (native_name == NULL) {
		return NULL;
	}
	path = talloc_asprintf(
		native_name, "%s/..namedfork/rsrc", base_name->base_name);
	if (path == NULL) {
		TALLOC_FREE(native_name);
		return NULL;
	}
	TALLOC_FREE(native_name->base_name);
	native_name->base_name = path;
	return native_name;
}

static int tc_native_rsrc_statat(
	vfs_handle_struct *handle,
	const struct files_struct *dirfsp,
	const struct smb_filename *smb_fname,
	SMB_STRUCT_STAT *sbuf,
	int flags)
{
	TALLOC_CTX *frame = talloc_stackframe();
	struct smb_filename *native_name = tc_native_rsrc_name(
		frame, smb_fname);
	int ret;

	if (native_name == NULL) {
		TALLOC_FREE(frame);
		errno = ENOMEM;
		return -1;
	}
	ret = SMB_VFS_NEXT_FSTATAT(
		handle, dirfsp, native_name, sbuf, flags);
	TALLOC_FREE(frame);
	return ret;
}

static uint64_t readdir_attr_rfork_size_native(
	vfs_handle_struct *handle,
	const struct smb_filename *smb_fname)
{
	SMB_STRUCT_STAT st = {0};

	/* HFS warns when a directory is probed for a named resource fork. */
	if (!S_ISREG(smb_fname->st.st_ex_mode)) {
		return 0;
	}

	if (tc_native_rsrc_statat(
			handle, handle->conn->cwd_fsp, smb_fname, &st, 0) != 0 ||
	    st.st_ex_size <= 0)
	{
		return 0;
	}
	return st.st_ex_size;
}

static int fruit_open_rsrc_native(vfs_handle_struct *handle,
				  files_struct *fsp,
				  int flags,
				  mode_t mode)
{
	TALLOC_CTX *frame = talloc_stackframe();
	struct smb_filename *native_name = NULL;
	struct vfs_open_how how = {
		.flags = flags,
		.mode = mode,
	};
	SMB_STRUCT_STAT st = {0};
	int fd;

	if (!S_ISREG(fsp->base_fsp->fsp_name->st.st_ex_mode)) {
		TALLOC_FREE(frame);
		errno = ENOENT;
		return -1;
	}
	native_name = tc_native_rsrc_name(frame, fsp->base_fsp->fsp_name);
	if (native_name == NULL) {
		TALLOC_FREE(frame);
		errno = ENOMEM;
		return -1;
	}
	if (!(flags & O_CREAT)) {
		int stat_ret = SMB_VFS_NEXT_FSTATAT(
			handle, fsp->conn->cwd_fsp, native_name, &st, 0);

		if (stat_ret != 0 || st.st_ex_size == 0) {
			int error = stat_ret == 0 ? ENOENT : errno;

			TALLOC_FREE(frame);
			errno = error;
			return -1;
		}
	}
	fd = SMB_VFS_NEXT_OPENAT(
		handle, fsp->conn->cwd_fsp, native_name, fsp, &how);
	TALLOC_FREE(frame);
	return fd;
}

static int fruit_fstatat_rsrc_native(struct vfs_handle_struct *handle,
				     const struct files_struct *dirfsp,
				     const struct smb_filename *smb_relname,
				     SMB_STRUCT_STAT *sbuf,
				     int flags)
{
	struct smb_filename base_name = *smb_relname;
	SMB_STRUCT_STAT resource_st = {0};
	int ret;

	base_name.stream_name = NULL;
	ret = SMB_VFS_NEXT_FSTATAT(
		handle, dirfsp, &base_name, sbuf, flags);
	if (ret != 0) {
		return ret;
	}
	if (!S_ISREG(sbuf->st_ex_mode)) {
		errno = ENOENT;
		return -1;
	}
	ret = tc_native_rsrc_statat(
		handle, dirfsp, smb_relname, &resource_st, flags);
	if (ret != 0 || resource_st.st_ex_size == 0) {
		if (ret == 0) {
			errno = ENOENT;
		}
		return -1;
	}
	sbuf->st_ex_ino = hash_inode(sbuf, smb_relname->stream_name);
	sbuf->st_ex_size = resource_st.st_ex_size;
	sbuf->st_ex_mode &= ~S_IFMT;
	sbuf->st_ex_mode |= S_IFREG;
	sbuf->st_ex_blocks = sbuf->st_ex_size / STAT_ST_BLOCKSIZE + 1;
	return 0;
}

static int fruit_fstat_rsrc_native(vfs_handle_struct *handle,
				   files_struct *fsp,
				   SMB_STRUCT_STAT *sbuf)
{
	SMB_STRUCT_STAT resource_st = {0};
	int ret;

	ret = SMB_VFS_NEXT_FSTAT(handle, fsp, &resource_st);
	if (ret != 0) {
		return ret;
	}
	*sbuf = fsp->base_fsp->fsp_name->st;
	sbuf->st_ex_ino = hash_inode(sbuf, fsp->fsp_name->stream_name);
	sbuf->st_ex_size = resource_st.st_ex_size;
	return 0;
}

static NTSTATUS fruit_streaminfo_rsrc_native(
	vfs_handle_struct *handle,
	const struct smb_filename *smb_fname,
	TALLOC_CTX *mem_ctx,
	unsigned int *pnum_streams,
	struct stream_struct **pstreams)
{
	uint64_t rlen;

	filter_empty_rsrc_stream(pnum_streams, pstreams);
	rlen = readdir_attr_rfork_size_native(handle, smb_fname);
	if (rlen == 0) {
		return NT_STATUS_OK;
	}
	if (!add_fruit_stream(mem_ctx, pnum_streams, pstreams,
			      AFPRESOURCE_STREAM_NAME, rlen,
			      smb_roundup(handle->conn, rlen)))
	{
		return NT_STATUS_NO_MEMORY;
	}
	return NT_STATUS_OK;
}
