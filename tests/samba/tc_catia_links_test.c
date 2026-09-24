/* Execute the real vfs_catia link hooks (Samba patch 0045) against a recording NEXT module.
 * macOS sends ':' '*' '?' ... as private-use code points that vfs_fruit tells catia to map
 * back to the real characters on disk; symlink reads, creates and the xattr calls a
 * descriptor-less link makes by path must all see the name on disk. Kept apart from
 * tc_native_links_test: including vfs_catia.c links most of the VFS layer into the binary. */
#include "includes.h"
#include "system/filesys.h"
#include "smbd/smbd.h"
#include "smbd/globals.h"

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: %s errno=%d\n", __FILE__, __LINE__, #x, errno); exit(90); } } while (0)

/* vfs_catia maps the private-use characters macOS sends (as vfs_fruit configures it) back to
 * the real ones on disk. Its link hooks run against a recording NEXT module. */
static const char *test_catia_maps[] = { "0x3a:0xf022", "0x2a:0xf021", NULL };
#define lp_parm_string_list(snum, type, option, def) \
	((snum) != -1 && strcmp((type), "catia") == 0 && strcmp((option), "mappings") == 0 ? \
	 test_catia_maps : (def))
#define vfs_catia_init regression_catia_init
#include "vfs_catia.c"
#undef lp_parm_string_list

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

static struct vfs_fn_pointers next_fns = {
	.readlinkat_fn = next_readlinkat,
	.symlinkat_fn = next_symlinkat,
	.fgetxattr_fn = next_fgetxattr,
	.fsetxattr_fn = next_fsetxattr,
	.fremovexattr_fn = next_fremovexattr,
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
	CHECK(strcmp(argv[1], "catia_links") == 0 || strcmp(argv[1], "all") == 0);
	{
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
		TALLOC_FREE(lnk);
		TALLOC_FREE(dir);
	}
	TALLOC_FREE(frame);
	return 0;
}
