/* Unit tests for the one-shot HFS migrator. These include the production
 * parser and migration code with only the AirPort private syscalls mocked. */
#include "includes.h"
#include "system/filesys.h"
#include "lib/dbwrap/dbwrap.h"

/* Linux aliases ENOATTR to ENODATA; keep them distinct there so a missing
 * attribute and missing data cannot be confused. NetBSD and macOS already
 * separate them, and the real xattr_tdb library returns the platform value,
 * so overriding it there would make the production check miss its own errno. */
#if ENOATTR == ENODATA
#undef ENOATTR
#define ENOATTR 193
#endif
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
static int fsync_error;              /* errno the fsync hook fails with (0 = real fsync) */
static int sync_calls;
static unsigned progress_resets;

static void migration_test_progress(void)
{
	progress_resets++;
}

static int migration_test_fsync(int fd)
{
	if (fsync_error != 0) {
		errno = fsync_error;
		return -1;
	}
	return fsync(fd);
}

static void migration_test_sync(void)
{
	sync_calls++;
}
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
#define fsync migration_test_fsync
#define sync migration_test_sync
#define dbwrap_transaction_commit migration_test_commit
#define TC_MIGRATION_PROGRESS_HOOK() migration_test_progress()
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
#undef fsync
#undef sync
#undef dbwrap_transaction_commit
#undef TC_MIGRATION_PROGRESS_HOOK

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

static int child_exit_status(pid_t child)
{
	int status;

	CHECK(waitpid(child, &status, 0) == child);
	CHECK(WIFEXITED(status));
	return WEXITSTATUS(status);
}

static void test_guard(void)
{
	char root[] = "/tmp/tc-migrate-guard.XXXXXX";
	char log[PATH_MAX];
	char fifo[PATH_MAX];
	char capture[] = "/tmp/tc-migrate-guard-output.XXXXXX";
	struct itimerval timer;
	pid_t child;
	int capture_fd;
	int saved_stdout;
	char *guarded[] = {"migrate", "--stall-seconds", "1", "--log", log,
		"inspect-root", root, NULL};
	char *invalid_zero[] = {"migrate", "--stall-seconds", "0", "inspect-root", root, NULL};
	char *invalid_duplicate[] = {"migrate", "--stall-seconds", "1",
		"--stall-seconds", "2", "inspect-root", root, NULL};
	char *invalid_log[] = {"migrate", "--log", log, "inspect-root", root, NULL};

	CHECK(mkdtemp(root) != NULL);
	snprintf(log, sizeof(log), "%s/log", root);
	snprintf(fifo, sizeof(fifo), "%s/fifo", root);
	CHECK(tc_xattr_hfs_migrate_program_main(5, invalid_zero) == 2);
	CHECK(tc_xattr_hfs_migrate_program_main(7, invalid_duplicate) == 2);
	CHECK(tc_xattr_hfs_migrate_program_main(5, invalid_log) == 2);

	CHECK(mkfifo(fifo, 0600) == 0);
	child = fork(); CHECK(child >= 0);
	if (child == 0) {
		char *stalled[] = {"migrate", "--stall-seconds", "1", "--log", fifo,
			"inspect-root", root, NULL};
		_exit(tc_xattr_hfs_migrate_program_main(7, stalled));
	}
	CHECK(child_exit_status(child) == TC_STALL_EXIT);
	unlink(fifo);

	child = fork(); CHECK(child >= 0);
	if (child == 0) {
		int i;
		CHECK(tc_guard_start(1) == 0);
		for (i = 0; i < 3; i++) {
			usleep(600000);
			tc_progress();
		}
		_exit(tc_guard_finish(0));
	}
	CHECK(child_exit_status(child) == 0);

	capture_fd = mkstemp(capture);
	saved_stdout = dup(STDOUT_FILENO);
	CHECK(capture_fd >= 0 && saved_stdout >= 0);
	CHECK(dup2(capture_fd, STDOUT_FILENO) >= 0);
	CHECK(tc_xattr_hfs_migrate_program_main(7, guarded) == 0);
	CHECK(dup2(saved_stdout, STDOUT_FILENO) >= 0);
	close(saved_stdout); close(capture_fd); unlink(capture);
	CHECK(getitimer(ITIMER_REAL, &timer) == 0 && !timerisset(&timer.it_value));
	CHECK(write(STDERR_FILENO, "", 0) == 0);

	child = fork(); CHECK(child >= 0);
	if (child == 0) {
		char *flush_failure[] = {"migrate", "--stall-seconds", "1",
			"inspect-root", root, NULL};
		close(STDOUT_FILENO);
		_exit(tc_xattr_hfs_migrate_program_main(5, flush_failure));
	}
	CHECK(child_exit_status(child) == 4);
	unlink(log); rmdir(root);
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
	progress_resets = 0;
	CHECK(tc_migrate_appledouble(&migration, base_fd, base) == 0);
	CHECK(progress_resets > 10);
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
	/* Even malformed native FinderInfo is replaced by the valid TDB value. */
	CHECK(tc_airport_fsetxattr(fd, TC_FINDERINFO_XATTR, "old", 3, 0) == 0);
	CHECK(tc_airport_fsetxattr(fd, "com.apple.metadata:_kMDItemUserTags",
				 "old", 3, 0) == 0);
	CHECK(tc_airport_fsetxattr(fd, "security.NTACL", "old", 3, 0) == 0);
	CHECK(tc_airport_fsetxattr(fd, "user.DosStream.windows:$DATA",
				 "old", 4, 0) == 0);
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

	/* TDB has no attribute timestamps: its selected representation replaces
	 * a conflicting native value during copy. Cleanup must reject disagreement
	 * rather than quietly retiring the source. */
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
	argv[3] = discard_const_p(char, "stream");
	stored->value[0] = 'X';
	rc = tc_xattr_hfs_migrate_program_main(5, argv);
	CHECK(rc != 0 && access(tdb_path, F_OK) == 0);
	argv[1] = discard_const_p(char, "copy");
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 0);
	stored = find_xattr(TC_FINDERINFO_XATTR);
	CHECK(stored != NULL && stored->value[0] == 'S');
	argv[1] = discard_const_p(char, "cleanup");
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
	CHECK(tc_verify_resource(
		      fileno(left), fileno(right), 0, sizeof(value)) == 0);
	CHECK(pwrite(fileno(right), changed, sizeof(changed), 0) == sizeof(changed));
	errno = 0;
	CHECK(tc_verify_resource(
		      fileno(left), fileno(right), 0, sizeof(value)) == 1);
	CHECK(ftruncate(fileno(right), sizeof(value) - 1) == 0);
	errno = 0;
	CHECK(tc_verify_resource(
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
	/* A detached disk is another device. The unit test has only one, so
	 * the pending row carries a foreign devid; the same-device row of a
	 * deleted file would instead be a proven orphan (see test_orphans). */
	struct file_id id, orphan = {.devid = 123456, .inode = 654321}, returned_id;
	struct db_context *db;
	DATA_BLOB blob = data_blob_null;
	int fd;
	char *argv[] = {"migrate", "copy", tdb, "netatalk", root, NULL};

	CHECK(mkdtemp(root) != NULL);
	CHECK(mkdtemp(absent) != NULL);
	snprintf(absent_object, sizeof(absent_object), "%s/object", absent);
	fd = open(absent_object, O_CREAT | O_RDWR, 0600); CHECK(fd >= 0);
	CHECK(fstat(fd, &st) == 0); returned_id = tc_file_id(&st); close(fd);
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
	/* The missing volume becomes visible in a later deploy's root scan. Its
	 * rows are keyed by the device it is attached at, so the returning
	 * disk's row is the real file id under the scanned root; the foreign
	 * row that stood in for it is retired the same way a re-attached disk
	 * would be found. */
	db = dbwrap_local_open(frame, tdb, 0, TDB_DEFAULT, O_RDWR, 0,
		DBWRAP_LOCK_ORDER_2, DBWRAP_FLAG_NONE); CHECK(db != NULL);
	CHECK(xattr_tdb_setattr(db, &returned_id, "com.apple.test", "missing", 7, 0) == 0);
	CHECK(xattr_tdb_removeattr(db, &orphan, "com.apple.test") == 0);
	TALLOC_FREE(db);
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
	progress_resets = 0;
	CHECK(tc_scan_root(&m, root) == 0); CHECK(m.counts.entries == 153);
	CHECK(progress_resets >= 150);
	directory_read_error = true;
	CHECK(tc_scan_root(&m, root) == -1);
	directory_read_error = false;
	unlink(object); rmdir(dir);
	for (i = 0; i < 150; i++) {
		snprintf(object, sizeof(object), "%s/band-%d", root, i); unlink(object);
	}
	rmdir(root); TALLOC_FREE(frame);
}

static int read_stdout_capture(int (*call)(const char *), const char *argument,
			       char *buffer, size_t size)
{
	char capture[] = "/tmp/tc-migrate-stdout.XXXXXX";
	int capture_fd = mkstemp(capture);
	int saved = dup(STDOUT_FILENO);
	int rc;
	ssize_t got;

	CHECK(capture_fd != -1 && saved != -1);
	fflush(stdout);
	CHECK(dup2(capture_fd, STDOUT_FILENO) != -1);
	rc = call(argument);
	fflush(stdout);
	CHECK(dup2(saved, STDOUT_FILENO) != -1);
	close(saved);
	got = pread(capture_fd, buffer, size - 1, 0);
	CHECK(got >= 0);
	buffer[got] = '\0';
	close(capture_fd);
	unlink(capture);
	return rc;
}

static void test_fingerprint(void)
{
	char root[64] = "/tmp/tc-migrate-fingerprint.XXXXXX";
	char path[96];
	char expected[96];
	char output[96];
	const uint8_t content[] = {'t', 'd', 'b', 0, 255, 7};
	uint64_t hash = 0xcbf29ce484222325ULL;
	size_t i;
	int fd;

	CHECK(mkdtemp(root) != NULL);
	snprintf(path, sizeof(path), "%s/xattr.tdb", root);
	fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0600);
	CHECK(fd != -1);
	CHECK(write_all(fd, content, sizeof(content)) == 0);
	for (i = 0; i < sizeof(content); i++) {
		hash ^= content[i];
		hash *= 0x100000001b3ULL;
	}
	snprintf(expected, sizeof(expected), "fingerprint=%zu-%016" PRIx64 "\n",
		 sizeof(content), hash);
	CHECK(read_stdout_capture(tc_print_fingerprint, path, output, sizeof(output)) == 0);
	CHECK(strcmp(output, expected) == 0);

	/* The generation must move with any byte of the database. */
	CHECK(pwrite(fd, "T", 1, 0) == 1);
	CHECK(read_stdout_capture(tc_print_fingerprint, path, output, sizeof(output)) == 0);
	CHECK(strncmp(output, "fingerprint=6-", 14) == 0);
	CHECK(strcmp(output, expected) != 0);
	close(fd);

	unlink(path);
	errno = 0;
	CHECK(read_stdout_capture(tc_print_fingerprint, path, output, sizeof(output)) == -1);
	CHECK(output[0] == '\0');
	rmdir(root);
}

static void test_boundary(void)
{
	TALLOC_CTX *frame = talloc_stackframe();
	struct tc_migration m = {.mem_ctx = frame, .phase = TC_PHASE_COPY};
	char root[] = "/tmp/tc-boundary.XXXXXX", object[128];
	struct stat st;
	int fd;

	CHECK(mkdtemp(root) != NULL);
	snprintf(object, sizeof(object), "%s/object", root);
	fd = open(object, O_CREAT | O_RDWR, 0600);
	CHECK(fd >= 0);
	close(fd);
	CHECK(lstat(root, &st) == 0);
	/* An entry on a different device is a nested mount: skipped, not walked
	 * and not an error. The same walk on the right device visits both. */
	m.root_dev = (uint64_t)st.st_dev + 1;
	CHECK(tc_scan_path(&m, root, false) == 0);
	CHECK(m.counts.boundary_skipped == 1 && m.counts.entries == 0);
	CHECK(tc_scan_root(&m, root) == 0);
	CHECK(m.counts.boundary_skipped == 1 && m.counts.entries == 2);
	CHECK(m.num_complete_devs == 1 && m.complete_devs[0] == (uint64_t)st.st_dev);
	unlink(object);
	rmdir(root);
	TALLOC_FREE(frame);
}

static void write_orphan_rows(const char *tdb_path,
			      uint64_t devid,
			      uint64_t first_inode,
			      unsigned count,
			      uint64_t unresolved_devid)
{
	TALLOC_CTX *frame = talloc_stackframe();
	struct db_context *db;
	const uint8_t value[] = {9};
	unsigned i;

	db = dbwrap_local_open(
		frame, tdb_path, 0, TDB_DEFAULT, O_RDWR | O_CREAT, 0600,
		DBWRAP_LOCK_ORDER_2, DBWRAP_FLAG_NONE);
	CHECK(db != NULL);
	for (i = 0; i < count; i++) {
		struct file_id id = {.devid = devid, .inode = first_inode + i};
		CHECK(xattr_tdb_setattr(db, &id, "com.apple.test", value, sizeof(value), 0) == 0);
	}
	if (unresolved_devid != 0) {
		struct file_id id = {.devid = unresolved_devid, .inode = 0x3344};
		CHECK(xattr_tdb_setattr(db, &id, "com.apple.test", value, sizeof(value), 0) == 0);
	}
	TALLOC_FREE(db);
	TALLOC_FREE(frame);
}

static void test_orphans(void)
{
	char root[64] = "/tmp/tc-migrate-orphans.XXXXXX";
	char tdb_path[96];
	char first_slot[128];
	char second_slot[128];
	char object[96];
	struct stat st;
	uint64_t missing_inode;
	int fd;
	char *argv[] = {
		discard_const_p(char, "tc_xattr_hfs_migrate"),
		discard_const_p(char, "cleanup"),
		tdb_path,
		discard_const_p(char, "stream"),
		root,
		NULL,
	};

	CHECK(mkdtemp(root) != NULL);
	snprintf(tdb_path, sizeof(tdb_path), "%s/xattr.tdb", root);
	snprintf(first_slot, sizeof(first_slot), "%s.orphaned.1", tdb_path);
	snprintf(second_slot, sizeof(second_slot), "%s.orphaned.2", tdb_path);
	snprintf(object, sizeof(object), "%s/object", root);
	fd = open(object, O_RDWR | O_CREAT | O_TRUNC, 0600);
	CHECK(fd != -1);
	CHECK(fstat(fd, &st) == 0);
	close(fd);
	/* Two rows on this device whose inodes no longer exist. The file just
	 * created is the only inode the walk can claim, so a far-away inode
	 * number is safely absent. */
	missing_inode = (uint64_t)st.st_ino + 0x10000000ULL;

	/* Every remaining row proven orphaned: the closed database moves to
	 * the first free slot instead of being deleted. */
	reset_xattrs();
	write_orphan_rows(tdb_path, st.st_dev, missing_inode, 2, 0);
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 0);
	CHECK(access(tdb_path, F_OK) == -1 && errno == ENOENT);
	CHECK(access(first_slot, F_OK) == 0);

	/* A second run never overwrites the first quarantine. */
	write_orphan_rows(tdb_path, st.st_dev, missing_inode, 1, 0);
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 0);
	CHECK(access(tdb_path, F_OK) == -1 && errno == ENOENT);
	CHECK(access(first_slot, F_OK) == 0);
	CHECK(access(second_slot, F_OK) == 0);
	CHECK(stat(first_slot, &st) == 0 && st.st_size > 0);

	/* One unresolved row (a device nobody walked) keeps the database live
	 * even when every other row is a proven orphan. */
	CHECK(lstat(root, &st) == 0);
	write_orphan_rows(tdb_path, st.st_dev, missing_inode, 1, 0x1122);
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 0);
	CHECK(access(tdb_path, F_OK) == 0);
	CHECK(access(second_slot, F_OK) == 0);
	CHECK(stat(tdb_path, &st) == 0);
	{
		char third_slot[128];
		snprintf(third_slot, sizeof(third_slot), "%s.orphaned.3", tdb_path);
		CHECK(access(third_slot, F_OK) == -1);
	}

	/* A walk that fails proves nothing: the rows stay unresolved and the
	 * database is untouched. */
	directory_read_error = true;
	CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 4);
	directory_read_error = false;
	CHECK(access(tdb_path, F_OK) == 0);
	unlink(tdb_path);

	/* Review 2 R10: a directory fsync failure after the rename is an error,
	 * not a durable success -- the data is at the slot, the run fails. */
	{
		char third_slot[128];
		snprintf(third_slot, sizeof(third_slot), "%s.orphaned.3", tdb_path);
		CHECK(lstat(root, &st) == 0);
		write_orphan_rows(tdb_path, st.st_dev, missing_inode, 1, 0);
		fsync_error = EIO;
		CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 4);
		fsync_error = 0;
		CHECK(access(tdb_path, F_OK) == -1 && errno == ENOENT);
		CHECK(access(third_slot, F_OK) == 0);
		unlink(third_slot);

		/* A filesystem that refuses directory fsync falls back to sync(2)
		 * and the quarantine counts as complete. */
		write_orphan_rows(tdb_path, st.st_dev, missing_inode, 1, 0);
		fsync_error = ENOTSUP;
		sync_calls = 0;
		CHECK(tc_xattr_hfs_migrate_program_main(5, argv) == 0);
		fsync_error = 0;
		CHECK(sync_calls >= 1);
		CHECK(access(tdb_path, F_OK) == -1 && errno == ENOENT);
		CHECK(access(third_slot, F_OK) == 0);
		unlink(third_slot);
	}

	unlink(first_slot);
	unlink(second_slot);
	unlink(object);
	rmdir(root);
}

/* Apple diskd can remove a mount while other disks remain. Completion is
 * volume-scoped; these cases exercise the real read-only TDB merge and coverage
 * logic with only the private native-xattr calls replaced by the hooks above. */
static void multi_source_line(FILE *input, unsigned index, const char *uuid,
                              const char *relative, const char *path)
{
    struct tc_source_stat st;
    CHECK(tc_source_stat_read(path, &st) == 0);
    fprintf(input, "S %u %s ", index, uuid);
    tc_hex_print(input, (const uint8_t *)relative, strlen(relative)); fprintf(input, " ");
    tc_hex_print(input, (const uint8_t *)path, strlen(path));
    fprintf(input, " stream %"PRIu64" %"PRIu64" %"PRIu64" %"PRId64" %ld %016"PRIx64"\n",
            st.dev, st.inode, st.size, st.mtime, st.nsec, st.hash);
}
static void multi_prepare(struct tc_multi *multi, TALLOC_CTX *ctx,
                          const char *root, const char *older, const char *newer)
{
    FILE *input = tmpfile();
    struct stat st;
    CHECK(input != NULL && stat(root, &st) == 0);
    memset(multi, 0, sizeof(*multi)); multi->ctx = ctx;
    fprintf(input, "TCMIGRATE1\n");
    multi_source_line(input, 0, "11111111-1111-1111-1111-111111111111", ".samba4/private/old.tdb", older);
    multi_source_line(input, 1, "22222222-2222-2222-2222-222222222222", ".samba4/private/new.tdb", newer);
    fprintf(input, "R 33333333-3333-3333-3333-333333333333 ");
    tc_hex_print(input, (const uint8_t *)root, strlen(root));
    fprintf(input, " %"PRIu64" %"PRIu64"\nE\n", (uint64_t)st.st_dev, (uint64_t)st.st_ino);
    rewind(input);
    CHECK(tc_multi_read(multi, input) == 0);
    fclose(input);
}
static void multi_value(const char *path, const struct file_id *id, const char *name, const void *value, size_t size)
{
    TALLOC_CTX *frame = talloc_stackframe();
    struct db_context *db = dbwrap_local_open(frame, path, 0, TDB_DEFAULT,
        O_RDWR | O_CREAT, 0600, DBWRAP_LOCK_ORDER_2, DBWRAP_FLAG_NONE);
    CHECK(db != NULL);
    CHECK(xattr_tdb_setattr(db, id, name, value, size, 0) == 0);
    TALLOC_FREE(frame);
}
static void test_multi(void)
{
    TALLOC_CTX *frame = talloc_stackframe();
    char root[] = "/tmp/tc-multi.XXXXXX", private_dir[128], old[160], newer[160], object[128], quarantine[192];
    struct tc_multi multi;
    struct tc_counts counts;
    struct stat st;
    struct file_id id;
    struct tc_source_stat before[2], after;
    struct timeval dates[2] = {{.tv_sec = 1234567890}, {.tv_sec = 1234567890}};
    const uint8_t low[] = {'l','o','w',0}, high[] = {'h','i',0}, extent_low[] = {'x'}, extent_high[] = {'y'};
    uint8_t anchor_low[] = {'a',1}, anchor_high[] = {'b',1};
    uint8_t finder[AFP_FinderSize] = {0x41};
    int fd;
    CHECK(mkdtemp(root) != NULL);
    snprintf(private_dir, sizeof(private_dir), "%s/.samba4", root); CHECK(mkdir(private_dir, 0700) == 0);
    snprintf(old, sizeof(old), "%s/old.tdb", private_dir);
    snprintf(newer, sizeof(newer), "%s/new.tdb", private_dir);
    snprintf(object, sizeof(object), "%s/object", root);
    fd = open(object, O_CREAT | O_RDWR, 0600); CHECK(fd >= 0 && fstat(fd, &st) == 0); close(fd);
    id = tc_file_id(&st);
    multi_value(old, &id, "com.apple.test", low, sizeof(low));
    multi_value(old, &id, "com.apple.unique", low, sizeof(low));
    multi_value(newer, &id, "com.apple.test", high, sizeof(high));
    multi_value(old, &id, "user.DosStream.sample:$DATA", anchor_low, sizeof(anchor_low));
    multi_value(old, &id, "user.DosStreamExt.1.sample:$DATA", extent_low, sizeof(extent_low));
    multi_value(newer, &id, "user.DosStream.sample:$DATA", anchor_high, sizeof(anchor_high));
    multi_value(newer, &id, "user.DosStreamExt.1.sample:$DATA", extent_high, sizeof(extent_high));
    multi_value(newer, &id, TC_FINDERINFO_XATTR, finder, sizeof(finder));
	CHECK(utimes(old, dates) == 0 && utimes(newer, dates) == 0);
	CHECK(tc_source_stat_read(old, &before[0]) == 0 && tc_source_stat_read(newer, &before[1]) == 0);
	progress_resets = 0;
	multi_prepare(&multi, frame, root, old, newer);
	CHECK(progress_resets >= 4); /* request records, hashing, and TDB keys */
    {
        struct tc_multi_source a = multi.sources[0], b = multi.sources[1];
        struct tc_multi_source *left = &a, *right = &b;
        CHECK(tc_rank_compare(&left, &right) < 0); /* UUID breaks equal timestamps. */
        memcpy(b.uuid, a.uuid, sizeof(b.uuid));
        CHECK(tc_rank_compare(&left, &right) > 0); /* Then bytewise payload path. */
        b.stat.nsec++;
        CHECK(tc_rank_compare(&left, &right) < 0); /* Preserve subsecond mtime. */
        a.stat.mtime++;
        CHECK(tc_rank_compare(&left, &right) > 0);
    }
    {
        struct tc_migration scan = {
            .mem_ctx = frame, .multi = &multi, .phase = TC_PHASE_COPY
        };
        reset_xattrs();
        progress_resets = 0;
        fd = open(object, O_RDONLY);
        CHECK(fd >= 0 && tc_multi_file(&scan, fd, object, &st, false) == 0);
        close(fd);
        /* Eight source names, four native writes, and file completion. */
        CHECK(progress_resets >= 13);
    }
    reset_xattrs();
    CHECK(tc_multi_scan(&multi, &counts) == 0);
    CHECK(find_xattr("com.apple.test") != NULL && find_xattr("com.apple.test")->size == sizeof(high));
    CHECK(!memcmp(find_xattr("com.apple.test")->value, high, sizeof(high)));
    CHECK(find_xattr("com.apple.unique") != NULL);
    CHECK(find_xattr(TC_FINDERINFO_XATTR) != NULL && find_xattr(TC_FINDERINFO_XATTR)->value[0] == 0x41);
    CHECK(find_xattr("user.DosStream.sample:$DATA") != NULL);
    CHECK(find_xattr("user.DosStream.sample:$DATA")->size == 3);
    CHECK(!memcmp(find_xattr("user.DosStream.sample:$DATA")->value, "by\0", 3));
    CHECK(!multi.sources[0].coverage[0] && !multi.sources[1].coverage[0]);
    CHECK(tc_source_stat_read(old, &after) == 0 && tc_source_stat_same(&before[0], &after));
    CHECK(tc_source_stat_read(newer, &after) == 0 && tc_source_stat_same(&before[1], &after));
    /* Cleanup verifies the merged winner, never the superseded old value. A
     * flush failure cannot produce completion coverage or retire any source. */
    multi.phase = TC_PHASE_CLEANUP;
    fsync_error = EIO;
    CHECK(tc_multi_scan(&multi, &counts) == -1);
    CHECK(!multi.sources[0].coverage[0] && !multi.sources[1].coverage[0]);
    fsync_error = 0;
    CHECK(tc_multi_scan(&multi, &counts) == 0);
    CHECK(multi.sources[0].coverage[0] == 1 && multi.sources[1].coverage[0] == 1);
    CHECK(tc_source_stat_read(old, &after) == 0 && tc_source_stat_same(&before[0], &after));
    CHECK(tc_source_stat_read(newer, &after) == 0 && tc_source_stat_same(&before[1], &after));
    /* A missing disk represented by unresolved lower-ranked coverage keeps
     * the newer DB active, so interruption cannot reverse precedence. */
    multi.sources[0].coverage[0] = 0;
    CHECK(tc_multi_retire(&multi) == 0);
    CHECK(access(old, F_OK) == 0 && access(newer, F_OK) == 0);
    multi.sources[0].coverage[0] = 1;
    CHECK(tc_multi_retire(&multi) == 0);
    CHECK(access(old, F_OK) != 0 && access(newer, F_OK) != 0);
    TALLOC_FREE(frame);

    /* Whole-file mtime dominates UUID ties. No fragments from an older DB
     * may fill a missing extent in the selected newer logical value. */
    frame = talloc_stackframe();
    multi_value(old, &id, "com.apple.test", low, sizeof(low));
    multi_value(newer, &id, "com.apple.test", high, sizeof(high));
    dates[1].tv_sec++;
    CHECK(utimes(old, dates) == 0);
    dates[1].tv_sec--;
    CHECK(utimes(newer, dates) == 0);
    multi_prepare(&multi, frame, root, old, newer); reset_xattrs();
    CHECK(tc_multi_scan(&multi, &counts) == 0);
    CHECK(!memcmp(find_xattr("com.apple.test")->value, low, sizeof(low)));
    /* A source changed after inspection invalidates the whole scan. */
    dates[1].tv_sec++;
    CHECK(utimes(newer, dates) == 0);
    CHECK(tc_multi_validate_sources(&multi) == -1);
    TALLOC_FREE(frame);
    unlink(old); unlink(newer);

    frame = talloc_stackframe();
    multi_value(old, &id, "user.DosStream.sample:$DATA", anchor_low, sizeof(anchor_low));
    multi_value(old, &id, "user.DosStreamExt.1.sample:$DATA", extent_low, sizeof(extent_low));
    multi_value(newer, &id, "user.DosStream.sample:$DATA", anchor_high, sizeof(anchor_high));
    CHECK(utimes(old, dates) == 0 && utimes(newer, dates) == 0);
    multi_prepare(&multi, frame, root, old, newer); reset_xattrs();
    CHECK(tc_multi_scan(&multi, &counts) == -1);
    CHECK(access(old, F_OK) == 0 && access(newer, F_OK) == 0);
    TALLOC_FREE(frame); unlink(old); unlink(newer);

    frame = talloc_stackframe();
    write_orphan_rows(old, st.st_dev, (uint64_t)st.st_ino + 0x10000000ULL, 1, 0);
    multi_value(newer, &id, "com.apple.test", high, sizeof(high));
    CHECK(utimes(old, dates) == 0 && utimes(newer, dates) == 0);
    multi_prepare(&multi, frame, root, old, newer); reset_xattrs();
    CHECK(tc_multi_scan(&multi, &counts) == 0);
    multi.phase = TC_PHASE_CLEANUP;
    CHECK(tc_multi_scan(&multi, &counts) == 0 && multi.sources[0].coverage[0] == 2);
    CHECK(tc_multi_retire(&multi) == 0);
    snprintf(quarantine, sizeof(quarantine), "%s.orphaned.1", old);
    CHECK(access(old, F_OK) != 0 && access(newer, F_OK) != 0 && access(quarantine, F_OK) == 0);
    CHECK(tc_source_stat_read(quarantine, &after) == 0 && tc_source_stat_same(&multi.sources[0].stat, &after));
    TALLOC_FREE(frame); unlink(quarantine);
    unlink(object); rmdir(private_dir); rmdir(root);
}

int main(int argc, char **argv)
{
	CHECK(argc == 2);
	setup_logging(argv[0], DEBUG_STDERR);
	if (!strcmp(argv[1], "guard") || !strcmp(argv[1], "all")) test_guard();
	if (!strcmp(argv[1], "multi") || !strcmp(argv[1], "all")) test_multi();
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
	if (strcmp(argv[1], "scan") == 0 || strcmp(argv[1], "all") == 0) {
		test_scan();
		test_boundary();
	}
	if (strcmp(argv[1], "orphans") == 0 || strcmp(argv[1], "all") == 0) {
		test_orphans();
		test_fingerprint();
	}
	if (strcmp(argv[1], "errors") == 0 || strcmp(argv[1], "all") == 0) {
		test_errors();
	}
	if (strcmp(argv[1], "multi") != 0 &&
	    strcmp(argv[1], "guard") != 0 &&
	    strcmp(argv[1], "all") != 0 &&
	    strcmp(argv[1], "appledouble") != 0 &&
	    strcmp(argv[1], "embedded_xattrs") != 0 &&
	    strcmp(argv[1], "resource") != 0 &&
	    strcmp(argv[1], "cleanup") != 0 &&
	    strcmp(argv[1], "tdb") != 0 &&
	    strcmp(argv[1], "errors") != 0 &&
	    strcmp(argv[1], "resume") != 0 &&
	    strcmp(argv[1], "orphans") != 0 &&
	    strcmp(argv[1], "scan") != 0)
	{
		CHECK(false);
	}
	CHECK(!talloc_stackframe_exists());
	_exit(0);
}
