/* Execute the real vfs_catia link hooks (Samba patches 0058 and 0059) against a recording NEXT module.
 * macOS sends ':' '*' '?' ... as private-use code points that vfs_fruit tells catia to map
 * back to the real characters on disk; symlink reads, creates and the xattr calls a
 * descriptor-less link makes by path must all see the name on disk. Link targets are mapped
 * the same way, through catia's translate_name: the production tc_native_links_map_target()
 * and the real FSCTL_GET_REPARSE_POINT symlink path of util_reparse.c. Kept apart from
 * tc_native_links_test: including vfs_catia.c links most of the VFS layer into the binary. */
#include "includes.h"
#include "system/filesys.h"
#include "smbd/smbd.h"
#include "smbd/globals.h"

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: %s errno=%d\n", __FILE__, __LINE__, #x, errno); exit(90); } } while (0)

/* vfs_catia maps the private-use characters macOS sends (as vfs_fruit configures it) back to
 * the real ones on disk. Its link hooks run against a recording NEXT module. */
static const char *test_catia_maps[] = { "0x3a:0xf022", "0x2a:0xf021", "0x5c:0xf026", NULL };
#define lp_parm_string_list(snum, type, option, def) \
	((snum) != -1 && strcmp((type), "catia") == 0 && strcmp((option), "mappings") == 0 ? \
	 test_catia_maps : (def))
#define vfs_catia_init regression_catia_init
#include "vfs_catia.c"
#undef lp_parm_string_list

/* util_reparse.c reads a link's target; smbd_base links the production copy, so this one
 * runs under distinct names with the link and its directory supplied here. */
static bool links_enabled = true;
static const char *native_target;
static NTSTATUS test_parent_pathref(TALLOC_CTX *ctx, struct files_struct *dirfsp,
	const struct smb_filename *name, struct smb_filename **parent, struct smb_filename **atname);
static NTSTATUS test_read_symlink_reparse(TALLOC_CTX *ctx, struct files_struct *dirfsp,
	struct smb_filename *name, struct reparse_data_buffer **reparse);
#define fsctl_get_reparse_point t_fsctl_get_reparse_point
#define fsctl_get_reparse_tag t_fsctl_get_reparse_tag
#define fsctl_set_reparse_point t_fsctl_set_reparse_point
#define fsctl_del_reparse_point t_fsctl_del_reparse_point
#define fdos_mode(fsp) ((void)(fsp), FILE_ATTRIBUTE_REPARSE_POINT)
#define parent_pathref test_parent_pathref
#define read_symlink_reparse test_read_symlink_reparse
#define tc_native_links_enabled(conn) ((void)(conn), links_enabled)
#include "util_reparse.c"
#undef tc_native_links_enabled
#undef read_symlink_reparse
#undef parent_pathref
#undef fdos_mode

static char next_seen[256];

static int next_readlinkat(vfs_handle_struct *h, const struct files_struct *dirfsp,
	const struct smb_filename *name, char *buf, size_t bufsiz)
{
	(void)h; (void)dirfsp;
	strlcpy(next_seen, name->base_name, sizeof(next_seen));
	CHECK(bufsiz > 0);
	buf[0] = 't';
	return 1;
}

static int next_symlinkat(vfs_handle_struct *h, const struct smb_filename *target,
	struct files_struct *dirfsp, const struct smb_filename *name)
{
	(void)h; (void)dirfsp;
	CHECK(strcmp(target->base_name, "t") == 0);
	strlcpy(next_seen, name->base_name, sizeof(next_seen));
	return 0;
}

/* What an fd-less link's xattr backend sees: the file name it will reach by path. */
static void seen_xattr(struct files_struct *fsp, const char *name)
{
	snprintf(next_seen, sizeof(next_seen), "%s|%s", fsp->fsp_name->base_name, name);
}

static ssize_t next_fgetxattr(vfs_handle_struct *h, struct files_struct *fsp, const char *name,
	void *value, size_t size)
{
	(void)h; (void)value; (void)size;
	seen_xattr(fsp, name);
	return 0;
}

static int next_fsetxattr(vfs_handle_struct *h, struct files_struct *fsp, const char *name,
	const void *value, size_t size, int flags)
{
	(void)h; (void)value; (void)size; (void)flags;
	seen_xattr(fsp, name);
	return 0;
}

static int next_fremovexattr(vfs_handle_struct *h, struct files_struct *fsp, const char *name)
{
	(void)h;
	seen_xattr(fsp, name);
	return 0;
}

/* What vfs_default's NetBSD 4 fdopendir() fallback would opendir(): fsp's name. */
static int next_fdopendir_errno;
static DIR *next_fdopendir(vfs_handle_struct *h, files_struct *fsp, const char *mask,
	uint32_t attributes)
{
	(void)h; (void)mask; (void)attributes;
	strlcpy(next_seen, fsp->fsp_name->base_name, sizeof(next_seen));
	if (next_fdopendir_errno != 0) {
		errno = next_fdopendir_errno;
		return NULL;
	}
	return (DIR *)fsp; /* any non-NULL handle; never dereferenced */
}

/* vfs_default: nothing to map below catia. */
static NTSTATUS next_translate_name(vfs_handle_struct *h, const char *name,
	enum vfs_translate_direction direction, TALLOC_CTX *ctx, char **mapped)
{
	(void)h; (void)name; (void)direction; (void)ctx; (void)mapped;
	return NT_STATUS_NONE_MAPPED;
}

static NTSTATUS test_parent_pathref(TALLOC_CTX *ctx, struct files_struct *dirfsp,
	const struct smb_filename *name, struct smb_filename **parent, struct smb_filename **atname)
{
	(void)dirfsp;
	*parent = synthetic_smb_fname(ctx, ".", NULL, NULL, 0, 0);
	CHECK(*parent != NULL);
	*atname = synthetic_smb_fname(*parent, name->base_name, NULL, NULL, 0, 0);
	CHECK(*atname != NULL);
	return NT_STATUS_OK;
}

/* files.c: the link's own bytes, a relative target. */
static NTSTATUS test_read_symlink_reparse(TALLOC_CTX *ctx, struct files_struct *dirfsp,
	struct smb_filename *name, struct reparse_data_buffer **reparse)
{
	(void)dirfsp; (void)name;
	*reparse = talloc_zero(ctx, struct reparse_data_buffer);
	CHECK(*reparse != NULL);
	(*reparse)->tag = IO_REPARSE_TAG_SYMLINK;
	(*reparse)->parsed.lnk.substitute_name = talloc_strdup(*reparse, native_target);
	(*reparse)->parsed.lnk.flags = SYMLINK_FLAG_RELATIVE;
	CHECK((*reparse)->parsed.lnk.substitute_name != NULL);
	return NT_STATUS_OK;
}

/* The target a client reads for "lnk" through FSCTL_GET_REPARSE_POINT. */
static const char *client_target(TALLOC_CTX *ctx, struct files_struct *lnk, const char *native)
{
	struct reparse_data_buffer *buf = talloc_zero(ctx, struct reparse_data_buffer);
	uint8_t *data = NULL;
	uint32_t tag = 0, len = 0;
	CHECK(buf != NULL);
	native_target = native;
	CHECK(NT_STATUS_IS_OK(t_fsctl_get_reparse_point(lnk, ctx, &tag, &data, UINT16_MAX, &len)));
	CHECK(tag == IO_REPARSE_TAG_SYMLINK);
	CHECK(NT_STATUS_IS_OK(reparse_data_buffer_parse(buf, buf, data, len)));
	return buf->parsed.lnk.substitute_name;
}

static struct vfs_fn_pointers next_fns = {
	.readlinkat_fn = next_readlinkat,
	.symlinkat_fn = next_symlinkat,
	.fgetxattr_fn = next_fgetxattr,
	.fsetxattr_fn = next_fsetxattr,
	.fremovexattr_fn = next_fremovexattr,
	.translate_name_fn = next_translate_name,
	.fdopendir_fn = next_fdopendir,
};

static struct files_struct *test_fsp(TALLOC_CTX *ctx, connection_struct *conn, const char *name,
	mode_t mode)
{
	struct files_struct *fsp = talloc_zero(ctx, struct files_struct);
	CHECK(fsp != NULL);
	fsp->conn = conn;
	fsp->fsp_name = synthetic_smb_fname(fsp, name, NULL, NULL, 0, 0);
	CHECK(fsp->fsp_name != NULL);
	fsp->fsp_name->st.st_ex_mode = mode;
	fsp->fh = fd_handle_create(fsp);
	CHECK(fsp->fh != NULL);
	fsp_set_fd(fsp, -1);
	return fsp;
}


int main(int argc, char **argv)
{
	TALLOC_CTX *frame = talloc_stackframe();

	CHECK(argc == 2);
	CHECK(strcmp(argv[1], "catia_links") == 0 || strcmp(argv[1], "catia_fdopendir") == 0 ||
	      strcmp(argv[1], "all") == 0);
	if (strcmp(argv[1], "catia_fdopendir") == 0 || strcmp(argv[1], "all") == 0) {
		/*
		 * A directory named "x:y" on disk ("x/y" in Finder) is "x<U+F022>y" to the
		 * client. Opening it for listing must reach the name on disk: NetBSD 4 has
		 * no fdopendir(), and vfs_default reopens the directory by name.
		 */
		connection_struct *conn = talloc_zero(frame, connection_struct);
		struct vfs_handle_struct *next = talloc_zero(frame, struct vfs_handle_struct);
		struct vfs_handle_struct *cat = talloc_zero(frame, struct vfs_handle_struct);
		struct files_struct *dir = NULL;
		CHECK(conn != NULL && next != NULL && cat != NULL);
		conn->params = talloc_zero(conn, struct share_params);
		CHECK(conn->params != NULL);
		conn->params->service = 1;
		next->conn = cat->conn = conn;
		next->fns = &next_fns;
		cat->fns = &vfs_catia_fns;
		cat->next = next;
		dir = test_fsp(conn, conn, "share/x\xef\x80\xa2y", S_IFDIR | 0755);
		CHECK(catia_fdopendir(cat, dir, NULL, 0) == (DIR *)dir);
		CHECK(strcmp(next_seen, "share/x:y") == 0);
		/* The handle keeps the client's name for everything after the open. */
		CHECK(strcmp(dir->fsp_name->base_name, "share/x\xef\x80\xa2y") == 0);
		/* A plain name passes through unchanged. */
		TALLOC_FREE(dir);
		dir = test_fsp(conn, conn, "share/plain", S_IFDIR | 0755);
		CHECK(catia_fdopendir(cat, dir, NULL, 0) == (DIR *)dir);
		CHECK(strcmp(next_seen, "share/plain") == 0);
		/* A failed open keeps its errno for OpenDir_fsp() and restores the name. */
		TALLOC_FREE(dir);
		dir = test_fsp(conn, conn, "share/gone\xef\x80\xa2", S_IFDIR | 0755);
		next_fdopendir_errno = ENOENT;
		errno = 0;
		CHECK(catia_fdopendir(cat, dir, NULL, 0) == NULL && errno == ENOENT);
		CHECK(strcmp(next_seen, "share/gone:") == 0);
		CHECK(strcmp(dir->fsp_name->base_name, "share/gone\xef\x80\xa2") == 0);
		next_fdopendir_errno = 0;
		TALLOC_FREE(dir);
	}
	if (strcmp(argv[1], "catia_links") == 0 || strcmp(argv[1], "all") == 0) {
		/* "x:y" and "a*b" as macOS sends them: U+F022 and U+F021, UTF-8 encoded. */
		connection_struct *conn = talloc_zero(frame, connection_struct);
		struct vfs_handle_struct *next = talloc_zero(frame, struct vfs_handle_struct);
		struct vfs_handle_struct *cat = talloc_zero(frame, struct vfs_handle_struct);
		struct smb_filename target = { .base_name = discard_const_p(char, "t") };
		struct smb_filename *mac = synthetic_smb_fname(frame, "x\xef\x80\xa2y", NULL, NULL, 0, 0);
		struct smb_filename *plain = synthetic_smb_fname(frame, "plain", NULL, NULL, 0, 0);
		struct files_struct *dir = NULL, *lnk = NULL;
		char buf[8];
		CHECK(conn != NULL && next != NULL && cat != NULL && mac != NULL && plain != NULL);
		conn->params = talloc_zero(conn, struct share_params);
		CHECK(conn->params != NULL);
		conn->params->service = 1;
		next->conn = cat->conn = conn;
		next->fns = &next_fns;
		cat->fns = &vfs_catia_fns;
		cat->next = next;
		dir = test_fsp(conn, conn, "d", S_IFDIR | 0755);
		/* Reading and creating a link reach the name on disk; the caller's name is kept. */
		CHECK(catia_readlinkat(cat, dir, mac, buf, sizeof(buf)) == 1 && strcmp(next_seen, "x:y") == 0);
		CHECK(catia_symlinkat(cat, &target, dir, mac) == 0 && strcmp(next_seen, "x:y") == 0);
		CHECK(strcmp(mac->base_name, "x\xef\x80\xa2y") == 0);
		CHECK(catia_readlinkat(cat, dir, plain, buf, sizeof(buf)) == 1 && strcmp(next_seen, "plain") == 0);
		/* A link opened as itself has no fd: its xattr backend reaches it by (mapped) path. */
		lnk = test_fsp(conn, conn, "d/x\xef\x80\xa2y", S_IFLNK | 0755);
		CHECK(catia_fgetxattr(cat, lnk, "user.a\xef\x80\xa1" "b", NULL, 0) == 0);
		CHECK(strcmp(next_seen, "d/x:y|user.a*b") == 0);
		CHECK(catia_fsetxattr(cat, lnk, "user.k", "v", 1, 0) == 0 && strcmp(next_seen, "d/x:y|user.k") == 0);
		CHECK(catia_fremovexattr(cat, lnk, "user.k") == 0 && strcmp(next_seen, "d/x:y|user.k") == 0);
		/* After each call the handle keeps the name the client knows it by. */
		CHECK(strcmp(lnk->fsp_name->base_name, "d/x\xef\x80\xa2y") == 0);

		/*
		 * Link targets: a client's "x<U+F022>y" is "x:y" on disk, a mapped '\\' is a
		 * literal '\\' in a name, and '/' separators are never touched.
		 */
		conn->vfs_handles = cat;
		{
			char *t = talloc_strdup(frame, "../d/x\xef\x80\xa2y/a\xef\x80\xa6" "b\xef\x80\xa1");
			CHECK(t != NULL);
			CHECK(NT_STATUS_IS_OK(tc_native_links_map_target(conn, frame, &t, vfs_translate_to_unix)));
			CHECK(strcmp(t, "../d/x:y/a\\b*") == 0);
			CHECK(NT_STATUS_IS_OK(tc_native_links_map_target(conn, frame, &t,
									 vfs_translate_to_windows)));
			CHECK(strcmp(t, "../d/x\xef\x80\xa2y/a\xef\x80\xa6" "b\xef\x80\xa1") == 0);
			TALLOC_FREE(t);
			t = talloc_strdup(frame, "plain/target");
			CHECK(t != NULL);
			CHECK(NT_STATUS_IS_OK(tc_native_links_map_target(conn, frame, &t, vfs_translate_to_unix)));
			CHECK(strcmp(t, "plain/target") == 0);
			TALLOC_FREE(t);
		}
		/*
		 * FSCTL_GET_REPARSE_POINT: Windows and macOS read the target in their own names,
		 * mapped before the separators turn into '\\' (macOS maps both back itself), so
		 * a literal '\\' on disk cannot pass for a separator.
		 */
		CHECK(strcmp(client_target(frame, lnk, "d/x:y/a\\b"),
			     "d\\x\xef\x80\xa2y\\a\xef\x80\xa6" "b") == 0);
		CHECK(strcmp(client_target(frame, lnk, "plain/t"), "plain\\t") == 0);
		/* POSIX clients get the bytes on disk. */
		lnk->fsp_flags.posix_open = true;
		CHECK(strcmp(client_target(frame, lnk, "d/x:y/a\\b"), "d/x:y/a\\b") == 0);
		lnk->fsp_flags.posix_open = false;
		/* With native links off, upstream behavior: the target unchanged. */
		links_enabled = false;
		CHECK(strcmp(client_target(frame, lnk, "d/x:y/a\\b"), "d/x:y/a\\b") == 0);
		links_enabled = true;
		conn->vfs_handles = NULL;
		TALLOC_FREE(lnk);
		TALLOC_FREE(dir);
	}
	TALLOC_FREE(frame);
	return 0;
}
