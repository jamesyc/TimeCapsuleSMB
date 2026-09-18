/* Unit tests for the one-shot HFS migrator. These include the production
 * parser and migration code with only the AirPort private syscalls mocked. */
#include "includes.h"
#include "system/filesys.h"
#include "lib/dbwrap/dbwrap.h"

#undef ENOATTR
#define ENOATTR 193
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: %s (errno=%d)\n", __FILE__, __LINE__, #x, errno); fflush(stderr); _exit(90); } } while (0)

struct test_xattr {
	bool exists;
	char name[128];
	uint8_t value[4096];
	size_t size;
};

static struct test_xattr test_xattrs[16];

static void reset_xattrs(void)
{
	ZERO_ARRAY(test_xattrs);
	errno = 0;
}

static struct test_xattr *find_xattr(const char *name)
{
	size_t i;

	for (i = 0; i < ARRAY_SIZE(test_xattrs); i++) {
		if (test_xattrs[i].exists &&
		    strcmp(test_xattrs[i].name, name) == 0)
		{
			return &test_xattrs[i];
		}
	}
	return NULL;
}

static long test_migrate_syscall_377(int fd,
				     const char *name,
				     const void *value,
				     size_t size,
				     int flags)
{
	struct test_xattr *xattr = find_xattr(name);
	size_t i;

	(void)fd;
	(void)flags;
	if (size > sizeof(test_xattrs[0].value)) {
		errno = E2BIG;
		return -1;
	}
	if (xattr == NULL) {
		for (i = 0; i < ARRAY_SIZE(test_xattrs); i++) {
			if (!test_xattrs[i].exists) {
				xattr = &test_xattrs[i];
				break;
			}
		}
	}
	if (xattr == NULL) {
		errno = ENOSPC;
		return -1;
	}
	xattr->exists = true;
	snprintf(xattr->name, sizeof(xattr->name), "%s", name);
	memcpy(xattr->value, value, size);
	xattr->size = size;
	return 0;
}

static long test_migrate_syscall_380(int fd,
				     const char *name,
				     void *value,
				     size_t size)
{
	struct test_xattr *xattr = find_xattr(name);

	(void)fd;
	if (xattr == NULL) {
		errno = ENOATTR;
		return -1;
	}
	if (value == NULL) {
		return xattr->size;
	}
	if (size < xattr->size) {
		errno = ERANGE;
		return -1;
	}
	memcpy(value, xattr->value, xattr->size);
	return xattr->size;
}

static long test_migrate_syscall_383(int fd, char *list, size_t size)
{
	size_t required = 0;
	size_t i;

	(void)fd;
	for (i = 0; i < ARRAY_SIZE(test_xattrs); i++) {
		if (test_xattrs[i].exists) {
			required += strlen(test_xattrs[i].name) + 1;
		}
	}
	if (list == NULL) {
		return required;
	}
	if (size < required) {
		errno = ERANGE;
		return -1;
	}
	required = 0;
	for (i = 0; i < ARRAY_SIZE(test_xattrs); i++) {
		size_t name_size;

		if (!test_xattrs[i].exists) {
			continue;
		}
		name_size = strlen(test_xattrs[i].name) + 1;
		memcpy(list + required, test_xattrs[i].name, name_size);
		required += name_size;
	}
	return required;
}

static long test_migrate_syscall_386(int fd, const char *name)
{
	struct test_xattr *xattr = find_xattr(name);

	(void)fd;
	if (xattr == NULL) {
		errno = ENOATTR;
		return -1;
	}
	ZERO_STRUCTP(xattr);
	return 0;
}

static int test_migrate_flock(int fd, int operation)
{
	(void)fd;
	CHECK(operation == LOCK_EX || operation == LOCK_UN);
	return 0;
}

#define TC_AIRPORT_NATIVE_XATTR_SYSCALLS 1
#define TC_MIGRATE_CALL(number) TC_MIGRATE_CALL_I(number)
#define TC_MIGRATE_CALL_I(number) test_migrate_syscall_##number
#define TC_AIRPORT_XATTR_SYSCALL(number, ...) \
	TC_MIGRATE_CALL(number)(__VA_ARGS__)
#define TC_AIRPORT_XATTR_FLOCK(fd, operation) test_migrate_flock((fd), (operation))
#define TC_AIRPORT_PATH_IS_HFS(path) true
static int injected_read_fd = -2;
static off_t injected_read_offset;
static ino_t injected_read_inode;
static int interrupted_reads;
static bool partial_reads;
static bool directory_read_error;
static bool commit_error;
static int collection_allocations_before_failure = -1;
static ssize_t migration_test_pread(int fd, void *value, size_t size, off_t offset)
{
	if (interrupted_reads > 0) {
		interrupted_reads--; errno = EINTR; return -1;
	}
	struct stat read_st;
	if ((fd == injected_read_fd || (injected_read_inode != 0 &&
	    fstat(fd, &read_st) == 0 && read_st.st_ino == injected_read_inode)) &&
	    offset >= injected_read_offset) {
		errno = EIO; return -1;
	}
	if (partial_reads && size > 1) { size /= 2; }
	return pread(fd, value, size, offset);
}
static struct dirent *migration_test_readdir(DIR *dir)
{
	if (directory_read_error) { errno = EIO; return NULL; }
	return readdir(dir);
}
static int migration_test_commit(struct db_context *db)
{
	if (commit_error) { dbwrap_transaction_cancel(db); errno = EIO; return -1; }
	return dbwrap_transaction_commit(db);
}
static void *migration_test_talloc_realloc_array(
	const void *ctx, void *ptr, size_t el_size, unsigned count, const char *name)
{
	if (collection_allocations_before_failure == 0) {
		errno = ENOMEM;
		return NULL;
	}
	if (collection_allocations_before_failure > 0) {
		collection_allocations_before_failure--;
	}
	return _talloc_realloc_array(ctx, ptr, el_size, count, name);
}
#define pread migration_test_pread
#define readdir migration_test_readdir
#define dbwrap_transaction_commit migration_test_commit
#undef talloc_realloc
#define talloc_realloc(ctx, ptr, type, count) \
	(type *)migration_test_talloc_realloc_array( \
		ctx, ptr, sizeof(type), count, #type)
#define main tc_xattr_hfs_migrate_program_main
#include "../utils/tc_xattr_hfs_migrate.c"
#undef main
#undef talloc_realloc
#undef pread
#undef readdir
#undef dbwrap_transaction_commit

static int write_all(int fd, const void *value, size_t size)
{
	const uint8_t *bytes = value;
	size_t done = 0;

	while (done < size) {
		ssize_t ret = write(fd, bytes + done, size - done);

		if (ret <= 0) {
			return -1;
		}
		done += ret;
	}
	return 0;
}

static void make_appledouble(uint8_t *value,
			     size_t size,
			     uint32_t finder_length,
			     const void *resource,
			     uint32_t resource_length)
{
	uint32_t resource_offset = TC_AD_HEADER_SIZE + 2 * TC_AD_ENTRY_SIZE +
		finder_length;

	CHECK(size == resource_offset + resource_length);
	ZERO_ARRAY_LEN(value, size);
	PUSH_BE_U32(value, 0, TC_AD_MAGIC);
	PUSH_BE_U32(value, 4, TC_AD_VERSION);
	memcpy(value + TC_AD_FILLER_OFFSET, "Netatalk        ", TC_AD_FILLER_SIZE);
	PUSH_BE_U16(value, 24, 2);
	PUSH_BE_U32(value, 26, TC_AD_FINDERI);
	PUSH_BE_U32(value, 30, TC_AD_HEADER_SIZE + 2 * TC_AD_ENTRY_SIZE);
	PUSH_BE_U32(value, 34, finder_length);
	PUSH_BE_U32(value, 38, TC_AD_RFORK);
	PUSH_BE_U32(value, 42, resource_offset);
	PUSH_BE_U32(value, 46, resource_length);
	if (resource_length != 0) {
		memcpy(value + resource_offset, resource, resource_length);
	}
}

static void parse_bytes(const uint8_t *value,
			 size_t size,
			 struct tc_appledouble *ad,
			 int expected)
{
	FILE *file = tmpfile();
	struct stat st;

	CHECK(file != NULL);
	CHECK(write_all(fileno(file), value, size) == 0);
	CHECK(fstat(fileno(file), &st) == 0);
	CHECK(tc_parse_appledouble(fileno(file), &st, ad) == expected);
	fclose(file);
}

static void test_appledouble(void)
{
	uint8_t resource[32];
	uint8_t valid[82 + sizeof(resource)];
	uint8_t corrupt[sizeof(valid)];
	struct tc_appledouble ad = {0};

	memset(resource, 0x5a, sizeof(resource));
	make_appledouble(valid, sizeof(valid), AFP_FinderSize,
			 resource, sizeof(resource));
	parse_bytes(valid, sizeof(valid), &ad, 0);
	CHECK(ad.finderinfo.present && ad.finderinfo.offset == 50);
	CHECK(ad.finderinfo.length == AFP_FinderSize);
	CHECK(ad.resource.present && ad.resource.offset == 82);
	CHECK(ad.resource.length == sizeof(resource));
	free(ad.header);

	memcpy(corrupt, valid, sizeof(valid));
	PUSH_BE_U32(corrupt, 0, 0);
	parse_bytes(corrupt, sizeof(corrupt), &ad, 1);
	free(ad.header);

	memcpy(corrupt, valid, sizeof(valid));
	PUSH_BE_U32(corrupt, 46, UINT32_MAX);
	errno = 0;
	parse_bytes(corrupt, sizeof(corrupt), &ad, -1);
	CHECK(errno == EINVAL);
	free(ad.header);

	memcpy(corrupt, valid, sizeof(valid));
	PUSH_BE_U32(corrupt, 38, TC_AD_FINDERI);
	errno = 0;
	parse_bytes(corrupt, sizeof(corrupt), &ad, -1);
	CHECK(errno == EINVAL);
	free(ad.header);

	memcpy(corrupt, valid, sizeof(valid));
	PUSH_BE_U32(corrupt, 38, 3);
	parse_bytes(corrupt, sizeof(corrupt), &ad, 0);
	CHECK(ad.unsupported_entries);
	free(ad.header);
}

static void test_embedded_xattrs(void)
{
	const char name[] = "com.apple.metadata:_kMDItemUserTags";
	const uint8_t tag[] = {'r', 'e', 'd'};
	uint8_t value[256] = {0};
	struct tc_appledouble ad = {0};
	struct tc_migration migration = {
		.phase = TC_PHASE_COPY,
	};
	struct test_xattr *stored;
	size_t finder_offset = 50;
	size_t attr_header = finder_offset + AFP_FinderSize + 2;
	size_t attr_entry = attr_header + TC_AD_XATTR_HEADER_SIZE;
	size_t data_start = (attr_entry + TC_AD_XATTR_ENTRY_SIZE +
		strlen(name) + 1 + 3) & ~(size_t)3;
	size_t total_size = data_start + sizeof(tag);

	PUSH_BE_U32(value, 0, TC_AD_MAGIC);
	PUSH_BE_U32(value, 4, TC_AD_VERSION);
	memcpy(value + TC_AD_FILLER_OFFSET, TC_AD_OSX_FILLER, TC_AD_FILLER_SIZE);
	PUSH_BE_U16(value, 24, 2);
	PUSH_BE_U32(value, 26, TC_AD_FINDERI);
	PUSH_BE_U32(value, 30, finder_offset);
	PUSH_BE_U32(value, 34, total_size - finder_offset);
	PUSH_BE_U32(value, 38, TC_AD_RFORK);
	PUSH_BE_U32(value, 42, total_size);
	PUSH_BE_U32(value, 46, 0);
	value[finder_offset] = 0x44;
	PUSH_BE_U32(value, attr_header, TC_AD_XATTR_MAGIC);
	PUSH_BE_U32(value, attr_header + 8, total_size);
	PUSH_BE_U32(value, attr_header + 12, data_start);
	PUSH_BE_U32(value, attr_header + 16, sizeof(tag));
	PUSH_BE_U16(value, attr_header + 34, 1);
	PUSH_BE_U32(value, attr_entry, data_start);
	PUSH_BE_U32(value, attr_entry + 4, sizeof(tag));
	value[attr_entry + 10] = strlen(name) + 1;
	memcpy(value + attr_entry + TC_AD_XATTR_ENTRY_SIZE,
	       name, strlen(name) + 1);
	memcpy(value + data_start, tag, sizeof(tag));
	ad.header = value;
	ad.header_size = total_size;
	ad.finderinfo = (struct tc_ad_entry) {
		.present = true,
		.offset = finder_offset,
		.length = total_size - finder_offset,
	};

	reset_xattrs();
	CHECK(tc_migrate_appledouble_finderinfo(
		      &migration, 42, "object", &ad) == 0);
	stored = find_xattr(TC_FINDERINFO_XATTR);
	CHECK(stored != NULL && stored->size == AFP_FinderSize);
	CHECK(stored->value[0] == 0x44);
	CHECK(tc_migrate_appledouble_xattrs(
		      &migration, 42, "object", &ad) == 0);
	stored = find_xattr(name);
	CHECK(stored != NULL && stored->size == sizeof(tag));
	CHECK(memcmp(stored->value, tag, sizeof(tag)) == 0);

	migration.phase = TC_PHASE_CLEANUP;
	reset_xattrs();
	errno = 0;
	CHECK(tc_migrate_appledouble_xattrs(
		      &migration, 42, "object", &ad) == -1);
	CHECK(errno == EIO);
}

static void make_resource_tree(char root[PATH_MAX],
			       char base[PATH_MAX],
			       char sidecar[PATH_MAX],
			       char native[PATH_MAX])
{
	snprintf(root, PATH_MAX, "/tmp/tc-migrate-test.XXXXXX");
	CHECK(mkdtemp(root) != NULL);
	snprintf(base, PATH_MAX, "%s/base", root);
	snprintf(sidecar, PATH_MAX, "%s/._base", root);
	snprintf(native, PATH_MAX, "%s/..namedfork/rsrc", base);
	CHECK(mkdir(base, 0700) == 0);
	{
		char namedfork[PATH_MAX];
		snprintf(namedfork, sizeof(namedfork), "%s/..namedfork", base);
		CHECK(mkdir(namedfork, 0700) == 0);
	}
}

static void remove_resource_tree(const char *root,
				 const char *base,
				 const char *sidecar,
				 const char *native)
{
	char namedfork[PATH_MAX];

	unlink(sidecar);
	unlink(native);
	snprintf(namedfork, sizeof(namedfork), "%s/..namedfork", base);
	rmdir(namedfork);
	rmdir(base);
	rmdir(root);
}

static void test_resource(void)
{
	char root[PATH_MAX], base[PATH_MAX], sidecar[PATH_MAX], native[PATH_MAX];
	uint8_t *resource = malloc(1024 * 1024);
	uint8_t *appledouble = malloc(82 + 1024 * 1024);
	struct tc_migration migration = {.phase = TC_PHASE_COPY};
	int sidecar_fd;
	int base_fd;
	struct stat st;

	CHECK(resource != NULL && appledouble != NULL);
	memset(resource, 0xa5, 1024 * 1024);
	make_appledouble(appledouble, 82 + 1024 * 1024, AFP_FinderSize,
			 resource, 1024 * 1024);
	make_resource_tree(root, base, sidecar, native);
	sidecar_fd = open(sidecar, O_WRONLY | O_CREAT | O_TRUNC, 0600);
	CHECK(sidecar_fd != -1);
	CHECK(write_all(sidecar_fd, appledouble, 82 + 1024 * 1024) == 0);
	close(sidecar_fd);
	base_fd = open(base, O_RDONLY);
	CHECK(base_fd != -1);
	reset_xattrs();
	CHECK(tc_migrate_appledouble(&migration, base_fd, base) == 0);
	CHECK(stat(native, &st) == 0 && st.st_size == 1024 * 1024);
	CHECK(find_xattr(TC_RESOURCE_MARKER_XATTR) == NULL);
	CHECK(access(sidecar, F_OK) == 0);

	migration.phase = TC_PHASE_CLEANUP;
	/* A difference in the first chunk must not hide a later read failure. */
	{
		int native_fd = open(native, O_RDWR); CHECK(native_fd >= 0);
		CHECK(pwrite(native_fd, "B", 1, 0) == 1);
		CHECK(fstat(native_fd, &st) == 0);
		injected_read_inode = st.st_ino; injected_read_offset = TC_COPY_SIZE;
		CHECK(tc_migrate_appledouble(&migration, base_fd, base) == -1);
		CHECK(access(sidecar, F_OK) == 0);
		injected_read_inode = 0;
		CHECK(pwrite(native_fd, resource, 1024 * 1024, 0) == 1024 * 1024);
		close(native_fd);
		CHECK(stat(sidecar, &st) == 0);
		injected_read_inode = st.st_ino;
		CHECK(tc_migrate_appledouble(&migration, base_fd, base) == -1);
		CHECK(access(sidecar, F_OK) == 0);
		injected_read_inode = 0;
	}
	interrupted_reads = 2; partial_reads = true;
	CHECK(tc_migrate_appledouble(&migration, base_fd, base) == 0);
	partial_reads = false;
	CHECK(access(sidecar, F_OK) == -1 && errno == ENOENT);
	close(base_fd);
	remove_resource_tree(root, base, sidecar, native);
	free(resource);
	free(appledouble);
}

static void test_cleanup(void)
{
	uint8_t marker[16];
	struct tc_migration copy = {.phase = TC_PHASE_COPY};
	struct tc_migration cleanup = {.phase = TC_PHASE_CLEANUP};
	struct tc_migration keys = {0};
	struct tc_tdb_key key_storage[2] = {0};
	struct file_id first = {.devid = 1, .inode = 2};
	struct file_id missing = {.devid = 3, .inode = 4};
	const uint8_t value[] = {1, 2, 3};

	reset_xattrs();
	CHECK(tc_native_write_verified(
		      &copy, 42, "object", "com.apple.test",
		      value, sizeof(value), false) == 0);
	CHECK(tc_native_write_verified(
		      &cleanup, 42, "object", "com.apple.test",
		      value, sizeof(value), false) == 0);

	reset_xattrs();
	errno = 0;
	CHECK(tc_native_write_verified(
		      &cleanup, 42, "object", "com.apple.test",
		      value, sizeof(value), false) == -1);
	CHECK(errno == EIO);

	reset_xattrs();
	CHECK(tc_set_resource_marker(42, 12345) == 0);
	CHECK(tc_resource_marker_state(42, 12345) == 1);
	CHECK(tc_resource_marker_state(42, 1) == -1);
	CHECK(tc_remove_resource_marker(42) == 0);
	CHECK(tc_resource_marker_state(42, 12345) == 0);

	memcpy(marker, TC_RESOURCE_MARKER_MAGIC, 8);
	PUSH_BE_U64(marker, 8, 9);
	CHECK(test_migrate_syscall_377(
		      42, TC_RESOURCE_MARKER_XATTR,
		      marker, sizeof(marker), 0) == 0);
	CHECK(tc_resource_marker_state(42, 9) == 1);

	push_file_id_16(key_storage[0].data, &first);
	keys.tdb_keys = key_storage;
	keys.num_tdb_keys = ARRAY_SIZE(key_storage);
	CHECK(tc_mark_tdb_key(&keys, &first) == 1);
	CHECK(keys.counts.tdb_matched == 1 && key_storage[0].matched);
	CHECK(tc_mark_tdb_key(&keys, &first) == 1);
	CHECK(keys.counts.tdb_matched == 1);
	CHECK(tc_mark_tdb_key(&keys, &missing) == 0);

	reset_xattrs();
	CHECK(test_migrate_syscall_377(
		      42, TC_FINDERINFO_XATTR, value, sizeof(value), 0) == 0);
	errno = 0;
	CHECK(tc_migrate_finderinfo(&copy, 42, "object", &first) == -1);
	CHECK(errno == EIO);
}

static void test_tdb_migration(void)
{
	TALLOC_CTX *frame = talloc_stackframe();
	char root[64] = "/tmp/tc-migrate-tdb.XXXXXX";
	char object[96];
	char tdb_path[96];
	char orphan_path[96];
	struct db_context *db;
	struct stat st;
	struct file_id id;
	struct file_id orphan = {.devid = 0x1122, .inode = 0x3344};
	struct tc_migration copy = {
		.mem_ctx = frame,
		.legacy_metadata = "netatalk",
		.phase = TC_PHASE_COPY,
	};
	uint8_t stream_finder[AFP_FinderSize] = {0};
	uint8_t netatalk_finder[AFP_FinderSize] = {0};
	uint8_t afpinfo[AFP_INFO_SIZE + 1] = {0};
	uint8_t netatalk[82];
	const uint8_t tags[] = {'r', 'e', 'd', 0};
	const uint8_t acl[] = {1, 2, 3};
	const uint8_t windows_anchor[] = {'a', 'b', 1};
	const uint8_t windows_extent[] = {'c'};
	const uint8_t orphan_value[] = {9};
	struct test_xattr *stored;
	int fd;
	int rc;
	char *argv[] = {
		discard_const_p(char, "tc_xattr_hfs_migrate"),
		discard_const_p(char, "cleanup"),
		tdb_path,
		discard_const_p(char, "netatalk"),
		root,
		NULL,
	};

	CHECK(mkdtemp(root) != NULL);
	snprintf(object, sizeof(object), "%s/object", root);
	snprintf(tdb_path, sizeof(tdb_path), "%s/xattr.tdb", root);
	snprintf(orphan_path, sizeof(orphan_path), "%s/orphan.tdb", root);
	fd = open(object, O_RDWR | O_CREAT | O_TRUNC, 0600);
	CHECK(fd != -1);
	CHECK(fstat(fd, &st) == 0);
	id = tc_file_id(&st);
	db = dbwrap_local_open(
		frame, tdb_path, 0, TDB_DEFAULT, O_RDWR | O_CREAT, 0600,
		DBWRAP_LOCK_ORDER_2, DBWRAP_FLAG_NONE);
	CHECK(db != NULL);
	copy.db = db;
	stream_finder[0] = 'S';
	netatalk_finder[0] = 'N';
	PUSH_BE_U32(afpinfo, 0, AFP_Signature);
	PUSH_BE_U32(afpinfo, 4, AFP_Version);
	memcpy(afpinfo + AFP_OFF_FinderInfo, stream_finder, AFP_FinderSize);
	afpinfo[AFP_INFO_SIZE] = 0;
	make_appledouble(netatalk, sizeof(netatalk), AFP_FinderSize, NULL, 0);
	memcpy(netatalk + 50, netatalk_finder, AFP_FinderSize);
	CHECK(xattr_tdb_setattr(
		      db, &id, TC_AFPINFO_XATTR,
		      afpinfo, sizeof(afpinfo), 0) == 0);
	CHECK(xattr_tdb_setattr(
		      db, &id, TC_NETATALK_META_XATTR,
		      netatalk, sizeof(netatalk), 0) == 0);
	CHECK(xattr_tdb_setattr(
		      db, &id,
		      "user.DosStream.com.apple.metadata:_kMDItemUserTags:$DATA",
		      tags, sizeof(tags), 0) == 0);
	CHECK(xattr_tdb_setattr(
		      db, &id, "security.NTACL", acl, sizeof(acl), 0) == 0);
	CHECK(xattr_tdb_setattr(
		      db, &id, "user.DosStream.windows:$DATA",
		      windows_anchor, sizeof(windows_anchor), 0) == 0);
	CHECK(xattr_tdb_setattr(
		      db, &id, "user.DosStreamExt.1.windows:$DATA",
		      windows_extent, sizeof(windows_extent), 0) == 0);
	CHECK(tc_collect_tdb_keys(&copy) == 0);
	CHECK(copy.counts.tdb_total == 1);
	reset_xattrs();
	CHECK(tc_migrate_tdb_record(&copy, fd, object, &st) == 0);
	CHECK(copy.counts.tdb_matched == 1 && copy.counts.tdb_records == 1);
	stored = find_xattr(TC_FINDERINFO_XATTR);
	CHECK(stored != NULL && stored->size == AFP_FinderSize);
	CHECK(stored->value[0] == 'N');
	stored = find_xattr("com.apple.metadata:_kMDItemUserTags");
	CHECK(stored != NULL && stored->size == sizeof(tags) - 1);
	CHECK(memcmp(stored->value, tags, sizeof(tags) - 1) == 0);
	stored = find_xattr("security.NTACL");
	CHECK(stored != NULL && stored->size == sizeof(acl));
	CHECK(memcmp(stored->value, acl, sizeof(acl)) == 0);
	stored = find_xattr("user.DosStream.windows:$DATA");
	CHECK(stored != NULL && stored->size == 4);
	CHECK(memcmp(stored->value, "abc\0", 4) == 0);

	/* Exercise the other public metadata setting against the same record.
	 * The cleanup below then proves that a valid native value wins if the
	 * selected legacy representation disagrees. */
	CHECK(tc_airport_fremovexattr(fd, TC_FINDERINFO_XATTR) == 0);
	copy.legacy_metadata = "stream";
	copy.tdb_keys[0].matched = false;
	copy.counts.tdb_matched = 0;
	CHECK(tc_migrate_tdb_record(&copy, fd, object, &st) == 0);
	stored = find_xattr(TC_FINDERINFO_XATTR);
	CHECK(stored != NULL && stored->size == AFP_FinderSize);
	CHECK(stored->value[0] == 'S');

	/* The real program's cleanup pass must re-read the TDB, verify all native
	 * values, and remove it only after the sole key matches this object. */
	TALLOC_FREE(copy.db);
	rc = tc_xattr_hfs_migrate_program_main(5, argv);
	CHECK(rc == 0);
	CHECK(access(tdb_path, F_OK) == -1 && errno == ENOENT);
	stored = find_xattr(TC_FINDERINFO_XATTR);
	CHECK(stored != NULL && stored->value[0] == 'S');

	/* A key for a detached volume is not data loss: cleanup succeeds but keeps
	 * the dormant database for a later deploy with that disk attached. */
	db = dbwrap_local_open(
		frame, orphan_path, 0, TDB_DEFAULT, O_RDWR | O_CREAT, 0600,
		DBWRAP_LOCK_ORDER_2, DBWRAP_FLAG_NONE);
	CHECK(db != NULL);
	CHECK(xattr_tdb_setattr(
		      db, &orphan, "com.apple.test",
		      orphan_value, sizeof(orphan_value), 0) == 0);
	TALLOC_FREE(db);
	argv[2] = orphan_path;
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 0);
	CHECK(access(orphan_path, F_OK) == 0);

	close(fd);
	unlink(orphan_path);
	unlink(object);
	rmdir(root);
	TALLOC_FREE(frame);
}

static void test_errors(void)
{
	FILE *left = tmpfile();
	FILE *right = tmpfile();
	uint8_t value[32];
	uint8_t changed[32];
	char root[PATH_MAX], base[PATH_MAX], sidecar[PATH_MAX], native[PATH_MAX];
	uint8_t unsupported[82];
	struct tc_migration migration = {.phase = TC_PHASE_COPY};
	int base_fd;
	int sidecar_fd;

	CHECK(left != NULL && right != NULL);
	memset(value, 1, sizeof(value));
	memset(changed, 2, sizeof(changed));
	CHECK(write_all(fileno(left), value, sizeof(value)) == 0);
	CHECK(write_all(fileno(right), value, sizeof(value)) == 0);
	CHECK(tc_verify_resource(&migration,
		      fileno(left), fileno(right), 0, sizeof(value)) == 0);
	CHECK(pwrite(fileno(right), changed, sizeof(changed), 0) == sizeof(changed));
	errno = 0;
	CHECK(tc_verify_resource(&migration,
		      fileno(left), fileno(right), 0, sizeof(value)) == 1);
	CHECK(ftruncate(fileno(right), sizeof(value) - 1) == 0);
	errno = 0;
	CHECK(tc_verify_resource(&migration,
		      fileno(left), fileno(right), 0, sizeof(value)) == 1);
	fclose(left);
	fclose(right);

	left = tmpfile();
	CHECK(left != NULL);
	CHECK(write_all(fileno(left), tc_empty_resourcefork,
			sizeof(tc_empty_resourcefork)) == 0);
	CHECK(tc_is_empty_resourcefork(
		      fileno(left), 0, sizeof(tc_empty_resourcefork)));
	fclose(left);

	make_appledouble(unsupported, sizeof(unsupported), AFP_FinderSize, NULL, 0);
	PUSH_BE_U32(unsupported, 38, 3);
	make_resource_tree(root, base, sidecar, native);
	sidecar_fd = open(sidecar, O_WRONLY | O_CREAT | O_TRUNC, 0600);
	CHECK(sidecar_fd != -1);
	CHECK(write_all(sidecar_fd, unsupported, sizeof(unsupported)) == 0);
	close(sidecar_fd);
	base_fd = open(base, O_RDONLY);
	CHECK(base_fd != -1);
	reset_xattrs();
	errno = 0;
	CHECK(tc_migrate_appledouble(&migration, base_fd, base) == -1);
	CHECK(errno == ENOTSUP);
	CHECK(access(sidecar, F_OK) == 0);
	close(base_fd);
	/* A volume root must not consume a sibling /Volumes/._dkN file. */
	CHECK(tc_scan_root(&migration, base) == 0);
	CHECK(access(sidecar, F_OK) == 0);
	remove_resource_tree(root, base, sidecar, native);
}

static void test_resume(void)
{
	TALLOC_CTX *frame = talloc_stackframe();
	char root[] = "/tmp/tc-resume.XXXXXX";
	char object[128], tdb[128], absent[] = "/tmp/tc-absent.XXXXXX";
	char absent_object[128], returned[128];
	struct stat st;
	struct file_id id, orphan = {.devid = 123456, .inode = 654321};
	struct db_context *db;
	DATA_BLOB blob = data_blob_null;
	int fd;
	char *argv[] = {"migrate", "copy", tdb, "netatalk", root, NULL};

	CHECK(mkdtemp(root) != NULL);
	CHECK(mkdtemp(absent) != NULL);
	snprintf(absent_object, sizeof(absent_object), "%s/object", absent);
	fd = open(absent_object, O_CREAT | O_RDWR, 0600); CHECK(fd >= 0);
	CHECK(fstat(fd, &st) == 0); orphan = tc_file_id(&st); close(fd);
	snprintf(returned, sizeof(returned), "%s/returned", root);
	snprintf(object, sizeof(object), "%s/object", root);
	snprintf(tdb, sizeof(tdb), "%s/xattr.tdb", root);
	fd = open(object, O_CREAT | O_RDWR, 0600); CHECK(fd >= 0);
	CHECK(fstat(fd, &st) == 0); id = tc_file_id(&st);
	db = dbwrap_local_open(frame, tdb, 0, TDB_DEFAULT, O_CREAT | O_RDWR,
		0600, DBWRAP_LOCK_ORDER_2, DBWRAP_FLAG_NONE); CHECK(db != NULL);
	CHECK(xattr_tdb_setattr(db, &id, "user.DosStream.windows:$DATA", "old", 4, 0) == 0);
	CHECK(xattr_tdb_setattr(db, &orphan, "com.apple.test", "missing", 7, 0) == 0);
	TALLOC_FREE(db); reset_xattrs();
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 0);
	argv[1] = "cleanup"; commit_error = true;
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) != 0);
	commit_error = false;
	db = dbwrap_local_open(frame, tdb, 0, TDB_DEFAULT, O_RDWR, 0,
		DBWRAP_LOCK_ORDER_2, DBWRAP_FLAG_NONE); CHECK(db != NULL);
	CHECK(xattr_tdb_getattr(db, frame, &id, "user.DosStream.windows:$DATA", &blob) == 4);
	TALLOC_FREE(db);
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 0);
	CHECK(access(tdb, F_OK) == 0); /* missing disk is still pending */
	db = dbwrap_local_open(frame, tdb, 0, TDB_DEFAULT, O_RDWR, 0,
		DBWRAP_LOCK_ORDER_2, DBWRAP_FLAG_NONE); CHECK(db != NULL);
	CHECK(xattr_tdb_getattr(db, frame, &id, "user.DosStream.windows:$DATA", &blob) < 0);
	CHECK(xattr_tdb_getattr(db, frame, &orphan, "com.apple.test", &blob) == 7);
	TALLOC_FREE(db);
	CHECK(tc_airport_fsetxattr(fd, "user.DosStream.windows:$DATA", "new", 4, 0) == 0);
	argv[1] = "copy"; CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 0);
	CHECK(memcmp(find_xattr("user.DosStream.windows:$DATA")->value, "new", 4) == 0);
	CHECK(tc_airport_fremovexattr(fd, "user.DosStream.windows:$DATA") == 0);
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 0);
	CHECK(find_xattr("user.DosStream.windows:$DATA") == NULL);
	/* The missing volume becomes visible in a later boot's root scan. */
	CHECK(rename(absent, returned) == 0);
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 0);
	argv[1] = "cleanup";
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 0);
	CHECK(access(tdb, F_OK) == -1 && errno == ENOENT);
	CHECK(memcmp(find_xattr("com.apple.test")->value, "missing", 7) == 0);
	snprintf(absent_object, sizeof(absent_object), "%s/object", returned);
	unlink(absent_object); rmdir(returned);
	close(fd); unlink(object); rmdir(root); TALLOC_FREE(frame);
}

static void test_tdb_collection_failures(void)
{
	TALLOC_CTX *frame = talloc_stackframe();
	char root[64] = "/tmp/tc-migrate-collect.XXXXXX";
	char malformed_path[96];
	char allocation_path[96];
	uint8_t first_key[16] = {1};
	uint8_t second_key[16] = {2};
	uint8_t malformed_key[] = {3, 4, 5};
	uint8_t value[] = {6};
	struct db_context *db;
	char *argv[] = {
		discard_const_p(char, "tc_xattr_hfs_migrate"),
		discard_const_p(char, "cleanup"),
		malformed_path,
		discard_const_p(char, "stream"),
		root,
		NULL,
	};

	CHECK(mkdtemp(root) != NULL);
	snprintf(malformed_path, sizeof(malformed_path), "%s/malformed.tdb", root);
	snprintf(allocation_path, sizeof(allocation_path), "%s/allocation.tdb", root);
	db = dbwrap_local_open(
		frame, malformed_path, 0, TDB_DEFAULT, O_RDWR | O_CREAT, 0600,
		DBWRAP_LOCK_ORDER_2, DBWRAP_FLAG_NONE);
	CHECK(db != NULL);
	CHECK(NT_STATUS_IS_OK(dbwrap_store(
		db, (TDB_DATA){.dptr = first_key, .dsize = sizeof(first_key)},
		(TDB_DATA){.dptr = value, .dsize = sizeof(value)}, DBWRAP_REPLACE)));
	CHECK(NT_STATUS_IS_OK(dbwrap_store(
		db, (TDB_DATA){.dptr = malformed_key, .dsize = sizeof(malformed_key)},
		(TDB_DATA){.dptr = value, .dsize = sizeof(value)}, DBWRAP_REPLACE)));
	TALLOC_FREE(db);
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 3);
	CHECK(access(malformed_path, F_OK) == 0);
	db = dbwrap_local_open(
		frame, malformed_path, 0, TDB_DEFAULT, O_RDONLY, 0,
		DBWRAP_LOCK_ORDER_2, DBWRAP_FLAG_NONE);
	CHECK(db != NULL);
	CHECK(dbwrap_exists(
		db, (TDB_DATA){.dptr = first_key, .dsize = sizeof(first_key)}));
	CHECK(dbwrap_exists(
		db, (TDB_DATA){.dptr = malformed_key, .dsize = sizeof(malformed_key)}));
	TALLOC_FREE(db);

	db = dbwrap_local_open(
		frame, allocation_path, 0, TDB_DEFAULT, O_RDWR | O_CREAT, 0600,
		DBWRAP_LOCK_ORDER_2, DBWRAP_FLAG_NONE);
	CHECK(db != NULL);
	CHECK(NT_STATUS_IS_OK(dbwrap_store(
		db, (TDB_DATA){.dptr = first_key, .dsize = sizeof(first_key)},
		(TDB_DATA){.dptr = value, .dsize = sizeof(value)}, DBWRAP_REPLACE)));
	CHECK(NT_STATUS_IS_OK(dbwrap_store(
		db, (TDB_DATA){.dptr = second_key, .dsize = sizeof(second_key)},
		(TDB_DATA){.dptr = value, .dsize = sizeof(value)}, DBWRAP_REPLACE)));
	TALLOC_FREE(db);
	argv[2] = allocation_path;
	collection_allocations_before_failure = 1;
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 3);
	collection_allocations_before_failure = -1;
	CHECK(access(allocation_path, F_OK) == 0);
	db = dbwrap_local_open(
		frame, allocation_path, 0, TDB_DEFAULT, O_RDONLY, 0,
		DBWRAP_LOCK_ORDER_2, DBWRAP_FLAG_NONE);
	CHECK(db != NULL);
	CHECK(dbwrap_exists(
		db, (TDB_DATA){.dptr = first_key, .dsize = sizeof(first_key)}));
	CHECK(dbwrap_exists(
		db, (TDB_DATA){.dptr = second_key, .dsize = sizeof(second_key)}));
	TALLOC_FREE(db);

	CHECK(unlink(malformed_path) == 0);
	CHECK(unlink(allocation_path) == 0);
	CHECK(rmdir(root) == 0);
	TALLOC_FREE(frame);
}

static void test_status(void)
{
	TALLOC_CTX *frame = talloc_stackframe();
	/* The scan enumerates everything under its root, so the status file and the
	 * databases live beside it rather than inside: a run must never walk what
	 * the run before it wrote. */
	char root[] = "/tmp/tc-status.XXXXXX";
	char side[] = "/tmp/tc-status-side.XXXXXX";
	char status[128], object[128], missing[128], tdb[128], malformed[128];
	char line[32];
	uint8_t short_key[] = {7, 8, 9};
	uint8_t short_value[] = {1};
	char *argv[] = {"migrate", "copy", "-", "netatalk", root, NULL};
	char *bad[] = {"migrate", "sideways", "-", "netatalk", root, NULL};
	struct db_context *db;
	struct file_id id;
	struct stat st;
	FILE *in;
	int fd;

	CHECK(mkdtemp(root) != NULL);
	CHECK(mkdtemp(side) != NULL);
	snprintf(status, sizeof(status), "%s/status", side);
	snprintf(object, sizeof(object), "%s/object", root);
	snprintf(missing, sizeof(missing), "%s/absent/status", side);
	snprintf(tdb, sizeof(tdb), "%s/xattr.tdb", side);
	snprintf(malformed, sizeof(malformed), "%s/malformed.tdb", side);
	fd = open(object, O_CREAT | O_RDWR, 0600); CHECK(fd >= 0);
	CHECK(fstat(fd, &st) == 0); id = tc_file_id(&st); close(fd);

	/* Without the variable nothing is written: the file only exists because a
	 * caller asked for it, and an older caller asks for nothing. */
	unsetenv("TC_XATTR_STATUS_PATH");
	reset_xattrs();
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 0);
	CHECK(access(status, F_OK) == -1);

	CHECK(setenv("TC_XATTR_STATUS_PATH", status, 1) == 0);
	reset_xattrs();
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 0);
	in = fopen(status, "r"); CHECK(in != NULL);
	CHECK(fgets(line, sizeof(line), in) != NULL);
	CHECK(atoi(line) == 0);
	fclose(in);

	/* An argument the caller got wrong still reports, so the deploy can tell a
	 * migrator that refused its arguments from one that never ran. */
	CHECK(unlink(status) == 0);
	CHECK(tc_xattr_hfs_migrate_program_main(5, bad) == 2);
	in = fopen(status, "r"); CHECK(in != NULL);
	CHECK(fgets(line, sizeof(line), in) != NULL);
	CHECK(atoi(line) == 2);
	fclose(in);

	/* The failed migration is the case this exists for: the device shell calls
	 * the child successful either way, so a status of 4 is the only evidence
	 * that survives. */
	CHECK(unlink(status) == 0);
	db = dbwrap_local_open(frame, tdb, 0, TDB_DEFAULT, O_CREAT | O_RDWR,
		0600, DBWRAP_LOCK_ORDER_2, DBWRAP_FLAG_NONE); CHECK(db != NULL);
	CHECK(xattr_tdb_setattr(db, &id, "user.DosStream.windows:$DATA", "old", 4, 0) == 0);
	TALLOC_FREE(db);
	argv[1] = "cleanup"; argv[2] = tdb;
	reset_xattrs();
	commit_error = true;
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 4);
	commit_error = false;
	in = fopen(status, "r"); CHECK(in != NULL);
	CHECK(fgets(line, sizeof(line), in) != NULL);
	CHECK(atoi(line) == 4);
	fclose(in);

	/* A database that cannot be read reports its own code before any scan. */
	CHECK(unlink(status) == 0);
	db = dbwrap_local_open(frame, malformed, 0, TDB_DEFAULT, O_CREAT | O_RDWR,
		0600, DBWRAP_LOCK_ORDER_2, DBWRAP_FLAG_NONE); CHECK(db != NULL);
	CHECK(NT_STATUS_IS_OK(dbwrap_store(
		db, (TDB_DATA){.dptr = short_key, .dsize = sizeof(short_key)},
		(TDB_DATA){.dptr = short_value, .dsize = sizeof(short_value)},
		DBWRAP_REPLACE)));
	TALLOC_FREE(db);
	argv[2] = malformed;
	reset_xattrs();
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 3);
	in = fopen(status, "r"); CHECK(in != NULL);
	CHECK(fgets(line, sizeof(line), in) != NULL);
	CHECK(atoi(line) == 3);
	fclose(in);
	argv[1] = "copy"; argv[2] = discard_const_p(char, "-");

	/* A path that cannot be written leaves no file and fails nothing. */
	CHECK(unlink(status) == 0);
	CHECK(setenv("TC_XATTR_STATUS_PATH", missing, 1) == 0);
	reset_xattrs();
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 0);
	CHECK(access(missing, F_OK) == -1);

	/* An empty value means the same as unset: the run writes nothing even
	 * though a variable is set, so a file already there is left alone. */
	CHECK(setenv("TC_XATTR_STATUS_PATH", status, 1) == 0);
	reset_xattrs();
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 0);
	CHECK(access(status, F_OK) == 0);
	CHECK(setenv("TC_XATTR_STATUS_PATH", "", 1) == 0);
	CHECK(unlink(status) == 0);
	reset_xattrs();
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 0);
	CHECK(access(status, F_OK) == -1);
	unsetenv("TC_XATTR_STATUS_PATH");

	CHECK(unlink(tdb) == 0);
	CHECK(unlink(malformed) == 0);
	CHECK(unlink(object) == 0);
	CHECK(rmdir(root) == 0);
	CHECK(rmdir(side) == 0);
	TALLOC_FREE(frame);
}

static void test_scan(void)
{
	TALLOC_CTX *frame = talloc_stackframe();
	struct tc_migration m = {.mem_ctx = frame, .phase = TC_PHASE_COPY};
	char root[] = "/tmp/tc-scan.XXXXXX", dir[128], object[160];
	int fd, i;
	CHECK(mkdtemp(root) != NULL);
	for (i = 0; i < 150; i++) {
		snprintf(object, sizeof(object), "%s/band-%d", root, i);
		fd = open(object, O_CREAT | O_RDWR, 0600); CHECK(fd >= 0); close(fd);
	}
	snprintf(dir, sizeof(dir), "%s/._ordinary", root); CHECK(mkdir(dir, 0700) == 0);
	snprintf(object, sizeof(object), "%s/object", dir);
	fd = open(object, O_CREAT | O_RDWR, 0600); CHECK(fd >= 0); close(fd);
	CHECK(tc_scan_root(&m, root) == 0); CHECK(m.counts.entries == 153);
	directory_read_error = true;
	CHECK(tc_scan_root(&m, root) == -1);
	directory_read_error = false;
	unlink(object); rmdir(dir);
	for (i = 0; i < 150; i++) {
		snprintf(object, sizeof(object), "%s/band-%d", root, i); unlink(object);
	}
	rmdir(root); TALLOC_FREE(frame);
}

int main(int argc, char **argv)
{
	CHECK(argc == 2);
	if (strcmp(argv[1], "appledouble") == 0 || strcmp(argv[1], "all") == 0) {
		test_appledouble();
	}
	if (strcmp(argv[1], "embedded_xattrs") == 0 || strcmp(argv[1], "all") == 0) {
		test_embedded_xattrs();
	}
	if (strcmp(argv[1], "resource") == 0 || strcmp(argv[1], "all") == 0) {
		test_resource();
	}
	if (strcmp(argv[1], "cleanup") == 0 || strcmp(argv[1], "all") == 0) {
		test_cleanup();
	}
	if (strcmp(argv[1], "tdb") == 0 || strcmp(argv[1], "all") == 0) {
		test_tdb_migration();
		test_tdb_collection_failures();
	}
	if (strcmp(argv[1], "resume") == 0 || strcmp(argv[1], "all") == 0) { test_resume(); }
	if (strcmp(argv[1], "scan") == 0 || strcmp(argv[1], "all") == 0) { test_scan(); }
	if (strcmp(argv[1], "status") == 0 || strcmp(argv[1], "all") == 0) { test_status(); }
	if (strcmp(argv[1], "errors") == 0 || strcmp(argv[1], "all") == 0) {
		test_errors();
	}
	if (strcmp(argv[1], "all") != 0 &&
	    strcmp(argv[1], "appledouble") != 0 &&
	    strcmp(argv[1], "embedded_xattrs") != 0 &&
	    strcmp(argv[1], "resource") != 0 &&
	    strcmp(argv[1], "cleanup") != 0 &&
	    strcmp(argv[1], "tdb") != 0 &&
	    strcmp(argv[1], "errors") != 0 &&
	    strcmp(argv[1], "resume") != 0 &&
	    strcmp(argv[1], "scan") != 0 &&
	    strcmp(argv[1], "status") != 0)
	{
		CHECK(false);
	}
	CHECK(!talloc_stackframe_exists());
	_exit(0);
}
