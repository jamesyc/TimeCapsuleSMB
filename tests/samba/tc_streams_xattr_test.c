/* Execute the real streams_xattr implementation with an in-memory xattr
 * backend. Missing attributes deliberately use ENOATTR, including on Linux,
 * where we make it distinct from ENODATA to exercise the NetBSD contract. */
#include "includes.h"
#include "system/filesys.h"
#include "smbd/smbd.h"
#include "smbd/globals.h"
#include "lib/global_contexts.h"

#undef ENOATTR
#define ENOATTR 193
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: %s (errno=%d)\n", __FILE__, __LINE__, #x, errno); exit(90); } } while (0)

struct test_attr { char name[128]; uint8_t data[64]; size_t size; };
static struct test_attr attrs[8];
static unsigned removes;
static int fail_extent, fail_primary;
static struct files_struct *expected_parent, *test_file;

static struct test_attr *find_attr(const char *name)
{
	unsigned i;
	for (i = 0; i < ARRAY_SIZE(attrs); i++) {
		if (strcmp(attrs[i].name, name) == 0) return &attrs[i];
	}
	return NULL;
}

static int test_set(struct files_struct *fsp, const char *name,
	const void *value, size_t size, int flags)
{
	struct test_attr *a = find_attr(name);
	unsigned i;
	(void)flags;
	CHECK(fsp == test_file && size <= sizeof(attrs[0].data));
	if (a == NULL) {
		for (i = 0; i < ARRAY_SIZE(attrs); i++) {
			if (attrs[i].name[0] == 0) { a = &attrs[i]; break; }
		}
	}
	CHECK(a != NULL);
	snprintf(a->name, sizeof(a->name), "%s", name);
	memcpy(a->data, value, size); a->size = size;
	return 0;
}

static ssize_t test_get(struct files_struct *fsp, const char *name, void *value, size_t size)
{
	struct test_attr *a = find_attr(name);
	CHECK(fsp == test_file);
	if (fail_extent && strstr(name, "Ext.")) { errno = fail_extent; return -1; }
	if (a == NULL) { errno = ENOATTR; return -1; }
	if (size < a->size) { errno = ERANGE; return -1; }
	memcpy(value, a->data, a->size); return a->size;
}

static int test_remove(struct files_struct *fsp, const char *name)
{
	struct test_attr *a = find_attr(name);
	CHECK(fsp == test_file); removes++;
	if (fail_primary && !strstr(name, "Ext.")) { errno = fail_primary; return -1; }
	if (fail_extent && strstr(name, "Ext.")) { errno = fail_extent; return -1; }
	if (a == NULL) { errno = ENOATTR; return -1; }
	ZERO_STRUCTP(a); return 0;
}

static NTSTATUS test_pathref(TALLOC_CTX *ctx, const struct files_struct *parent,
	const char *name, const char *stream, const SMB_STRUCT_STAT *st,
	NTTIME twrp, uint32_t flags, struct smb_filename **out)
{
	(void)stream; (void)st; (void)twrp; (void)flags;
	if (parent != expected_parent || strcmp(name, "object") != 0) {
		return NT_STATUS_OBJECT_NAME_NOT_FOUND;
	}
	*out = talloc_zero(ctx, struct smb_filename);
	CHECK(*out != NULL); (*out)->fsp = test_file;
	return NT_STATUS_OK;
}

#undef SMB_VFS_FGETXATTR
#undef SMB_VFS_FSETXATTR
#undef SMB_VFS_FREMOVEXATTR
#define SMB_VFS_FGETXATTR(f,n,v,s) test_get(f,n,v,s)
#define SMB_VFS_FSETXATTR(f,n,v,s,g) test_set(f,n,v,s,g)
#define SMB_VFS_FREMOVEXATTR(f,n) test_remove(f,n)
/* Eight bytes make multi-extent cases small and readable. */
#define lp_smbd_max_xattr_size(snum) 8
#define synthetic_pathref test_pathref
#define vfs_streams_xattr_init regression_streams_xattr_init
#include "vfs_streams_xattr.c"
#undef synthetic_pathref

int main(int argc, char **argv)
{
	TALLOC_CTX *frame = talloc_stackframe();
	struct connection_struct conn = {0};
	struct files_struct root = {0}, sub = {0}, file = {0};
	struct streams_xattr_config config = {
		.prefix = "user.DosStream.", .ext_prefix = "user.DosStreamExt.",
		.max_extents = 3, .store_stream_type = true,
	};
	struct vfs_handle_struct handle = {.conn = &conn, .data = &config};
	struct smb_filename name = {.base_name = "object", .stream_name = ":test:$DATA"};
	const char large[] = "abcdefghijklmnopqr";
	const char small[] = "abcdefgh";
	char buf[64] = {0};
	int ret;
	CHECK(argc == 2);
	conn.cwd_fsp = &root; root.conn = sub.conn = file.conn = &conn;
	test_file = &file; expected_parent = &root;
	if (strcmp(argv[1], "charset_types") == 0) {
		char mutable[] = "alpha";
		const char immutable[] = "alpha";
		char *cursor = mutable;
		CHECK(__builtin_types_compatible_p(__typeof__(strchr_m(mutable, 'p')), char *));
		CHECK(__builtin_types_compatible_p(__typeof__(strchr_m(immutable, 'p')), const char *));
		CHECK(__builtin_types_compatible_p(__typeof__(strrchr_m(immutable, 'a')), const char *));
		CHECK(__builtin_types_compatible_p(__typeof__(strstr_m(immutable, "ph")), const char *));
		CHECK(strchr_m(cursor++, 'p') == mutable + 2 && cursor == mutable + 1);
		CHECK(strrchr_m(mutable, 'a') == mutable + 4);
		CHECK(strstr_m(immutable, "ph") == immutable + 2);
	} else if (strcmp(argv[1], "root_delete") == 0 || strcmp(argv[1], "nested_delete") == 0) {
		if (argv[1][0] == 'n') expected_parent = &sub;
		config.max_extents = 1; /* shipped max xattrs per stream = 2 */
		CHECK(fsetxattr_multi(&config, &file, "test:$DATA", "abc", 4, 0) == 0);
		CHECK(streams_xattr_unlinkat(&handle, expected_parent, &name, 0) == 0);
		CHECK(find_attr("user.DosStream.test:$DATA") == NULL && removes == 2);
	} else if (strcmp(argv[1], "extent_delete") == 0) {
		CHECK(fsetxattr_multi(&config, &file, "test:$DATA", large, sizeof(large), 0) == 0);
		CHECK(streams_xattr_unlinkat(&handle, &root, &name, 0) == 0);
		CHECK(find_attr("user.DosStream.test:$DATA") == NULL);
		CHECK(find_attr("user.DosStreamExt.1.test:$DATA") == NULL);
		CHECK(find_attr("user.DosStreamExt.2.test:$DATA") == NULL);
	} else if (strcmp(argv[1], "missing_primary") == 0) {
		CHECK(streams_xattr_unlinkat(&handle, &root, &name, 0) == -1 && errno == ENOENT);
	} else if (strcmp(argv[1], "invalid_stream") == 0) {
		name.stream_name = ":test:$INVALID";
		CHECK(streams_xattr_unlinkat(&handle, &root, &name, 0) == -1 && errno == EINVAL);
		CHECK(removes == 0);
	} else if (strcmp(argv[1], "missing_path") == 0) {
		name.base_name = "missing";
		CHECK(streams_xattr_unlinkat(&handle, &root, &name, 0) == -1 && errno == ENOENT);
		CHECK(removes == 0);
	} else if (strcmp(argv[1], "primary_error") == 0 || strcmp(argv[1], "extent_error") == 0) {
		CHECK(fsetxattr_multi(&config, &file, "test:$DATA", "abc", 4, 0) == 0);
		if (argv[1][0] == 'p') fail_primary = EACCES; else fail_extent = EIO;
		ret = streams_xattr_unlinkat(&handle, &root, &name, 0);
		CHECK(ret == -1 && errno == (fail_primary ? EACCES : EIO));
		CHECK((find_attr("user.DosStream.test:$DATA") != NULL) == (fail_primary != 0));
	} else if (strcmp(argv[1], "roundtrip_shrink") == 0) {
		CHECK(fsetxattr_multi(&config, &file, "test:$DATA", large, sizeof(large), 0) == 0);
		CHECK(fgetxattr_multi(&config, &file, "test:$DATA", buf, sizeof(buf)) == sizeof(large));
		CHECK(memcmp(buf, large, sizeof(large)) == 0);
		CHECK(fsetxattr_multi(&config, &file, "test:$DATA", small, sizeof(small), 0) == 0);
		CHECK(find_attr("user.DosStreamExt.2.test:$DATA") == NULL);
		CHECK(fgetxattr_multi(&config, &file, "test:$DATA", buf, sizeof(buf)) == sizeof(small));
		CHECK(memcmp(buf, small, sizeof(small)) == 0);
	} else if (strcmp(argv[1], "shrink_missing") == 0) {
		/* With spare slots, shrinking cleanup must stop at the first hole. */
		CHECK(fsetxattr_multi(&config, &file, "test:$DATA", small, sizeof(small), 0) == 0);
		CHECK(removes == 1);
	} else if (strcmp(argv[1], "short_read") == 0 || strcmp(argv[1], "read_error") == 0) {
		CHECK(fsetxattr_multi(&config, &file, "test:$DATA", large, sizeof(large), 0) == 0);
		CHECK(test_remove(&file, "user.DosStreamExt.1.test:$DATA") == 0);
		if (argv[1][0] == 'r') fail_extent = EIO;
		ret = fgetxattr_multi(&config, &file, "test:$DATA", buf, sizeof(buf));
		if (fail_extent) CHECK(ret == -1 && errno == EIO);
		else { CHECK(ret == 8); CHECK(memcmp(buf, large, 7) == 0 && buf[7] == 0); }
	} else { CHECK(false); }
	TALLOC_FREE(frame);
	return 0;
}
