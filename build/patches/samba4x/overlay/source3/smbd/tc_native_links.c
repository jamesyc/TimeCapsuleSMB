/*
 * Time Capsule native symlinks.
 *
 * Apple's AFP server shares an HFS volume with this smbd. AFP and SSH create
 * real POSIX symlinks, while macOS SMB clients create Minshall+French "XSym"
 * files when the server does not advertise reparse point support. Keep the
 * disk native: present native links to SMB clients as symlink reparse points,
 * and turn an XSym file or symlink reparse placeholder a client has just
 * created into a native link when the creating handle closes. Existing XSym
 * files are left as they are: macOS reads them as links, and rewriting one
 * (delete + create) makes it native. Reparse points without a native form
 * (FIFOs, sockets, devices, junctions) are refused.
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 3 of the License, or
 * (at your option) any later version.
 */

#include "includes.h"
#include "smbd/smbd.h"
#include "smbd/globals.h"
#include "locking/share_mode_lock.h"
#include "lib/util/server_id.h"
#include "messages.h"
#include "librpc/gen_ndr/ndr_open_files.h"
#include "lib/util/sys_rw.h"
#include "libcli/smb/reparse.h"
#include "modules/util_reparse.h"
#include "MacExtensions.h"
#include <gnutls/gnutls.h>
#include <gnutls/crypto.h>

#define TC_XSYM_MAGIC "XSym\n"
#define TC_XSYM_MAGIC_LEN 5
#define TC_XSYM_HEADER_LEN 43 /* magic, "%04d\n", 32 hex digits and '\n' */
/* WSL symlink; Samba's reparse parser does not know it. */
#define TC_IO_REPARSE_TAG_LX_SYMLINK 0xA000001D

bool tc_native_links_enabled(const struct connection_struct *conn)
{
	return lp_parm_bool(SNUM(conn), "tc", "native symlinks", false);
}

/*
 * macOS resolves every symlink itself once it knows an entry is one, so for
 * an AAPL connection a terminal link is always the object being operated on.
 * Its rename request does not set FILE_OPEN_REPARSE_POINT (the client types
 * the source as VREG/VDIR), so following the link on the server would rename
 * or delete the target instead.
 */
bool tc_client_resolves_links(const struct connection_struct *conn)
{
	return conn != NULL && conn->sconn != NULL &&
	       conn->sconn->client_resolves_symlinks &&
	       tc_native_links_enabled(conn);
}

static bool tc_xsym_md5_hex(const uint8_t *data,
			    size_t len,
			    char hex[33])
{
	uint8_t digest[16];
	size_t i;
	int rc;

	rc = gnutls_hash_fast(GNUTLS_DIG_MD5, data, len, digest);
	if (rc < 0) {
		return false;
	}
	for (i = 0; i < sizeof(digest); i++) {
		snprintf(hex + i * 2, 3, "%02x", digest[i]);
	}
	return true;
}

/*
 * Validate an XSym body exactly as the macOS client does (smbfs_node.c
 * smb_check_for_windows_symlink): magic, a 4-digit decimal length and a
 * newline, then the MD5 of the target. Padding after the target is not
 * checked by the client either. A native link additionally cannot hold an
 * empty target or a NUL byte.
 */
static bool tc_xsym_parse(TALLOC_CTX *mem_ctx,
			  const uint8_t *buf,
			  size_t buflen,
			  char **_target)
{
	char hex[33];
	size_t len = 0;
	size_t i;

	if (buflen != TC_XSYM_FILE_SIZE) {
		return false;
	}
	if (memcmp(buf, TC_XSYM_MAGIC, TC_XSYM_MAGIC_LEN) != 0) {
		return false;
	}
	for (i = TC_XSYM_MAGIC_LEN; i < TC_XSYM_MAGIC_LEN + 4; i++) {
		if (buf[i] < '0' || buf[i] > '9') {
			return false;
		}
		len = len * 10 + (buf[i] - '0');
	}
	if (buf[TC_XSYM_MAGIC_LEN + 4] != '\n' ||
	    buf[TC_XSYM_HEADER_LEN - 1] != '\n') {
		return false;
	}
	if (len == 0 || len > buflen - TC_XSYM_HEADER_LEN) {
		return false;
	}
	if (memchr(buf + TC_XSYM_HEADER_LEN, '\0', len) != NULL) {
		return false;
	}
	if (!tc_xsym_md5_hex(buf + TC_XSYM_HEADER_LEN, len, hex)) {
		return false;
	}
	if (memcmp(buf + TC_XSYM_MAGIC_LEN + 5, hex, 32) != 0) {
		return false;
	}
	*_target = talloc_strndup(mem_ctx,
				  (const char *)buf + TC_XSYM_HEADER_LEN,
				  len);
	return *_target != NULL;
}

/*
 * Build the 1067-byte body macOS writes for a symlink (smbfs_smb.c
 * smbfs_create_windows_symlink_data): header, target, one newline, then
 * space padding.
 */
static bool tc_xsym_format(const char *target, uint8_t buf[TC_XSYM_FILE_SIZE])
{
	size_t len = strlen(target);
	char hex[33];
	size_t pos;

	if (len == 0 || len > TC_XSYM_FILE_SIZE - TC_XSYM_HEADER_LEN) {
		return false;
	}
	if (!tc_xsym_md5_hex((const uint8_t *)target, len, hex)) {
		return false;
	}
	memcpy(buf, TC_XSYM_MAGIC, TC_XSYM_MAGIC_LEN);
	snprintf((char *)buf + TC_XSYM_MAGIC_LEN, 6, "%04zu\n", len);
	memcpy(buf + TC_XSYM_MAGIC_LEN + 5, hex, 32);
	buf[TC_XSYM_HEADER_LEN - 1] = '\n';
	memcpy(buf + TC_XSYM_HEADER_LEN, target, len);
	pos = TC_XSYM_HEADER_LEN + len;
	if (pos < TC_XSYM_FILE_SIZE) {
		buf[pos++] = '\n';
		memset(buf + pos, ' ', TC_XSYM_FILE_SIZE - pos);
	}
	return true;
}

/* The XSym body macOS would read for a native link. */
static NTSTATUS tc_link_xsym_body(struct files_struct *fsp,
				  uint8_t buf[TC_XSYM_FILE_SIZE])
{
	struct smb_filename *parent = NULL;
	struct smb_filename *atname = NULL;
	char *target = NULL;
	NTSTATUS status;
	bool ok;

	status = parent_pathref(talloc_tos(),
				fsp->conn->cwd_fsp,
				fsp->fsp_name,
				&parent,
				&atname);
	if (!NT_STATUS_IS_OK(status)) {
		return status;
	}
	status = readlink_talloc(talloc_tos(), parent->fsp, atname, &target);
	TALLOC_FREE(parent);
	if (!NT_STATUS_IS_OK(status)) {
		return status;
	}
	ok = tc_xsym_format(target, buf);
	TALLOC_FREE(target);
	return ok ? NT_STATUS_OK : NT_STATUS_ACCESS_DENIED;
}

/*
 * A Mac that created a link keeps its cached view of the XSym file it wrote
 * until its attribute cache expires, and reads that file's data to resolve
 * the link. After the close-time conversion the name is a native link, so
 * serve the same bytes from it. Only Mac link-object handles get here.
 */
NTSTATUS tc_native_links_read_xsym(struct files_struct *fsp,
				   TALLOC_CTX *mem_ctx,
				   DATA_BLOB *out,
				   off_t offset,
				   size_t length)
{
	uint8_t buf[TC_XSYM_FILE_SIZE];
	NTSTATUS status;
	size_t n;

	status = tc_link_xsym_body(fsp, buf);
	if (!NT_STATUS_IS_OK(status)) {
		return status;
	}
	if (offset < 0 || offset >= TC_XSYM_FILE_SIZE) {
		return NT_STATUS_END_OF_FILE;
	}
	n = MIN(length, (size_t)(TC_XSYM_FILE_SIZE - offset));
	*out = data_blob_talloc(mem_ctx, buf + offset, n);
	if (n > 0 && out->data == NULL) {
		return NT_STATUS_NO_MEMORY;
	}
	return NT_STATUS_OK;
}

/*
 * Likewise, right after creating a link a Mac may restate the XSym file it
 * wrote, as a zero-length write at its end (sets the size it expects) on a
 * new handle to the name that is now a native link. Accept writes that
 * leave that XSym view unchanged; anything else would need a real file.
 */
NTSTATUS tc_native_links_write_xsym(struct files_struct *fsp,
				    const uint8_t *data,
				    size_t length,
				    off_t offset)
{
	uint8_t buf[TC_XSYM_FILE_SIZE];
	NTSTATUS status;

	status = tc_link_xsym_body(fsp, buf);
	if (!NT_STATUS_IS_OK(status)) {
		return status;
	}
	if (offset < 0 || offset > TC_XSYM_FILE_SIZE ||
	    length > (size_t)(TC_XSYM_FILE_SIZE - offset) ||
	    (length > 0 && memcmp(buf + offset, data, length) != 0)) {
		DBG_NOTICE("%s is a native link; refusing a %zu-byte write at "
			   "%jd\n",
			   fsp_str_dbg(fsp), length, (intmax_t)offset);
		return NT_STATUS_ACCESS_DENIED;
	}
	return NT_STATUS_OK;
}

/*
 * Map a link target between the names on disk and the names SMB clients use.
 * With fruit:encoding = native, catia maps the characters Windows cannot put
 * in a name (controls, '"' '*' ':' '<' '>' '?' '\\' '|') to private-use code
 * points on the wire, e.g. ':' <-> U+F022. A link's own name gets that
 * mapping in every VFS call, but its target is data to the VFS, so the names
 * inside it are mapped here. '/' is never mapped, so a whole target maps in
 * one call. Without a mapping module the target stays as it is.
 */
NTSTATUS tc_native_links_map_target(struct connection_struct *conn,
				    TALLOC_CTX *mem_ctx,
				    char **_target,
				    enum vfs_translate_direction direction)
{
	char *mapped = NULL;
	NTSTATUS status;

	status = SMB_VFS_TRANSLATE_NAME(conn, *_target, direction, mem_ctx,
					&mapped);
	if (NT_STATUS_EQUAL(status, NT_STATUS_NONE_MAPPED)) {
		return NT_STATUS_OK;
	}
	if (!NT_STATUS_IS_OK(status)) {
		return status;
	}
	TALLOC_FREE(*_target);
	*_target = mapped;
	return NT_STATUS_OK;
}

/*
 * Clients told FILE_SUPPORTS_REPARSE_POINTS create a link as an empty file
 * plus FSCTL_SET_REPARSE_POINT: Windows and Linux (default) with the symlink
 * tag, Linux symlink=nfs with an NFS_SPECFILE_LNK payload and symlink=wsl
 * with an LX_SYMLINK payload. Turn any of them into a native target. Windows
 * '\\' separators become '/', and Windows-only absolute forms (\\??\\ device
 * paths, UNC paths, drive letters) have no meaning on the disk. Everything
 * else (FIFOs, sockets, devices, junctions) has no native form here.
 *
 * A symlink-tag target names files as the client sees them, so its names are
 * mapped back to the disk's (a client's "a<U+F022>b" is "a:b" on disk), after
 * the separators: catia maps U+F026 to a literal '\\' in a name. NFS and WSL
 * payloads carry POSIX bytes and are kept as they are.
 */
static NTSTATUS tc_reparse_native_target(struct connection_struct *conn,
					 TALLOC_CTX *mem_ctx,
					 const uint8_t *data,
					 size_t len,
					 char **_target)
{
	struct reparse_data_buffer *buf = NULL;
	const char *sub = NULL;
	char *target = NULL;
	bool windows = false;
	NTSTATUS status;

	if (len >= 8 && PULL_LE_U32(data, 0) == TC_IO_REPARSE_TAG_LX_SYMLINK) {
		/* [MS-FSCC] has no layout; WSL writes version 2, then UTF-8. */
		size_t datalen = PULL_LE_U16(data, 4);

		if (datalen != len - 8 || datalen <= 4 ||
		    PULL_LE_U32(data, 8) != 2 ||
		    memchr(data + 12, '\0', datalen - 4) != NULL) {
			return NT_STATUS_IO_REPARSE_DATA_INVALID;
		}
		target = talloc_strndup(mem_ctx,
					(const char *)data + 12,
					datalen - 4);
		if (target == NULL) {
			return NT_STATUS_NO_MEMORY;
		}
		goto check;
	}

	buf = talloc_zero(mem_ctx, struct reparse_data_buffer);
	if (buf == NULL) {
		return NT_STATUS_NO_MEMORY;
	}
	status = reparse_data_buffer_parse(buf, buf, data, len);
	if (!NT_STATUS_IS_OK(status)) {
		TALLOC_FREE(buf);
		return status;
	}
	switch (buf->tag) {
	case IO_REPARSE_TAG_SYMLINK:
		sub = buf->parsed.lnk.substitute_name;
		windows = true;
		break;
	case IO_REPARSE_TAG_NFS:
		if (buf->parsed.nfs.type == NFS_SPECFILE_LNK) {
			sub = buf->parsed.nfs.data.lnk_target;
		}
		break;
	default:
		break;
	}
	if (sub == NULL) {
		TALLOC_FREE(buf);
		return NT_STATUS_NOT_SUPPORTED;
	}
	if (windows &&
	    (strncmp(sub, "\\??\\", 4) == 0 ||
	     strncmp(sub, "\\\\", 2) == 0 ||
	     (isalpha((unsigned char)sub[0]) && sub[1] == ':'))) {
		TALLOC_FREE(buf);
		return NT_STATUS_NOT_SUPPORTED;
	}
	target = talloc_strdup(mem_ctx, sub);
	TALLOC_FREE(buf);
	if (target == NULL) {
		return NT_STATUS_NO_MEMORY;
	}
	if (windows) {
		string_replace(target, '\\', '/');
		status = tc_native_links_map_target(conn, mem_ctx, &target,
						    vfs_translate_to_unix);
		if (!NT_STATUS_IS_OK(status)) {
			TALLOC_FREE(target);
			return status;
		}
	}
check:
	if (target[0] == '\0' || strlen(target) >= PATH_MAX) {
		TALLOC_FREE(target);
		return NT_STATUS_NOT_SUPPORTED;
	}
	*_target = target;
	return NT_STATUS_OK;
}

uint32_t tc_native_links_fs_capabilities(const struct connection_struct *conn)
{
	/*
	 * macOS reads reparse symlinks without this bit and would switch to
	 * SET_REPARSE_POINT creation with it; it keeps creating XSym files,
	 * which become native at close. Windows and Linux need it to create
	 * links; tc_native_links_check_set() refuses what has no native form.
	 */
	if (!tc_native_links_enabled(conn) || tc_client_resolves_links(conn)) {
		return 0;
	}
	return FILE_SUPPORTS_REPARSE_POINTS;
}

/*
 * Before upstream stores anything, refuse every SET_REPARSE_POINT the
 * close-time conversion cannot turn into a native link: upstream would keep
 * the payload in an xattr on an empty file, which AFP and SSH see as a plain
 * file. Only a symlink payload on a placeholder this handle just created
 * passes. Clients remove their placeholder when the SET fails (Windows
 * CreateSymbolicLinkW, Linux smb2_create_reparse_inode, Samba's
 * cli_create_reparse_point).
 */
NTSTATUS tc_native_links_check_set(struct files_struct *fsp,
				   uint32_t reparse_tag,
				   const uint8_t *data,
				   size_t len)
{
	SMB_STRUCT_STAT sbuf;
	char *target = NULL;
	NTSTATUS status;

	if (!tc_native_links_enabled(fsp->conn)) {
		return NT_STATUS_OK;
	}
	status = tc_reparse_native_target(fsp->conn, talloc_tos(), data, len,
					  &target);
	TALLOC_FREE(target);
	if (!NT_STATUS_IS_OK(status)) {
		DBG_NOTICE("%s: reparse tag 0x%08" PRIx32 " has no native "
			   "form: %s\n",
			   fsp_str_dbg(fsp), reparse_tag, nt_errstr(status));
		return status;
	}
	if (fsp->op == NULL ||
	    fsp->op->global->create_action != FILE_WAS_CREATED ||
	    fsp_is_alternate_stream(fsp) ||
	    fsp_get_io_fd(fsp) == -1 ||
	    SMB_VFS_FSTAT(fsp, &sbuf) != 0 ||
	    !S_ISREG(sbuf.st_ex_mode) ||
	    sbuf.st_ex_size != 0 ||
	    sbuf.st_ex_nlink != 1) {
		DBG_NOTICE("%s is not a fresh placeholder, refusing symlink\n",
			   fsp_str_dbg(fsp));
		return NT_STATUS_NOT_SUPPORTED;
	}
	return NT_STATUS_OK;
}

/*
 * Read the start of a created file. Its creating handle may be write-only:
 * Linux mfsymlinks creates the XSym file with GENERIC_WRITE, which smbd opens
 * O_WRONLY. Then read through an internal pathref of the same file, found by
 * "name" in its directory (the aside name once it was moved, otherwise its
 * own) and accepted only if it is the same regular file. The client's handle
 * never gets more access than it asked for. Without O_PATH a pathref is an
 * O_RDONLY fd opened as root; with O_PATH it cannot be read, and the file is
 * kept as it is. openat_pathref_fsp() does not apply "veto files", which hide
 * the aside name.
 */
static ssize_t tc_read_created(struct files_struct *fsp,
			       const char *name,
			       const SMB_STRUCT_STAT *fst,
			       uint8_t *buf,
			       size_t len)
{
	struct smb_filename *parent = NULL;
	struct smb_filename *atname = NULL;
	struct smb_filename *rel = NULL;
	NTSTATUS status;
	ssize_t n;
	int fd;

	n = SMB_VFS_PREAD(fsp, buf, len, 0);
	if (n >= 0 || errno != EBADF) {
		return n;
	}
	status = parent_pathref(talloc_tos(),
				fsp->conn->cwd_fsp,
				fsp->fsp_name,
				&parent,
				&atname);
	if (!NT_STATUS_IS_OK(status)) {
		goto fail;
	}
	rel = synthetic_smb_fname(talloc_tos(),
				  name != NULL ? name : atname->base_name,
				  NULL,
				  NULL,
				  atname->twrp,
				  atname->flags);
	if (rel == NULL) {
		goto fail;
	}
	status = openat_pathref_fsp(parent->fsp, rel);
	if (!NT_STATUS_IS_OK(status) ||
	    rel->fsp == NULL ||
	    !S_ISREG(rel->st.st_ex_mode) ||
	    rel->st.st_ex_dev != fst->st_ex_dev ||
	    rel->st.st_ex_ino != fst->st_ex_ino) {
		DBG_NOTICE("%s: cannot read it through another handle\n",
			   fsp_str_dbg(fsp));
		goto fail;
	}
	fd = fsp_get_pathref_fd(rel->fsp);
	n = (fd == -1) ? -1 : sys_pread(fd, buf, len, 0);
	TALLOC_FREE(rel);
	TALLOC_FREE(parent);
	return n;
fail:
	TALLOC_FREE(rel);
	TALLOC_FREE(parent);
	errno = EBADF;
	return -1;
}

/*
 * The link target a created file stands for: a complete XSym body (macOS,
 * Linux mfsymlinks) or an empty placeholder carrying a symlink reparse
 * payload. *sbuf is the fstat of the creating handle; "name" is where the
 * file is in its directory if it was moved (see tc_read_created()).
 */
static bool tc_candidate_target(TALLOC_CTX *mem_ctx,
				struct files_struct *fsp,
				const char *name,
				SMB_STRUCT_STAT *sbuf,
				char **_target)
{
	uint8_t buf[TC_XSYM_FILE_SIZE + 1];
	ssize_t nread;

	if (SMB_VFS_FSTAT(fsp, sbuf) != 0 ||
	    !S_ISREG(sbuf->st_ex_mode) ||
	    sbuf->st_ex_nlink != 1) {
		return false;
	}
	if (sbuf->st_ex_size == 0) {
		/* SET_REPARSE_POINT placeholder, see tc_native_links_check_set() */
		uint8_t *data = NULL;
		uint32_t tag = 0, datalen = 0;
		NTSTATUS status;

		if (!(fdos_mode(fsp) & FILE_ATTRIBUTE_REPARSE_POINT)) {
			return false;
		}
		status = fsctl_get_reparse_point(fsp, mem_ctx, &tag,
						 &data, UINT16_MAX, &datalen);
		if (!NT_STATUS_IS_OK(status)) {
			return false;
		}
		status = tc_reparse_native_target(fsp->conn, mem_ctx, data,
						  datalen, _target);
		TALLOC_FREE(data);
		return NT_STATUS_IS_OK(status);
	}
	if (sbuf->st_ex_size != TC_XSYM_FILE_SIZE) {
		return false;
	}
	nread = tc_read_created(fsp, name, sbuf, buf, sizeof(buf));
	if (nread != TC_XSYM_FILE_SIZE) {
		return false;
	}
	return tc_xsym_parse(mem_ctx, buf, nread, _target);
}

/* A resource fork cannot live on a link; keep such a file as it is. */
static bool tc_has_resource_fork(struct files_struct *fsp)
{
	struct stream_struct *streams = NULL;
	unsigned int num_streams = 0;
	unsigned int i;
	bool found = true;
	NTSTATUS status;

	status = vfs_fstreaminfo(fsp, talloc_tos(), &num_streams, &streams);
	if (!NT_STATUS_IS_OK(status)) {
		return true;
	}
	found = false;
	for (i = 0; i < num_streams; i++) {
		if (strequal(streams[i].name, AFPRESOURCE_STREAM_NAME ":$DATA") &&
		    streams[i].size > 0) {
			found = true;
		}
	}
	TALLOC_FREE(streams);
	return found;
}

/*
 * Before the share mode entry goes away, decide whether this handle created a
 * complete XSym file (or a SET_REPARSE_POINT symlink placeholder). Only the
 * creating handle qualifies: an existing XSym file is never converted because
 * someone opened it.
 */
bool tc_native_links_close_prepare(TALLOC_CTX *mem_ctx,
				   struct files_struct *fsp,
				   enum file_close_type close_type,
				   struct tc_xsym_candidate *cand)
{
	SMB_STRUCT_STAT sbuf;

	ZERO_STRUCTP(cand);

	if (close_type != NORMAL_CLOSE ||
	    !tc_native_links_enabled(fsp->conn) ||
	    fsp->op == NULL ||
	    fsp->op->global->create_action != FILE_WAS_CREATED ||
	    fsp_is_alternate_stream(fsp) ||
	    fsp->fsp_flags.is_directory ||
	    fsp->fsp_flags.delete_on_close ||
	    fsp_get_io_fd(fsp) == -1)
	{
		return false;
	}
	if (!tc_candidate_target(mem_ctx, fsp, NULL, &sbuf, &cand->target)) {
		return false;
	}
	if (tc_has_resource_fork(fsp)) {
		DBG_NOTICE("%s has a resource fork, keeping it as a file\n",
			   fsp_str_dbg(fsp));
		TALLOC_FREE(cand->target);
		return false;
	}
	cand->dev = sbuf.st_ex_dev;
	cand->ino = sbuf.st_ex_ino;
	cand->size = sbuf.st_ex_size;
	return true;
}

struct tc_sole_open_state {
	struct files_struct *fsp;
	struct server_id self;
	bool other;
};

static bool tc_sole_open_fn(struct share_mode_entry *e,
			    bool *modified,
			    void *private_data)
{
	struct tc_sole_open_state *state = private_data;

	if (e->share_file_id == fh_get_gen_id(state->fsp->fh) &&
	    server_id_equal(&state->self, &e->pid)) {
		return false;
	}
	if (share_entry_stale_pid(e)) {
		return false;
	}
	state->other = true;
	return true;
}

/*
 * Called by close_share_mode_lock_prepare() under the share mode lock: the
 * conversion may only run when the closing handle is the file's only open,
 * POSIX opens and other names of it included.
 */
bool tc_native_links_sole_open(struct share_mode_lock *lck,
			       struct files_struct *fsp)
{
	struct tc_sole_open_state state = {
		.fsp = fsp,
		.self = messaging_server_id(fsp->conn->sconn->msg_ctx),
	};

	if (!share_mode_forall_entries(lck, tc_sole_open_fn, &state)) {
		return false;
	}
	return !state.other;
}

/*
 * Put the file taken aside back under its name unless another object took
 * the name while it was aside. HFS supports hard links, but link + unlink is
 * not safe here: vfs_fruit removes a file's resource fork together with the
 * name it is unlinked by, which would strip it from the restored file.
 *
 * Known race, accepted: the check below and the rename are two calls, and
 * rename() replaces. An AFP or SSH writer that creates the name exactly
 * between them loses that new file to the original. NetBSD has no
 * renameat2(RENAME_NOREPLACE), so no atomic no-clobber rename exists here.
 * The window is two back-to-back syscalls in one process on a rare error
 * path; everything created while the name was aside is still caught by the
 * check. smbd runs without pthreads, gen 1-4 Time Capsules have a single
 * core and gen 5 has two, so another writer rarely gets to run in that gap.
 * Keeping the file aside instead would hide the client's file (the aside
 * name is vetoed) on every ordinary failure, so we live with the race.
 */
static void tc_restore_aside(struct connection_struct *conn,
			     struct files_struct *dirfsp,
			     struct smb_filename *aside,
			     struct smb_filename *atname)
{
	SMB_STRUCT_STAT sbuf;
	int ret;

	ret = SMB_VFS_FSTATAT(conn, dirfsp, atname, &sbuf, AT_SYMLINK_NOFOLLOW);
	if (ret == 0 || errno != ENOENT) {
		DBG_ERR("%s was taken while converting; leaving the original "
			"as %s\n",
			atname->base_name, aside->base_name);
		return;
	}
	ret = SMB_VFS_RENAMEAT(conn,
			       dirfsp,
			       aside,
			       dirfsp,
			       atname,
			       &(struct vfs_rename_how){ .flags = 0 });
	if (ret != 0) {
		DBG_ERR("restoring %s from %s failed: %s\n",
			atname->base_name, aside->base_name, strerror(errno));
	}
}

/*
 * Like Samba's own callers, read attribute lists and values into a growing
 * buffer: VFS modules above the store (vfs_acl_xattr) do not accept a NULL
 * buffer size query.
 */
static ssize_t tc_read_xattr(struct files_struct *fsp,
			     const char *name,
			     TALLOC_CTX *mem_ctx,
			     char **_buf)
{
	size_t size = 256;

	for (;;) {
		char *buf = talloc_zero_size(mem_ctx, size);
		ssize_t n;

		if (buf == NULL) {
			errno = ENOMEM;
			return -1;
		}
		n = (name == NULL) ?
			SMB_VFS_FLISTXATTR(fsp, buf, size) :
			SMB_VFS_FGETXATTR(fsp, name, buf, size);
		if (n >= 0) {
			*_buf = buf;
			return n;
		}
		TALLOC_FREE(buf);
		if (errno != ERANGE || size >= 1024 * 1024) {
			return -1;
		}
		size *= 4;
	}
}

/*
 * Is "name" streams_xattr's attribute for "stream": its prefix, the stream
 * name (matched without case, as stream names are), and ":$DATA" unless
 * stream types are not stored. Only exact names count: a client's own stream
 * may well contain these words.
 */
static bool tc_is_stream_xattr(const struct connection_struct *conn,
			       const char *name,
			       const char *stream)
{
	const char *prefix = lp_parm_const_string(SNUM(conn),
						  "streams_xattr",
						  "prefix",
						  SAMBA_XATTR_DOSSTREAM_PREFIX);
	size_t prefix_len = strlen(prefix);
	size_t stream_len = strlen(stream);

	if (strncmp(name, prefix, prefix_len) != 0) {
		return false;
	}
	name += prefix_len;
	if (strncasecmp_m(name, stream, stream_len) != 0) {
		return false;
	}
	name += stream_len;
	return name[0] == '\0' || strequal(name, ":$DATA");
}

/*
 * Attributes a link must not take from the file it replaces: the reparse
 * payload (the link replaces it), and Finder info and resource forks in each
 * of their stores (native HFS, netatalk, streams_xattr). HFS keeps a link's
 * own FinderInfo (type 'slnk', creator 'rhap') and lets it be overwritten; a
 * link cannot carry a resource fork. Anything else, whatever its name, is
 * copied.
 */
static bool tc_link_skips_xattr(const struct connection_struct *conn,
				const char *name)
{
	static const char *const reserved[] = {
		SAMBA_XATTR_REPARSE_ATTRIB,
		"com.apple.FinderInfo",
		"com.apple.ResourceFork",
		"org.netatalk.Metadata",
		"user.org.netatalk.Metadata",
		"org.netatalk.ResourceFork",
		"user.org.netatalk.ResourceFork",
	};
	size_t i;

	if (name[0] == '\0') {
		return true;
	}
	for (i = 0; i < ARRAY_SIZE(reserved); i++) {
		if (strcmp(name, reserved[i]) == 0) {
			return true;
		}
	}
	/* streams_xattr stores ":AFP_AfpInfo" without its leading colon */
	return tc_is_stream_xattr(conn, name, AFPINFO_STREAM_NAME + 1) ||
	       tc_is_stream_xattr(conn, name, AFPRESOURCE_STREAM_NAME + 1);
}

/*
 * Copy what a client stored on the created file (DOS attributes and create
 * time, NT ACL, provenance, named streams, WSL attributes) onto the new link,
 * through the whole VFS stack so every attribute lands in the store it
 * normally uses; tc_link_skips_xattr() lists what stays behind. Times are best
 * effort: HFS ignores them on links, as it does for AFP.
 */
static bool tc_copy_link_metadata(struct files_struct *from,
				  const SMB_STRUCT_STAT *from_st,
				  struct files_struct *to)
{
	struct smb_file_time ft = smb_file_time_omit();
	char *names = NULL;
	ssize_t size;
	ssize_t off;
	bool ok = false;

	size = tc_read_xattr(from, NULL, talloc_tos(), &names);
	if (size < 0) {
		DBG_NOTICE("listing attributes of %s failed: %s\n",
			   fsp_str_dbg(from), strerror(errno));
		return false;
	}
	for (off = 0; off < size; off += strlen(names + off) + 1) {
		const char *name = names + off;
		char *value = NULL;
		ssize_t vlen;
		int ret;

		if (tc_link_skips_xattr(from->conn, name)) {
			continue;
		}
		vlen = tc_read_xattr(from, name, names, &value);
		if (vlen < 0) {
			DBG_NOTICE("reading %s of %s failed: %s\n",
				   name, fsp_str_dbg(from), strerror(errno));
			goto out;
		}
		ret = SMB_VFS_FSETXATTR(to, name, value, vlen, 0);
		TALLOC_FREE(value);
		if (ret != 0) {
			DBG_NOTICE("copying %s to link %s failed: %s\n",
				   name, fsp_str_dbg(to), strerror(errno));
			goto out;
		}
	}

	ft.atime = from_st->st_ex_atime;
	ft.mtime = from_st->st_ex_mtime;
	if (SMB_VFS_FNTIMES(to, &ft) != 0 &&
	    errno != ENOSYS && errno != ENOTSUP && errno != EOPNOTSUPP) {
		DBG_NOTICE("setting times of link %s failed: %s\n",
			   fsp_str_dbg(to), strerror(errno));
		goto out;
	}
	ok = true;
out:
	TALLOC_FREE(names);
	return ok;
}

/*
 * Replace the created file with a native link of the same name. The caller
 * (close_remove_share_mode) holds the share mode lock, has checked with
 * tc_native_links_sole_open() that this is the only open, and has not closed
 * the fd yet.
 *
 * The name is never replaced blindly: whatever is there is renamed aside
 * first and checked to be this very file, still unchanged; the link is then
 * created with symlinkat(), which fails rather than replace an object that
 * appeared meanwhile. On any failure the original goes back, except when
 * another client has taken the name, whose object wins, or when the new
 * link can no longer be checked: then the original stays aside, where no
 * other object can be lost to it. For the microseconds between
 * the two steps the name does not exist: the creating client, whose
 * requests are handled one at a time, never sees that, other connections
 * may. An SMB open that walked to the file before the lock was
 * taken can still end up on the removed inode, as with delete on close.
 *
 * A file taken aside and no longer needed is left in cand->aside for
 * tc_native_links_close_finish(), which removes it after fd_close().
 */
bool tc_native_links_close_commit(struct files_struct *fsp,
				  struct tc_xsym_candidate *cand)
{
	struct connection_struct *conn = fsp->conn;
	struct smb_filename *parent = NULL;
	struct smb_filename *atname = NULL;
	struct smb_filename *aside = NULL;
	struct smb_filename *link = NULL;
	struct smb_filename target = { .base_name = cand->target };
	SMB_STRUCT_STAT fst, sbuf, created;
	char *now_target = NULL;
	char *tmp = NULL;
	mode_t old_umask;
	bool converted = false;
	bool same;
	NTSTATUS status;
	int ret;

	status = parent_pathref(talloc_tos(),
				conn->cwd_fsp,
				fsp->fsp_name,
				&parent,
				&atname);
	if (!NT_STATUS_IS_OK(status)) {
		DBG_NOTICE("parent_pathref(%s) failed: %s\n",
			   fsp_str_dbg(fsp), nt_errstr(status));
		goto out;
	}

	/* vfs_fruit's renameat requires a valid stat of the source. */
	ret = SMB_VFS_FSTATAT(conn, parent->fsp, atname, &atname->st,
			      AT_SYMLINK_NOFOLLOW);
	if (ret != 0 ||
	    atname->st.st_ex_dev != cand->dev ||
	    atname->st.st_ex_ino != cand->ino) {
		DBG_NOTICE("%s is no longer the created file, keeping it\n",
			   fsp_str_dbg(fsp));
		goto out;
	}

	tmp = talloc_asprintf(talloc_tos(),
			      ".tc-xsym.%llu.%lu",
			      (unsigned long long)cand->ino,
			      (unsigned long)getpid());
	if (tmp == NULL) {
		goto out;
	}
	aside = synthetic_smb_fname(talloc_tos(), tmp, NULL, NULL, 0, 0);
	if (aside == NULL) {
		goto out;
	}

	/* 1. Take whatever holds the name now. */
	ret = SMB_VFS_RENAMEAT(conn,
			       parent->fsp,
			       atname,
			       parent->fsp,
			       aside,
			       &(struct vfs_rename_how){ .flags = 0 });
	if (ret != 0) {
		DBG_NOTICE("moving %s aside failed: %s, keeping it\n",
			   fsp_str_dbg(fsp), strerror(errno));
		goto out;
	}

	/* 2. It must be this file, unchanged since close_prepare. */
	ret = SMB_VFS_FSTATAT(conn, parent->fsp, aside, &aside->st,
			      AT_SYMLINK_NOFOLLOW);
	same = ret == 0 &&
	       tc_candidate_target(talloc_tos(), fsp, tmp, &fst, &now_target) &&
	       aside->st.st_ex_dev == fst.st_ex_dev &&
	       aside->st.st_ex_ino == fst.st_ex_ino &&
	       fst.st_ex_dev == cand->dev &&
	       fst.st_ex_ino == cand->ino &&
	       fst.st_ex_size == cand->size &&
	       strcmp(now_target, cand->target) == 0;
	TALLOC_FREE(now_target);
	if (!same) {
		DBG_NOTICE("%s changed while converting, keeping it\n",
			   fsp_str_dbg(fsp));
		tc_restore_aside(conn, parent->fsp, aside, atname);
		goto out;
	}

	/*
	 * 3. Create the link; symlinkat() never replaces. smbd runs with
	 * umask 0, AFP and SSH create links as 0755, and NetBSD takes a
	 * link's mode from the umask.
	 */
	old_umask = umask(022);
	ret = SMB_VFS_SYMLINKAT(conn, &target, parent->fsp, atname);
	umask(old_umask);
	if (ret != 0 && errno == EEXIST) {
		/* A newer object took the name; it wins over this link. */
		DBG_NOTICE("%s was recreated while converting; dropping the "
			   "link it replaced\n",
			   fsp_str_dbg(fsp));
		cand->aside = talloc_strdup(fsp->fsp_name, tmp);
		goto out;
	}
	if (ret != 0) {
		DBG_NOTICE("symlink for %s failed: %s, keeping it\n",
			   fsp_str_dbg(fsp), strerror(errno));
		tc_restore_aside(conn, parent->fsp, aside, atname);
		goto out;
	}

	/* 4. Carry the file's metadata over, or undo. */
	ret = SMB_VFS_FSTATAT(conn, parent->fsp, atname, &created,
			      AT_SYMLINK_NOFOLLOW);
	if (ret != 0 && errno != ENOENT) {
		int err = errno;

		/*
		 * An error such as EIO says nothing about the link: removing
		 * whatever holds the name, or renaming the original back over
		 * it, could destroy another object, and removing the original
		 * would lose the metadata not yet copied. Keep the original
		 * aside instead, as when removing it fails in close_finish().
		 * The link was created, so commit it as in step 5.
		 */
		DBG_ERR("checking the link for %s failed: %s; leaving the "
			"original as %s\n",
			fsp_str_dbg(fsp), strerror(err), tmp);
		sync();
		goto out;
	}
	if (ret != 0 || !S_ISLNK(created.st_ex_mode)) {
		DBG_NOTICE("the link for %s vanished while converting\n",
			   fsp_str_dbg(fsp));
		cand->aside = talloc_strdup(fsp->fsp_name, tmp);
		goto out;
	}
	link = synthetic_smb_fname(talloc_tos(),
				   atname->base_name,
				   NULL,
				   NULL,
				   atname->twrp,
				   atname->flags);
	if (link != NULL) {
		status = openat_pathref_fsp_lcomp(parent->fsp,
						  link,
						  UCF_LCOMP_LNK_OK);
	}
	if (link == NULL ||
	    !NT_STATUS_IS_OK(status) ||
	    link->fsp == NULL ||
	    link->st.st_ex_ino != created.st_ex_ino ||
	    !tc_copy_link_metadata(fsp, &fst, link->fsp)) {
		DBG_NOTICE("carrying metadata of %s to its link failed, "
			   "keeping it\n",
			   fsp_str_dbg(fsp));
		TALLOC_FREE(link);
		/*
		 * The link is new and set up: commit it before removing it.
		 * Commit first, so no flush sits between the check below and
		 * the unlink it guards.
		 */
		sync();
		ret = SMB_VFS_FSTATAT(conn, parent->fsp, atname, &sbuf,
				      AT_SYMLINK_NOFOLLOW);
		if (ret == 0 && S_ISLNK(sbuf.st_ex_mode) &&
		    sbuf.st_ex_dev == created.st_ex_dev &&
		    sbuf.st_ex_ino == created.st_ex_ino) {
			atname->st = sbuf;
			SMB_VFS_UNLINKAT(conn, parent->fsp, atname, 0);
			tc_restore_aside(conn, parent->fsp, aside, atname);
		} else if (ret == 0 || errno == ENOENT) {
			/* Another client replaced or removed the link, as in step 4. */
			DBG_NOTICE("the link for %s was taken while undoing\n",
				   fsp_str_dbg(fsp));
			cand->aside = talloc_strdup(fsp->fsp_name, tmp);
		} else {
			DBG_ERR("checking the link for %s failed: %s; leaving "
				"the original as %s\n",
				fsp_str_dbg(fsp), strerror(errno), tmp);
		}
		goto out;
	}
	TALLOC_FREE(link);

	/*
	 * 5. The original is removed once its fd is closed, in
	 * tc_native_links_close_finish(), so no deleted file stays open.
	 *
	 * Commit the HFS journal now. Without it, completed conversions left
	 * the devices' HFS driver ready to panic: a later journal transaction,
	 * in whichever process ended it, released a vnode whose update opened
	 * a nested transaction ("jnl: start_tr: active_tr is NULL"). The
	 * trigger was only narrowed to a completed conversion; the syncs here,
	 * in the rollback above and in close_finish() are what stopped it.
	 * Links are created rarely, so the cost is acceptable.
	 */
	cand->aside = talloc_strdup(fsp->fsp_name, tmp);
	sync();

	converted = true;
	DBG_INFO("converted %s to a native symlink\n", fsp_str_dbg(fsp));
	notify_fname(conn,
		     NOTIFY_ACTION_MODIFIED,
		     FILE_NOTIFY_CHANGE_ATTRIBUTES,
		     fsp->fsp_name,
		     NULL);
out:
	TALLOC_FREE(link);
	TALLOC_FREE(aside);
	TALLOC_FREE(tmp);
	TALLOC_FREE(parent);
	TALLOC_FREE(cand->target);
	return converted;
}

/*
 * After fd_close(): remove the original a conversion took aside, now that no
 * descriptor holds it. It still has its private name; check that it is the
 * same file before unlinking it through the VFS, which also drops any
 * sidecar data kept for it.
 */
void tc_native_links_close_finish(struct files_struct *fsp,
				  struct tc_xsym_candidate *cand)
{
	struct connection_struct *conn = fsp->conn;
	struct smb_filename *parent = NULL;
	struct smb_filename *atname = NULL;
	struct smb_filename *aside = NULL;
	NTSTATUS status;
	int ret;

	if (cand->aside == NULL) {
		return;
	}
	status = parent_pathref(talloc_tos(),
				conn->cwd_fsp,
				fsp->fsp_name,
				&parent,
				&atname);
	if (!NT_STATUS_IS_OK(status)) {
		DBG_ERR("parent_pathref(%s) failed: %s; %s is left behind\n",
			fsp_str_dbg(fsp), nt_errstr(status), cand->aside);
		goto out;
	}
	aside = synthetic_smb_fname(talloc_tos(), cand->aside, NULL, NULL, 0, 0);
	if (aside == NULL) {
		goto out;
	}
	ret = SMB_VFS_FSTATAT(conn, parent->fsp, aside, &aside->st,
			      AT_SYMLINK_NOFOLLOW);
	if (ret != 0 || !S_ISREG(aside->st.st_ex_mode) ||
	    aside->st.st_ex_dev != cand->dev ||
	    aside->st.st_ex_ino != cand->ino) {
		DBG_ERR("%s is no longer the file converted from %s; leaving "
			"it\n",
			cand->aside, fsp_str_dbg(fsp));
		goto out;
	}
	ret = SMB_VFS_UNLINKAT(conn, parent->fsp, aside, 0);
	if (ret != 0) {
		DBG_ERR("removing %s after converting %s failed: %s\n",
			cand->aside, fsp_str_dbg(fsp), strerror(errno));
	}
	/* See step 5 of tc_native_links_close_commit(). */
	sync();
out:
	TALLOC_FREE(aside);
	TALLOC_FREE(parent);
	TALLOC_FREE(cand->aside);
}

/*
 * Named streams on a native link hold the link's own attributes, as Apple's
 * AFP server stores them. create_file_unixpath() re-resolves a stream's base
 * with openat_pathref_fsp(), which follows the link; open the link itself
 * instead. Returns NT_STATUS_NOT_A_REPARSE_POINT when the base is not a
 * native link (or the feature is off) so the caller keeps its normal path.
 */
NTSTATUS tc_native_links_stream_base(struct connection_struct *conn,
				     struct files_struct *dirfsp,
				     struct smb_filename *smb_fname_base)
{
	struct smb_filename *rel = NULL;
	const char *last = NULL;
	NTSTATUS status;

	if (!tc_native_links_enabled(conn)) {
		return NT_STATUS_NOT_A_REPARSE_POINT;
	}
	if (SMB_VFS_LSTAT(conn, smb_fname_base) != 0 ||
	    !S_ISLNK(smb_fname_base->st.st_ex_mode)) {
		SET_STAT_INVALID(smb_fname_base->st);
		return NT_STATUS_NOT_A_REPARSE_POINT;
	}
	if (dirfsp == NULL || dirfsp == conn->cwd_fsp) {
		/* Never fall back to following the link. */
		return NT_STATUS_NOT_SUPPORTED;
	}

	last = strrchr_m(smb_fname_base->base_name, '/');
	last = (last == NULL) ? smb_fname_base->base_name : last + 1;
	rel = synthetic_smb_fname(talloc_tos(),
				  last,
				  NULL,
				  NULL,
				  smb_fname_base->twrp,
				  smb_fname_base->flags);
	if (rel == NULL) {
		return NT_STATUS_NO_MEMORY;
	}
	status = openat_pathref_fsp_lcomp(dirfsp, rel, UCF_LCOMP_LNK_OK);
	if (NT_STATUS_IS_OK(status) && !S_ISLNK(rel->st.st_ex_mode)) {
		/* Replaced between the lstat and the open. */
		status = NT_STATUS_OBJECT_NAME_NOT_FOUND;
	}
	if (NT_STATUS_IS_OK(status)) {
		smb_fname_base->st = rel->st;
		status = move_smb_fname_fsp_link(smb_fname_base, rel);
	}
	TALLOC_FREE(rel);
	return status;
}

/*
 * Attributes of a native link opened as itself. It is always a symlink
 * reparse point, even if a client stored other DOS attributes on it. Windows
 * only traverses a directory symlink that also carries
 * FILE_ATTRIBUTE_DIRECTORY, so non-Mac clients see that bit when the target
 * is a directory. macOS decides by the reparse tag and gets no extra bit.
 */
uint32_t tc_native_links_dos_mode(struct files_struct *fsp, uint32_t dosmode)
{
	struct smb_filename *target = NULL;

	if (!S_ISLNK(fsp->fsp_name->st.st_ex_mode) ||
	    !tc_native_links_enabled(fsp->conn)) {
		return dosmode;
	}
	dosmode |= FILE_ATTRIBUTE_REPARSE_POINT;
	dosmode &= ~(FILE_ATTRIBUTE_NORMAL | FILE_ATTRIBUTE_DIRECTORY);

	if (tc_client_resolves_links(fsp->conn) ||
	    fsp->fsp_flags.posix_open) {
		return dosmode;
	}
	target = cp_smb_filename_nostream(talloc_tos(), fsp->fsp_name);
	if (target == NULL) {
		return dosmode;
	}
	if (SMB_VFS_STAT(fsp->conn, target) == 0 &&
	    S_ISDIR(target->st.st_ex_mode)) {
		dosmode |= FILE_ATTRIBUTE_DIRECTORY;
	}
	TALLOC_FREE(target);
	return dosmode;
}
