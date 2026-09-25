/* Execute the real Time Capsule native-link helpers (source3/smbd/tc_native_links.c)
 * against a real directory. Samba's VFS indirection, the share-mode table, xattr storage
 * and change notification are replaced; symlink, rename, unlink, readlink and stat are
 * real syscalls. Hooks inside the replaced VFS calls let other "clients" act between the
 * conversion's steps. */
#include "includes.h"
#include "system/filesys.h"
#include "smbd/smbd.h"
#include "smbd/globals.h"
#include "locking/share_mode_lock.h"
#include "lib/util/sys_rw.h"
#include "librpc/gen_ndr/smbXsrv.h"
#include "librpc/gen_ndr/open_files.h"
#include "librpc/gen_ndr/ndr_open_files.h"
#include "lib/util/server_id.h"
#include "messages.h"
#include "MacExtensions.h"
#include "libcli/smb/reparse.h"

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: %s errno=%d\n", __FILE__, __LINE__, #x, errno); exit(90); } } while (0)

static bool feature_enabled = true;
/* SMB_VFS_TRANSLATE_NAME: a catia module mapping ':' and '\\' is loaded, or none is. */
static bool name_mapping = true;
static NTSTATUS fail_translate;
static unsigned translate_calls;
static int fail_symlink, fail_rename, fail_readlink, fail_setxattr, fail_ntimes, fail_unlink_aside;
static unsigned notifies;
/* sync() commits the HFS journal after a conversion; see tc_native_links_close_commit(). */
static unsigned syncs;
static unsigned ntimes_calls;
static struct timespec ntimes_mtime;
/* The SET_REPARSE_POINT payload upstream stores on a placeholder. */
static uint8_t stored_reparse[512];
static size_t stored_reparse_len;
static off_t resource_fork_size;
static char workdir[PATH_MAX];
/* Something another client does while a conversion is between two steps. */
static void (*before_grab)(void);
static void (*after_grab)(void);
/* Names read through an internal pathref because the creating handle was write-only. */
static char pathref_reads[4][64];
static size_t num_pathref_reads;

static void to_stat_ex(const struct stat *st, SMB_STRUCT_STAT *out)
{
	ZERO_STRUCTP(out);
	out->st_ex_dev = st->st_dev;
	out->st_ex_ino = st->st_ino;
	out->st_ex_mode = st->st_mode;
	out->st_ex_nlink = st->st_nlink;
	out->st_ex_size = st->st_size;
	out->st_ex_atime.tv_sec = st->st_atime;
	out->st_ex_mtime.tv_sec = st->st_mtime;
}

static int test_fstat(struct files_struct *fsp, SMB_STRUCT_STAT *out)
{
	struct stat st;
	if (fstat(fsp_get_io_fd(fsp), &st) != 0) return -1;
	to_stat_ex(&st, out);
	return 0;
}

static int test_fstatat(const struct smb_filename *name, SMB_STRUCT_STAT *out)
{
	struct stat st;
	if (lstat(name->base_name, &st) != 0) return -1;
	to_stat_ex(&st, out);
	return 0;
}

static int test_symlinkat(const struct smb_filename *target, const struct smb_filename *name)
{
	if (fail_symlink) { errno = fail_symlink; return -1; }
	return symlink(target->base_name, name->base_name);
}

static bool is_aside(const char *name)
{
	return strncmp(name, ".tc-xsym.", 9) == 0;
}

static int test_renameat(const struct smb_filename *from, const struct smb_filename *to)
{
	int ret;
	CHECK(VALID_STAT(from->st)); /* vfs_fruit contract */
	if (is_aside(to->base_name) && before_grab != NULL) before_grab();
	if (fail_rename) { errno = fail_rename; return -1; }
	ret = rename(from->base_name, to->base_name);
	if (ret == 0 && is_aside(to->base_name) && after_grab != NULL) after_grab();
	return ret;
}

static int test_unlinkat(const struct smb_filename *name)
{
	if (fail_unlink_aside && is_aside(name->base_name)) { errno = fail_unlink_aside; return -1; }
	return unlink(name->base_name);
}

static NTSTATUS test_parent_pathref(TALLOC_CTX *ctx, struct files_struct *dirfsp,
	const struct smb_filename *name, struct smb_filename **parent, struct smb_filename **atname)
{
	(void)dirfsp;
	*parent = talloc_zero(ctx, struct smb_filename);
	CHECK(*parent != NULL);
	(*parent)->fsp = talloc_zero(*parent, struct files_struct);
	*atname = synthetic_smb_fname(*parent, name->base_name, NULL, NULL, 0, 0);
	CHECK(*atname != NULL);
	return NT_STATUS_OK;
}

/* A link opened as itself: no fd, its own lstat. */
static NTSTATUS test_openat_pathref_lcomp(struct files_struct *dirfsp, struct smb_filename *name,
	uint32_t ucf_flags)
{
	struct files_struct *fsp = NULL;
	(void)dirfsp;
	CHECK(ucf_flags == UCF_LCOMP_LNK_OK);
	if (test_fstatat(name, &name->st) != 0) return map_nt_error_from_unix(errno);
	fsp = talloc_zero(name, struct files_struct);
	CHECK(fsp != NULL);
	fsp->fh = fd_handle_create(fsp);
	CHECK(fsp->fh != NULL);
	fsp_set_fd(fsp, -1);
	fsp->fsp_name = name;
	name->fsp = fsp;
	return NT_STATUS_OK;
}

static int close_pathref(struct files_struct *fsp)
{
	if (fsp_get_pathref_fd(fsp) != -1) close(fsp_get_pathref_fd(fsp));
	fsp_set_fd(fsp, -1); /* fd_handle's destructor insists */
	return 0;
}

/* An internal pathref without O_PATH: an O_RDONLY fd, whatever the client's access. */
static NTSTATUS test_openat_pathref(const struct files_struct *dirfsp, struct smb_filename *name)
{
	struct files_struct *fsp = NULL;
	(void)dirfsp;
	CHECK(name->fsp == NULL && strchr(name->base_name, '/') == NULL);
	if (test_fstatat(name, &name->st) != 0) return map_nt_error_from_unix(errno);
	fsp = talloc_zero(name, struct files_struct);
	CHECK(fsp != NULL);
	fsp->fh = fd_handle_create(fsp);
	CHECK(fsp->fh != NULL);
	fsp->fsp_flags.is_pathref = true;
	fsp_set_fd(fsp, open(name->base_name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK));
	CHECK(fsp_get_pathref_fd(fsp) != -1);
	talloc_set_destructor(fsp, close_pathref);
	fsp->fsp_name = name;
	name->fsp = fsp;
	CHECK(num_pathref_reads < ARRAY_SIZE(pathref_reads));
	strlcpy(pathref_reads[num_pathref_reads++], name->base_name, sizeof(pathref_reads[0]));
	return NT_STATUS_OK;
}

static NTSTATUS test_readlink(TALLOC_CTX *ctx, struct files_struct *dirfsp,
	struct smb_filename *name, char **out)
{
	char buf[PATH_MAX];
	ssize_t n;
	(void)dirfsp;
	if (fail_readlink) return NT_STATUS_ACCESS_DENIED;
	n = readlink(name->base_name, buf, sizeof(buf));
	if (n < 0) return map_nt_error_from_unix(errno);
	*out = talloc_strndup(ctx, buf, n);
	return NT_STATUS_OK;
}

static int test_stat(struct smb_filename *name)
{
	struct stat st;
	if (stat(name->base_name, &st) != 0) return -1;
	to_stat_ex(&st, &name->st);
	return 0;
}

static uint32_t test_fdos_mode(struct files_struct *fsp)
{
	(void)fsp;
	return stored_reparse_len ? FILE_ATTRIBUTE_REPARSE_POINT : FILE_ATTRIBUTE_ARCHIVE;
}

static NTSTATUS test_get_reparse_point(struct files_struct *fsp, TALLOC_CTX *ctx,
	uint32_t *tag, uint8_t **data, uint32_t max_len, uint32_t *len)
{
	(void)fsp; (void)max_len;
	if (stored_reparse_len == 0) return NT_STATUS_NOT_A_REPARSE_POINT;
	*tag = PULL_LE_U32(stored_reparse, 0);
	*data = talloc_memdup(ctx, stored_reparse, stored_reparse_len);
	*len = stored_reparse_len;
	return NT_STATUS_OK;
}

static NTSTATUS test_fstreaminfo(struct files_struct *fsp, TALLOC_CTX *ctx,
	unsigned int *num, struct stream_struct **streams)
{
	(void)fsp;
	*num = 0;
	*streams = NULL;
	if (resource_fork_size == 0) return NT_STATUS_OK;
	*streams = talloc_zero_array(ctx, struct stream_struct, 1);
	(*streams)[0].name = talloc_strdup(*streams, ":AFP_Resource:$DATA");
	(*streams)[0].size = resource_fork_size;
	*num = 1;
	return NT_STATUS_OK;
}

/* xattrs, keyed by inode like a real store: the created file's and the link's are distinct. */
static struct { bool used; SMB_INO_T ino; char name[64]; uint8_t value[600]; size_t len; } xattrs[64];

static SMB_INO_T ino_of(struct files_struct *fsp)
{
	SMB_STRUCT_STAT st;
	if (fsp_get_pathref_fd(fsp) != -1) {
		CHECK(test_fstat(fsp, &st) == 0);
		return st.st_ex_ino;
	}
	CHECK(S_ISLNK(fsp->fsp_name->st.st_ex_mode));
	return fsp->fsp_name->st.st_ex_ino;
}

static int xfind(SMB_INO_T ino, const char *name)
{
	size_t i;
	for (i = 0; i < ARRAY_SIZE(xattrs); i++)
		if (xattrs[i].used && xattrs[i].ino == ino && strcmp(xattrs[i].name, name) == 0) return i;
	return -1;
}

static void xput(SMB_INO_T ino, const char *name, const void *value, size_t len)
{
	int i = xfind(ino, name);
	if (i < 0) for (i = 0; xattrs[i].used; i++) CHECK(i + 1 < (int)ARRAY_SIZE(xattrs));
	CHECK(len <= sizeof(xattrs[i].value) && strlen(name) < sizeof(xattrs[i].name));
	xattrs[i].used = true;
	xattrs[i].ino = ino;
	strlcpy(xattrs[i].name, name, sizeof(xattrs[i].name));
	memcpy(xattrs[i].value, value, len);
	xattrs[i].len = len;
}

/* vfs_acl_xattr walks the returned list, so a NULL size query crashes smbd: never allowed. */
static ssize_t test_flistxattr(struct files_struct *fsp, char *list, size_t size)
{
	SMB_INO_T ino = ino_of(fsp);
	size_t i, need = 0;
	CHECK(list != NULL && size > 0);
	for (i = 0; i < ARRAY_SIZE(xattrs); i++) {
		if (!xattrs[i].used || xattrs[i].ino != ino) continue;
		need += strlen(xattrs[i].name) + 1;
	}
	if (need > size) { errno = ERANGE; return -1; }
	for (i = 0, need = 0; i < ARRAY_SIZE(xattrs); i++) {
		if (!xattrs[i].used || xattrs[i].ino != ino) continue;
		memcpy(list + need, xattrs[i].name, strlen(xattrs[i].name) + 1);
		need += strlen(xattrs[i].name) + 1;
	}
	return need;
}

static ssize_t test_fgetxattr(struct files_struct *fsp, const char *name, void *value, size_t size)
{
	int i = xfind(ino_of(fsp), name);
	CHECK(value != NULL && size > 0);
	if (i < 0) { errno = ENOATTR; return -1; }
	if (size < xattrs[i].len) { errno = ERANGE; return -1; }
	memcpy(value, xattrs[i].value, xattrs[i].len);
	return xattrs[i].len;
}

static int test_fsetxattr(struct files_struct *fsp, const char *name, const void *value, size_t size,
	int flags)
{
	CHECK(flags == 0);
	if (fail_setxattr) { errno = fail_setxattr; return -1; }
	xput(ino_of(fsp), name, value, size);
	return 0;
}

static int test_fntimes(struct files_struct *fsp, struct smb_file_time *ft)
{
	CHECK(S_ISLNK(fsp->fsp_name->st.st_ex_mode)); /* only the new link gets times */
	ntimes_calls++;
	ntimes_mtime = ft->mtime;
	if (fail_ntimes) { errno = fail_ntimes; return -1; }
	return 0;
}

/* What catia does with fruit:encoding = native, for two of its characters: the client's
 * U+F022 and U+F026 (UTF-8 encoded) are ':' and '\\' on disk. '/' is never mapped. */
static const struct { const char *wire, *disk; } name_map[] = {
	{ "\xef\x80\xa2", ":" }, { "\xef\x80\xa6", "\\" },
};

static NTSTATUS test_translate_name(const char *name, enum vfs_translate_direction direction,
	TALLOC_CTX *ctx, char **mapped)
{
	char *out = NULL;
	size_t i;
	translate_calls++;
	if (!NT_STATUS_IS_OK(fail_translate)) return fail_translate;
	if (!name_mapping) return NT_STATUS_NONE_MAPPED; /* vfs_default: nothing to map */
	out = talloc_strdup(ctx, "");
	while (*name != '\0') {
		const char *from = NULL, *to = NULL;
		for (i = 0; i < ARRAY_SIZE(name_map) && from == NULL; i++) {
			from = direction == vfs_translate_to_unix ? name_map[i].wire : name_map[i].disk;
			to = direction == vfs_translate_to_unix ? name_map[i].disk : name_map[i].wire;
			if (strncmp(name, from, strlen(from)) != 0) from = NULL;
		}
		if (from != NULL) {
			out = talloc_strdup_append(out, to);
			name += strlen(from);
		} else {
			out = talloc_asprintf_append(out, "%c", *name++);
		}
		CHECK(out != NULL);
	}
	*mapped = out;
	return NT_STATUS_OK;
}

/* The share-mode table as close_share_mode_lock_prepare() sees it. */
static struct share_mode_entry entries[4];
static size_t num_entries;
static const struct server_id self = { .pid = 4242, .unique_id = 7 };

static bool test_forall_entries(struct share_mode_lock *lck,
	bool (*fn)(struct share_mode_entry *e, bool *modified, void *private_data), void *private_data)
{
	size_t i;
	bool modified = false;
	(void)lck;
	for (i = 0; i < num_entries; i++) {
		if (fn(&entries[i], &modified, private_data)) break;
	}
	return true;
}

static struct server_id test_messaging_server_id(const struct messaging_context *msg_ctx)
{
	(void)msg_ctx;
	return self;
}

static bool test_stale(struct share_mode_entry *e)
{
	return e->stale;
}

#define lp_parm_bool(snum, type, option, def) (feature_enabled)
#define lp_parm_const_string(snum, type, option, def) (def)
#define openat_pathref_fsp test_openat_pathref
#define parent_pathref test_parent_pathref
#define readlink_talloc test_readlink
#define openat_pathref_fsp_lcomp test_openat_pathref_lcomp
#define notify_fname(conn, action, filter, name, lease) (notifies++)
#define sync() (syncs++)
#define share_mode_forall_entries test_forall_entries
#define messaging_server_id test_messaging_server_id
#define share_entry_stale_pid test_stale
#define vfs_fstreaminfo test_fstreaminfo
#undef SMB_VFS_FSTAT
#undef SMB_VFS_PREAD
#undef SMB_VFS_FSTATAT
#undef SMB_VFS_SYMLINKAT
#undef SMB_VFS_RENAMEAT
#undef SMB_VFS_UNLINKAT
#undef SMB_VFS_STAT
#undef SMB_VFS_FLISTXATTR
#undef SMB_VFS_FGETXATTR
#undef SMB_VFS_FSETXATTR
#undef SMB_VFS_FNTIMES
#undef SMB_VFS_TRANSLATE_NAME
#define SMB_VFS_FSTAT(fsp, st) test_fstat(fsp, st)
#define SMB_VFS_PREAD(fsp, data, n, off) pread(fsp_get_io_fd(fsp), data, n, off)
#define SMB_VFS_FSTATAT(conn, dirfsp, name, st, flags) test_fstatat(name, st)
#define SMB_VFS_SYMLINKAT(conn, target, dirfsp, name) test_symlinkat(target, name)
#define SMB_VFS_RENAMEAT(conn, sd, from, dd, to, how) test_renameat(from, to)
#define SMB_VFS_UNLINKAT(conn, dirfsp, name, flags) test_unlinkat(name)
#define SMB_VFS_STAT(conn, name) test_stat(name)
#define SMB_VFS_FLISTXATTR(fsp, list, size) test_flistxattr(fsp, list, size)
#define SMB_VFS_FGETXATTR(fsp, name, value, size) test_fgetxattr(fsp, name, value, size)
#define SMB_VFS_FSETXATTR(fsp, name, value, size, flags) test_fsetxattr(fsp, name, value, size, flags)
#define SMB_VFS_FNTIMES(fsp, ft) test_fntimes(fsp, ft)
#define SMB_VFS_TRANSLATE_NAME(conn, name, dir, ctx, out) test_translate_name(name, dir, ctx, out)
#define fdos_mode test_fdos_mode
#define fsctl_get_reparse_point test_get_reparse_point
/* smbd_base links the production copy; exercise this one under distinct names. */
#define tc_native_links_enabled t_native_links_enabled
#define tc_client_resolves_links t_client_resolves_links
#define tc_xsym_parse t_xsym_parse
#define tc_xsym_format t_xsym_format
#define tc_native_links_read_xsym t_read_xsym
#define tc_native_links_write_xsym t_write_xsym
#define tc_native_links_close_prepare t_close_prepare
#define tc_native_links_sole_open t_sole_open
#define tc_native_links_close_commit t_close_commit
#define tc_native_links_close_finish t_close_finish
#define tc_native_links_check_set t_check_set
#define tc_native_links_fs_capabilities t_fs_capabilities
#define tc_native_links_dos_mode t_dos_mode
#define tc_native_links_stream_base t_stream_base
#define tc_native_links_map_target t_map_target
#include "smbd/tc_native_links.c"

/* Digests the macOS client wrote on a Time Capsule share (2026-09-23 captures). */
static const struct { const char *target, *md5; } apple_vectors[] = {
	{ "t.txt", "8454272a4276bc0a45ff46f8bf22a15f" },
	{ "../nowhere", "722d95a43646533268865ae62a41059c" },
};

static void apple_body(const char *target, const char *md5, uint8_t out[TC_XSYM_FILE_SIZE])
{
	int n = snprintf((char *)out, TC_XSYM_FILE_SIZE, "XSym\n%04zu\n%s\n%s\n", strlen(target), md5, target);
	memset(out + n, ' ', TC_XSYM_FILE_SIZE - n);
}

/* A symlink reparse payload as Windows CreateSymbolicLinkW and Linux symlink=native send it. */
static size_t symlink_payload(const char *target, bool relative, uint8_t *out, size_t outlen)
{
	struct reparse_data_buffer buf = {
		.tag = IO_REPARSE_TAG_SYMLINK,
		.parsed.lnk = {
			.substitute_name = discard_const_p(char, target),
			.print_name = discard_const_p(char, target),
			.flags = relative ? SYMLINK_FLAG_RELATIVE : 0,
		},
	};
	ssize_t n = reparse_data_buffer_marshall(&buf, out, outlen);
	CHECK(n > 0 && (size_t)n <= outlen);
	return n;
}

/* Linux reparse=nfs (mknod) and symlink=nfs payloads. */
static size_t nfs_payload(uint64_t type, const char *target, uint8_t *out, size_t outlen)
{
	struct reparse_data_buffer buf = {
		.tag = IO_REPARSE_TAG_NFS,
		.parsed.nfs = { .type = type, .data.lnk_target = discard_const_p(char, target) },
	};
	ssize_t n = reparse_data_buffer_marshall(&buf, out, outlen);
	CHECK(n > 0 && (size_t)n <= outlen);
	return n;
}

/* Linux symlink=wsl: LX_SYMLINK, version 2, UTF-8 target without a NUL. */
static size_t lx_payload(uint32_t version, const void *target, size_t len, uint8_t *out)
{
	PUSH_LE_U32(out, 0, TC_IO_REPARSE_TAG_LX_SYMLINK);
	PUSH_LE_U16(out, 4, 4 + len);
	PUSH_LE_U16(out, 6, 0);
	PUSH_LE_U32(out, 8, version);
	memcpy(out + 12, target, len);
	return 12 + len;
}

/* A payload-less tag: AF_UNIX sockets, WSL FIFOs, junction stubs, unknown tags. */
static size_t bare_payload(uint32_t tag, uint8_t *out)
{
	PUSH_LE_U32(out, 0, tag);
	PUSH_LE_U16(out, 4, 0);
	PUSH_LE_U16(out, 6, 0);
	return 8;
}

static void put_file(const char *name, const void *data, size_t len)
{
	int fd = open(name, O_CREAT | O_TRUNC | O_WRONLY, 0644);
	CHECK(fd >= 0 && write(fd, data, len) == (ssize_t)len && close(fd) == 0);
}

static bool has_content(const char *name, const void *data, size_t len)
{
	uint8_t buf[TC_XSYM_FILE_SIZE + 1];
	int fd = open(name, O_RDONLY | O_NOFOLLOW);
	ssize_t n;
	if (fd < 0) return false;
	n = read(fd, buf, sizeof(buf));
	close(fd);
	return n == (ssize_t)len && memcmp(buf, data, len) == 0;
}

struct handle {
	struct smbd_server_connection sconn;
	struct connection_struct conn;
	struct smbXsrv_open_global global;
	struct smbXsrv_open op;
	struct files_struct fsp;
};

static void open_handle(TALLOC_CTX *ctx, struct handle *h, const char *name, uint32_t action, int flags)
{
	ZERO_STRUCTP(h);
	h->conn.sconn = &h->sconn;
	h->global.create_action = action;
	h->op.global = &h->global;
	h->fsp.op = &h->op;
	h->fsp.conn = &h->conn;
	h->fsp.fh = fd_handle_create(ctx);
	CHECK(h->fsp.fh != NULL);
	fh_set_gen_id(h->fsp.fh, 99);
	fsp_set_fd(&h->fsp, open(name, flags));
	CHECK(fsp_get_io_fd(&h->fsp) >= 0);
	h->fsp.fsp_name = synthetic_smb_fname(ctx, name, NULL, NULL, 0, 0);
	CHECK(h->fsp.fsp_name != NULL);
}

static void close_handle(struct handle *h)
{
	close(fsp_get_io_fd(&h->fsp));
	fsp_set_fd(&h->fsp, -1);
}

static bool is_link_to(const char *name, const char *target)
{
	char buf[PATH_MAX];
	ssize_t n = readlink(name, buf, sizeof(buf) - 1);
	if (n < 0) return false;
	buf[n] = 0;
	return strcmp(buf, target) == 0;
}

static bool is_regular(const char *name)
{
	struct stat st;
	return lstat(name, &st) == 0 && S_ISREG(st.st_mode);
}

static bool no_temp_left(void)
{
	DIR *d = opendir(".");
	struct dirent *e;
	bool ok = true;
	CHECK(d != NULL);
	while ((e = readdir(d)) != NULL) {
		if (is_aside(e->d_name) || strncmp(e->d_name, ".tc-symlink.", 12) == 0) ok = false;
	}
	closedir(d);
	return ok;
}

static void reset_hooks(void)
{
	before_grab = after_grab = NULL;
	fail_symlink = fail_rename = fail_readlink = fail_setxattr = fail_ntimes = fail_unlink_aside = 0;
	resource_fork_size = 0;
	stored_reparse_len = 0;
	name_mapping = true;
	fail_translate = NT_STATUS_OK;
	translate_calls = 0;
	notifies = ntimes_calls = syncs = 0;
	num_pathref_reads = 0;
	ZERO_STRUCT(xattrs);
}

/* close_normal_file() order: prepare with the fd open; close_remove_share_mode() commits
 * under the share mode lock, still before fd_close(); the original is removed after it.
 * Returns whether it converted. */
static bool convert_as(TALLOC_CTX *ctx, const char *name, uint32_t action, enum file_close_type type,
	int flags)
{
	struct handle h;
	struct tc_xsym_candidate cand;
	bool converted = false;
	open_handle(ctx, &h, name, action, flags);
	if (t_close_prepare(ctx, &h.fsp, type, &cand)) converted = t_close_commit(&h.fsp, &cand);
	TALLOC_FREE(cand.target);
	close_handle(&h);
	t_close_finish(&h.fsp, &cand);
	return converted;
}

static bool convert(TALLOC_CTX *ctx, const char *name, uint32_t action, enum file_close_type type)
{
	return convert_as(ctx, name, action, type, O_RDWR);
}

/* Race actors, run from inside the conversion's rename. */
static void replace_name(void)
{
	/* Another client renames the created file away and puts a new file there. */
	CHECK(rename("raced", "raced.moved") == 0);
	put_file("raced", "newer", 5);
}

static void create_in_gap(void)
{
	put_file("gap", "newer", 5);
}

int main(int argc, char **argv)
{
	TALLOC_CTX *frame = talloc_stackframe();
	uint8_t body[TC_XSYM_FILE_SIZE], made[TC_XSYM_FILE_SIZE];
	char *target = NULL;
	size_t i;
	const char *c;
	bool all;

	CHECK(argc == 2);
	all = strcmp(argv[1], "all") == 0;
	c = argv[1];
	CHECK(snprintf(workdir, sizeof(workdir), "%s/tc-native-links.XXXXXX", getenv("TMPDIR") ? getenv("TMPDIR") : ".") > 0);
	CHECK(mkdtemp(workdir) != NULL && chdir(workdir) == 0);

	if (all || strcmp(c, "apple_format") == 0) {
		/* Byte-for-byte what macOS writes, and parsed back to the same target. */
		for (i = 0; i < ARRAY_SIZE(apple_vectors); i++) {
			apple_body(apple_vectors[i].target, apple_vectors[i].md5, body);
			CHECK(t_xsym_format(apple_vectors[i].target, made));
			CHECK(memcmp(body, made, sizeof(body)) == 0);
			CHECK(t_xsym_parse(frame, body, sizeof(body), &target));
			CHECK(strcmp(target, apple_vectors[i].target) == 0);
		}
	}
	if (all || strcmp(c, "format_limits") == 0) {
		char longest[TC_XSYM_FILE_SIZE - 43 + 2];
		memset(longest, 'a', sizeof(longest));
		longest[TC_XSYM_FILE_SIZE - 43] = 0; /* 1024 bytes: no room for newline or padding */
		CHECK(t_xsym_format(longest, made));
		CHECK(made[TC_XSYM_FILE_SIZE - 1] == 'a');
		CHECK(t_xsym_parse(frame, made, sizeof(made), &target) && strlen(target) == 1024);
		longest[TC_XSYM_FILE_SIZE - 43] = 'a';
		longest[TC_XSYM_FILE_SIZE - 43 + 1] = 0;
		CHECK(!t_xsym_format(longest, made));
		CHECK(!t_xsym_format("", made));
		/* Non-ASCII targets are counted in bytes, and a ':' is kept literally. */
		CHECK(t_xsym_format("dir with space/\xc3\xbc:colon", made));
		CHECK(memcmp(made, "XSym\n0023\n", 10) == 0); /* 15 + 2 (ü) + 6 bytes */
		CHECK(t_xsym_parse(frame, made, sizeof(made), &target));
		CHECK(strcmp(target, "dir with space/\xc3\xbc:colon") == 0);
	}
	if (all || strcmp(c, "parse_rejects") == 0) {
		apple_body("t.txt", apple_vectors[0].md5, body);
		CHECK(!t_xsym_parse(frame, body, sizeof(body) - 1, &target));
		memcpy(made, body, sizeof(body)); made[0] = 'x';
		CHECK(!t_xsym_parse(frame, made, sizeof(made), &target));
		memcpy(made, body, sizeof(body)); made[6] = 'x';
		CHECK(!t_xsym_parse(frame, made, sizeof(made), &target));
		memcpy(made, body, sizeof(body)); made[9] = ' ';
		CHECK(!t_xsym_parse(frame, made, sizeof(made), &target));
		memcpy(made, body, sizeof(body)); made[42] = ' ';
		CHECK(!t_xsym_parse(frame, made, sizeof(made), &target));
		memcpy(made, body, sizeof(body)); memcpy(made + 5, "0000", 4);
		CHECK(!t_xsym_parse(frame, made, sizeof(made), &target));
		memcpy(made, body, sizeof(body)); memcpy(made + 5, "1025", 4);
		CHECK(!t_xsym_parse(frame, made, sizeof(made), &target));
		memcpy(made, body, sizeof(body)); made[10] ^= 1; /* digest */
		CHECK(!t_xsym_parse(frame, made, sizeof(made), &target));
		memcpy(made, body, sizeof(body)); made[44] = 0; /* NUL inside the target */
		CHECK(!t_xsym_parse(frame, made, sizeof(made), &target));
		memset(made, 'L', sizeof(made)); /* an ordinary 1067-byte file */
		CHECK(!t_xsym_parse(frame, made, sizeof(made), &target));
		/* Padding is not validated by the client, so it is not validated here. */
		memcpy(made, body, sizeof(body)); made[1066] = 'z';
		CHECK(t_xsym_parse(frame, made, sizeof(made), &target) && strcmp(target, "t.txt") == 0);
	}
	if (all || strcmp(c, "convert_created") == 0) {
		reset_hooks();
		t_xsym_format("sub/target", body);
		put_file("created", body, sizeof(body));
		CHECK(convert(frame, "created", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(is_link_to("created", "sub/target") && no_temp_left() && notifies == 1);
		/* The journal is committed after the link is made and after the original goes. */
		CHECK(syncs == 2);
		{
			/* Same mode as AFP and SSH create; Linux ignores symlink modes. */
			struct stat st;
			CHECK(lstat("created", &st) == 0);
#ifdef __NetBSD__
			CHECK((st.st_mode & 07777) == 0755);
#endif
		}
		{
			/* The original is only unlinked after its fd is closed: unlinking a file
			 * smbd still holds panics the HFS journal on these devices. */
			struct handle h;
			struct tc_xsym_candidate cand;
			t_xsym_format("t.txt", body);
			put_file("held", body, sizeof(body));
			syncs = 0;
			open_handle(frame, &h, "held", FILE_WAS_CREATED, O_RDWR);
			CHECK(t_close_prepare(frame, &h.fsp, NORMAL_CLOSE, &cand));
			CHECK(t_close_commit(&h.fsp, &cand));
			CHECK(is_link_to("held", "t.txt") && cand.aside != NULL && is_regular(cand.aside));
			CHECK(syncs == 1);
			close_handle(&h);
			t_close_finish(&h.fsp, &cand);
			CHECK(cand.aside == NULL && no_temp_left() && syncs == 2);
			/* Nothing is removed if the private name no longer holds that file. */
			put_file("held2", body, sizeof(body));
			open_handle(frame, &h, "held2", FILE_WAS_CREATED, O_RDWR);
			CHECK(t_close_prepare(frame, &h.fsp, NORMAL_CLOSE, &cand));
			CHECK(t_close_commit(&h.fsp, &cand));
			close_handle(&h);
			/* Keep the old inode alive so the filesystem cannot reuse its number. */
			CHECK(rename(cand.aside, "held2.old") == 0);
			put_file(cand.aside, "someone else", 12);
			t_close_finish(&h.fsp, &cand);
			CHECK(!no_temp_left());
			CHECK(system("rm -f .tc-xsym.* held2.old") == 0);
		}
		t_xsym_format("/Users/somebody/abs target", body);
		put_file("absolute", body, sizeof(body));
		CHECK(convert(frame, "absolute", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(is_link_to("absolute", "/Users/somebody/abs target"));
		CHECK(num_pathref_reads == 0);
	}
	if (all || strcmp(c, "convert_write_only") == 0) {
		/* Linux mfsymlinks creates the file with GENERIC_WRITE only, so smbd holds it
		 * O_WRONLY. It is read through an internal handle: by its name before the commit,
		 * by its private name during it. */
		struct handle h;
		struct tc_xsym_candidate cand;
		reset_hooks();
		t_xsym_format("sub/target", body);
		put_file("wronly", body, sizeof(body));
		CHECK(convert_as(frame, "wronly", FILE_WAS_CREATED, NORMAL_CLOSE, O_WRONLY));
		CHECK(is_link_to("wronly", "sub/target") && no_temp_left() && syncs == 2);
		CHECK(num_pathref_reads == 2 && strcmp(pathref_reads[0], "wronly") == 0);
		CHECK(is_aside(pathref_reads[1]));
		/* A rewrite after close_prepare is still seen. */
		reset_hooks();
		put_file("wrewritten", body, sizeof(body));
		open_handle(frame, &h, "wrewritten", FILE_WAS_CREATED, O_WRONLY);
		CHECK(t_close_prepare(frame, &h.fsp, NORMAL_CLOSE, &cand));
		t_xsym_format("elsewhere", made);
		put_file("wrewritten", made, sizeof(made));
		CHECK(!t_close_commit(&h.fsp, &cand));
		close_handle(&h);
		t_close_finish(&h.fsp, &cand);
		CHECK(has_content("wrewritten", made, sizeof(made)) && no_temp_left());
		/* Only the file the handle holds is read: another file now at its name is not. */
		reset_hooks();
		put_file("wmoved", body, sizeof(body));
		open_handle(frame, &h, "wmoved", FILE_WAS_CREATED, O_WRONLY);
		CHECK(rename("wmoved", "wmoved.old") == 0);
		put_file("wmoved", body, sizeof(body));
		CHECK(!t_close_prepare(frame, &h.fsp, NORMAL_CLOSE, &cand));
		CHECK(num_pathref_reads == 1);
		close_handle(&h);
		CHECK(is_regular("wmoved") && is_regular("wmoved.old"));
		/* Nor a name that is gone. */
		reset_hooks();
		put_file("wgone", body, sizeof(body));
		open_handle(frame, &h, "wgone", FILE_WAS_CREATED, O_WRONLY);
		CHECK(rename("wgone", "wgone.old") == 0);
		CHECK(!t_close_prepare(frame, &h.fsp, NORMAL_CLOSE, &cand));
		close_handle(&h);
		CHECK(is_regular("wgone.old") && notifies == 0);
	}
	if (all || strcmp(c, "convert_refused") == 0) {
		struct handle h;
		struct tc_xsym_candidate cand;
		reset_hooks();
		t_xsym_format("t.txt", body);
		/* An existing XSym file is never converted because someone opened it. */
		put_file("opened", body, sizeof(body));
		CHECK(!convert(frame, "opened", FILE_WAS_OPENED, NORMAL_CLOSE) && is_regular("opened"));
		CHECK(!convert(frame, "opened", FILE_WAS_OVERWRITTEN, NORMAL_CLOSE) && is_regular("opened"));
		put_file("errclose", body, sizeof(body));
		CHECK(!convert(frame, "errclose", FILE_WAS_CREATED, ERROR_CLOSE) && is_regular("errclose"));
		put_file("disabled", body, sizeof(body));
		feature_enabled = false;
		CHECK(!convert(frame, "disabled", FILE_WAS_CREATED, NORMAL_CLOSE) && is_regular("disabled"));
		feature_enabled = true;
		put_file("short", body, sizeof(body) - 1);
		CHECK(!convert(frame, "short", FILE_WAS_CREATED, NORMAL_CLOSE) && is_regular("short"));
		memset(made, 'L', sizeof(made));
		put_file("plain", made, sizeof(made));
		CHECK(!convert(frame, "plain", FILE_WAS_CREATED, NORMAL_CLOSE) && is_regular("plain"));
		put_file("hardlinked", body, sizeof(body));
		CHECK(link("hardlinked", "hardlinked2") == 0);
		CHECK(!convert(frame, "hardlinked", FILE_WAS_CREATED, NORMAL_CLOSE) && is_regular("hardlinked"));
		/* A resource fork has nowhere to go on a link. */
		put_file("forked", body, sizeof(body));
		resource_fork_size = 10;
		CHECK(!convert(frame, "forked", FILE_WAS_CREATED, NORMAL_CLOSE) && is_regular("forked"));
		resource_fork_size = 0;
		put_file("deleting", body, sizeof(body));
		open_handle(frame, &h, "deleting", FILE_WAS_CREATED, O_RDONLY);
		h.fsp.fsp_flags.delete_on_close = true;
		CHECK(!t_close_prepare(frame, &h.fsp, NORMAL_CLOSE, &cand));
		close_handle(&h);
		put_file("stream", body, sizeof(body));
		open_handle(frame, &h, "stream", FILE_WAS_CREATED, O_RDONLY);
		h.fsp.base_fsp = &h.fsp; /* a named stream handle has a base */
		h.fsp.fsp_name->stream_name = discard_const_p(char, ":s:$DATA");
		CHECK(!t_close_prepare(frame, &h.fsp, NORMAL_CLOSE, &cand));
		close_handle(&h);
		CHECK(no_temp_left() && notifies == 0 && syncs == 0);
	}
	if (all || strcmp(c, "sole_open") == 0) {
		/* Only the closing handle may be open, whoever else holds the file. */
		struct handle h;
		put_file("sole", "", 0);
		open_handle(frame, &h, "sole", FILE_WAS_CREATED, O_RDONLY);
		ZERO_STRUCT(entries);
		entries[0] = (struct share_mode_entry){ .pid = self, .share_file_id = 99 };
		num_entries = 1;
		CHECK(t_sole_open(NULL, &h.fsp));
		/* Another handle of this process, another smbd, and a POSIX open all count. */
		entries[1] = (struct share_mode_entry){ .pid = self, .share_file_id = 100 };
		num_entries = 2;
		CHECK(!t_sole_open(NULL, &h.fsp));
		entries[1] = (struct share_mode_entry){ .pid = { .pid = 5000 }, .share_file_id = 99 };
		CHECK(!t_sole_open(NULL, &h.fsp));
		entries[1] = (struct share_mode_entry){ .pid = { .pid = 5000 }, .flags = SHARE_ENTRY_FLAG_POSIX_OPEN };
		CHECK(!t_sole_open(NULL, &h.fsp));
		/* An entry left by a dead process does not. */
		entries[1].stale = true;
		CHECK(t_sole_open(NULL, &h.fsp));
		close_handle(&h);
	}
	if (all || strcmp(c, "commit_races") == 0) {
		/* Other clients act between the conversion's steps. Nothing of theirs is lost. */
		reset_hooks();
		t_xsym_format("t.txt", body);
		/* The name is replaced after the identity check, before it is taken aside. */
		put_file("raced", body, sizeof(body));
		before_grab = replace_name;
		CHECK(!convert(frame, "raced", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(has_content("raced", "newer", 5) && has_content("raced.moved", body, sizeof(body)));
		/* A new object takes the name while it is empty: it wins, nothing is replaced. */
		reset_hooks();
		put_file("gap", body, sizeof(body));
		after_grab = create_in_gap;
		CHECK(!convert(frame, "gap", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(has_content("gap", "newer", 5) && no_temp_left());
		CHECK(syncs == 1); /* only for removing the dropped original */
		reset_hooks();
		/* The body changed after close_prepare read it. */
		reset_hooks();
		{
			struct handle h;
			struct tc_xsym_candidate cand;
			put_file("rewritten", body, sizeof(body));
			open_handle(frame, &h, "rewritten", FILE_WAS_CREATED, O_RDWR);
			CHECK(t_close_prepare(frame, &h.fsp, NORMAL_CLOSE, &cand));
			t_xsym_format("elsewhere", made);
			put_file("rewritten", made, sizeof(made)); /* same inode, O_TRUNC */
			CHECK(!t_close_commit(&h.fsp, &cand));
			close_handle(&h);
			t_close_finish(&h.fsp, &cand);
			CHECK(has_content("rewritten", made, sizeof(made)));
			/* A hard link appeared after close_prepare. */
			put_file("linked", body, sizeof(body));
			open_handle(frame, &h, "linked", FILE_WAS_CREATED, O_RDWR);
			CHECK(t_close_prepare(frame, &h.fsp, NORMAL_CLOSE, &cand));
			CHECK(link("linked", "linked2") == 0);
			CHECK(!t_close_commit(&h.fsp, &cand));
			close_handle(&h);
			t_close_finish(&h.fsp, &cand);
			CHECK(is_regular("linked") && is_regular("linked2"));
			/* The name already refers to another file when the commit starts. */
			put_file("swapped", body, sizeof(body));
			open_handle(frame, &h, "swapped", FILE_WAS_CREATED, O_RDWR);
			CHECK(t_close_prepare(frame, &h.fsp, NORMAL_CLOSE, &cand));
			CHECK(rename("swapped", "swapped.old") == 0);
			put_file("swapped", "other", 5);
			CHECK(!t_close_commit(&h.fsp, &cand));
			close_handle(&h);
			t_close_finish(&h.fsp, &cand);
			CHECK(has_content("swapped", "other", 5) && has_content("swapped.old", body, sizeof(body)));
		}
		CHECK(no_temp_left() && notifies == 0 && syncs == 0);
	}
	if (all || strcmp(c, "commit_failures") == 0) {
		/* Any failure puts the original back; the XSym file still works for macOS. */
		reset_hooks();
		t_xsym_format("t.txt", body);
		put_file("nosymlink", body, sizeof(body));
		fail_symlink = EPERM; /* e.g. a FAT USB disk */
		CHECK(!convert(frame, "nosymlink", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(has_content("nosymlink", body, sizeof(body)) && syncs == 0);
		reset_hooks();
		put_file("norename", body, sizeof(body));
		fail_rename = EIO;
		CHECK(!convert(frame, "norename", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(has_content("norename", body, sizeof(body)) && syncs == 0);
		reset_hooks();
		put_file("noxattr", body, sizeof(body));
		{
			struct stat st;
			CHECK(stat("noxattr", &st) == 0);
			xput(st.st_ino, "user.DOSATTRIB", "d", 1);
		}
		fail_setxattr = ENOSPC;
		CHECK(!convert(frame, "noxattr", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(has_content("noxattr", body, sizeof(body)) && no_temp_left());
		/* The new link was set up before the copy failed: commit, then remove it. */
		CHECK(syncs == 1);
		reset_hooks();
		put_file("notimes", body, sizeof(body));
		fail_ntimes = EIO;
		CHECK(!convert(frame, "notimes", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(has_content("notimes", body, sizeof(body)) && no_temp_left() && syncs == 1);
		/* A filesystem that cannot set link times at all is not a failure. */
		reset_hooks();
		put_file("linktimes", body, sizeof(body));
		fail_ntimes = EOPNOTSUPP;
		CHECK(convert(frame, "linktimes", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(is_link_to("linktimes", "t.txt"));
		CHECK(no_temp_left() && notifies == 1 && syncs == 2);
		/* The link is in place; only removing the original failed. It stays aside. */
		reset_hooks();
		put_file("noremove", body, sizeof(body));
		fail_unlink_aside = EBUSY;
		CHECK(convert(frame, "noremove", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(is_link_to("noremove", "t.txt") && !no_temp_left() && syncs == 2);
		CHECK(system("rm -f .tc-xsym.*") == 0);
	}
	if (all || strcmp(c, "metadata") == 0) {
		/* What the client stored on the created file moves to the link. */
		static const char *copied[] = {
			"user.DOSATTRIB", "security.NTACL", "user.DosStream.com.apple.provenance:$DATA",
			"user.$LXUID",
			/* The client's own streams, however close their names come. */
			"user.DosStream.backup.AFP_AfpInfo.notes:$DATA", "user.DosStream.AFP_AfpInfo.bak:$DATA",
			"user.DosStream.backup.AFP_Resource.notes:$DATA", "user.DosStream.AFP_AfpInfo:$DATAX",
			"user.DosStream.report.org.netatalk.notes:$DATA", "user.org.netatalk.Metadata.bak",
			"com.apple.FinderInfo.copy", "AFP_AfpInfo",
		};
		/* HFS owns a link's Finder info (slnk/rhap); a link has no resource fork. */
		static const char *kept_behind[] = {
			"com.apple.FinderInfo", "com.apple.ResourceFork", "user.DosStream.AFP_AfpInfo:$DATA",
			"user.DosStream.afp_resource:$DATA", "user.DosStream.AFP_AfpInfo",
			"user.DosStream.AFP_Resource:$data", "org.netatalk.Metadata", "user.org.netatalk.Metadata",
			"org.netatalk.ResourceFork", "user.org.netatalk.ResourceFork",
		};
		struct stat st, lst;
		struct timespec mtime = { .tv_sec = 1600000000 };
		reset_hooks();
		t_xsym_format("t.txt", body);
		put_file("meta", body, sizeof(body));
		CHECK(utimensat(AT_FDCWD, "meta", (struct timespec[]){ mtime, mtime }, 0) == 0);
		CHECK(stat("meta", &st) == 0);
		for (i = 0; i < ARRAY_SIZE(copied); i++) xput(st.st_ino, copied[i], copied[i], strlen(copied[i]));
		xput(st.st_ino, SAMBA_XATTR_REPARSE_ATTRIB, "payload", 7);
		for (i = 0; i < ARRAY_SIZE(kept_behind); i++) xput(st.st_ino, kept_behind[i], "k", 1);
		/* Longer than the first read buffer: the list and a value both grow on ERANGE. */
		{
			static uint8_t big[500];
			char name[64];
			memset(big, 'b', sizeof(big));
			xput(st.st_ino, "user.DosStream.big:$DATA", big, sizeof(big));
			for (i = 0; i < 8; i++) {
				snprintf(name, sizeof(name), "user.DosStream.long-stream-name-%02zu:$DATA", i);
				xput(st.st_ino, name, "s", 1);
			}
		}
		CHECK(convert(frame, "meta", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(lstat("meta", &lst) == 0 && S_ISLNK(lst.st_mode));
		for (i = 0; i < ARRAY_SIZE(copied); i++) {
			int x = xfind(lst.st_ino, copied[i]);
			CHECK(x >= 0 && xattrs[x].len == strlen(copied[i]));
			CHECK(memcmp(xattrs[x].value, copied[i], xattrs[x].len) == 0);
		}
		{
			int x = xfind(lst.st_ino, "user.DosStream.big:$DATA");
			CHECK(x >= 0 && xattrs[x].len == 500 && xattrs[x].value[499] == 'b');
			CHECK(xfind(lst.st_ino, "user.DosStream.long-stream-name-07:$DATA") >= 0);
		}
		/* The payload became the link itself. */
		CHECK(xfind(lst.st_ino, SAMBA_XATTR_REPARSE_ATTRIB) < 0);
		for (i = 0; i < ARRAY_SIZE(kept_behind); i++) CHECK(xfind(lst.st_ino, kept_behind[i]) < 0);
		CHECK(ntimes_calls == 1 && ntimes_mtime.tv_sec == mtime.tv_sec);
		/* A Windows placeholder's attributes move the same way. */
		reset_hooks();
		put_file("winmeta", "", 0);
		CHECK(stat("winmeta", &st) == 0);
		xput(st.st_ino, "user.DOSATTRIB", "w", 1);
		stored_reparse_len = symlink_payload("t.txt", true, stored_reparse, sizeof(stored_reparse));
		xput(st.st_ino, SAMBA_XATTR_REPARSE_ATTRIB, stored_reparse, 8);
		CHECK(convert(frame, "winmeta", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(lstat("winmeta", &lst) == 0 && S_ISLNK(lst.st_mode));
		CHECK(xfind(lst.st_ino, "user.DOSATTRIB") >= 0);
		CHECK(xfind(lst.st_ino, SAMBA_XATTR_REPARSE_ATTRIB) < 0);
		CHECK(no_temp_left());
	}
	if (all || strcmp(c, "read_xsym") == 0) {
		struct connection_struct conn = {0};
		struct files_struct fsp = {0};
		DATA_BLOB out = data_blob_null;
		fsp.conn = &conn;
		CHECK(symlink("../nowhere", "native") == 0);
		fsp.fsp_name = synthetic_smb_fname(frame, "native", NULL, NULL, 0, 0);
		apple_body("../nowhere", apple_vectors[1].md5, body);
		CHECK(NT_STATUS_IS_OK(t_read_xsym(&fsp, frame, &out, 0, 4096)));
		CHECK(out.length == sizeof(body) && memcmp(out.data, body, sizeof(body)) == 0);
		CHECK(NT_STATUS_IS_OK(t_read_xsym(&fsp, frame, &out, 1000, 100)));
		CHECK(out.length == 67 && memcmp(out.data, body + 1000, 67) == 0);
		CHECK(NT_STATUS_IS_OK(t_read_xsym(&fsp, frame, &out, 5, 4)));
		CHECK(out.length == 4 && memcmp(out.data, "0010", 4) == 0);
		CHECK(NT_STATUS_EQUAL(t_read_xsym(&fsp, frame, &out, 1067, 10), NT_STATUS_END_OF_FILE));
		fail_readlink = 1;
		CHECK(NT_STATUS_EQUAL(t_read_xsym(&fsp, frame, &out, 0, 10), NT_STATUS_ACCESS_DENIED));
		fail_readlink = 0;
		CHECK(is_link_to("native", "../nowhere"));
	}
	if (all || strcmp(c, "write_xsym") == 0) {
		/* A Mac restating the XSym file it just wrote: only writes that change nothing. */
		struct connection_struct conn = {0};
		struct files_struct fsp = {0};
		fsp.conn = &conn;
		CHECK(symlink("../nowhere", "restated") == 0);
		fsp.fsp_name = synthetic_smb_fname(frame, "restated", NULL, NULL, 0, 0);
		apple_body("../nowhere", apple_vectors[1].md5, body);
		CHECK(NT_STATUS_IS_OK(t_write_xsym(&fsp, NULL, 0, TC_XSYM_FILE_SIZE))); /* seen from macOS */
		CHECK(NT_STATUS_IS_OK(t_write_xsym(&fsp, NULL, 0, 0)));
		CHECK(NT_STATUS_IS_OK(t_write_xsym(&fsp, body, sizeof(body), 0)));
		CHECK(NT_STATUS_IS_OK(t_write_xsym(&fsp, body + 5, 38, 5)));
		memcpy(made, body, sizeof(body)); made[20] ^= 1;
		CHECK(NT_STATUS_EQUAL(t_write_xsym(&fsp, made, sizeof(made), 0), NT_STATUS_ACCESS_DENIED));
		CHECK(NT_STATUS_EQUAL(t_write_xsym(&fsp, body, 2, TC_XSYM_FILE_SIZE - 1), NT_STATUS_ACCESS_DENIED));
		CHECK(NT_STATUS_EQUAL(t_write_xsym(&fsp, NULL, 0, TC_XSYM_FILE_SIZE + 1), NT_STATUS_ACCESS_DENIED));
		CHECK(NT_STATUS_EQUAL(t_write_xsym(&fsp, NULL, 0, -1), NT_STATUS_ACCESS_DENIED));
		fail_readlink = 1;
		CHECK(NT_STATUS_EQUAL(t_write_xsym(&fsp, NULL, 0, 0), NT_STATUS_ACCESS_DENIED));
		fail_readlink = 0;
		CHECK(is_link_to("restated", "../nowhere"));
	}
	if (all || strcmp(c, "reparse_created") == 0) {
		/* Empty placeholder + SET_REPARSE_POINT, native at close, for every symlink form. */
		reset_hooks();
		put_file("win", "", 0);
		stored_reparse_len = symlink_payload("sub\\target", true, stored_reparse, sizeof(stored_reparse));
		CHECK(convert(frame, "win", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(is_link_to("win", "sub/target") && no_temp_left() && notifies == 1);
		put_file("win-up", "", 0);
		stored_reparse_len = symlink_payload("..\\x\\y", true, stored_reparse, sizeof(stored_reparse));
		CHECK(convert(frame, "win-up", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(is_link_to("win-up", "../x/y"));
		/* Linux symlink=nfs and symlink=wsl send POSIX targets: '\\' is a name character. */
		put_file("nfs", "", 0);
		stored_reparse_len = nfs_payload(NFS_SPECFILE_LNK, "sub/a\\b", stored_reparse, sizeof(stored_reparse));
		CHECK(convert(frame, "nfs", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(is_link_to("nfs", "sub/a\\b"));
		put_file("wsl", "", 0);
		stored_reparse_len = lx_payload(2, "/abs/x y", 8, stored_reparse);
		CHECK(convert(frame, "wsl", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(is_link_to("wsl", "/abs/x y"));
		/*
		 * A symlink-tag target names files as the client sees them: its mapped
		 * characters are the disk's ':' and '\\' ("a:b" is "a<U+F022>b" to it). The
		 * separators turn into '/' first, so a mapped '\\' stays inside its name.
		 */
		put_file("win-map", "", 0);
		stored_reparse_len = symlink_payload("sub\\a\xef\x80\xa2" "b\\c\xef\x80\xa6" "d", true,
						     stored_reparse, sizeof(stored_reparse));
		CHECK(convert(frame, "win-map", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(is_link_to("win-map", "sub/a:b/c\\d") && translate_calls > 0);
		/* Absolute targets map the same way. */
		put_file("win-abs", "", 0);
		stored_reparse_len = symlink_payload("\\Volumes\\x\xef\x80\xa2y", false,
						     stored_reparse, sizeof(stored_reparse));
		CHECK(convert(frame, "win-abs", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(is_link_to("win-abs", "/Volumes/x:y"));
		/* Without a mapping module (no catia) the target is kept as sent. */
		name_mapping = false;
		put_file("win-raw", "", 0);
		stored_reparse_len = symlink_payload("a\xef\x80\xa2" "b", true, stored_reparse,
						     sizeof(stored_reparse));
		CHECK(convert(frame, "win-raw", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(is_link_to("win-raw", "a\xef\x80\xa2" "b"));
		name_mapping = true;
		/* NFS and WSL targets are POSIX bytes: never mapped, even the same code points. */
		translate_calls = 0;
		put_file("nfs-raw", "", 0);
		stored_reparse_len = nfs_payload(NFS_SPECFILE_LNK, "a\xef\x80\xa2" "b", stored_reparse,
						 sizeof(stored_reparse));
		CHECK(convert(frame, "nfs-raw", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(is_link_to("nfs-raw", "a\xef\x80\xa2" "b"));
		put_file("wsl-raw", "", 0);
		stored_reparse_len = lx_payload(2, "a\xef\x80\xa2" "b", 5, stored_reparse);
		CHECK(convert(frame, "wsl-raw", FILE_WAS_CREATED, NORMAL_CLOSE));
		CHECK(is_link_to("wsl-raw", "a\xef\x80\xa2" "b") && translate_calls == 0);
		/* A target that cannot be mapped is not converted; the placeholder stays. */
		fail_translate = NT_STATUS_ILLEGAL_CHARACTER;
		put_file("win-bad", "", 0);
		stored_reparse_len = symlink_payload("a\xef\x80\xa2" "b", true, stored_reparse,
						     sizeof(stored_reparse));
		CHECK(!convert(frame, "win-bad", FILE_WAS_CREATED, NORMAL_CLOSE) && is_regular("win-bad"));
		fail_translate = NT_STATUS_OK;
		/* An empty file without a payload is just an empty file. */
		stored_reparse_len = 0;
		put_file("empty", "", 0);
		CHECK(!convert(frame, "empty", FILE_WAS_CREATED, NORMAL_CLOSE) && is_regular("empty"));
		/* A payload with no native form stored some other way is never converted. */
		put_file("fifo", "", 0);
		stored_reparse_len = nfs_payload(NFS_SPECFILE_FIFO, NULL, stored_reparse, sizeof(stored_reparse));
		CHECK(!convert(frame, "fifo", FILE_WAS_CREATED, NORMAL_CLOSE) && is_regular("fifo"));
		stored_reparse_len = 0;
	}
	if (all || strcmp(c, "reparse_refused") == 0) {
		struct handle h;
		uint8_t buf[512];
		size_t n;
		const char *windows_only[] = { "\\??\\C:\\x", "C:\\x", "\\\\server\\share\\x" };
		const uint32_t no_native_form[] = {
			0x80000023, /* AF_UNIX socket (Linux) */
			0x80000024, /* LX_FIFO (WSL) */
			0xA0000003, /* MOUNT_POINT (junction) */
			0x12345678, /* unknown */
		};
		reset_hooks();
		put_file("fresh", "", 0);
		open_handle(frame, &h, "fresh", FILE_WAS_CREATED, O_RDONLY);
		n = symlink_payload("t.txt", true, buf, sizeof(buf));
		CHECK(NT_STATUS_IS_OK(t_check_set(&h.fsp, IO_REPARSE_TAG_SYMLINK, buf, n)));
		n = nfs_payload(NFS_SPECFILE_LNK, "t.txt", buf, sizeof(buf));
		CHECK(NT_STATUS_IS_OK(t_check_set(&h.fsp, IO_REPARSE_TAG_NFS, buf, n)));
		n = lx_payload(2, "t.txt", 5, buf);
		CHECK(NT_STATUS_IS_OK(t_check_set(&h.fsp, TC_IO_REPARSE_TAG_LX_SYMLINK, buf, n)));
		/* A symlink target the name mapping rejects is refused with its status. */
		fail_translate = NT_STATUS_ILLEGAL_CHARACTER;
		n = symlink_payload("t.txt", true, buf, sizeof(buf));
		CHECK(NT_STATUS_EQUAL(t_check_set(&h.fsp, IO_REPARSE_TAG_SYMLINK, buf, n),
				      NT_STATUS_ILLEGAL_CHARACTER));
		fail_translate = NT_STATUS_OK;
		for (i = 0; i < ARRAY_SIZE(windows_only); i++) {
			n = symlink_payload(windows_only[i], false, buf, sizeof(buf));
			CHECK(NT_STATUS_EQUAL(t_check_set(&h.fsp, IO_REPARSE_TAG_SYMLINK, buf, n),
					      NT_STATUS_NOT_SUPPORTED));
		}
		/* Linux mknod (reparse=nfs): FIFOs, sockets and devices have no native form here. */
		n = nfs_payload(NFS_SPECFILE_FIFO, NULL, buf, sizeof(buf));
		CHECK(NT_STATUS_EQUAL(t_check_set(&h.fsp, IO_REPARSE_TAG_NFS, buf, n), NT_STATUS_NOT_SUPPORTED));
		n = nfs_payload(NFS_SPECFILE_SOCK, NULL, buf, sizeof(buf));
		CHECK(NT_STATUS_EQUAL(t_check_set(&h.fsp, IO_REPARSE_TAG_NFS, buf, n), NT_STATUS_NOT_SUPPORTED));
		for (i = 0; i < ARRAY_SIZE(no_native_form); i++) {
			n = bare_payload(no_native_form[i], buf);
			CHECK(NT_STATUS_EQUAL(t_check_set(&h.fsp, no_native_form[i], buf, n), NT_STATUS_NOT_SUPPORTED));
		}
		/* Malformed WSL symlinks: wrong version, embedded NUL, empty target, bad length. */
		n = lx_payload(1, "t.txt", 5, buf);
		CHECK(NT_STATUS_EQUAL(t_check_set(&h.fsp, TC_IO_REPARSE_TAG_LX_SYMLINK, buf, n),
				      NT_STATUS_IO_REPARSE_DATA_INVALID));
		n = lx_payload(2, "t\0x", 3, buf);
		CHECK(NT_STATUS_EQUAL(t_check_set(&h.fsp, TC_IO_REPARSE_TAG_LX_SYMLINK, buf, n),
				      NT_STATUS_IO_REPARSE_DATA_INVALID));
		n = lx_payload(2, "", 0, buf);
		CHECK(NT_STATUS_EQUAL(t_check_set(&h.fsp, TC_IO_REPARSE_TAG_LX_SYMLINK, buf, n),
				      NT_STATUS_IO_REPARSE_DATA_INVALID));
		n = lx_payload(2, "t.txt", 5, buf);
		CHECK(NT_STATUS_EQUAL(t_check_set(&h.fsp, TC_IO_REPARSE_TAG_LX_SYMLINK, buf, n - 1),
				      NT_STATUS_IO_REPARSE_DATA_INVALID));
		/* A disabled share keeps upstream's handling. */
		feature_enabled = false;
		n = bare_payload(0x80000023, buf);
		CHECK(NT_STATUS_IS_OK(t_check_set(&h.fsp, 0x80000023, buf, n)));
		feature_enabled = true;
		close_handle(&h);
		/* Only a placeholder this handle created, still empty, may become a link... */
		n = symlink_payload("t.txt", true, buf, sizeof(buf));
		open_handle(frame, &h, "fresh", FILE_WAS_OPENED, O_RDONLY);
		CHECK(NT_STATUS_EQUAL(t_check_set(&h.fsp, IO_REPARSE_TAG_SYMLINK, buf, n), NT_STATUS_NOT_SUPPORTED));
		close_handle(&h);
		put_file("full", "data", 4);
		open_handle(frame, &h, "full", FILE_WAS_CREATED, O_RDONLY);
		CHECK(NT_STATUS_EQUAL(t_check_set(&h.fsp, IO_REPARSE_TAG_SYMLINK, buf, n), NT_STATUS_NOT_SUPPORTED));
		/* ...and other tags are refused on existing files too. */
		n = bare_payload(0x80000023, buf);
		CHECK(NT_STATUS_EQUAL(t_check_set(&h.fsp, 0x80000023, buf, n), NT_STATUS_NOT_SUPPORTED));
		close_handle(&h);
		/* A Windows-only payload stored some other way is still never made native. */
		put_file("stored", "", 0);
		stored_reparse_len = symlink_payload("\\??\\C:\\x", false, stored_reparse, sizeof(stored_reparse));
		CHECK(!convert(frame, "stored", FILE_WAS_CREATED, NORMAL_CLOSE) && is_regular("stored"));
		stored_reparse_len = 0;
	}
	if (all || strcmp(c, "capabilities") == 0) {
		struct smbd_server_connection sconn = {0};
		struct connection_struct conn = {.sconn = &sconn};
		/* Windows and Linux need the bit to create links; macOS must keep writing XSym. */
		CHECK(t_fs_capabilities(&conn) == FILE_SUPPORTS_REPARSE_POINTS);
		sconn.client_resolves_symlinks = true;
		CHECK(t_fs_capabilities(&conn) == 0);
		CHECK(t_client_resolves_links(&conn));
		sconn.client_resolves_symlinks = false;
		feature_enabled = false;
		CHECK(t_fs_capabilities(&conn) == 0);
		feature_enabled = true;
	}
	if (all || strcmp(c, "dos_mode") == 0) {
		struct smbd_server_connection sconn = {0};
		struct connection_struct conn = {.sconn = &sconn};
		struct files_struct fsp = {.conn = &conn};
		struct stat st;
		CHECK(mkdir("dirtarget", 0755) == 0 && symlink("dirtarget", "dirlink") == 0);
		CHECK(symlink("missing", "danglink") == 0);
		fsp.fsp_name = synthetic_smb_fname(frame, "dirlink", NULL, NULL, 0, 0);
		CHECK(lstat("dirlink", &st) == 0);
		to_stat_ex(&st, &fsp.fsp_name->st);
		/* Windows traverses a directory symlink only with the DIRECTORY bit. */
		CHECK(t_dos_mode(&fsp, FILE_ATTRIBUTE_NORMAL) ==
		      (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY));
		/* macOS decides by the tag; stored attributes never hide the reparse bit. */
		sconn.client_resolves_symlinks = true;
		CHECK(t_dos_mode(&fsp, FILE_ATTRIBUTE_ARCHIVE | FILE_ATTRIBUTE_DIRECTORY) ==
		      (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_ARCHIVE));
		sconn.client_resolves_symlinks = false;
		fsp.fsp_name->base_name = discard_const_p(char, "danglink");
		CHECK(lstat("danglink", &st) == 0);
		to_stat_ex(&st, &fsp.fsp_name->st);
		CHECK(t_dos_mode(&fsp, 0) == FILE_ATTRIBUTE_REPARSE_POINT);
		/* Regular files and disabled shares are untouched. */
		fsp.fsp_name->base_name = discard_const_p(char, "dirtarget");
		CHECK(lstat("dirtarget", &st) == 0);
		to_stat_ex(&st, &fsp.fsp_name->st);
		CHECK(t_dos_mode(&fsp, FILE_ATTRIBUTE_DIRECTORY) == FILE_ATTRIBUTE_DIRECTORY);
		fsp.fsp_name->base_name = discard_const_p(char, "dirlink");
		CHECK(lstat("dirlink", &st) == 0);
		to_stat_ex(&st, &fsp.fsp_name->st);
		feature_enabled = false;
		CHECK(t_dos_mode(&fsp, FILE_ATTRIBUTE_NORMAL) == FILE_ATTRIBUTE_NORMAL);
		feature_enabled = true;
	}
	if (!all && strcmp(c, "apple_format") && strcmp(c, "format_limits") && strcmp(c, "parse_rejects") &&
	    strcmp(c, "convert_created") && strcmp(c, "convert_write_only") && strcmp(c, "convert_refused") &&
	    strcmp(c, "sole_open") && strcmp(c, "commit_races") && strcmp(c, "commit_failures") && strcmp(c, "metadata") &&
	    strcmp(c, "read_xsym") && strcmp(c, "write_xsym") && strcmp(c, "reparse_created") && strcmp(c, "reparse_refused") &&
	    strcmp(c, "capabilities") && strcmp(c, "dos_mode")) {
		CHECK(false);
	}
	/* workdir may be relative to the directory the driver was started in. */
	CHECK(chdir("..") == 0);
	CHECK(system(talloc_asprintf(frame, "rm -rf '%s'", workdir)) == 0);
	TALLOC_FREE(frame);
	return 0;
}
