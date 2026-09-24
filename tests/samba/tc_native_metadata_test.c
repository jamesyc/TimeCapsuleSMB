/* Exercise the native-HFS fruit and xattr_tdb paths with controlled lower VFS
 * and AirPort private-syscall backends. */
#include "includes.h"
#include "system/filesys.h"
#include "smbd/smbd.h"
#include "smbd/globals.h"
#include "smbd/fd_handle.h"
#include "lib/adouble.h"

struct db_context;

#undef ENOATTR
#define ENOATTR 193
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: %s (errno=%d)\n", __FILE__, __LINE__, #x, errno); fflush(stderr); _exit(90); } } while (0)

struct test_store {
	bool exists;
	char name[128];
	uint8_t data[4096];
	size_t size;
	int get_error;
	int set_error;
	int remove_error;
};

static struct test_store native_store;
/* Attributes of a symlink itself, reached by path (patch 0045). */
static struct test_store link_store;
static const char *link_store_path = "/share/link";
static bool link_list_duplicates;
static unsigned link_ops;
static struct test_store fake_tdb;
static unsigned native_gets;
static unsigned native_sets;
static unsigned native_removes;
static unsigned native_locks;
static unsigned native_unlocks;
static unsigned native_lock_depth;
static bool require_native_lock;
static unsigned tdb_gets;
static unsigned next_xattr_gets;
static unsigned next_xattr_sets;
static unsigned next_xattr_removes;
static bool native_set_returns_size;
static ssize_t native_shrink_on_read;
static int native_second_read_error;
static bool tdb_create_tags_on_list;
static off_t next_streaminfo_size;
static int native_options;
static char mutation_order[16];
static size_t mutation_count;
static mode_t test_fstatat_base_mode;
static int test_fstatat_stream_error;
static int test_fgetxattr_error;
static struct files_struct *test_pathref_fsp;
static unsigned next_openat_calls;
static int next_openat_failures;
static int next_openat_error;
static int next_openat_flags;
static int next_pwrite_error;
static int primary_remove_error;
static int ad_fset_error;
static off_t test_resource_size;
static int test_resource_stat_error;
static char test_openat_name[256];
static bool test_openat_had_stream;
static const struct files_struct *test_openat_dirfsp;
static char test_fstatat_name[256];
static const struct files_struct *test_fstatat_dirfsp;
static bool strict_native_at_context;

static int test_primary_remove(void);

static int test_xattr_flock(int fd, int operation)
{
	if (fd == -1) {
		/* As the kernel does for a descriptor-less handle. */
		errno = EBADF;
		return -1;
	}
	CHECK(fd == 42);
	if (operation == LOCK_EX) {
		CHECK(native_lock_depth == 0);
		native_lock_depth = 1;
		native_locks++;
		return 0;
	}
	CHECK(operation == LOCK_UN && native_lock_depth == 1);
	native_lock_depth = 0;
	native_unlocks++;
	return 0;
}

static void record_mutation(char backend)
{
	CHECK(mutation_count + 1 < sizeof(mutation_order));
	mutation_order[mutation_count++] = backend;
	mutation_order[mutation_count] = '\0';
}

static void reset_stores(void)
{
	ZERO_STRUCT(native_store);
	ZERO_STRUCT(fake_tdb);
	native_gets = native_sets = native_removes = tdb_gets = 0;
	native_locks = native_unlocks = native_lock_depth = 0;
	require_native_lock = false;
	next_xattr_gets = next_xattr_sets = next_xattr_removes = 0;
	native_set_returns_size = false;
	native_shrink_on_read = -1;
	native_second_read_error = 0;
	tdb_create_tags_on_list = false;
	next_streaminfo_size = -1;
	native_options = -1;
	test_fstatat_base_mode = S_IFREG | 0600;
	test_fstatat_stream_error = ENOENT;
	test_fgetxattr_error = ENOATTR;
	next_openat_calls = 0;
	next_openat_failures = 0;
	next_openat_error = ENOENT;
	next_openat_flags = 0;
	next_pwrite_error = 0;
	primary_remove_error = 0;
	ad_fset_error = 0;
	test_resource_size = 0;
	test_resource_stat_error = 0;
	test_openat_name[0] = '\0';
	test_openat_had_stream = false;
	test_openat_dirfsp = NULL;
	test_fstatat_name[0] = '\0';
	test_fstatat_dirfsp = NULL;
	ZERO_STRUCT(link_store);
	link_list_duplicates = false;
	link_ops = 0;
	mutation_count = 0;
	mutation_order[0] = '\0';
	errno = 0;
}

static void seed_store(struct test_store *store,
			const char *name,
			const void *data,
			size_t size)
{
	CHECK(size <= sizeof(store->data));
	store->exists = true;
	snprintf(store->name, sizeof(store->name), "%s", name);
	memcpy(store->data, data, size);
	store->size = size;
}

static long test_native_syscall_376(const char *path,
				    const char *name,
				    const void *value,
				    size_t size,
				    int options)
{
	link_ops++;
	CHECK(options == 0);
	if (strcmp(path, link_store_path) != 0) {
		errno = ENOENT;
		return -1;
	}
	seed_store(&link_store, name, value, size);
	/* NetBSD 4 returns the size written; the wrapper must report 0. */
	return (long)size;
}

static long test_native_syscall_379(const char *path,
				    const char *name,
				    void *value,
				    size_t size)
{
	link_ops++;
	if (strcmp(path, link_store_path) != 0) {
		errno = ENOENT;
		return -1;
	}
	if (!link_store.exists || strcmp(link_store.name, name) != 0) {
		errno = ENOATTR;
		return -1;
	}
	if (value == NULL) {
		return link_store.size;
	}
	if (size < link_store.size) {
		errno = ERANGE;
		return -1;
	}
	memcpy(value, link_store.data, link_store.size);
	return link_store.size;
}

static long test_native_syscall_382(const char *path, char *list, size_t size)
{
	size_t one, required;

	link_ops++;
	if (strcmp(path, link_store_path) != 0) {
		errno = ENOENT;
		return -1;
	}
	if (!link_store.exists) {
		return 0;
	}
	one = strlen(link_store.name) + 1;
	/* NetBSD 6 lists an HFS attribute twice. */
	required = link_list_duplicates ? 2 * one : one;
	if (list == NULL) {
		return required;
	}
	if (size < required) {
		errno = ERANGE;
		return -1;
	}
	memcpy(list, link_store.name, one);
	if (link_list_duplicates) {
		memcpy(list + one, link_store.name, one);
	}
	return required;
}

static long test_native_syscall_385(const char *path, const char *name)
{
	link_ops++;
	if (strcmp(path, link_store_path) != 0) {
		errno = ENOENT;
		return -1;
	}
	if (!link_store.exists || strcmp(link_store.name, name) != 0) {
		errno = ENOATTR;
		return -1;
	}
	ZERO_STRUCT(link_store);
	return 0;
}

static long test_native_syscall_377(int fd,
				    const char *name,
				    const void *value,
				    size_t size,
				    int options)
{
	if (require_native_lock) {
		CHECK(native_lock_depth == 1);
	}
	if (fd != 42) {
		errno = EBADF;
		return -1;
	}
	native_options = options;
	native_sets++;
	record_mutation('N');
	if (native_store.set_error != 0) {
		errno = native_store.set_error;
		return -1;
	}
	seed_store(&native_store, name, value, size);
	return native_set_returns_size ? (long)size : 0;
}

static long test_native_syscall_380(int fd,
				    const char *name,
				    void *value,
				    size_t size)
{
	if (fd != 42) {
		errno = EBADF;
		return -1;
	}
	native_gets++;
	if (native_store.get_error != 0) {
		errno = native_store.get_error;
		return -1;
	}
	if (!native_store.exists || strcmp(native_store.name, name) != 0) {
		errno = ENOATTR;
		return -1;
	}
	if (value == NULL) {
		return native_store.size;
	}
	if (native_second_read_error != 0) {
		ZERO_STRUCT(native_store);
		errno = native_second_read_error;
		return -1;
	}
	if (native_shrink_on_read >= 0 &&
	    (size_t)native_shrink_on_read < native_store.size)
	{
		native_store.size = native_shrink_on_read;
	}
	if (size < native_store.size) {
		errno = ERANGE;
		return -1;
	}
	memcpy(value, native_store.data, native_store.size);
	return native_store.size;
}

static long test_native_syscall_383(int fd, char *list, size_t size)
{
	size_t required;

	if (fd != 42) {
		errno = EBADF;
		return -1;
	}
	if (!native_store.exists) {
		return 0;
	}
	required = strlen(native_store.name) + 1;
	if (list == NULL) {
		return required;
	}
	if (size < required) {
		errno = ERANGE;
		return -1;
	}
	memcpy(list, native_store.name, required);
	return required;
}

static long test_native_syscall_386(int fd, const char *name)
{
	if (require_native_lock) {
		CHECK(native_lock_depth == 1);
	}
	if (fd != 42) {
		errno = EBADF;
		return -1;
	}
	native_removes++;
	record_mutation('N');
	if (native_store.remove_error != 0) {
		errno = native_store.remove_error;
		return -1;
	}
	if (!native_store.exists || strcmp(native_store.name, name) != 0) {
		errno = ENOATTR;
		return -1;
	}
	ZERO_STRUCT(native_store);
	return 0;
}

static ssize_t test_tdb_getattr(struct db_context *db,
				 TALLOC_CTX *mem_ctx,
				 const struct file_id *id,
				 const char *name,
				 DATA_BLOB *blob)
{
	(void)db;
	(void)id;
	tdb_gets++;
	if (fake_tdb.get_error != 0) {
		errno = fake_tdb.get_error;
		return -1;
	}
	if (!fake_tdb.exists || strcmp(fake_tdb.name, name) != 0) {
		errno = ENOATTR;
		return -1;
	}
	blob->length = fake_tdb.size;
	blob->data = talloc_memdup(mem_ctx, fake_tdb.data, fake_tdb.size);
	if (fake_tdb.size != 0 && blob->data == NULL) {
		errno = ENOMEM;
		return -1;
	}
	return fake_tdb.size;
}

static int test_tdb_setattr(struct db_context *db,
			    const struct file_id *id,
			    const char *name,
			    const void *value,
			    size_t size,
			    int flags)
{
	(void)db;
	(void)id;
	(void)flags;
	record_mutation('T');
	if (fake_tdb.set_error != 0) {
		errno = fake_tdb.set_error;
		return -1;
	}
	seed_store(&fake_tdb, name, value, size);
	return 0;
}

static ssize_t test_tdb_listattr(struct db_context *db,
				 const struct file_id *id,
				 char *list,
				 size_t size)
{
	size_t required;
	(void)db;
	(void)id;
	if (tdb_create_tags_on_list) {
		static const uint8_t value[] = {'t', 0};

		tdb_create_tags_on_list = false;
		seed_store(&fake_tdb,
			   "user.DosStream.com.apple.metadata:_kMDItemUserTags:$DATA",
			   value, sizeof(value));
	}
	if (!fake_tdb.exists) {
		return 0;
	}
	required = strlen(fake_tdb.name) + 1;
	if (size == 0) {
		return required;
	}
	if (size < required) {
		errno = ERANGE;
		return -1;
	}
	memcpy(list, fake_tdb.name, required);
	return required;
}

static int test_tdb_removeattr(struct db_context *db,
			       const struct file_id *id,
			       const char *name)
{
	(void)db;
	(void)id;
	record_mutation('T');
	if (fake_tdb.remove_error != 0) {
		errno = fake_tdb.remove_error;
		return -1;
	}
	if (!fake_tdb.exists || strcmp(fake_tdb.name, name) != 0) {
		errno = ENOATTR;
		return -1;
	}
	ZERO_STRUCT(fake_tdb);
	return 0;
}

static int test_next_fstat(struct vfs_handle_struct *handle,
			   struct files_struct *fsp,
			   SMB_STRUCT_STAT *sbuf)
{
	(void)handle;
	(void)fsp;
	ZERO_STRUCTP(sbuf);
	sbuf->st_ex_mode = S_IFREG | 0600;
	sbuf->st_ex_size = test_resource_size;
	return 0;
}

static struct file_id test_next_file_id(struct vfs_handle_struct *handle,
					const SMB_STRUCT_STAT *sbuf)
{
	struct file_id id = {0};
	(void)handle;
	(void)sbuf;
	return id;
}

static ssize_t test_next_fgetxattr(struct vfs_handle_struct *handle,
				    struct files_struct *fsp,
				    const char *name,
				    void *value,
				    size_t size)
{
	(void)handle;
	(void)fsp;
	(void)name;
	(void)value;
	(void)size;
	next_xattr_gets++;
	errno = ENOATTR;
	return -1;
}

static int test_next_fsetxattr(struct vfs_handle_struct *handle,
				struct files_struct *fsp,
				const char *name,
				const void *value,
				size_t size,
				int flags)
{
	(void)handle;
	(void)fsp;
	(void)name;
	(void)value;
	(void)size;
	(void)flags;
	next_xattr_sets++;
	return 0;
}

static int test_next_fremovexattr(struct vfs_handle_struct *handle,
				   struct files_struct *fsp,
				   const char *name)
{
	(void)handle;
	(void)fsp;
	(void)name;
	next_xattr_removes++;
	return 0;
}

static int test_next_fstatat(struct vfs_handle_struct *handle,
			     const struct files_struct *dirfsp,
			     const struct smb_filename *smb_fname,
			     SMB_STRUCT_STAT *sbuf,
			     int flags)
{
	(void)handle;
	(void)flags;
	test_fstatat_dirfsp = dirfsp;
	snprintf(test_fstatat_name, sizeof(test_fstatat_name), "%s",
		smb_fname->base_name);
	if (strstr(smb_fname->base_name, "/..namedfork/rsrc") != NULL) {
		if (strict_native_at_context &&
		    smb_fname->base_name[0] != '/' && dirfsp == NULL)
		{
			errno = EINVAL;
			return -1;
		}
		if (test_resource_stat_error != 0) {
			errno = test_resource_stat_error;
			return -1;
		}
		ZERO_STRUCTP(sbuf);
		sbuf->st_ex_mode = S_IFREG | 0600;
		sbuf->st_ex_size = test_resource_size;
		return 0;
	}
	if (smb_fname->stream_name != NULL) {
		errno = test_fstatat_stream_error;
		return -1;
	}
	ZERO_STRUCTP(sbuf);
	sbuf->st_ex_mode = test_fstatat_base_mode;
	sbuf->st_ex_size = 4096;
	sbuf->st_ex_blocks = 8;
	sbuf->st_ex_ino = 100;
	return 0;
}

static ssize_t test_fgetxattr(struct files_struct *fsp,
			      const char *name,
			      void *value,
			      size_t size)
{
	if (strcmp(name, "com.apple.FinderInfo") == 0) {
		return test_native_syscall_380(
			fsp_get_pathref_fd(fsp), name, value, size);
	}
	errno = test_fgetxattr_error;
	return -1;
}

static int test_fsetxattr(struct files_struct *fsp,
			  const char *name,
			  const void *value,
			  size_t size,
			  int flags)
{
	return test_native_syscall_377(
		fsp_get_pathref_fd(fsp), name, value, size, flags);
}

static int test_fremovexattr(struct files_struct *fsp, const char *name)
{
	if (strcmp(name, "com.apple.FinderInfo") == 0) {
		return test_native_syscall_386(fsp_get_pathref_fd(fsp), name);
	}
	return test_primary_remove();
}

static NTSTATUS test_openat_pathref_fsp_lcomp(
	struct files_struct *dirfsp,
	struct smb_filename *smb_fname,
	uint32_t ucf_flags)
{
	(void)dirfsp;
	(void)ucf_flags;
	smb_fname->fsp = test_pathref_fsp;
	return NT_STATUS_OK;
}

static NTSTATUS test_openat_pathref_fsp(
	struct files_struct *dirfsp,
	struct smb_filename *smb_fname)
{
	return test_openat_pathref_fsp_lcomp(dirfsp, smb_fname, 0);
}

static uint64_t test_smb_roundup(connection_struct *conn, uint64_t value)
{
	(void)conn;
	return value;
}

static NTSTATUS test_next_fstreaminfo(struct vfs_handle_struct *handle,
				      struct files_struct *fsp,
				      TALLOC_CTX *mem_ctx,
				      unsigned int *pnum_streams,
				      struct stream_struct **pstreams)
{
	(void)handle;
	(void)fsp;
	if (next_streaminfo_size < 0) {
		return NT_STATUS_OK;
	}
	*pstreams = talloc_zero_array(mem_ctx, struct stream_struct, 1);
	CHECK(*pstreams != NULL);
	(*pstreams)[0].name = talloc_strdup(*pstreams, ":AFP_AfpInfo:$DATA");
	CHECK((*pstreams)[0].name != NULL);
	(*pstreams)[0].size = next_streaminfo_size;
	*pnum_streams = 1;
	return NT_STATUS_OK;
}

static int test_next_openat(struct vfs_handle_struct *handle,
			    const struct files_struct *dirfsp,
			    const struct smb_filename *smb_fname,
			    struct files_struct *fsp,
			    const struct vfs_open_how *how)
{
	(void)handle;
	(void)fsp;
	next_openat_calls++;
	next_openat_flags = how->flags;
	test_openat_dirfsp = dirfsp;
	snprintf(test_openat_name, sizeof(test_openat_name), "%s",
		smb_fname->base_name);
	test_openat_had_stream = smb_fname->stream_name != NULL;
	if (strict_native_at_context &&
	    smb_fname->base_name[0] != '/' && dirfsp == NULL)
	{
		errno = EINVAL;
		return -1;
	}
	if (next_openat_failures > 0) {
		next_openat_failures--;
		errno = next_openat_error;
		return -1;
	}
	return 77;
}

static ssize_t test_next_pwrite(struct vfs_handle_struct *handle,
				struct files_struct *fsp,
				const void *data,
				size_t n,
				off_t offset)
{
	(void)handle;
	(void)fsp;
	(void)data;
	(void)offset;
	record_mutation('P');
	if (next_pwrite_error != 0) {
		errno = next_pwrite_error;
		return -1;
	}
	return n;
}

static int test_primary_remove(void)
{
	record_mutation('P');
	if (primary_remove_error != 0) {
		errno = primary_remove_error;
		return -1;
	}
	return 0;
}

static int test_ad_fset(struct vfs_handle_struct *handle,
			struct adouble *ad,
			struct files_struct *fsp)
{
	(void)handle;
	(void)ad;
	(void)fsp;
	record_mutation('P');
	if (ad_fset_error != 0) {
		errno = ad_fset_error;
		return -1;
	}
	return 0;
}

static NTSTATUS test_missing_synthetic_pathref(void)
{
	return NT_STATUS_OBJECT_NAME_NOT_FOUND;
}

#define TC_AIRPORT_NATIVE_XATTR_SYSCALLS 1
#define TC_TEST_XATTR_CALL(number) TC_TEST_XATTR_CALL_I(number)
#define TC_TEST_XATTR_CALL_I(number) test_native_syscall_##number
#define TC_AIRPORT_XATTR_SYSCALL(number, ...) \
	TC_TEST_XATTR_CALL(number)(__VA_ARGS__)
#define TC_AIRPORT_XATTR_FLOCK(fd, operation) test_xattr_flock((fd), (operation))
#define TC_AIRPORT_PATH_IS_HFS(path) false
#undef SMB_VFS_NEXT_FSTAT
#define SMB_VFS_NEXT_FSTAT(h, f, s) test_next_fstat((h), (f), (s))
#undef SMB_VFS_NEXT_FILE_ID_CREATE
#define SMB_VFS_NEXT_FILE_ID_CREATE(h, s) test_next_file_id((h), (s))
#undef SMB_VFS_NEXT_FGETXATTR
#define SMB_VFS_NEXT_FGETXATTR(h, f, n, v, s) test_next_fgetxattr((h), (f), (n), (v), (s))
#undef SMB_VFS_NEXT_FSETXATTR
#define SMB_VFS_NEXT_FSETXATTR(h, f, n, v, s, x) test_next_fsetxattr((h), (f), (n), (v), (s), (x))
#undef SMB_VFS_NEXT_FREMOVEXATTR
#define SMB_VFS_NEXT_FREMOVEXATTR(h, f, n) test_next_fremovexattr((h), (f), (n))
#define xattr_tdb_getattr test_tdb_getattr
#define xattr_tdb_setattr test_tdb_setattr
#define xattr_tdb_listattr test_tdb_listattr
#define xattr_tdb_removeattr test_tdb_removeattr
#define openat_pathref_fsp test_openat_pathref_fsp
#undef vfs_xattr_tdb_init
#define vfs_xattr_tdb_init regression_xattr_tdb_init
#include "vfs_xattr_tdb.c"
#undef vfs_xattr_tdb_init
#undef openat_pathref_fsp
#undef xattr_tdb_removeattr
#undef xattr_tdb_listattr
#undef xattr_tdb_setattr
#undef xattr_tdb_getattr
#undef SMB_VFS_NEXT_FREMOVEXATTR
#undef SMB_VFS_NEXT_FSETXATTR
#undef SMB_VFS_NEXT_FGETXATTR

#define smb_roundup test_smb_roundup
#undef SMB_VFS_NEXT_OPENAT
#define SMB_VFS_NEXT_OPENAT(h, d, n, f, o) test_next_openat((h), (d), (n), (f), (o))
#undef SMB_VFS_NEXT_PWRITE
#define SMB_VFS_NEXT_PWRITE(h, f, d, n, o) test_next_pwrite((h), (f), (d), (n), (o))
#undef SMB_VFS_NEXT_UNLINKAT
#define SMB_VFS_NEXT_UNLINKAT(h, d, n, f) test_primary_remove()
#undef SMB_VFS_FREMOVEXATTR
#define SMB_VFS_FREMOVEXATTR(f, n) test_fremovexattr((f), (n))
#undef SMB_VFS_FSETXATTR
#define SMB_VFS_FSETXATTR(f, n, v, s, x) test_fsetxattr((f), (n), (v), (s), (x))
#undef SMB_VFS_NEXT_FSTATAT
#define SMB_VFS_NEXT_FSTATAT(h, d, n, s, f) test_next_fstatat((h), (d), (n), (s), (f))
#undef SMB_VFS_NEXT_FSTREAMINFO
#define SMB_VFS_NEXT_FSTREAMINFO(h, f, m, n, s) test_next_fstreaminfo((h), (f), (m), (n), (s))
#undef SMB_VFS_FGETXATTR
#define SMB_VFS_FGETXATTR(f, n, v, s) test_fgetxattr((f), (n), (v), (s))
#define ad_get(...) (errno = ENOENT, (struct adouble *)NULL)
#define ad_fget(...) ((struct adouble *)NULL)
#define ad_fset(h, a, f) test_ad_fset((h), (a), (f))
#define synthetic_pathref(...) test_missing_synthetic_pathref()
#define openat_pathref_fsp_lcomp test_openat_pathref_fsp_lcomp
#undef vfs_fruit_init
#define vfs_fruit_init regression_fruit_init
#include "vfs_fruit.c"
#undef vfs_fruit_init
#undef openat_pathref_fsp_lcomp
#undef synthetic_pathref
#undef ad_fset
#undef ad_fget
#undef ad_get
#undef SMB_VFS_FGETXATTR
#undef SMB_VFS_FSETXATTR
#undef SMB_VFS_FREMOVEXATTR
#undef SMB_VFS_NEXT_FSTREAMINFO
#undef SMB_VFS_NEXT_FSTATAT
#undef SMB_VFS_NEXT_UNLINKAT
#undef SMB_VFS_NEXT_PWRITE
#undef SMB_VFS_NEXT_OPENAT
#undef smb_roundup

/* Exercise the real stream fragmentation layer against the real native
 * xattr backend; a backend-only test misses the synthetic marker boundary. */
static struct vfs_handle_struct stream_backend_handle;
#define SMB_VFS_NEXT_FSTAT(h, f, s) test_next_fstat((h), (f), (s))
#define SMB_VFS_NEXT_FILE_ID_CREATE(h, s) test_next_file_id((h), (s))
#define SMB_VFS_NEXT_FGETXATTR(h, f, n, v, s) test_next_fgetxattr((h), (f), (n), (v), (s))
#define SMB_VFS_NEXT_FSETXATTR(h, f, n, v, s, x) test_next_fsetxattr((h), (f), (n), (v), (s), (x))
#define SMB_VFS_NEXT_FREMOVEXATTR(h, f, n) test_next_fremovexattr((h), (f), (n))
#define SMB_VFS_NEXT_OPENAT(h, d, n, f, o) test_next_openat((h), (d), (n), (f), (o))
#define SMB_VFS_NEXT_PWRITE(h, f, d, n, o) test_next_pwrite((h), (f), (d), (n), (o))
#define SMB_VFS_NEXT_UNLINKAT(h, d, n, f) test_primary_remove()
#define SMB_VFS_NEXT_FSTATAT(h, d, n, s, f) test_next_fstatat((h), (d), (n), (s), (f))
#define SMB_VFS_NEXT_FSTREAMINFO(h, f, m, n, s) test_next_fstreaminfo((h), (f), (m), (n), (s))
#define SMB_VFS_FGETXATTR(f,n,v,s) xattr_tdb_fgetxattr(&stream_backend_handle,f,n,v,s)
#define SMB_VFS_FSETXATTR(f,n,v,s,g) xattr_tdb_fsetxattr(&stream_backend_handle,f,n,v,s,g)
#define SMB_VFS_FREMOVEXATTR(f,n) xattr_tdb_fremovexattr(&stream_backend_handle,f,n)
#define lp_smbd_max_xattr_size(snum) 3802
#undef vfs_streams_xattr_init
#define vfs_streams_xattr_init regression_native_streams_init
#include "vfs_streams_xattr.c"
#undef vfs_streams_xattr_init
#undef lp_smbd_max_xattr_size
#undef SMB_VFS_FGETXATTR
#undef SMB_VFS_FSETXATTR
#undef SMB_VFS_FREMOVEXATTR

static void test_native_stream_boundary(files_struct *fsp)
{
	struct xattr_tdb_config backend = {.native_hfs = true};
	struct streams_xattr_config config = {
		.prefix = "user.DosStream.", .ext_prefix = "user.DosStreamExt.",
		.max_extents = 34, .store_stream_type = true, .native_hfs = true,
	};
	const char *name = "com.apple.test:$DATA";
	size_t sizes[] = {0, 3801, 3802};
	uint8_t value[3804], output[3804];
	size_t i;

	stream_backend_handle = (struct vfs_handle_struct) {.conn = fsp->conn, .data = &backend};
	for (i = 0; i < ARRAY_SIZE(sizes); i++) {
		size_t size = sizes[i];
		reset_stores();
		memset(value, 0x61, sizeof(value));
		value[size] = 0;
		CHECK(fsetxattr_multi(&config, fsp, name, value, size + 1, 0) == 0);
		CHECK(native_store.size == size);
		CHECK(strcmp(native_store.name, "com.apple.test") == 0);
		CHECK(fgetxattr_multi(&config, fsp, name, output, sizeof(output)) == size + 1);
		CHECK(memcmp(value, output, size + 1) == 0);
		/* Values initially created by AFP/native migration use the same path. */
		seed_store(&native_store, "com.apple.test", value, size);
		CHECK(fgetxattr_multi(&config, fsp, name, output, sizeof(output)) == size + 1);
		memset(value, 0x62, sizeof(value)); value[3803] = 0;
		CHECK(fsetxattr_multi(&config, fsp, name, value, 3804, 0) == -1);
		CHECK(errno == E2BIG && native_store.size == size);
		CHECK(size == 0 || native_store.data[0] == 0x61);
		/* Shrinking must not leave a synthetic extent or trailing bytes. */
		CHECK(fsetxattr_multi(&config, fsp, name, "x", 2, 0) == 0);
		CHECK(native_store.size == 1 && native_store.data[0] == 'x');
	}
}

static void init_file(TALLOC_CTX *mem_ctx,
		      connection_struct *conn,
		      files_struct *fsp,
		      struct smb_filename *smb_fname)
{
	fsp->conn = conn;
	fsp->fh = fd_handle_create(mem_ctx);
	CHECK(fsp->fh != NULL);
	fsp_set_fd(fsp, 42);
	fsp->fsp_name = smb_fname;
	test_pathref_fsp = fsp;
	smb_fname->base_name = discard_const_p(char, "object");
	smb_fname->fsp = fsp;
	smb_fname->st.st_ex_mode = S_IFREG | 0600;
}

static void init_stream_file(TALLOC_CTX *mem_ctx,
			     connection_struct *conn,
			     files_struct *base_fsp,
			     files_struct *stream_fsp,
			     struct smb_filename *stream_name)
{
	stream_fsp->conn = conn;
	stream_fsp->fh = fd_handle_create(mem_ctx);
	CHECK(stream_fsp->fh != NULL);
	fsp_set_fd(stream_fsp, -1);
	stream_fsp->base_fsp = base_fsp;
	stream_fsp->fsp_name = stream_name;
	stream_name->base_name = discard_const_p(char, "object");
	stream_name->stream_name = discard_const_p(char, ":AFP_AfpInfo");
	stream_name->fsp = stream_fsp;
	stream_name->st.st_ex_mode = S_IFREG | 0600;
}

static void remove_fio(struct vfs_handle_struct *handle,
		       files_struct *fsp)
{
	struct fio *fio = VFS_FETCH_FSP_EXTENSION(handle, fsp);
	int fd = fsp_get_pathref_fd(fsp);

	if (fio != NULL && fio->fake_fd && fd != -1) {
		CHECK(vfs_fake_fd_close(fd) == 0);
	}
	fsp_set_fd(fsp, -1);
	VFS_REMOVE_FSP_EXTENSION(handle, fsp);
}


static void test_syscall_abi(void)
{
	const uint8_t value[] = {1, 2, 3, 4};
	uint8_t result[sizeof(value)] = {0};

	reset_stores();
	require_native_lock = true;
	native_set_returns_size = true;
	CHECK(tc_airport_fsetxattr(42, "test", value, sizeof(value), 0) == 0);
	CHECK(native_sets == 1 && native_options == 0);
	CHECK(native_locks == 1 && native_unlocks == 1 && native_lock_depth == 0);
	CHECK(tc_airport_fgetxattr(42, "test", NULL, 0) == sizeof(value));
	CHECK(tc_airport_fgetxattr(42, "test", result, sizeof(result)) == sizeof(value));
	CHECK(memcmp(result, value, sizeof(value)) == 0);
	CHECK(tc_airport_fremovexattr(42, "test") == 0);
	CHECK(!native_store.exists);
	CHECK(native_locks == 2 && native_unlocks == 2 && native_lock_depth == 0);
	require_native_lock = false;

	reset_stores();
	CHECK(tc_airport_fsetxattr(42, "test", value, sizeof(value), 0) == 0);
	native_store.get_error = EACCES;
	CHECK(tc_airport_fgetxattr(42, "test", result, sizeof(result)) == -1);
	CHECK(errno == EACCES);

	reset_stores();
	require_native_lock = true;
	CHECK(tc_airport_fsetxattr(
		      42, "test", value, sizeof(value), XATTR_CREATE) == 0);
	errno = 0;
	CHECK(tc_airport_fsetxattr(
		      42, "test", value, sizeof(value), XATTR_CREATE) == -1);
	CHECK(errno == EEXIST);
	CHECK(native_sets == 1);
	CHECK(native_locks == 2 && native_unlocks == 2 && native_lock_depth == 0);
	require_native_lock = false;
}

static void test_native_xattrs(struct vfs_handle_struct *handle,
			       struct files_struct *fsp,
			       TALLOC_CTX *mem_ctx)
{
	struct xattr_tdb_config config = {.native_hfs = true};
	const char *stream_name =
		"user.DosStream.com.apple.metadata:_kMDItemUserTags:$DATA";
	const char *native_name = "com.apple.metadata:_kMDItemUserTags";
	const uint8_t original[] = {'r', 'e', 'd'};
	const uint8_t updated[] = {'b', 'l', 'u', 'e', 0};
	uint8_t result[32] = {0};
	uint8_t extended[] = {'x', 1};

	handle->data = &config;
	reset_stores();
	seed_store(&native_store, native_name, original, sizeof(original));
	CHECK(xattr_tdb_fgetxattr(handle, fsp, stream_name, NULL, 0) ==
	      sizeof(original) + 1);
	CHECK(xattr_tdb_fgetxattr(handle, fsp, stream_name,
				 result, sizeof(result)) == sizeof(original) + 1);
	CHECK(memcmp(result, original, sizeof(original)) == 0);
	CHECK(result[sizeof(original)] == 0);
	CHECK(tdb_gets == 0);

	CHECK(xattr_tdb_fsetxattr(handle, fsp, stream_name,
				 updated, sizeof(updated), 0) == 0);
	CHECK(native_store.size == sizeof(updated) - 1);
	CHECK(memcmp(native_store.data, updated, sizeof(updated) - 1) == 0);
	CHECK(native_options == 0);

	errno = 0;
	CHECK(xattr_tdb_fsetxattr(handle, fsp, stream_name,
				 extended, sizeof(extended), 0) == -1);
	CHECK(errno == E2BIG);

	errno = 0;
	CHECK(xattr_tdb_fgetxattr(
		      handle, fsp,
		      "user.DosStream.com.apple.FinderInfo:$DATA",
		      result, sizeof(result)) == -1);
	CHECK(errno == ENOATTR);

	errno = 0;
	CHECK(xattr_tdb_fsetxattr(
		      handle, fsp, "com.apple.ResourceFork",
		      updated, sizeof(updated), 0) == -1);
	CHECK(errno == ENOTSUP);

	CHECK(xattr_tdb_fremovexattr(handle, fsp, stream_name) == 0);
	CHECK(!native_store.exists);

	/* getxattrat is a separate async VFS entry point used by callers that do
	 * not already have an fsp. It must expose the same canonical stream. */
	reset_stores();
	seed_store(&native_store, native_name, original, sizeof(original));
	{
		struct tevent_context *ev = tevent_context_init(mem_ctx);
		struct tevent_req *req;
		struct vfs_aio_state aio_state = {0};
		uint8_t *async_value = NULL;
		ssize_t async_size;

		CHECK(ev != NULL);
		req = xattr_tdb_getxattrat_send(
			mem_ctx, ev, handle, NULL, fsp->fsp_name,
			stream_name, sizeof(result));
		CHECK(req != NULL && tevent_req_poll(req, ev));
		async_size = xattr_tdb_getxattrat_recv(
			req, &aio_state, mem_ctx, &async_value);
		CHECK(async_size == sizeof(original) + 1);
		CHECK(async_value != NULL);
		CHECK(memcmp(async_value, original, sizeof(original)) == 0);
		CHECK(async_value[sizeof(original)] == 0);
		TALLOC_FREE(req);
		TALLOC_FREE(async_value);

		req = xattr_tdb_getxattrat_send(
			mem_ctx, ev, handle, NULL, fsp->fsp_name,
			"user.DosStream.com.apple.FinderInfo:$DATA", 32);
		CHECK(req != NULL && tevent_req_poll(req, ev));
		async_size = xattr_tdb_getxattrat_recv(
			req, &aio_state, mem_ctx, &async_value);
		CHECK(async_size == -1 && aio_state.error == ENOATTR);
		TALLOC_FREE(req);
		TALLOC_FREE(ev);
	}
}

static void test_native_xattr_list(struct vfs_handle_struct *handle,
				   struct files_struct *fsp)
{
	struct xattr_tdb_config config = {.native_hfs = true};
	const char *native_name = "com.apple.metadata:_kMDItemUserTags";
	const char *stream_name =
		"user.DosStream.com.apple.metadata:_kMDItemUserTags:$DATA";
	const uint8_t value[] = {1};
	char list[256] = {0};
	ssize_t required;

	handle->data = &config;
	reset_stores();
	seed_store(&native_store, "com.apple.FinderInfo", value, sizeof(value));
	CHECK(xattr_tdb_flistxattr(handle, fsp, NULL, 0) == 0);

	reset_stores();
	seed_store(&native_store, "com.apple.ResourceFork", value, sizeof(value));
	CHECK(xattr_tdb_flistxattr(handle, fsp, NULL, 0) == 0);

	reset_stores();
	seed_store(&native_store, native_name, value, sizeof(value));
	required = strlen(native_name) + 1 + strlen(stream_name) + 1;
	CHECK(xattr_tdb_flistxattr(handle, fsp, NULL, 0) == required);
	errno = 0;
	CHECK(xattr_tdb_flistxattr(handle, fsp, list, required - 1) == -1);
	CHECK(errno == ERANGE);
	CHECK(xattr_tdb_flistxattr(handle, fsp, list, sizeof(list)) == required);
	CHECK(strcmp(list, native_name) == 0);
	CHECK(strcmp(list + strlen(native_name) + 1, stream_name) == 0);
}

static void test_non_hfs_tdb(struct vfs_handle_struct *handle,
			     struct files_struct *fsp)
{
	struct xattr_tdb_config config = {
		.db = discard_const_p(struct db_context, (void *)1),
		.native_hfs = false,
	};
	const char *name = "user.DosStream.example:$DATA";
	const uint8_t value[] = {'v', 0};
	uint8_t result[8] = {0};

	handle->data = &config;
	reset_stores();
	seed_store(&fake_tdb, name, value, sizeof(value));
	CHECK(xattr_tdb_fgetxattr(handle, fsp, name,
				 result, sizeof(result)) == sizeof(value));
	CHECK(memcmp(result, value, sizeof(value)) == 0);
	CHECK(native_gets == 0);

	CHECK(xattr_tdb_fsetxattr(handle, fsp, name, value, sizeof(value), 0) == 0);
	CHECK(fake_tdb.exists && native_sets == 0);

	config.ignore_user_xattr = true;
	CHECK(xattr_tdb_fgetxattr(handle, fsp, name,
				 result, sizeof(result)) == -1);
	CHECK(next_xattr_gets == 1);
}

static void make_afpinfo(TALLOC_CTX *mem_ctx,
			 const uint8_t finderinfo[AFP_FinderSize],
			 uint8_t buf[AFP_INFO_SIZE])
{
	AfpInfo *ai = afpinfo_new(mem_ctx);

	CHECK(ai != NULL);
	memcpy(ai->afpi_FinderInfo, finderinfo, AFP_FinderSize);
	CHECK(afpinfo_pack(ai, (char *)buf) == AFP_INFO_SIZE);
	TALLOC_FREE(ai);
}

static void test_finderinfo(struct vfs_handle_struct *handle,
			    connection_struct *conn,
			    files_struct *base_fsp,
			    TALLOC_CTX *mem_ctx)
{
	struct fruit_config_data config = {
		.meta = FRUIT_META_NATIVE_HFS,
		.rsrc = FRUIT_RSRC_NATIVE_HFS,
		.native_hfs = true,
	};
	struct smb_filename stream_name = {0};
	files_struct stream_fsp = {0};
	uint8_t original[AFP_FinderSize] = {1};
	uint8_t updated[AFP_FinderSize] = {2};
	uint8_t zero[AFP_FinderSize] = {0};
	uint8_t afpinfo[AFP_INFO_SIZE];
	uint8_t result[AFP_INFO_SIZE] = {0};
	int fd;

	handle->data = &config;
	init_stream_file(mem_ctx, conn, base_fsp, &stream_fsp, &stream_name);
	reset_stores();
	seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
		   original, sizeof(original));
	fd = fruit_open_meta(handle, NULL, &stream_name, &stream_fsp, O_RDWR, 0600);
	CHECK(fd != -1);
	fsp_set_fd(&stream_fsp, fd);
	CHECK(fruit_pread_meta(handle, &stream_fsp,
			      result, sizeof(result), 0) == AFP_INFO_SIZE);
	CHECK(memcmp(result + AFP_OFF_FinderInfo,
		     original, AFP_FinderSize) == 0);

	make_afpinfo(mem_ctx, updated, afpinfo);
	CHECK(fruit_pwrite_meta(handle, &stream_fsp,
			       afpinfo, sizeof(afpinfo), 0) == AFP_INFO_SIZE);
	CHECK(strcmp(mutation_order, "N") == 0);
	CHECK(native_store.size == AFP_FinderSize);
	CHECK(memcmp(native_store.data, updated, AFP_FinderSize) == 0);

	make_afpinfo(mem_ctx, zero, afpinfo);
	CHECK(fruit_pwrite_meta(handle, &stream_fsp,
			       afpinfo, sizeof(afpinfo), 0) == AFP_INFO_SIZE);
	CHECK(!native_store.exists);
	remove_fio(handle, &stream_fsp);

	reset_stores();
	init_stream_file(mem_ctx, conn, base_fsp, &stream_fsp, &stream_name);
	errno = 0;
	CHECK(fruit_open_meta(
		      handle, NULL, &stream_name, &stream_fsp, O_RDONLY, 0600) == -1);
	CHECK(errno == ENOENT);
	remove_fio(handle, &stream_fsp);

	/* Match the old fruit backend: a newly created empty metadata stream
	 * reads as a valid, empty AFP_AfpInfo record before its first write. */
	init_stream_file(mem_ctx, conn, base_fsp, &stream_fsp, &stream_name);
	fd = fruit_open_meta(
		handle, NULL, &stream_name, &stream_fsp, O_RDWR | O_CREAT, 0600);
	CHECK(fd != -1);
	fsp_set_fd(&stream_fsp, fd);
	memset(result, 0xff, sizeof(result));
	CHECK(fruit_pread_meta(handle, &stream_fsp,
			      result, sizeof(result), 0) == AFP_INFO_SIZE);
	CHECK(RIVAL(result, 0) == AFP_Signature);
	CHECK(all_zero(result + AFP_OFF_FinderInfo, AFP_FinderSize));
	remove_fio(handle, &stream_fsp);
}

static void test_finderinfo_views(struct vfs_handle_struct *handle,
				  files_struct *fsp,
				  struct smb_filename *smb_fname,
				  TALLOC_CTX *mem_ctx)
{
	struct fruit_config_data config = {
		.meta = FRUIT_META_NATIVE_HFS,
		.rsrc = FRUIT_RSRC_NATIVE_HFS,
		.native_hfs = true,
	};
	struct smb_filename stream_name = {
		.base_name = discard_const_p(char, "object"),
		.stream_name = discard_const_p(char, ":AFP_AfpInfo"),
	};
	struct readdir_attr_data attr = {0};
	struct stream_struct *streams = NULL;
	SMB_STRUCT_STAT sbuf = {0};
	uint8_t finderinfo[AFP_FinderSize] = {0};
	unsigned int num_streams = 0;
	NTSTATUS status;

	finderinfo[0] = 'T';
	finderinfo[8] = 0x40;
	finderinfo[24] = 0x20;
	handle->data = &config;
	reset_stores();
	seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
		   finderinfo, sizeof(finderinfo));
	CHECK(readdir_attr_meta_finderi(handle, smb_fname, &attr));
	CHECK(attr.attr_data.aapl.finder_info[0] == 'T');
	CHECK(attr.attr_data.aapl.finder_info[8] == 0x40);
	CHECK(attr.attr_data.aapl.finder_info[10] == 0x20);

	CHECK(fruit_fstatat_meta(
		      handle, &config, NULL, &stream_name, &sbuf, 0) == 0);
	CHECK(sbuf.st_ex_size == AFP_INFO_SIZE);
	CHECK(S_ISREG(sbuf.st_ex_mode));

	status = fruit_streaminfo_meta(
		handle, fsp, smb_fname, mem_ctx, &num_streams, &streams, false);
	CHECK(NT_STATUS_IS_OK(status));
	CHECK(num_streams == 1);
	CHECK(strequal_m(streams[0].name, AFPINFO_STREAM));
	CHECK(streams[0].size == AFP_INFO_SIZE);
	TALLOC_FREE(streams);

	CHECK(fruit_unlink_meta(handle, NULL, &stream_name) == 0);
	CHECK(!native_store.exists);
}

static void test_resource_backend(struct vfs_handle_struct *handle,
				  connection_struct *conn,
				  files_struct *base_fsp,
				  TALLOC_CTX *mem_ctx)
{
	struct fruit_config_data config = {
		.meta = FRUIT_META_NATIVE_HFS,
		.rsrc = FRUIT_RSRC_NATIVE_HFS,
		.native_hfs = true,
	};
	struct smb_filename stream_name = {0};
	files_struct stream_fsp = {0};
	struct fio *fio;
	int fd;

	handle->data = &config;
	init_stream_file(mem_ctx, conn, base_fsp, &stream_fsp, &stream_name);
	stream_name.stream_name = discard_const_p(char, ":AFP_Resource");
	reset_stores();
	strict_native_at_context = true;
	fd = fruit_open_rsrc(
		handle, NULL, &stream_name, &stream_fsp,
		O_RDWR | O_CREAT | O_EXCL, 0600);
	CHECK(fd == 77);
	CHECK(strcmp(test_openat_name, "object/..namedfork/rsrc") == 0);
	CHECK(!test_openat_had_stream);
	CHECK(test_openat_dirfsp == conn->cwd_fsp);
	CHECK(next_openat_flags == (O_RDWR | O_CREAT | O_EXCL));
	fsp_set_fd(&stream_fsp, fd);
	fio = VFS_FETCH_FSP_EXTENSION(handle, &stream_fsp);
	CHECK(fio != NULL && fio->type == ADOUBLE_RSRC && !fio->fake_fd);
	CHECK(!fruit_must_handle_aio_stream(fio));
	test_resource_size = 4096;
	{
		SMB_STRUCT_STAT stream_st = {0};
		CHECK(fruit_fstat_rsrc(handle, &stream_fsp, &stream_st, fio) == 0);
		CHECK(stream_st.st_ex_size == 4096);
		CHECK(stream_st.st_ex_ino != base_fsp->fsp_name->st.st_ex_ino);
	}

	CHECK(fruit_pwrite_rsrc(
		      handle, &stream_fsp, "resource", 8, 0) == 8);
	CHECK(strcmp(mutation_order, "P") == 0);
	CHECK(fruit_unlink_rsrc(handle, NULL, &stream_name, false) == 0);
	remove_fio(handle, &stream_fsp);

	/* FILE_OVERWRITE opens an existing fork with O_TRUNC but not O_CREAT.
	 * Check existence before open so the post-open zero length is not mistaken
	 * for a missing stream. */
	reset_stores();
	test_resource_size = 4096;
	init_stream_file(mem_ctx, conn, base_fsp, &stream_fsp, &stream_name);
	stream_name.stream_name = discard_const_p(char, ":AFP_Resource");
	fd = fruit_open_rsrc(
		handle, NULL, &stream_name, &stream_fsp, O_RDWR | O_TRUNC, 0600);
	CHECK(fd == 77);
	CHECK(next_openat_calls == 1);
	CHECK(next_openat_flags == (O_RDWR | O_TRUNC));
	CHECK(strcmp(test_fstatat_name, "object/..namedfork/rsrc") == 0);
	CHECK(test_fstatat_dirfsp == conn->cwd_fsp);
	CHECK(test_openat_dirfsp == conn->cwd_fsp);
	fsp_set_fd(&stream_fsp, fd);
	remove_fio(handle, &stream_fsp);

	reset_stores();
	init_stream_file(mem_ctx, conn, base_fsp, &stream_fsp, &stream_name);
	stream_name.stream_name = discard_const_p(char, ":AFP_Resource");
	errno = 0;
	CHECK(fruit_open_rsrc(
		      handle, NULL, &stream_name, &stream_fsp, O_RDWR, 0600) == -1);
	CHECK(errno == ENOENT);
	CHECK(next_openat_calls == 0);
	remove_fio(handle, &stream_fsp);

	base_fsp->fsp_name->st.st_ex_mode = S_IFDIR | 0700;
	init_stream_file(mem_ctx, conn, base_fsp, &stream_fsp, &stream_name);
	stream_name.stream_name = discard_const_p(char, ":AFP_Resource");
	errno = 0;
	CHECK(fruit_open_rsrc(
		      handle, NULL, &stream_name, &stream_fsp,
		      O_RDWR | O_CREAT, 0600) == -1);
	CHECK(errno == ENOENT);
	base_fsp->fsp_name->st.st_ex_mode = S_IFREG | 0600;
	strict_native_at_context = false;
}

static void test_resource_views(struct vfs_handle_struct *handle,
				files_struct *fsp,
				struct smb_filename *smb_fname,
				TALLOC_CTX *mem_ctx)
{
	struct fruit_config_data config = {
		.meta = FRUIT_META_NATIVE_HFS,
		.rsrc = FRUIT_RSRC_NATIVE_HFS,
		.native_hfs = true,
	};
	struct smb_filename stream_name = {
		.base_name = discard_const_p(char, "object"),
		.stream_name = discard_const_p(char, ":AFP_Resource"),
	};
	struct stream_struct *streams = NULL;
	struct readdir_attr_data attr = {0};
	SMB_STRUCT_STAT sbuf = {0};
	unsigned int num_streams = 0;
	NTSTATUS status;

	handle->data = &config;
	reset_stores();
	test_resource_size = 1024 * 1024;
	CHECK(fruit_fstatat_rsrc(
		      handle, &config, NULL, &stream_name, &sbuf, 0) == 0);
	CHECK(strcmp(test_fstatat_name, "object/..namedfork/rsrc") == 0);
	CHECK(sbuf.st_ex_size == 1024 * 1024);
	CHECK(S_ISREG(sbuf.st_ex_mode));

	status = fruit_streaminfo_rsrc(
		handle, fsp, smb_fname, mem_ctx, &num_streams, &streams);
	CHECK(NT_STATUS_IS_OK(status));
	CHECK(num_streams == 1);
	CHECK(strequal_m(streams[0].name, AFPRESOURCE_STREAM));
	CHECK(streams[0].size == 1024 * 1024);
	TALLOC_FREE(streams);

	reset_stores();
	test_resource_size = 0;
	errno = 0;
	CHECK(fruit_fstatat_rsrc(
		      handle, &config, NULL, &stream_name, &sbuf, 0) == -1);
	CHECK(errno == ENOENT);

	config.readdir_attr_rsize = true;
	reset_stores();
	test_resource_size = 4096;
	status = readdir_attr_macmeta(handle, smb_fname, &attr);
	CHECK(NT_STATUS_IS_OK(status));
	CHECK(attr.attr_data.aapl.rfork_size == 4096);
	CHECK(strcmp(test_fstatat_name, "object/..namedfork/rsrc") == 0);

	smb_fname->st.st_ex_mode = S_IFDIR | 0700;
	reset_stores();
	test_resource_size = 4096;
	ZERO_STRUCT(attr);
	status = readdir_attr_macmeta(handle, smb_fname, &attr);
	CHECK(NT_STATUS_IS_OK(status));
	CHECK(attr.attr_data.aapl.rfork_size == 0);
	CHECK(test_fstatat_name[0] == '\0');

	smb_fname->st.st_ex_mode = S_IFLNK | 0700;
	reset_stores();
	test_resource_size = 4096;
	ZERO_STRUCT(attr);
	status = readdir_attr_macmeta(handle, smb_fname, &attr);
	CHECK(NT_STATUS_IS_OK(status));
	CHECK(attr.attr_data.aapl.rfork_size == 0);
	CHECK(test_fstatat_name[0] == '\0');
	smb_fname->st.st_ex_mode = S_IFREG | 0600;
}

static void test_link_xattrs(struct vfs_handle_struct *handle,
			     connection_struct *conn,
			     TALLOC_CTX *mem_ctx)
{
	struct xattr_tdb_config config = {.native_hfs = true};
	struct smb_filename link_name = {
		.base_name = discard_const_p(char, "link"),
		.st = {.st_ex_mode = S_IFLNK | 0777},
	};
	struct smb_filename file_name = {
		.base_name = discard_const_p(char, "link"),
		.st = {.st_ex_mode = S_IFREG | 0600},
	};
	files_struct link = {.conn = conn, .fsp_name = &link_name};
	files_struct pathref = {.conn = conn, .fsp_name = &file_name};
	const char *stream = "user.DosStream.com.apple.provenance:$DATA";
	const uint8_t value[] = {1, 2, 3, 4};
	const uint8_t prov[] = {'p', 'v', 0};
	char list[256];
	uint8_t result[16] = {0};

	/* Path ABI: act on the link, normalize both kernels' results. */
	reset_stores();
	CHECK(tc_airport_lsetxattr("/share/link", "user.t", value, sizeof(value), 0) == 0);
	CHECK(tc_airport_lgetxattr("/share/link", "user.t", NULL, 0) == sizeof(value));
	CHECK(tc_airport_lgetxattr("/share/link", "user.t", result, sizeof(result)) == sizeof(value));
	CHECK(memcmp(result, value, sizeof(value)) == 0);
	errno = 0;
	CHECK(tc_airport_lsetxattr("/share/link", "user.t", value, sizeof(value), XATTR_CREATE) == -1);
	CHECK(errno == EEXIST);
	errno = 0;
	CHECK(tc_airport_lsetxattr("/share/link", "user.u", value, sizeof(value), XATTR_REPLACE) == -1);
	CHECK(errno == ENOATTR);
	CHECK(tc_airport_lsetxattr("/share/link", "user.t", value, 2, XATTR_REPLACE) == 0);
	CHECK(link_store.size == 2);
	link_list_duplicates = true;
	CHECK(tc_airport_llistxattr("/share/link", NULL, 0) == 7);
	CHECK(tc_airport_llistxattr("/share/link", list, sizeof(list)) == 7);
	CHECK(strcmp(list, "user.t") == 0);
	errno = 0;
	CHECK(tc_airport_llistxattr("/share/link", list, 3) == -1 && errno == ERANGE);
	CHECK(tc_airport_lremovexattr("/share/link", "user.t") == 0);
	CHECK(!link_store.exists);
	CHECK(native_sets == 0 && native_gets == 0 && native_removes == 0);

	/* xattr_tdb: a descriptor-less link uses its own path, never the fd path. */
	handle->data = &config;
	conn->connectpath = discard_const_p(char, "/share");
	link.fh = fd_handle_create(mem_ctx);
	pathref.fh = fd_handle_create(mem_ctx);
	CHECK(link.fh != NULL && pathref.fh != NULL);
	fsp_set_fd(&link, -1);
	fsp_set_fd(&pathref, -1);
	reset_stores();
	CHECK(xattr_tdb_fsetxattr(handle, &link, "user.plain", value, sizeof(value), 0) == 0);
	CHECK(link_store.exists && strcmp(link_store.name, "user.plain") == 0);
	CHECK(xattr_tdb_fgetxattr(handle, &link, "user.plain", result, sizeof(result)) == sizeof(value));
	CHECK(xattr_tdb_flistxattr(handle, &link, list, sizeof(list)) == (ssize_t)strlen("user.plain") + 1);
	CHECK(xattr_tdb_fremovexattr(handle, &link, "user.plain") == 0);
	CHECK(!link_store.exists);
	/* Apple streams map to the native name on the link, as AFP stores them. */
	CHECK(xattr_tdb_fsetxattr(handle, &link, stream, prov, sizeof(prov), 0) == 0);
	CHECK(strcmp(link_store.name, "com.apple.provenance") == 0 && link_store.size == 2);
	CHECK(xattr_tdb_fgetxattr(handle, &link, stream, result, sizeof(result)) == sizeof(prov));
	CHECK(memcmp(result, prov, sizeof(prov)) == 0);
	CHECK(native_sets == 0 && native_gets == 0 && native_removes == 0);
	/* Any other descriptor-less handle still fails; nothing is followed. */
	reset_stores();
	errno = 0;
	CHECK(xattr_tdb_fgetxattr(handle, &pathref, "user.plain", result, sizeof(result)) == -1);
	CHECK(errno == EBADF && link_ops == 0);
	errno = 0;
	CHECK(xattr_tdb_fsetxattr(handle, &pathref, "user.plain", value, sizeof(value), 0) == -1);
	CHECK(errno == EBADF && link_ops == 0 && !link_store.exists);
	fsp_set_fd(&link, -1);
	fsp_set_fd(&pathref, -1);
}

int main(int argc, char **argv)
{
	TALLOC_CTX *frame = NULL;
	connection_struct *conn = NULL;
	files_struct file = {0};
	files_struct cwd = {0};
	struct smb_filename smb_fname = {0};
	struct smb_filename cwd_name = {
		.base_name = discard_const_p(char, "."),
	};
	struct vfs_handle_struct handle = {0};

	CHECK(argc == 2);
	if (strcmp(argv[1], "syscall_abi") == 0 || strcmp(argv[1], "all") == 0) {
		test_syscall_abi();
		if (strcmp(argv[1], "syscall_abi") == 0) {
			_exit(0);
		}
	}
	frame = talloc_stackframe();
	conn = talloc_zero(frame, connection_struct);
	CHECK(conn != NULL);
	handle.conn = conn;
	init_file(frame, conn, &file, &smb_fname);
	cwd.conn = conn;
	cwd.fh = fd_handle_create(frame);
	CHECK(cwd.fh != NULL);
	fsp_set_fd(&cwd, AT_FDCWD);
	cwd.fsp_name = &cwd_name;
	cwd_name.st.st_ex_mode = S_IFDIR | 0700;
	conn->cwd_fsp = &cwd;

	if (strcmp(argv[1], "all") == 0) {
		test_native_xattrs(&handle, &file, frame);
		test_native_xattr_list(&handle, &file);
		test_non_hfs_tdb(&handle, &file);
		test_finderinfo(&handle, conn, &file, frame);
		test_finderinfo_views(&handle, &file, &smb_fname, frame);
		test_resource_backend(&handle, conn, &file, frame);
		test_resource_views(&handle, &file, &smb_fname, frame);
		test_native_stream_boundary(&file);
		test_link_xattrs(&handle, conn, frame);
	} else if (strcmp(argv[1], "link_xattrs") == 0) {
		test_link_xattrs(&handle, conn, frame);
	} else if (strcmp(argv[1], "native_xattrs") == 0) {
		test_native_xattrs(&handle, &file, frame);
	} else if (strcmp(argv[1], "native_xattr_list") == 0) {
		test_native_xattr_list(&handle, &file);
	} else if (strcmp(argv[1], "non_hfs_tdb") == 0) {
		test_non_hfs_tdb(&handle, &file);
	} else if (strcmp(argv[1], "finderinfo") == 0) {
		test_finderinfo(&handle, conn, &file, frame);
	} else if (strcmp(argv[1], "finderinfo_views") == 0) {
		test_finderinfo_views(&handle, &file, &smb_fname, frame);
	} else if (strcmp(argv[1], "stream_boundary") == 0) {
		test_native_stream_boundary(&file);
	} else if (strcmp(argv[1], "resource_backend") == 0) {
		test_resource_backend(&handle, conn, &file, frame);
	} else if (strcmp(argv[1], "resource_views") == 0) {
		test_resource_views(&handle, &file, &smb_fname, frame);
	} else {
		CHECK(false);
	}
	fsp_set_fd(&file, -1);
	fsp_set_fd(&cwd, -1);
	TALLOC_FREE(frame);
	CHECK(!talloc_stackframe_exists());
	_exit(0);
}
