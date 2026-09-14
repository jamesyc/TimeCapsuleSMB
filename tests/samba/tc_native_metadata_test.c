/* Exercise the real Time Capsule metadata bridge with controlled TDB and
 * private-syscall backends. The configured Samba store remains authoritative;
 * native HFS metadata is only a missing-value fallback and write mirror. */
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
	uint8_t data[256];
	size_t size;
	int get_error;
	int set_error;
	int remove_error;
};

static struct test_store native_store;
static struct test_store fake_tdb;
static unsigned native_gets;
static unsigned native_sets;
static unsigned native_removes;
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

static long test_native_syscall_377(int fd,
				    const char *name,
				    const void *value,
				    size_t size,
				    int options)
{
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

static long test_native_syscall_386(int fd, const char *name)
{
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
	(void)dirfsp;
	(void)flags;
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
	(void)fsp;
	(void)name;
	(void)value;
	(void)size;
	errno = test_fgetxattr_error;
	return -1;
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
	(void)dirfsp;
	(void)smb_fname;
	(void)fsp;
	next_openat_calls++;
	next_openat_flags = how->flags;
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
#undef vfs_xattr_tdb_init
#define vfs_xattr_tdb_init regression_xattr_tdb_init
#include "vfs_xattr_tdb.c"
#undef vfs_xattr_tdb_init
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
#define SMB_VFS_FREMOVEXATTR(f, n) test_primary_remove()
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
#undef SMB_VFS_FREMOVEXATTR
#undef SMB_VFS_NEXT_FSTREAMINFO
#undef SMB_VFS_NEXT_FSTATAT
#undef SMB_VFS_NEXT_UNLINKAT
#undef SMB_VFS_NEXT_PWRITE
#undef SMB_VFS_NEXT_OPENAT
#undef smb_roundup

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
	native_set_returns_size = true;
	CHECK(tc_airport_fsetxattr(42, "test", value, sizeof(value)) == 0);
	CHECK(native_sets == 1 && native_options == 0);
	CHECK(tc_airport_fgetxattr(42, "test", NULL, 0) == sizeof(value));
	CHECK(tc_airport_fgetxattr(42, "test", result, sizeof(result)) == sizeof(value));
	CHECK(memcmp(result, value, sizeof(value)) == 0);
	CHECK(tc_airport_fremovexattr(42, "test") == 0);
	CHECK(!native_store.exists);

	reset_stores();
	CHECK(tc_airport_fsetxattr(42, "test", value, sizeof(value)) == 0);
	native_store.get_error = EACCES;
	CHECK(tc_airport_fgetxattr(42, "test", result, sizeof(result)) == -1);
	CHECK(errno == EACCES);
}

static void test_tags_read(struct vfs_handle_struct *handle,
			   struct files_struct *fsp)
{
	struct xattr_tdb_config config = {
		.db = discard_const_p(struct db_context, (void *)1),
		.time_capsule_native_metadata = true,
	};
	const uint8_t hfs[] = {'h', 'f', 's'};
	const uint8_t tdb[] = {'t', 'd', 'b', 0};
	uint8_t result[16] = {0};
	ssize_t ret;

	handle->data = &config;
	reset_stores();
	seed_store(&native_store, TC_USERTAGS_NATIVE_XATTR, hfs, sizeof(hfs));
	CHECK(xattr_tdb_fgetxattr(handle, fsp, TC_USERTAGS_STREAM_XATTR,
				   NULL, 0) == sizeof(hfs) + 1);
	errno = 0;
	CHECK(xattr_tdb_fgetxattr(handle, fsp, TC_USERTAGS_STREAM_XATTR,
				   result, sizeof(hfs)) == -1);
	CHECK(errno == ERANGE);
	ret = xattr_tdb_fgetxattr(handle, fsp, TC_USERTAGS_STREAM_XATTR,
				result, sizeof(result));
	CHECK(ret == sizeof(hfs) + 1);
	CHECK(memcmp(result, hfs, sizeof(hfs)) == 0 && result[sizeof(hfs)] == 0);

	reset_stores();
	seed_store(&native_store, TC_USERTAGS_NATIVE_XATTR, hfs, sizeof(hfs));
	native_shrink_on_read = 1;
	ret = xattr_tdb_fgetxattr(handle, fsp, TC_USERTAGS_STREAM_XATTR,
				result, sizeof(result));
	CHECK(ret == 2 && result[0] == hfs[0] && result[1] == 0);

	reset_stores();
	seed_store(&fake_tdb, TC_USERTAGS_STREAM_XATTR, tdb, sizeof(tdb));
	seed_store(&native_store, TC_USERTAGS_NATIVE_XATTR, hfs, sizeof(hfs));
	ret = xattr_tdb_fgetxattr(handle, fsp, TC_USERTAGS_STREAM_XATTR,
				result, sizeof(result));
	CHECK(ret == sizeof(tdb) && memcmp(result, tdb, sizeof(tdb)) == 0);
	CHECK(native_gets == 0);

	reset_stores();
	config.time_capsule_native_metadata = false;
	seed_store(&native_store, TC_USERTAGS_NATIVE_XATTR, hfs, sizeof(hfs));
	CHECK(xattr_tdb_fgetxattr(handle, fsp, TC_USERTAGS_STREAM_XATTR,
				   result, sizeof(result)) == -1);
	CHECK(native_gets == 0);

	reset_stores();
	config.time_capsule_native_metadata = true;
	config.ignore_user_xattr = true;
	seed_store(&fake_tdb, TC_USERTAGS_STREAM_XATTR, tdb, sizeof(tdb));
	ret = xattr_tdb_fgetxattr(handle, fsp, TC_USERTAGS_STREAM_XATTR,
				result, sizeof(result));
	CHECK(ret == sizeof(tdb) && memcmp(result, tdb, sizeof(tdb)) == 0);
	CHECK(next_xattr_gets == 0);
}

static void test_tags_write(struct vfs_handle_struct *handle,
			    struct files_struct *fsp)
{
	struct xattr_tdb_config config = {
		.db = discard_const_p(struct db_context, (void *)1),
		.time_capsule_native_metadata = true,
	};
	const uint8_t value[] = {'n', 'e', 'w', 0};
	const uint8_t old[] = {'o', 'l', 'd'};
	uint8_t multi[] = {'b', 'i', 'g', 1};

	handle->data = &config;
	reset_stores();
	native_set_returns_size = true;
	CHECK(xattr_tdb_fsetxattr(handle, fsp, TC_USERTAGS_STREAM_XATTR,
				   value, sizeof(value), 0) == 0);
	CHECK(strcmp(mutation_order, "TN") == 0);
	CHECK(fake_tdb.size == sizeof(value));
	CHECK(native_store.size == sizeof(value) - 1);
	CHECK(memcmp(native_store.data, value, sizeof(value) - 1) == 0);

	reset_stores();
	seed_store(&native_store, TC_USERTAGS_NATIVE_XATTR, old, sizeof(old));
	CHECK(xattr_tdb_fsetxattr(handle, fsp, TC_USERTAGS_STREAM_XATTR,
				   multi, sizeof(multi), 0) == 0);
	CHECK(strcmp(mutation_order, "TN") == 0);
	CHECK(fake_tdb.exists && !native_store.exists);

	reset_stores();
	seed_store(&native_store, TC_USERTAGS_NATIVE_XATTR, old, sizeof(old));
	CHECK(xattr_tdb_fsetxattr(handle, fsp, TC_USERTAGS_STREAM_XATTR,
				   "", 1, 0) == 0);
	CHECK(strcmp(mutation_order, "TN") == 0);
	CHECK(fake_tdb.exists && !native_store.exists);

	reset_stores();
	config.ignore_user_xattr = true;
	CHECK(xattr_tdb_fsetxattr(handle, fsp, TC_USERTAGS_STREAM_XATTR,
				   value, sizeof(value), 0) == 0);
	CHECK(next_xattr_sets == 0 && fake_tdb.exists && native_store.exists);
}

static void test_tags_delete(struct vfs_handle_struct *handle,
			     struct files_struct *fsp)
{
	struct xattr_tdb_config config = {
		.db = discard_const_p(struct db_context, (void *)1),
		.time_capsule_native_metadata = true,
	};
	const uint8_t tdb[] = {'t', 0};
	const uint8_t hfs[] = {'h'};

	handle->data = &config;
	reset_stores();
	seed_store(&fake_tdb, TC_USERTAGS_STREAM_XATTR, tdb, sizeof(tdb));
	seed_store(&native_store, TC_USERTAGS_NATIVE_XATTR, hfs, sizeof(hfs));
	CHECK(xattr_tdb_fremovexattr(handle, fsp,
				      TC_USERTAGS_STREAM_XATTR) == 0);
	CHECK(strcmp(mutation_order, "NT") == 0);

	reset_stores();
	config.ignore_user_xattr = true;
	seed_store(&fake_tdb, TC_USERTAGS_STREAM_XATTR, tdb, sizeof(tdb));
	seed_store(&native_store, TC_USERTAGS_NATIVE_XATTR, hfs, sizeof(hfs));
	CHECK(xattr_tdb_fremovexattr(handle, fsp,
				      TC_USERTAGS_STREAM_XATTR) == 0);
	CHECK(next_xattr_removes == 0 && !fake_tdb.exists && !native_store.exists);
	CHECK(!fake_tdb.exists && !native_store.exists);

	reset_stores();
	seed_store(&native_store, TC_USERTAGS_NATIVE_XATTR, hfs, sizeof(hfs));
	CHECK(xattr_tdb_fremovexattr(handle, fsp,
				      TC_USERTAGS_STREAM_XATTR) == 0);
	CHECK(strcmp(mutation_order, "NT") == 0);
}

static void test_tags_errors(struct vfs_handle_struct *handle,
			     struct files_struct *fsp)
{
	struct xattr_tdb_config config = {
		.db = discard_const_p(struct db_context, (void *)1),
		.time_capsule_native_metadata = true,
	};
	const uint8_t value[] = {'v', 0};
	const uint8_t hfs[] = {'h'};
	uint8_t result[8];

	handle->data = &config;
	reset_stores();
	fake_tdb.get_error = EIO;
	seed_store(&native_store, TC_USERTAGS_NATIVE_XATTR, hfs, sizeof(hfs));
	CHECK(xattr_tdb_fgetxattr(handle, fsp, TC_USERTAGS_STREAM_XATTR,
				   result, sizeof(result)) == -1);
	CHECK(native_gets == 0);

	reset_stores();
	fake_tdb.get_error = ENOTSUP;
	seed_store(&native_store, TC_USERTAGS_NATIVE_XATTR, hfs, sizeof(hfs));
	CHECK(xattr_tdb_fgetxattr(handle, fsp, TC_USERTAGS_STREAM_XATTR,
				   result, sizeof(result)) == -1);
	CHECK(native_gets == 0);

	reset_stores();
	native_store.get_error = ENOTSUP;
	CHECK(xattr_tdb_fgetxattr(handle, fsp, TC_USERTAGS_STREAM_XATTR,
				   result, sizeof(result)) == -1);
	CHECK(errno == ENOATTR && native_gets == 1);

	reset_stores();
	fake_tdb.set_error = EIO;
	CHECK(xattr_tdb_fsetxattr(handle, fsp, TC_USERTAGS_STREAM_XATTR,
				   value, sizeof(value), 0) == -1);
	CHECK(native_sets == 0 && strcmp(mutation_order, "T") == 0);

	reset_stores();
	native_store.set_error = EACCES;
	CHECK(xattr_tdb_fsetxattr(handle, fsp, TC_USERTAGS_STREAM_XATTR,
				   value, sizeof(value), 0) == 0);
	CHECK(fake_tdb.exists && strcmp(mutation_order, "TN") == 0);

	reset_stores();
	native_store.set_error = ENOTSUP;
	CHECK(xattr_tdb_fsetxattr(handle, fsp, TC_USERTAGS_STREAM_XATTR,
				   value, sizeof(value), 0) == 0);
	CHECK(fake_tdb.exists && strcmp(mutation_order, "TN") == 0);

	reset_stores();
	seed_store(&fake_tdb, TC_USERTAGS_STREAM_XATTR, value, sizeof(value));
	seed_store(&native_store, TC_USERTAGS_NATIVE_XATTR, hfs, sizeof(hfs));
	native_store.remove_error = EACCES;
	CHECK(xattr_tdb_fremovexattr(handle, fsp,
				      TC_USERTAGS_STREAM_XATTR) == -1);
	CHECK(fake_tdb.exists && strcmp(mutation_order, "N") == 0);

	reset_stores();
	seed_store(&fake_tdb, TC_USERTAGS_STREAM_XATTR, value, sizeof(value));
	native_store.remove_error = ENOTSUP;
	CHECK(xattr_tdb_fremovexattr(handle, fsp,
				      TC_USERTAGS_STREAM_XATTR) == 0);
	CHECK(!fake_tdb.exists && strcmp(mutation_order, "NT") == 0);
}

static void test_tags_list(struct vfs_handle_struct *handle,
			   struct files_struct *fsp)
{
	struct xattr_tdb_config config = {
		.db = discard_const_p(struct db_context, (void *)1),
		.time_capsule_native_metadata = true,
	};
	const uint8_t hfs[] = {'h'};
	const uint8_t tdb[] = {'t', 0};
	char list[256] = {0};
	ssize_t required;

	handle->data = &config;
	reset_stores();
	seed_store(&native_store, TC_USERTAGS_NATIVE_XATTR, hfs, sizeof(hfs));
	required = xattr_tdb_flistxattr(handle, fsp, NULL, 0);
	CHECK(required == sizeof(TC_USERTAGS_STREAM_XATTR));
	CHECK(xattr_tdb_flistxattr(handle, fsp, list, sizeof(list)) == required);
	CHECK(strcmp(list, TC_USERTAGS_STREAM_XATTR) == 0);

	reset_stores();
	seed_store(&fake_tdb, TC_USERTAGS_STREAM_XATTR, tdb, sizeof(tdb));
	seed_store(&native_store, TC_USERTAGS_NATIVE_XATTR, hfs, sizeof(hfs));
	CHECK(xattr_tdb_flistxattr(handle, fsp, list, sizeof(list)) ==
	      sizeof(TC_USERTAGS_STREAM_XATTR));
	CHECK(strcmp(list, TC_USERTAGS_STREAM_XATTR) == 0);
	CHECK(native_gets == 0);

	reset_stores();
	native_store.get_error = ENOTSUP;
	CHECK(xattr_tdb_flistxattr(handle, fsp, NULL, 0) == 0);
	CHECK(native_gets == 1);

	reset_stores();
	seed_store(&fake_tdb, "user.other", tdb, sizeof(tdb));
	seed_store(&native_store, TC_USERTAGS_NATIVE_XATTR, hfs, sizeof(hfs));
	required = sizeof("user.other") + sizeof(TC_USERTAGS_STREAM_XATTR);
	CHECK(xattr_tdb_flistxattr(handle, fsp, NULL, 0) == required);
	errno = 0;
	CHECK(xattr_tdb_flistxattr(handle, fsp, list, required - 1) == -1);
	CHECK(errno == ERANGE);
	ZERO_ARRAY(list);
	CHECK(xattr_tdb_flistxattr(handle, fsp, list, required) == required);
	CHECK(strcmp(list, "user.other") == 0);
	CHECK(strcmp(list + sizeof("user.other"), TC_USERTAGS_STREAM_XATTR) == 0);

	reset_stores();
	seed_store(&native_store, TC_USERTAGS_NATIVE_XATTR, hfs, sizeof(hfs));
	tdb_create_tags_on_list = true;
	CHECK(xattr_tdb_flistxattr(handle, fsp, list, sizeof(list)) ==
	      sizeof(TC_USERTAGS_STREAM_XATTR));
	CHECK(strcmp(list, TC_USERTAGS_STREAM_XATTR) == 0);
	CHECK(native_gets == 0);

	reset_stores();
	config.time_capsule_native_metadata = false;
	seed_store(&native_store, TC_USERTAGS_NATIVE_XATTR, hfs, sizeof(hfs));
	CHECK(xattr_tdb_flistxattr(handle, fsp, NULL, 0) == 0);
	CHECK(native_gets == 0);
}

static void test_finderinfo(struct files_struct *fsp)
{
	uint8_t value[AFP_FinderSize];
	uint8_t result[AFP_FinderSize];
	uint8_t zero[AFP_FinderSize] = {0};
	unsigned i;

	for (i = 0; i < ARRAY_SIZE(value); i++) {
		value[i] = i + 1;
	}
	reset_stores();
	seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
		   value, sizeof(value));
	CHECK(tc_native_finderinfo_read_fsp(fsp, result) == 1);
	CHECK(memcmp(result, value, sizeof(value)) == 0);

	reset_stores();
	CHECK(tc_native_finderinfo_read_fsp(fsp, result) == 0);

	reset_stores();
	seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
		   value, sizeof(value));
	native_second_read_error = ENOATTR;
	CHECK(tc_native_finderinfo_read_fsp(fsp, result) == 0);
	CHECK(native_gets == 2);

	reset_stores();
	seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
		   value, sizeof(value) - 1);
	CHECK(tc_native_finderinfo_read_fsp(fsp, result) == -1);
	CHECK(errno == EIO);

	reset_stores();
	native_store.get_error = ENOTSUP;
	CHECK(tc_native_finderinfo_read_fsp(fsp, result) == 0);
	native_store.get_error = ENOSYS;
	CHECK(tc_native_finderinfo_read_fsp(fsp, result) == 0);
	native_store.get_error = EACCES;
	CHECK(tc_native_finderinfo_read_fsp(fsp, result) == -1);
	CHECK(errno == EACCES);
	native_store.get_error = 0;
	native_store.set_error = ENOTSUP;
	tc_native_finderinfo_mirror(fsp, value);
	CHECK(native_sets == 1);

	reset_stores();
	native_set_returns_size = true;
	tc_native_finderinfo_mirror(fsp, value);
	CHECK(native_store.exists && native_store.size == AFP_FinderSize);
	CHECK(memcmp(native_store.data, value, sizeof(value)) == 0);
	tc_native_finderinfo_mirror(fsp, zero);
	CHECK(!native_store.exists);

	reset_stores();
	seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
		   value, sizeof(value));
	fsp->fsp_name->st.st_ex_mode = S_IFLNK | 0777;
	CHECK(tc_native_finderinfo_read_fsp(fsp, result) == 0);
	CHECK(native_gets == 0);
	tc_native_finderinfo_mirror(fsp, value);
	CHECK(native_sets == 0);
	CHECK(tc_native_finderinfo_remove_at(NULL, NULL, fsp->fsp_name) == 0);
	CHECK(native_removes == 0 && native_store.exists);
	fsp->fsp_name->st.st_ex_mode = S_IFREG | 0600;
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

static void test_finderinfo_io(struct vfs_handle_struct *handle,
			       connection_struct *conn,
			       files_struct *base_fsp,
			       TALLOC_CTX *mem_ctx)
{
	struct fruit_config_data config = {
		.time_capsule_native_metadata = true,
	};
	uint8_t original[AFP_FinderSize] = {1};
	uint8_t updated[AFP_FinderSize] = {2};
	uint8_t afpinfo[AFP_INFO_SIZE];
	uint8_t result[AFP_INFO_SIZE];
	enum fruit_meta modes[] = {FRUIT_META_STREAM, FRUIT_META_NETATALK};
	size_t i;

	make_afpinfo(mem_ctx, updated, afpinfo);
	for (i = 0; i < ARRAY_SIZE(modes); i++) {
		struct smb_filename stream_name = {0};
		files_struct stream_fsp = {0};
		struct fio *fio;
		int fd;

		init_stream_file(mem_ctx, conn, base_fsp,
				 &stream_fsp, &stream_name);
		config.meta = modes[i];
		handle->data = &config;
		reset_stores();
		seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
			   original, sizeof(original));
		if (modes[i] == FRUIT_META_STREAM) {
			next_openat_failures = 1;
		}

		fd = fruit_open_meta(handle, NULL, &stream_name,
				     &stream_fsp, O_RDWR, 0600);
		CHECK(fd != -1);
		fsp_set_fd(&stream_fsp, fd);
		CHECK(fruit_pread_meta(handle, &stream_fsp,
				       result, sizeof(result), 0) == AFP_INFO_SIZE);
		CHECK(memcmp(result + AFP_OFF_FinderInfo,
			     original, AFP_FinderSize) == 0);

		mutation_count = 0;
		mutation_order[0] = '\0';
		CHECK(fruit_pwrite_meta(handle, &stream_fsp,
					afpinfo, sizeof(afpinfo), 0) == AFP_INFO_SIZE);
		CHECK(strcmp(mutation_order, "PN") == 0);
		CHECK(native_store.exists &&
		      memcmp(native_store.data, updated, AFP_FinderSize) == 0);
		fio = VFS_FETCH_FSP_EXTENSION(handle, &stream_fsp);
		CHECK(fio != NULL && !fio->native_meta_fallback);
		if (modes[i] == FRUIT_META_STREAM) {
			CHECK(next_openat_calls == 2);
			CHECK((next_openat_flags & O_CREAT) != 0);
		}
		remove_fio(handle, &stream_fsp);
	}

	for (i = 0; i < ARRAY_SIZE(modes); i++) {
		struct smb_filename stream_name = {0};
		files_struct stream_fsp = {0};
		struct fio *fio;

		init_stream_file(mem_ctx, conn, base_fsp,
				 &stream_fsp, &stream_name);
		config.meta = modes[i];
		handle->data = &config;
		reset_stores();
		seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
			   original, sizeof(original));
		fio = VFS_ADD_FSP_EXTENSION(
			handle, &stream_fsp, struct fio, fio_destroy_fn);
		CHECK(fio != NULL);
		fio->handle = handle;
		fio->fsp = &stream_fsp;
		fio->type = ADOUBLE_META;
		fio->config = &config;
		if (modes[i] == FRUIT_META_STREAM) {
			next_pwrite_error = EIO;
		} else {
			ad_fset_error = EIO;
		}
		CHECK(fruit_pwrite_meta(handle, &stream_fsp,
					afpinfo, sizeof(afpinfo), 0) == -1);
		CHECK(strcmp(mutation_order, "P") == 0);
		CHECK(native_sets == 0 && native_removes == 0);
		CHECK(native_store.exists &&
		      memcmp(native_store.data, original, AFP_FinderSize) == 0);
		remove_fio(handle, &stream_fsp);
	}
}

static void test_finderinfo_unlink(struct vfs_handle_struct *handle,
				   connection_struct *conn,
				   files_struct *base_fsp,
				   TALLOC_CTX *mem_ctx)
{
	struct fruit_config_data config = {
		.time_capsule_native_metadata = true,
	};
	uint8_t value[AFP_FinderSize] = {1};
	enum fruit_meta modes[] = {FRUIT_META_STREAM, FRUIT_META_NETATALK};
	size_t i;

	for (i = 0; i < ARRAY_SIZE(modes); i++) {
		struct smb_filename stream_name = {0};
		files_struct stream_fsp = {0};

		init_stream_file(mem_ctx, conn, base_fsp,
				 &stream_fsp, &stream_name);
		config.meta = modes[i];
		handle->data = &config;

		reset_stores();
		seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
			   value, sizeof(value));
		primary_remove_error = EIO;
		CHECK(fruit_unlink_meta(handle, NULL, &stream_name) == -1);
		CHECK(strcmp(mutation_order, "NP") == 0);
		CHECK(!native_store.exists);

		reset_stores();
		seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
			   value, sizeof(value));
		native_store.remove_error = EACCES;
		CHECK(fruit_unlink_meta(handle, NULL, &stream_name) == -1);
		CHECK(strcmp(mutation_order, "N") == 0);
		CHECK(native_store.exists);

		reset_stores();
		seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
			   value, sizeof(value));
		primary_remove_error = ENOENT;
		CHECK(fruit_unlink_meta(handle, NULL, &stream_name) == 0);
		CHECK(strcmp(mutation_order, "NP") == 0);
		CHECK(!native_store.exists);
	}
}

static void test_finderinfo_readdir(struct vfs_handle_struct *handle,
				    struct files_struct *fsp,
				    struct smb_filename *smb_fname)
{
	struct fruit_config_data config = {
		.meta = FRUIT_META_STREAM,
		.time_capsule_native_metadata = true,
		.readdir_attr_finder_info = true,
	};
	struct readdir_attr_data attr = {.type = RDATTR_AAPL};
	uint8_t value[AFP_FinderSize];
	uint8_t zero[8] = {0};
	NTSTATUS status;
	unsigned int i;

	for (i = 0; i < ARRAY_SIZE(value); i++) {
		value[i] = i + 1;
	}
	handle->data = &config;
	reset_stores();
	smb_fname->st.st_ex_mode = S_IFDIR | 0700;
	seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
		   value, sizeof(value));
	status = readdir_attr_macmeta(handle, smb_fname, &attr);
	CHECK(NT_STATUS_IS_OK(status));
	CHECK(memcmp(attr.attr_data.aapl.finder_info, zero, sizeof(zero)) == 0);
	CHECK(memcmp(attr.attr_data.aapl.finder_info + 8,
		     value + 8, 2) == 0);
	CHECK(memcmp(attr.attr_data.aapl.finder_info + 10,
		     value + 24, 2) == 0);
	smb_fname->st.st_ex_mode = S_IFREG | 0600;
	fsp->fsp_name->st.st_ex_mode = S_IFREG | 0600;
}

static void test_finderinfo_fstat(struct vfs_handle_struct *handle,
				  struct files_struct *fsp,
				  struct smb_filename *smb_fname)
{
	struct fruit_config_data config = {
		.meta = FRUIT_META_STREAM,
		.time_capsule_native_metadata = true,
	};
	struct smb_filename stream_name = *smb_fname;
	SMB_STRUCT_STAT sbuf;
	uint8_t value[AFP_FinderSize] = {1};
	uint8_t zero[AFP_FinderSize] = {0};
	int ret;

	handle->data = &config;
	stream_name.stream_name = discard_const_p(char, AFPINFO_STREAM_NAME);
	reset_stores();
	test_fstatat_base_mode = S_IFDIR | 0700;
	seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
		   value, sizeof(value));
	ret = fruit_fstatat_meta(handle, &config, NULL, &stream_name,
				&sbuf, AT_SYMLINK_NOFOLLOW);
	CHECK(ret == 0);
	CHECK(S_ISREG(sbuf.st_ex_mode));
	CHECK(sbuf.st_ex_size == AFP_INFO_SIZE);
	CHECK(sbuf.st_ex_blocks == AFP_INFO_SIZE / STAT_ST_BLOCKSIZE + 1);

	reset_stores();
	seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
		   zero, sizeof(zero));
	errno = EACCES;
	CHECK(fruit_fstatat_meta(handle, &config, NULL, &stream_name,
					&sbuf, AT_SYMLINK_NOFOLLOW) == -1);
	CHECK(errno == ENOENT);

	reset_stores();
	test_fstatat_stream_error = ENOTSUP;
	seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
		   value, sizeof(value));
	CHECK(fruit_fstatat_meta(handle, &config, NULL, &stream_name,
					&sbuf, AT_SYMLINK_NOFOLLOW) == -1);
	CHECK(errno == ENOTSUP && native_gets == 0);

	reset_stores();
	native_store.get_error = EACCES;
	CHECK(fruit_fstatat_meta(handle, &config, NULL, &stream_name,
					&sbuf, AT_SYMLINK_NOFOLLOW) == -1);
	CHECK(errno == EACCES && native_gets == 1);

	reset_stores();
	config.meta = FRUIT_META_NETATALK;
	native_store.get_error = EACCES;
	CHECK(fruit_fstatat_meta(handle, &config, NULL, &stream_name,
					&sbuf, AT_SYMLINK_NOFOLLOW) == -1);
	CHECK(errno == EACCES && native_gets == 1);

	reset_stores();
	config.meta = FRUIT_META_NETATALK;
	test_fgetxattr_error = ENOTSUP;
	seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
		   value, sizeof(value));
	CHECK(fruit_fstatat_meta(handle, &config, NULL, &stream_name,
					&sbuf, AT_SYMLINK_NOFOLLOW) == -1);
	CHECK(errno == ENOTSUP && native_gets == 0);

	reset_stores();
	config.meta = FRUIT_META_STREAM;
	test_fstatat_base_mode = S_IFLNK | 0777;
	fsp->fsp_name->st.st_ex_mode = S_IFLNK | 0777;
	seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
		   value, sizeof(value));
	CHECK(fruit_fstatat_meta(handle, &config, NULL, &stream_name,
					&sbuf, AT_SYMLINK_NOFOLLOW) == -1);
	CHECK(errno == ENOENT && native_gets == 0);
	fsp->fsp_name->st.st_ex_mode = S_IFREG | 0600;
}

static void test_finderinfo_streaminfo(struct vfs_handle_struct *handle,
				       struct files_struct *fsp,
				       struct smb_filename *smb_fname,
				       TALLOC_CTX *mem_ctx)
{
	struct fruit_config_data config = {
		.meta = FRUIT_META_STREAM,
		.time_capsule_native_metadata = true,
	};
	uint8_t value[AFP_FinderSize] = {1};
	uint8_t zero[AFP_FinderSize] = {0};
	struct stream_struct *streams = NULL;
	unsigned int num_streams = 0;
	NTSTATUS status;

	handle->data = &config;
	reset_stores();
	seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
		   value, sizeof(value));
	status = fruit_streaminfo_meta(handle, fsp, smb_fname, mem_ctx,
				       &num_streams, &streams, false);
	CHECK(NT_STATUS_IS_OK(status));
	CHECK(num_streams == 1);
	CHECK(strequal_m(streams[0].name, AFPINFO_STREAM));
	CHECK(streams[0].size == AFP_INFO_SIZE);
	TALLOC_FREE(streams);

	reset_stores();
	seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
		   zero, sizeof(zero));
	num_streams = 0;
	status = fruit_streaminfo_meta(handle, fsp, smb_fname, mem_ctx,
				       &num_streams, &streams, false);
	CHECK(NT_STATUS_IS_OK(status) && num_streams == 0);

	reset_stores();
	seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
		   value, sizeof(value));
	streams = talloc_zero_array(mem_ctx, struct stream_struct, 1);
	CHECK(streams != NULL);
	streams[0].name = talloc_strdup(streams, AFPINFO_STREAM);
	CHECK(streams[0].name != NULL);
	streams[0].size = AFP_INFO_SIZE;
	num_streams = 1;
	status = fruit_streaminfo_meta(handle, fsp, smb_fname, mem_ctx,
				       &num_streams, &streams, false);
	CHECK(NT_STATUS_IS_OK(status) && num_streams == 1);
	CHECK(native_gets == 0);
	TALLOC_FREE(streams);

	reset_stores();
	seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
		   value, sizeof(value));
	config.time_capsule_native_metadata = false;
	num_streams = 0;
	status = fruit_streaminfo_meta(handle, fsp, smb_fname, mem_ctx,
				       &num_streams, &streams, false);
	CHECK(NT_STATUS_IS_OK(status) && num_streams == 0);
	CHECK(native_gets == 0);

	reset_stores();
	config.time_capsule_native_metadata = true;
	next_streaminfo_size = 0;
	seed_store(&native_store, TC_FINDERINFO_NATIVE_XATTR,
		   value, sizeof(value));
	global_fruit_config.nego_aapl = true;
	num_streams = 0;
	status = fruit_fstreaminfo(handle, fsp, mem_ctx,
				  &num_streams, &streams);
	global_fruit_config.nego_aapl = false;
	CHECK(NT_STATUS_IS_OK(status) && num_streams == 0);
	CHECK(native_gets == 0);
}

int main(int argc, char **argv)
{
	TALLOC_CTX *frame = NULL;
	connection_struct *conn = NULL;
	files_struct file = {0};
	struct smb_filename smb_fname = {0};
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
	if (strcmp(argv[1], "all") == 0) {
		test_tags_read(&handle, &file);
		test_tags_write(&handle, &file);
		test_tags_delete(&handle, &file);
		test_tags_errors(&handle, &file);
		test_tags_list(&handle, &file);
		test_finderinfo(&file);
		test_finderinfo_io(&handle, conn, &file, frame);
		test_finderinfo_unlink(&handle, conn, &file, frame);
		test_finderinfo_readdir(&handle, &file, &smb_fname);
		test_finderinfo_fstat(&handle, &file, &smb_fname);
		test_finderinfo_streaminfo(&handle, &file, &smb_fname, frame);
	} else if (strcmp(argv[1], "tags_read") == 0) {
		test_tags_read(&handle, &file);
	} else if (strcmp(argv[1], "tags_write") == 0) {
		test_tags_write(&handle, &file);
	} else if (strcmp(argv[1], "tags_delete") == 0) {
		test_tags_delete(&handle, &file);
	} else if (strcmp(argv[1], "tags_errors") == 0) {
		test_tags_errors(&handle, &file);
	} else if (strcmp(argv[1], "tags_list") == 0) {
		test_tags_list(&handle, &file);
	} else if (strcmp(argv[1], "finderinfo") == 0) {
		test_finderinfo(&file);
	} else if (strcmp(argv[1], "finderinfo_io") == 0) {
		test_finderinfo_io(&handle, conn, &file, frame);
		test_finderinfo_unlink(&handle, conn, &file, frame);
	} else if (strcmp(argv[1], "finderinfo_readdir") == 0) {
		test_finderinfo_readdir(&handle, &file, &smb_fname);
	} else if (strcmp(argv[1], "finderinfo_fstat") == 0) {
		test_finderinfo_fstat(&handle, &file, &smb_fname);
	} else if (strcmp(argv[1], "finderinfo_streaminfo") == 0) {
		test_finderinfo_streaminfo(&handle, &file, &smb_fname, frame);
	} else {
		CHECK(false);
	}
	fsp_set_fd(&file, -1);
	TALLOC_FREE(frame);
	CHECK(!talloc_stackframe_exists());
	/* This standalone driver has pulled in process-global Samba exit handlers
	 * through two full VFS modules. Its owned state is clean now; bypass those
	 * unrelated handlers, which are not valid on the NetBSD 4 test process. */
	_exit(0);
}
