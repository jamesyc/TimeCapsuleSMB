/* Included by conn_idle.c (patch 0041) for Apple disk rebinding. */

/*
 * Apple can revoke a bumped USB disk's vnode and remount the same dkN. A
 * tree records its volume identity at connect; when the manager's reload
 * signal reaches this smbd, only trees whose disk binding was revoked,
 * replaced or removed are disconnected. Other shares on the session stay
 * connected.
 */

bool conn_record_bindings(connection_struct *conn)
{
	const char *uuid;
	const char *device;
	char *path;
	if (IS_IPC(conn) || IS_PRINT(conn)) {
		return true;
	}
	uuid = lp_parm_const_string(SNUM(conn), "tc", "volume uuid", NULL);
	device = lp_parm_const_string(SNUM(conn), "tc", "volume device", NULL);
	if (uuid == NULL || uuid[0] == '\0' || device == NULL || device[0] == '\0') {
		return true;
	}
	/* Snapshot the configured root now: reload can retain in-use share
	 * definitions, and connectpath may have been canonicalized by the VFS. */
	path = lp_path(talloc_tos(), loadparm_s3_global_substitution(), SNUM(conn));
	conn->tc_volume_binding = path == NULL ? NULL :
		talloc_asprintf(conn, "%s|%s", uuid, path);
	TALLOC_FREE(path);
	conn->tc_volume_key = talloc_asprintf(conn, "volume %s", device);
	if (conn->tc_volume_binding == NULL || conn->tc_volume_key == NULL) {
		return false;
	}
	conn->tc_root_ino = conn->cwd_fsp->fsp_name->st.st_ex_ino;
	return true;
}

/* Apple can revoke a bumped USB vnode and remount the same dkN with the
 * same UUID/device/inode/fsid. On NetBSD 4, a retained real descriptor then
 * fails fstat with EBADF even though a fresh path succeeds. F_GETFD separates
 * that revoked vnode from an already closed descriptor. Ordinary I/O or
 * permission errors are not sufficient evidence to disconnect a whole tree. */
static bool tc_revoked_descriptor(int fd)
{
	struct stat st;
	int ret;
	if (fd < 0 || fcntl(fd, F_GETFD) == -1) {
		return false;
	}
	do {
		ret = fstat(fd, &st);
	} while (ret == -1 && errno == EINTR);
	return ret == -1 && (errno == EBADF || errno == ESTALE ||
			    errno == ENXIO || errno == ENODEV);
}

static bool tc_stale_disk_tree(connection_struct *conn, void *private_data)
{
	bool config_valid = *(bool *)private_data;
	files_struct *fsp;
	struct stat st;
	int ret;
	if (IS_IPC(conn) || IS_PRINT(conn) || conn->tc_volume_binding == NULL ||
	    conn->tc_volume_key == NULL) {
		return false;
	}
	if (config_valid) {
		const char *current = lp_parm_const_string(-1, "tc", conn->tc_volume_key, "");
		/* loadparm retains deleted in-use share definitions. Its global
		 * options are refreshed, so this mapping also identifies removed
		 * shares, replaced disks and changed export roots without open fsps. */
		if (strcmp(current, conn->tc_volume_binding) != 0) {
			return true;
		}
	}
	do {
		ret = stat(conn->connectpath, &st);
	} while (ret == -1 && errno == EINTR);
	if (ret == 0 && (!S_ISDIR(st.st_mode) ||
			st.st_dev != conn->base_share_dev || st.st_ino != conn->tc_root_ino)) {
		return true;
	}
	if (ret == -1 && (errno == ENOENT || errno == ENOTDIR || errno == ESTALE ||
			 errno == ENXIO || errno == ENODEV)) {
		return true;
	}
	for (fsp = conn->sconn->files; fsp != NULL; fsp = fsp->next) {
		if (fsp->conn != conn || fsp->fsp_flags.closing || fsp->fake_file_handle != NULL ||
		    fsp == conn->cwd_fsp || fsp->fsp_name == NULL ||
		    (!S_ISREG(fsp->fsp_name->st.st_ex_mode) && !S_ISDIR(fsp->fsp_name->st.st_ex_mode))) {
			continue;
		}
		/* cwd_fsp carries AT_FDCWD, not a retained root descriptor. Negative
		 * stat-only/path sentinels and fake handles must never close a tree. */
		if (tc_revoked_descriptor(fsp_get_pathref_fd(fsp))) {
			return true;
		}
	}
	return false;
}

void conn_refresh_bindings(struct smbd_server_connection *sconn, bool config_valid)
{
	/* A coalesced detach/replug may leave topology unchanged. Clear the
	 * current-directory shortcut before any new path operation. Existing
	 * async tdis drains AIO only on invalid trees, preserving other shares
	 * even when the same SMB session uses both disks. */
	reset_chdir_lastconn_cache();
	conn_force_tdis(sconn, tc_stale_disk_tree, &config_valid);
}
