/* One-shot migration of xattr_tdb metadata into AirPort HFS storage.
 * This is a standalone deployment helper, not part of the resident smbd. */
#include "includes.h"
#include "system/filesys.h"
#include "lib/dbwrap/dbwrap.h"
#include "source3/lib/xattr_tdb.h"
#include "source3/include/MacExtensions.h"
#include "source3/modules/airport_native_xattr.h"
#include <sys/time.h>

#define TC_HFS_XATTR_SIZE 3802
#define TC_HFS_STREAM_XATTRS 35
#define TC_STREAM_PREFIX "user.DosStream."
#define TC_EXT_PREFIX "user.DosStreamExt."
#define TC_STREAM_SUFFIX ":$DATA"
#define TC_AFPINFO_XATTR TC_STREAM_PREFIX "AFP_AfpInfo" TC_STREAM_SUFFIX
#define TC_NETATALK_META_XATTR "user.org.netatalk.Metadata"
#define TC_FINDERINFO_XATTR "com.apple.FinderInfo"
#define TC_AD_MAGIC 0x00051607
#define TC_AD_VERSION 0x00020000
#define TC_AD_HEADER_SIZE 26
#define TC_AD_ENTRY_SIZE 12
#define TC_AD_RFORK 2
#define TC_AD_FINDERI 9
#define TC_AD_FILLER_OFFSET 8
#define TC_AD_FILLER_SIZE 16
#define TC_AD_OSX_FILLER "Mac OS X        "
#define TC_AD_XATTR_MAGIC 0x41545452
#define TC_AD_XATTR_HEADER_SIZE 36
#define TC_AD_XATTR_ENTRY_SIZE 11
#define TC_AD_MAX_HEADER (64 * 1024)
#define TC_AD_MAX_ENTRIES 1024
#define TC_COPY_SIZE (64 * 1024)
#define TC_RESOURCE_MARKER_XATTR "user.TimeCapsuleSMB.ResourceMigration"
#define TC_RESOURCE_MARKER_MAGIC "TCV4RSRC"
#define TC_STALL_EXIT 75
#define TC_QUARANTINE_SUFFIX ".orphaned."
#define TC_QUARANTINE_MAX_SLOTS 99
#define TC_FNV64_OFFSET 0xcbf29ce484222325ULL
#define TC_FNV64_PRIME 0x100000001b3ULL

/* The appliance has no pthread support. A process-local real-time guard keeps
 * every blocking filesystem call covered; successful work rearms its one-shot
 * timer, while deploy retains a separate host-side emergency limit. */
struct tc_inactivity_guard {
	bool enabled;
	unsigned seconds;
	struct sigaction previous_action;
	int previous_stderr;
};

static struct tc_inactivity_guard tc_guard = { .previous_stderr = -1 };
#ifndef TC_MIGRATION_PROGRESS_HOOK
#define TC_MIGRATION_PROGRESS_HOOK() do { } while (0)
#endif

static void tc_stall_handler(int signal_number)
{
	(void)signal_number;
	_exit(TC_STALL_EXIT);
}

static int tc_guard_arm(void)
{
	struct itimerval timer = {0};

	timer.it_value.tv_sec = tc_guard.seconds;
	return setitimer(ITIMER_REAL, &timer, NULL);
}

static void tc_progress(void)
{
	int error;

	TC_MIGRATION_PROGRESS_HOOK();
	if (!tc_guard.enabled) {
		return;
	}
	error = errno;
	if (tc_guard_arm() != 0) {
		_exit(74);
	}
	errno = error;
}

static int tc_guard_start(unsigned seconds)
{
	struct sigaction action = {0};

	action.sa_handler = tc_stall_handler;
	if (sigemptyset(&action.sa_mask) != 0 ||
	    sigaction(SIGALRM, &action, &tc_guard.previous_action) != 0)
	{
		return -1;
	}
	tc_guard.enabled = true;
	tc_guard.seconds = seconds;
	if (tc_guard_arm() != 0) {
		int error = errno;
		tc_guard.enabled = false;
		sigaction(SIGALRM, &tc_guard.previous_action, NULL);
		errno = error;
		return -1;
	}
	return 0;
}

static int tc_guard_finish(int result)
{
	struct itimerval disabled = {0};
	int finish_error = 0;

	if (!tc_guard.enabled) {
		return result;
	}
	if (ferror(stdout) || fflush(stdout) != 0) {
		finish_error = errno ? errno : EIO;
	}
	if (fflush(stderr) != 0 && finish_error == 0) {
		finish_error = errno ? errno : EIO;
	}
	if (setitimer(ITIMER_REAL, &disabled, NULL) != 0 && finish_error == 0) {
		finish_error = errno;
	}
	if (sigaction(SIGALRM, &tc_guard.previous_action, NULL) != 0 && finish_error == 0) {
		finish_error = errno;
	}
	tc_guard.enabled = false;
	if (tc_guard.previous_stderr != -1) {
		if (dup2(tc_guard.previous_stderr, STDERR_FILENO) == -1 && finish_error == 0) {
			finish_error = errno;
		}
		close(tc_guard.previous_stderr);
		tc_guard.previous_stderr = -1;
	}
	if (finish_error != 0 && result == 0) {
		errno = finish_error;
		return 4;
	}
	return result;
}

static int tc_guard_log(const char *path)
{
	int log_fd;

	tc_guard.previous_stderr = dup(STDERR_FILENO);
	if (tc_guard.previous_stderr == -1) {
		return -1;
	}
	log_fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
	if (log_fd == -1 || dup2(log_fd, STDERR_FILENO) == -1) {
		int error = errno;
		if (log_fd != -1) {
			close(log_fd);
		}
		close(tc_guard.previous_stderr);
		tc_guard.previous_stderr = -1;
		errno = error;
		return -1;
	}
	close(log_fd);
	return 0;
}

static int tc_parse_guard_options(int *argc, char ***argv,
				  unsigned *stall_seconds, const char **log_path)
{
	char **values = *argv;
	int count = *argc;
	int source = 1, target = 1;
	bool have_stall = false, have_log = false;

	*stall_seconds = 0;
	*log_path = NULL;
	while (source < count) {
		if (strcmp(values[source], "--stall-seconds") == 0) {
			char *end;
			unsigned long parsed;
			if (have_stall || source + 1 >= count) {
				return -1;
			}
			errno = 0;
			parsed = strtoul(values[source + 1], &end, 10);
			if (errno != 0 || values[source + 1][0] == '\0' || *end != '\0' ||
			    parsed == 0 || parsed > INT_MAX)
			{
				return -1;
			}
			*stall_seconds = (unsigned)parsed;
			have_stall = true;
			source += 2;
			continue;
		}
		if (strcmp(values[source], "--log") == 0) {
			if (have_log || source + 1 >= count || values[source + 1][0] == '\0') {
				return -1;
			}
			*log_path = values[source + 1];
			have_log = true;
			source += 2;
			continue;
		}
		break;
	}
	if (have_log && !have_stall) {
		return -1;
	}
	while (source < count) {
		values[target++] = values[source++];
	}
	values[target] = NULL;
	*argc = target;
	return 0;
}

enum tc_phase {
	TC_PHASE_COPY,
	TC_PHASE_CLEANUP,
};

struct tc_tdb_key {
	uint8_t data[16];
	bool matched;
	bool retired;
};

struct tc_ad_entry {
	bool present;
	uint32_t offset;
	uint32_t length;
};

struct tc_appledouble {
	uint8_t *header;
	size_t header_size;
	struct tc_ad_entry finderinfo;
	struct tc_ad_entry resource;
	bool unsupported_entries;
};

struct tc_counts {
	uint64_t entries;
	uint64_t tdb_records;
	uint64_t xattrs_written;
	uint64_t streams_written;
	uint64_t finderinfo_written;
	uint64_t resources_written;
	uint64_t native_kept;
	uint64_t conflicts;
	uint64_t sidecars_seen;
	uint64_t sidecars_deleted;
	uint64_t tdb_total;
	uint64_t tdb_matched;
	uint64_t tdb_deleted;
	uint64_t tdb_retired;
	uint64_t orphaned;
	uint64_t unresolved;
	uint64_t boundary_skipped;
	uint64_t tdb_quarantined;
	uint64_t errors;
};

struct tc_multi;
struct tc_migration {
	struct tc_multi *multi;
	TALLOC_CTX *mem_ctx;
	struct db_context *db;
	const char *tdb_path;
	const char *legacy_metadata;
	enum tc_phase phase;
	struct tc_tdb_key *tdb_keys;
	size_t num_tdb_keys;
	int tdb_collect_error;
	/* st_dev of the root being walked; entries on another device are
	 * nested mounts and are skipped rather than crossed. */
	uint64_t root_dev;
	/* st_dev of every root that was walked without a single error. A
	 * row whose devid is in this set and that no file claimed is a proven
	 * orphan; any other unmatched row is unresolved and must be kept. */
	uint64_t *complete_devs;
	size_t num_complete_devs;
	struct tc_counts counts;
};

static int tc_multi_file(struct tc_migration *, int, const char *, const struct stat *, bool);

static bool tc_missing_error(int error)
{
	return error == ENOATTR || error == ENODATA || error == ENOENT;
}

static struct file_id tc_file_id(const struct stat *st)
{
	struct file_id id = {0};
	id.devid = st->st_dev;
	id.inode = st->st_ino;
	return id;
}

static int tc_collect_tdb_key(struct db_record *record, void *private_data)
{
	struct tc_migration *migration = private_data;
	struct tc_tdb_key *keys;
	TDB_DATA key = dbwrap_record_get_key(record);

	if (key.dsize != sizeof(migration->tdb_keys[0].data)) {
		migration->tdb_collect_error = errno = EINVAL;
		return -1;
	}
	/* Bound deploy coverage memory on the appliance. */
	if (migration->multi && migration->num_tdb_keys >= 262144) {
		migration->tdb_collect_error = errno = E2BIG;
		return -1;
	}
	keys = talloc_realloc(
		migration->mem_ctx,
		migration->tdb_keys,
		struct tc_tdb_key,
		migration->num_tdb_keys + 1);
	if (keys == NULL) {
		migration->tdb_collect_error = errno = ENOMEM;
		return -1;
	}
	migration->tdb_keys = keys;
	memcpy(keys[migration->num_tdb_keys].data, key.dptr, key.dsize);
	keys[migration->num_tdb_keys].matched = false;
	keys[migration->num_tdb_keys].retired = false;
	migration->num_tdb_keys++;
	tc_progress();
	return 0;
}

static int tc_collect_tdb_keys(struct tc_migration *migration)
{
	NTSTATUS status;

	if (migration->db == NULL) {
		return 0;
	}
	migration->tdb_collect_error = 0;
	status = dbwrap_traverse_read(
		migration->db, tc_collect_tdb_key, migration, NULL);
	/* TDB returns a positive record count when a callback stops traversal,
	 * so preserve callback failures separately from the NTSTATUS result. */
	if (migration->tdb_collect_error != 0) {
		errno = migration->tdb_collect_error;
		fprintf(stderr, "unable to enumerate TDB records: %s\n",
			strerror(errno));
		return -1;
	}
	if (!NT_STATUS_IS_OK(status)) {
		fprintf(stderr, "unable to enumerate TDB records: %s\n",
			nt_errstr(status));
		return -1;
	}
	migration->counts.tdb_total = migration->num_tdb_keys;
	return 0;
}

static int tc_mark_tdb_key(struct tc_migration *migration,
			   const struct file_id *id)
{
	uint8_t key[16];
	size_t i;

	push_file_id_16(key, id);
	/* The deployed databases average only a few thousand rows. A linear
	 * lookup keeps this one-shot helper small; replace it if that changes. */
	for (i = 0; i < migration->num_tdb_keys; i++) {
		if (memcmp(migration->tdb_keys[i].data, key, sizeof(key)) != 0) {
			continue;
		}
		if (!migration->tdb_keys[i].matched) {
			migration->tdb_keys[i].matched = true;
			migration->counts.tdb_matched++;
		}
		/* A failed first hardlink visit must not bypass verification later. */
		return migration->tdb_keys[i].retired ? 0 : 1;
	}
	return 0;
}

static int tc_native_get(TALLOC_CTX *mem_ctx,
			 int fd,
			 const char *name,
			 DATA_BLOB *blob)
{
	ssize_t size;
	ssize_t ret;

	*blob = data_blob_null;
	size = tc_airport_fgetxattr(fd, name, NULL, 0);
	if (size < 0) {
		return -1;
	}
	if (size == 0) {
		return 0;
	}
	blob->data = talloc_array(mem_ctx, uint8_t, size);
	if (blob->data == NULL) {
		errno = ENOMEM;
		return -1;
	}
	ret = tc_airport_fgetxattr(fd, name, blob->data, size);
	if (ret != size) {
		if (ret >= 0) {
			errno = EIO;
		}
		TALLOC_FREE(blob->data);
		return -1;
	}
	blob->length = size;
	return 0;
}

static int tc_native_write_verified(struct tc_migration *migration,
				    int fd,
				    const char *path,
				    const char *name,
				    const uint8_t *value,
				    size_t size,
				    bool keep_existing)
{
	TALLOC_CTX *frame = talloc_stackframe();
	DATA_BLOB existing = data_blob_null;
	int ret;

	ret = tc_native_get(frame, fd, name, &existing);
	if (ret == 0) {
		if (existing.length == size &&
		    (size == 0 || memcmp(existing.data, value, size) == 0))
		{
			migration->counts.native_kept++;
			tc_progress();
			TALLOC_FREE(frame);
			return 0;
		}
		if (keep_existing) {
			migration->counts.native_kept++;
			migration->counts.conflicts++;
			tc_progress();
			TALLOC_FREE(frame);
			return 0;
		}
	} else if (!tc_missing_error(errno)) {
		fprintf(stderr, "native read failed path=%s name=%s error=%s\n",
			path, name, strerror(errno));
		TALLOC_FREE(frame);
		return -1;
	}
	if (migration->phase == TC_PHASE_CLEANUP) {
		fprintf(stderr,
			"native verification mismatch path=%s name=%s\n",
			path, name);
		TALLOC_FREE(frame);
		errno = EIO;
		return -1;
	}

	if (tc_airport_fsetxattr(fd, name, value, size, 0) != 0) {
		fprintf(stderr, "native write failed path=%s name=%s size=%zu error=%s\n",
			path, name, size, strerror(errno));
		TALLOC_FREE(frame);
		return -1;
	}
	TALLOC_FREE(existing.data);
	if (tc_native_get(frame, fd, name, &existing) != 0 ||
	    existing.length != size ||
	    memcmp(existing.data, value, size) != 0)
	{
		fprintf(stderr, "native verification failed path=%s name=%s\n",
			path, name);
		TALLOC_FREE(frame);
		errno = EIO;
		return -1;
	}
	migration->counts.xattrs_written++;
	tc_progress();
	TALLOC_FREE(frame);
	return 0;
}

static ssize_t tc_tdb_get(struct tc_migration *migration,
			  TALLOC_CTX *mem_ctx,
			  const struct file_id *id,
			  const char *name,
			  DATA_BLOB *blob)
{
	return xattr_tdb_getattr(migration->db, mem_ctx, id, name, blob);
}

static bool tc_stream_anchor(const char *name)
{
	return strncmp(name, TC_STREAM_PREFIX, strlen(TC_STREAM_PREFIX)) == 0;
}

static bool tc_stream_extent(const char *name)
{
	return strncmp(name, TC_EXT_PREFIX, strlen(TC_EXT_PREFIX)) == 0;
}

static int tc_read_tdb_stream(struct tc_migration *migration,
			      TALLOC_CTX *mem_ctx,
			      const struct file_id *id,
			      const char *anchor_name,
			      DATA_BLOB *logical)
{
	DATA_BLOB anchor = data_blob_null;
	const char *raw_name;
	uint8_t extents;
	size_t total;
	size_t written;
	uint8_t i;
	ssize_t ret;

	*logical = data_blob_null;
	ret = tc_tdb_get(migration, mem_ctx, id, anchor_name, &anchor);
	if (ret < 0) {
		return -1;
	}
	if (anchor.length == 0) {
		errno = EINVAL;
		return -1;
	}
	extents = anchor.data[anchor.length - 1];
	if (extents >= TC_HFS_STREAM_XATTRS) {
		errno = EOVERFLOW;
		return -1;
	}
	total = anchor.length - 1;
	raw_name = anchor_name + strlen(TC_STREAM_PREFIX);
	for (i = 1; i <= extents; i++) {
		char *extent_name = talloc_asprintf(
			mem_ctx, "%s%u.%s", TC_EXT_PREFIX, i, raw_name);
		DATA_BLOB extent = data_blob_null;

		if (extent_name == NULL) {
			errno = ENOMEM;
			return -1;
		}
		ret = tc_tdb_get(migration, mem_ctx, id, extent_name, &extent);
		if (ret < 0 || total + extent.length < total) {
			if (ret >= 0) {
				errno = EOVERFLOW;
			}
			return -1;
		}
		total += extent.length;
	}
	logical->data = talloc_array(mem_ctx, uint8_t, total == 0 ? 1 : total);
	if (logical->data == NULL) {
		errno = ENOMEM;
		return -1;
	}
	memcpy(logical->data, anchor.data, anchor.length - 1);
	written = anchor.length - 1;
	for (i = 1; i <= extents; i++) {
		char *extent_name = talloc_asprintf(
			mem_ctx, "%s%u.%s", TC_EXT_PREFIX, i, raw_name);
		DATA_BLOB extent = data_blob_null;

		ret = tc_tdb_get(migration, mem_ctx, id, extent_name, &extent);
		if (ret < 0) {
			return -1;
		}
		memcpy(logical->data + written, extent.data, extent.length);
		written += extent.length;
	}
	logical->length = total;
	return 0;
}

static char *tc_apple_native_name(TALLOC_CTX *mem_ctx,
				  const char *anchor_name)
{
	const char *raw = anchor_name + strlen(TC_STREAM_PREFIX);
	size_t raw_len = strlen(raw);
	size_t suffix_len = strlen(TC_STREAM_SUFFIX);

	if (raw_len <= suffix_len ||
	    strncmp(raw, "com.apple.", strlen("com.apple.")) != 0 ||
	    strcmp(raw + raw_len - suffix_len, TC_STREAM_SUFFIX) != 0)
	{
		return NULL;
	}
	return talloc_strndup(mem_ctx, raw, raw_len - suffix_len);
}

/* TDB values are authoritative during the deploy migration. Stream extents
 * are verified before publishing their anchor, so an interrupted copy is
 * retryable and cleanup never retires a mismatched legacy record. */
static int tc_write_internal_stream(struct tc_migration *migration,
				    int fd,
				    const char *path,
				    const char *anchor_name,
				    const DATA_BLOB *logical)
{
	TALLOC_CTX *frame = talloc_stackframe();
	const char *raw_name = anchor_name + strlen(TC_STREAM_PREFIX);
	size_t first_len = MIN(logical->length, TC_HFS_XATTR_SIZE - 1);
	size_t remaining = logical->length - first_len;
	size_t extents = remaining == 0 ? 0 :
		(remaining + TC_HFS_XATTR_SIZE - 1) / TC_HFS_XATTR_SIZE;
	uint8_t *anchor;
	size_t offset;
	size_t i;

	if (extents >= TC_HFS_STREAM_XATTRS) {
		fprintf(stderr, "stream too large path=%s name=%s size=%zu\n",
			path, anchor_name, logical->length);
		TALLOC_FREE(frame);
		errno = EOVERFLOW;
		return -1;
	}
	anchor = talloc_array(frame, uint8_t, first_len + 1);
	if (anchor == NULL) {
		TALLOC_FREE(frame);
		errno = ENOMEM;
		return -1;
	}
	memcpy(anchor, logical->data, first_len);
	anchor[first_len] = extents;
	offset = first_len;
	for (i = 1; i <= extents; i++) {
		char *extent_name = talloc_asprintf(
			frame, "%s%zu.%s", TC_EXT_PREFIX, i, raw_name);
		size_t chunk = MIN(logical->length - offset, TC_HFS_XATTR_SIZE);

		if (extent_name == NULL ||
		    tc_native_write_verified(migration, fd, path, extent_name,
					     logical->data + offset,
					     chunk, false) != 0)
		{
			TALLOC_FREE(frame);
			return -1;
		}
		offset += chunk;
	}
	if (tc_native_write_verified(migration, fd, path, anchor_name,
				     anchor, first_len + 1, false) != 0)
	{
		TALLOC_FREE(frame);
		return -1;
	}
	migration->counts.streams_written++;
	TALLOC_FREE(frame);
	return 0;
}

static int tc_migrate_stream(struct tc_migration *migration,
			     int fd,
			     const char *path,
			     const struct file_id *id,
			     const char *anchor_name)
{
	TALLOC_CTX *frame = talloc_stackframe();
	DATA_BLOB logical = data_blob_null;
	char *native_name;
	int ret;

	if (strcmp(anchor_name, TC_AFPINFO_XATTR) == 0) {
		TALLOC_FREE(frame);
		return 0;
	}
	ret = tc_read_tdb_stream(
		migration, frame, id, anchor_name, &logical);
	if (ret != 0) {
		fprintf(stderr, "stream decode failed path=%s name=%s error=%s\n",
			path, anchor_name, strerror(errno));
		TALLOC_FREE(frame);
		return -1;
	}
	native_name = tc_apple_native_name(frame, anchor_name);
	if (native_name != NULL) {
		if (logical.length > TC_HFS_XATTR_SIZE) {
			fprintf(stderr, "Apple xattr too large path=%s name=%s size=%zu\n",
				path, native_name, logical.length);
			TALLOC_FREE(frame);
			errno = EOVERFLOW;
			return -1;
		}
		ret = tc_native_write_verified(
			migration, fd, path, native_name,
			logical.data, logical.length, false);
	} else {
		ret = tc_write_internal_stream(
			migration, fd, path, anchor_name, &logical);
	}
	TALLOC_FREE(frame);
	return ret;
}

static int tc_finderinfo_from_stream(struct tc_migration *migration,
				     TALLOC_CTX *mem_ctx,
				     const struct file_id *id,
				     uint8_t finderinfo[AFP_FinderSize])
{
	DATA_BLOB logical = data_blob_null;

	if (tc_read_tdb_stream(
			migration, mem_ctx, id, TC_AFPINFO_XATTR, &logical) != 0)
	{
		return -1;
	}
	if (logical.length != AFP_INFO_SIZE) {
		errno = EINVAL;
		return -1;
	}
	if (RIVAL(logical.data, 0) != AFP_Signature ||
	    RIVAL(logical.data, 4) != AFP_Version)
	{
		errno = EINVAL;
		return -1;
	}
	memcpy(finderinfo, logical.data + AFP_OFF_FinderInfo, AFP_FinderSize);
	return 0;
}

static int tc_appledouble_entry(const uint8_t *data,
				size_t size,
				uint32_t wanted_id,
				DATA_BLOB *entry)
{
	uint16_t count;
	uint16_t i;

	*entry = data_blob_null;
	if (size < TC_AD_HEADER_SIZE ||
	    PULL_BE_U32(data, 0) != TC_AD_MAGIC ||
	    PULL_BE_U32(data, 4) != TC_AD_VERSION)
	{
		errno = EINVAL;
		return -1;
	}
	count = PULL_BE_U16(data, 24);
	if (TC_AD_HEADER_SIZE + (size_t)count * TC_AD_ENTRY_SIZE > size) {
		errno = EINVAL;
		return -1;
	}
	for (i = 0; i < count; i++) {
		size_t descriptor = TC_AD_HEADER_SIZE +
			(size_t)i * TC_AD_ENTRY_SIZE;
		uint32_t id = PULL_BE_U32(data, descriptor);
		uint32_t offset = PULL_BE_U32(data, descriptor + 4);
		uint32_t length = PULL_BE_U32(data, descriptor + 8);

		if (id != wanted_id) {
			continue;
		}
		if ((size_t)offset + length < offset ||
		    (size_t)offset + length > size)
		{
			errno = EINVAL;
			return -1;
		}
		entry->data = discard_const_p(uint8_t, data) + offset;
		entry->length = length;
		return 0;
	}
	errno = ENOATTR;
	return -1;
}

static int tc_finderinfo_from_netatalk(struct tc_migration *migration,
				       TALLOC_CTX *mem_ctx,
				       const struct file_id *id,
				       uint8_t finderinfo[AFP_FinderSize])
{
	DATA_BLOB blob = data_blob_null;
	DATA_BLOB entry = data_blob_null;
	ssize_t ret;

	ret = tc_tdb_get(
		migration, mem_ctx, id, TC_NETATALK_META_XATTR, &blob);
	if (ret < 0) {
		return -1;
	}
	if (tc_appledouble_entry(
			blob.data, blob.length, TC_AD_FINDERI, &entry) != 0 ||
	    entry.length != AFP_FinderSize)
	{
		errno = EINVAL;
		return -1;
	}
	memcpy(finderinfo,
	       entry.data,
	       AFP_FinderSize);
	return 0;
}

static int tc_migrate_finderinfo(struct tc_migration *migration,
				 int fd,
				 const char *path,
				 const struct file_id *id)
{
	TALLOC_CTX *frame = talloc_stackframe();
	uint8_t finderinfo[AFP_FinderSize];
	int ret;

	if (strcmp(migration->legacy_metadata, "netatalk") == 0) {
		ret = tc_finderinfo_from_netatalk(
			migration, frame, id, finderinfo);
		if (ret != 0 && tc_missing_error(errno)) {
			ret = tc_finderinfo_from_stream(
				migration, frame, id, finderinfo);
		}
	} else {
		ret = tc_finderinfo_from_stream(
			migration, frame, id, finderinfo);
		if (ret != 0 && tc_missing_error(errno)) {
			ret = tc_finderinfo_from_netatalk(
				migration, frame, id, finderinfo);
		}
	}
	/* Some payloads already used Apple's raw FinderInfo name. Do not mark
	 * their TDB record verified without copying that value as well. */
	if (ret != 0 && tc_missing_error(errno)) {
		DATA_BLOB raw = data_blob_null;
		ret = tc_tdb_get(migration, frame, id, TC_FINDERINFO_XATTR, &raw);
		if (ret >= 0) {
			if (raw.length != AFP_FinderSize) { ret = -1; errno = EINVAL; }
			else { memcpy(finderinfo, raw.data, AFP_FinderSize); ret = 0; }
		}
	}
	if (ret != 0) {
		if (tc_missing_error(errno)) {
			TALLOC_FREE(frame);
			return 0;
		}
		fprintf(stderr, "FinderInfo decode failed path=%s error=%s\n",
			path, strerror(errno));
		TALLOC_FREE(frame);
		return -1;
	}
	ret = tc_native_write_verified(
		migration, fd, path, TC_FINDERINFO_XATTR,
		finderinfo, AFP_FinderSize, false);
	if (ret == 0) {
		migration->counts.finderinfo_written++;
	}
	TALLOC_FREE(frame);
	return ret;
}

static int tc_migrate_tdb_record(struct tc_migration *migration,
				 int fd,
				 const char *path,
				 const struct stat *st)
{
	TALLOC_CTX *frame;
	struct file_id id = tc_file_id(st);
	ssize_t list_size;
	ssize_t ret;
	char *list;
	size_t offset = 0;

	if (migration->db == NULL) {
		return 0;
	}
	if (tc_mark_tdb_key(migration, &id) == 0) {
		return 0;
	}
	frame = talloc_stackframe();
	migration->counts.tdb_records++;

	list_size = xattr_tdb_listattr(migration->db, &id, NULL, 0);
	if (list_size < 0) {
		fprintf(stderr, "TDB list failed path=%s error=%s\n",
			path, strerror(errno));
		TALLOC_FREE(frame);
		return -1;
	}
	if (list_size == 0) {
		TALLOC_FREE(frame);
		return 0;
	}
	list = talloc_array(frame, char, list_size);
	if (list == NULL) {
		TALLOC_FREE(frame);
		errno = ENOMEM;
		return -1;
	}
	ret = xattr_tdb_listattr(migration->db, &id, list, list_size);
	if (ret != list_size) {
		TALLOC_FREE(frame);
		return -1;
	}
	if (tc_migrate_finderinfo(migration, fd, path, &id) != 0) {
		TALLOC_FREE(frame);
		return -1;
	}
	while (offset < (size_t)list_size) {
		const char *name = list + offset;
		size_t remaining = (size_t)list_size - offset;
		size_t name_length = strnlen(name, remaining);
		size_t name_size;
		DATA_BLOB blob = data_blob_null;

		if (name_length == remaining) {
			TALLOC_FREE(frame);
			errno = EINVAL;
			return -1;
		}
		name_size = name_length + 1;
		offset += name_size;
		if (strcmp(name, TC_AFPINFO_XATTR) == 0 ||
		    strcmp(name, TC_NETATALK_META_XATTR) == 0 ||
		    strcmp(name, TC_FINDERINFO_XATTR) == 0 ||
		    tc_stream_extent(name))
		{
			continue;
		}
		if (tc_stream_anchor(name)) {
			if (tc_migrate_stream(migration, fd, path, &id, name) != 0) {
				TALLOC_FREE(frame);
				return -1;
			}
			continue;
		}
		ret = tc_tdb_get(migration, frame, &id, name, &blob);
		if (ret < 0) {
			TALLOC_FREE(frame);
			return -1;
		}
		if (blob.length > TC_HFS_XATTR_SIZE) {
			fprintf(stderr, "xattr too large path=%s name=%s size=%zu\n",
				path, name, blob.length);
			TALLOC_FREE(frame);
			errno = EOVERFLOW;
			return -1;
		}
		if (tc_native_write_verified(
				migration, fd, path, name,
				blob.data, blob.length, false) != 0)
		{
			TALLOC_FREE(frame);
			return -1;
		}
	}
	TALLOC_FREE(frame);
	return 0;
}

static int tc_pread_exact(int fd, void *value, size_t size, off_t offset)
{
	uint8_t *bytes = value;
	size_t done = 0;

	while (done < size) {
		ssize_t ret = pread(fd, bytes + done, size - done, offset + done);

		if (ret < 0 && errno == EINTR) {
			continue;
		}
		if (ret <= 0) {
			if (ret == 0) {
				errno = EIO;
			}
			return -1;
		}
		done += ret;
		tc_progress();
	}
	return 0;
}

/* Return 0 for valid AppleDouble, 1 for an ordinary ._ file, and -1 for a
 * corrupt AppleDouble container. Resource bytes remain on disk and are
 * streamed later; only the bounded header is retained here. */
static int tc_parse_appledouble(int fd,
				const struct stat *st,
				struct tc_appledouble *ad)
{
	uint16_t count;
	size_t descriptors_size;
	size_t i;

	ZERO_STRUCTP(ad);
	if (st->st_size < TC_AD_HEADER_SIZE) {
		return 1;
	}
	ad->header_size = MIN((off_t)TC_AD_MAX_HEADER, st->st_size);
	ad->header = malloc(ad->header_size);
	if (ad->header == NULL) {
		errno = ENOMEM;
		return -1;
	}
	if (tc_pread_exact(fd, ad->header, ad->header_size, 0) != 0) {
		return -1;
	}
	if (PULL_BE_U32(ad->header, 0) != TC_AD_MAGIC ||
	    PULL_BE_U32(ad->header, 4) != TC_AD_VERSION)
	{
		return 1;
	}
	count = PULL_BE_U16(ad->header, 24);
	if (count == 0 || count > TC_AD_MAX_ENTRIES) {
		errno = EINVAL;
		return -1;
	}
	descriptors_size = (size_t)count * TC_AD_ENTRY_SIZE;
	if (TC_AD_HEADER_SIZE + descriptors_size > ad->header_size) {
		errno = EINVAL;
		return -1;
	}
	for (i = 0; i < count; i++) {
		const uint8_t *descriptor = ad->header + TC_AD_HEADER_SIZE +
			i * TC_AD_ENTRY_SIZE;
		uint32_t id = PULL_BE_U32(descriptor, 0);
		struct tc_ad_entry *entry = NULL;
		uint32_t offset = PULL_BE_U32(descriptor, 4);
		uint32_t length = PULL_BE_U32(descriptor, 8);

		if ((uint64_t)offset + length > (uint64_t)st->st_size) {
			errno = EINVAL;
			return -1;
		}
		if (id == TC_AD_FINDERI) {
			entry = &ad->finderinfo;
		} else if (id == TC_AD_RFORK) {
			entry = &ad->resource;
		} else {
			ad->unsupported_entries = true;
			continue;
		}
		if (entry->present) {
			errno = EINVAL;
			return -1;
		}
		entry->present = true;
		entry->offset = offset;
		entry->length = length;
	}
	if (ad->finderinfo.present &&
	    (ad->finderinfo.length < AFP_FinderSize ||
	     (uint64_t)ad->finderinfo.offset + ad->finderinfo.length >
		ad->header_size))
	{
		errno = EINVAL;
		return -1;
	}
	return 0;
}

static int tc_migrate_appledouble_finderinfo(
	struct tc_migration *migration,
	int base_fd,
	const char *path,
	const struct tc_appledouble *ad)
{
	const uint8_t *finderinfo;

	if (!ad->finderinfo.present) {
		return 0;
	}
	finderinfo = ad->header + ad->finderinfo.offset;
	if (all_zero(finderinfo, AFP_FinderSize)) {
		return 0;
	}
	if (tc_native_write_verified(
			migration, base_fd, path, TC_FINDERINFO_XATTR,
			finderinfo, AFP_FinderSize, true) != 0)
	{
		return -1;
	}
	migration->counts.finderinfo_written++;
	return 0;
}

static int tc_migrate_appledouble_xattrs(
	struct tc_migration *migration,
	int base_fd,
	const char *path,
	const struct tc_appledouble *ad)
{
	const struct tc_ad_entry *finder = &ad->finderinfo;
	const uint8_t *header;
	size_t header_offset;
	uint32_t total_size;
	uint32_t data_start;
	uint32_t data_length;
	uint16_t count;
	size_t entry_offset;
	uint16_t i;

	if (!finder->present || finder->length == AFP_FinderSize) {
		return 0;
	}
	if (finder->length < AFP_FinderSize + 2 + TC_AD_XATTR_HEADER_SIZE) {
		errno = EINVAL;
		return -1;
	}
	if (memcmp(ad->header + TC_AD_FILLER_OFFSET,
		   TC_AD_OSX_FILLER, TC_AD_FILLER_SIZE) != 0)
	{
		errno = EINVAL;
		return -1;
	}
	header_offset = finder->offset + AFP_FinderSize + 2;
	header = ad->header + header_offset;
	if (PULL_BE_U32(header, 0) != TC_AD_XATTR_MAGIC) {
		errno = EINVAL;
		return -1;
	}
	total_size = PULL_BE_U32(header, 8);
	data_start = PULL_BE_U32(header, 12);
	data_length = PULL_BE_U32(header, 16);
	count = PULL_BE_U16(header, 34);
	if (count > TC_AD_MAX_ENTRIES ||
	    total_size > ad->header_size ||
	    total_size > (uint64_t)finder->offset + finder->length ||
	    data_start < header_offset + TC_AD_XATTR_HEADER_SIZE ||
	    (uint64_t)data_start + data_length > total_size)
	{
		errno = EINVAL;
		return -1;
	}
	entry_offset = header_offset + TC_AD_XATTR_HEADER_SIZE;
	for (i = 0; i < count; i++) {
		uint32_t value_offset;
		uint32_t value_length;
		uint8_t name_length;
		const char *name;

		entry_offset = (entry_offset + 3) & ~(size_t)3;
		if (entry_offset + TC_AD_XATTR_ENTRY_SIZE > data_start) {
			errno = EINVAL;
			return -1;
		}
		value_offset = PULL_BE_U32(ad->header, entry_offset);
		value_length = PULL_BE_U32(ad->header, entry_offset + 4);
		name_length = ad->header[entry_offset + 10];
		if (name_length == 0 ||
		    entry_offset + TC_AD_XATTR_ENTRY_SIZE + name_length > data_start ||
		    ad->header[entry_offset + TC_AD_XATTR_ENTRY_SIZE +
			name_length - 1] != '\0' ||
		    (uint64_t)value_offset + value_length > total_size)
		{
			errno = EINVAL;
			return -1;
		}
		name = (const char *)ad->header + entry_offset +
			TC_AD_XATTR_ENTRY_SIZE;
		if (strcmp(name, TC_FINDERINFO_XATTR) != 0 &&
		    strcmp(name, "com.apple.ResourceFork") != 0)
		{
			if (value_length > TC_HFS_XATTR_SIZE) {
				fprintf(stderr,
					"AppleDouble xattr too large path=%s name=%s size=%u\n",
					path, name, value_length);
				errno = EOVERFLOW;
				return -1;
			}
			if (tc_native_write_verified(
					migration, base_fd, path, name,
					ad->header + value_offset,
					value_length, true) != 0)
			{
				return -1;
			}
		}
		entry_offset += TC_AD_XATTR_ENTRY_SIZE + name_length;
	}
	return 0;
}

static char *tc_appledouble_path(TALLOC_CTX *mem_ctx, const char *path)
{
	const char *base = strrchr(path, '/');

	if (base == NULL) {
		return talloc_asprintf(mem_ctx, "._%s", path);
	}
	return talloc_asprintf(mem_ctx, "%.*s/._%s",
		(int)(base - path), path, base + 1);
}

/* The exact placeholder vfs_fruit already removes when
 * fruit:wipe_intentionally_left_blank_rfork=yes. */
static const uint8_t tc_empty_resourcefork[] = {
	0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x1E,
	0x54, 0x68, 0x69, 0x73, 0x20, 0x72, 0x65, 0x73,
	0x6F, 0x75, 0x72, 0x63, 0x65, 0x20, 0x66, 0x6F,
	0x72, 0x6B, 0x20, 0x69, 0x6E, 0x74, 0x65, 0x6E,
	0x74, 0x69, 0x6F, 0x6E, 0x61, 0x6C, 0x6C, 0x79,
	0x20, 0x6C, 0x65, 0x66, 0x74, 0x20, 0x62, 0x6C,
	0x61, 0x6E, 0x6B, 0x20, 0x20, 0x20, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x00,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x1E,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	0x00, 0x1C, 0x00, 0x1E, 0xFF, 0xFF
};

/* 1 means the known blank fork, 0 ordinary data, -1 a read failure. */
static int tc_is_empty_resourcefork(int source_fd, off_t offset, off_t length)
{
	uint8_t value[sizeof(tc_empty_resourcefork)];

	if (length != sizeof(value)) {
		return 0;
	}
	if (tc_pread_exact(source_fd, value, sizeof(value), offset) != 0) {
		return -1;
	}
	return memcmp(value, tc_empty_resourcefork, sizeof(value)) == 0;
}

/* Return 0 for equal readable forks, 1 for readable conflicts, -1 for errors.
 * Finish reading both forks even after finding a difference: a later disk
 * error must not turn an unverified legacy fork into disposable data. */
static int tc_verify_resource(int source_fd, int resource_fd,
			      off_t source_offset, off_t resource_length)
{
	uint8_t *source = NULL;
	uint8_t *native = NULL;
	struct stat native_st;
	off_t offset = 0;
	bool different;
	int result = -1;

	if (fstat(resource_fd, &native_st) != 0) {
		return -1;
	}
	different = native_st.st_size != resource_length;
	source = malloc(TC_COPY_SIZE);
	native = malloc(TC_COPY_SIZE);
	if (source == NULL || native == NULL) {
		errno = ENOMEM;
		goto out;
	}
	while (offset < MAX(resource_length, native_st.st_size)) {
		size_t source_size = offset < resource_length ?
			MIN((off_t)TC_COPY_SIZE, resource_length - offset) : 0;
		size_t native_size = offset < native_st.st_size ?
			MIN((off_t)TC_COPY_SIZE, native_st.st_size - offset) : 0;

		if (tc_pread_exact(source_fd, source, source_size,
				   source_offset + offset) != 0 ||
		    tc_pread_exact(resource_fd, native, native_size, offset) != 0)
		{
			goto out;
		}
		if (memcmp(source, native, MIN(source_size, native_size)) != 0) {
			different = true;
		}
		offset += MAX(source_size, native_size);
		tc_progress();
	}
	result = different ? 1 : 0;
out:
	free(source);
	free(native);
	return result;
}

/* Return 1 for our in-progress marker, 0 when absent, and -1 if a marker
 * belongs to a different source length or is corrupt. */
static int tc_resource_marker_state(int base_fd, off_t resource_length)
{
	uint8_t marker[16];
	ssize_t size;
	ssize_t ret;

	size = tc_airport_fgetxattr(
		base_fd, TC_RESOURCE_MARKER_XATTR, NULL, 0);
	if (size < 0) {
		return tc_missing_error(errno) ? 0 : -1;
	}
	if (size != sizeof(marker)) {
		errno = EINVAL;
		return -1;
	}
	ret = tc_airport_fgetxattr(
		base_fd, TC_RESOURCE_MARKER_XATTR, marker, sizeof(marker));
	if (ret != sizeof(marker) ||
	    memcmp(marker, TC_RESOURCE_MARKER_MAGIC, 8) != 0 ||
	    PULL_BE_U64(marker, 8) != (uint64_t)resource_length)
	{
		if (ret >= 0) {
			errno = EINVAL;
		}
		return -1;
	}
	return 1;
}

static int tc_set_resource_marker(int base_fd, off_t resource_length)
{
	uint8_t marker[16];

	memcpy(marker, TC_RESOURCE_MARKER_MAGIC, 8);
	PUSH_BE_U64(marker, 8, resource_length);
	return tc_airport_fsetxattr(
		base_fd, TC_RESOURCE_MARKER_XATTR, marker, sizeof(marker), 0);
}

static int tc_remove_resource_marker(int base_fd)
{
	int ret = tc_airport_fremovexattr(
		base_fd, TC_RESOURCE_MARKER_XATTR);

	if (ret == 0 || tc_missing_error(errno)) {
		return 0;
	}
	return -1;
}

static int tc_migrate_resource(struct tc_migration *migration,
			       int base_fd,
			       int source_fd,
			       const char *path,
			       const struct tc_ad_entry *resource)
{
	TALLOC_CTX *frame = talloc_stackframe();
	char *resource_path = NULL;
	struct stat native_st;
	uint8_t *buffer = NULL;
	off_t copied = 0;
	int marker_state;
	int comparison;
	int blank;
	int resource_fd = -1;
	int result = -1;

	if (!resource->present || resource->length == 0) {
		TALLOC_FREE(frame);
		return 0;
	}
	blank = tc_is_empty_resourcefork(source_fd, resource->offset, resource->length);
	if (blank != 0) {
		TALLOC_FREE(frame);
		return blank < 0 ? -1 : 0;
	}
	resource_path = talloc_asprintf(
		frame, "%s/..namedfork/rsrc", path);
	if (resource_path == NULL) {
		errno = ENOMEM;
		goto out;
	}
	resource_fd = open(
		resource_path,
		migration->phase == TC_PHASE_COPY ? O_RDWR | O_CREAT : O_RDONLY,
		0600);
	if (resource_fd == -1 || fstat(resource_fd, &native_st) != 0) {
		goto out;
	}
	marker_state = tc_resource_marker_state(base_fd, resource->length);
	if (marker_state < 0) {
		goto out;
	}
	if (migration->phase == TC_PHASE_CLEANUP &&
	    (marker_state != 0 || native_st.st_size == 0))
	{
		errno = EIO;
		goto out;
	}
	if (native_st.st_size > 0 && marker_state == 0) {
		comparison = tc_verify_resource(source_fd, resource_fd,
			resource->offset, resource->length);
		if (comparison < 0) {
			goto out;
		}
		migration->counts.conflicts += comparison != 0;
		migration->counts.native_kept++;
		result = 0;
		goto out;
	}
	if (marker_state == 0 &&
	    tc_set_resource_marker(base_fd, resource->length) != 0)
	{
		goto out;
	}
	buffer = malloc(TC_COPY_SIZE);
	if (buffer == NULL || ftruncate(resource_fd, 0) != 0) {
		errno = buffer == NULL ? ENOMEM : errno;
		goto out;
	}
	while (copied < resource->length) {
			size_t chunk = MIN(
				(off_t)TC_COPY_SIZE, resource->length - copied);
			ssize_t read_ret = pread(
				source_fd, buffer, chunk, resource->offset + copied);
			ssize_t write_ret;

			if (read_ret != chunk) {
				if (read_ret >= 0) {
					errno = EIO;
				}
				goto out;
			}
			write_ret = pwrite(resource_fd, buffer, chunk, copied);
			if (write_ret != chunk) {
				if (write_ret >= 0) {
					errno = EIO;
				}
				goto out;
			}
			copied += chunk;
			tc_progress();
	}
	if (ftruncate(resource_fd, resource->length) != 0 ||
	    fsync(resource_fd) != 0 ||
	    tc_verify_resource(source_fd, resource_fd,
		resource->offset, resource->length) != 0)
	{
		fprintf(stderr, "resource verification failed path=%s error=%s\n",
			path, strerror(errno));
		goto out;
	}
	if (tc_remove_resource_marker(base_fd) != 0) {
		goto out;
	}
	migration->counts.resources_written++;
	result = 0;
out:
	if (result != 0 && errno != ENOENT) {
		fprintf(stderr, "resource migration failed path=%s error=%s\n",
			path, strerror(errno));
	}
	if (resource_fd != -1) {
		close(resource_fd);
	}
	free(buffer);
	TALLOC_FREE(frame);
	return result;
}

static int tc_unlink_verified_sidecar(const char *path, const struct stat *source)
{
	TALLOC_CTX *frame = talloc_stackframe();
	struct stat current;
	const char *slash = strrchr(path, '/');
	char *parent = slash == NULL ? talloc_strdup(frame, ".") :
		talloc_strndup(frame, path, slash == path ? 1 : slash - path);
	int dir_fd = -1;
	int result = -1;

	/* Do not unlink a replacement that arrived while the old descriptor was
	 * being verified. Flush the directory before retiring the matching row. */
	if (parent == NULL || lstat(path, &current) != 0) {
		goto out;
	}
	if (current.st_dev != source->st_dev || current.st_ino != source->st_ino ||
	    current.st_size != source->st_size || current.st_mtime != source->st_mtime ||
	    current.st_ctime != source->st_ctime)
	{
		errno = EIO;
		goto out;
	}
	dir_fd = open(parent, O_RDONLY);
	if (dir_fd == -1 || unlink(path) != 0 || fsync(dir_fd) != 0) {
		goto out;
	}
	result = 0;
out:
	if (dir_fd != -1) { close(dir_fd); }
	TALLOC_FREE(frame);
	return result;
}

static int tc_migrate_appledouble(struct tc_migration *migration,
				  int base_fd,
				  const char *path)
{
	TALLOC_CTX *frame = talloc_stackframe();
	char *appledouble = tc_appledouble_path(frame, path);
	struct tc_appledouble ad = {0};
	struct stat source_st;
	int source_fd = -1;
	int parse_state;
	int result = -1;

	if (appledouble == NULL) {
		errno = ENOMEM;
		goto out;
	}
	source_fd = open(appledouble, O_RDONLY | O_NOFOLLOW);
	if (source_fd == -1) {
		if (errno == ENOENT) {
			result = 0;
		}
		goto out;
	}
	if (fstat(source_fd, &source_st) != 0 ||
	    !S_ISREG(source_st.st_mode))
	{
		goto out;
	}
	parse_state = tc_parse_appledouble(source_fd, &source_st, &ad);
	if (parse_state == 1) {
		result = 0;
		goto out;
	}
	if (parse_state != 0) {
		goto out;
	}
	migration->counts.sidecars_seen++;
	if (ad.unsupported_entries) {
		fprintf(stderr, "unsupported AppleDouble entries path=%s\n", path);
		errno = ENOTSUP;
		goto out;
	}
	if (tc_migrate_appledouble_finderinfo(
			migration, base_fd, path, &ad) != 0 ||
	    tc_migrate_appledouble_xattrs(
			migration, base_fd, path, &ad) != 0 ||
	    tc_migrate_resource(
			migration, base_fd, source_fd, path, &ad.resource) != 0)
	{
		goto out;
	}
	if (migration->phase == TC_PHASE_CLEANUP) {
		if (fsync(base_fd) != 0 ||
		    tc_unlink_verified_sidecar(appledouble, &source_st) != 0) {
			goto out;
		}
		migration->counts.sidecars_deleted++;
	}
	result = 0;
out:
	if (result != 0 && errno != ENOENT) {
		fprintf(stderr, "AppleDouble migration failed path=%s error=%s\n",
			path, strerror(errno));
	}
	if (source_fd != -1) {
		close(source_fd);
	}
	free(ad.header);
	TALLOC_FREE(frame);
	return result;
}

/* A TDB row is one file's complete metadata, including stream extents.
 * Retire it only after every value and sidecar has passed verification and
 * native writes are durable. Per-file commits preserve progress if a later
 * file or an absent disk prevents the rest of the migration from completing. */
static int tc_retire_tdb_record(struct tc_migration *migration,
			       int fd, const struct stat *st)
{
	struct file_id id = tc_file_id(st);
	uint8_t key[16];
	size_t i;
	NTSTATUS status;

	if (migration->phase != TC_PHASE_CLEANUP || migration->db == NULL) {
		return 0;
	}
	push_file_id_16(key, &id);
	for (i = 0; i < migration->num_tdb_keys; i++) {
		struct tc_tdb_key *entry = &migration->tdb_keys[i];

		if (memcmp(entry->data, key, sizeof(key)) != 0 || entry->retired) {
			continue;
		}
		if (fsync(fd) != 0 || dbwrap_transaction_start(migration->db) != 0) {
			return -1;
		}
		status = dbwrap_delete(migration->db, (TDB_DATA){.dptr = key, .dsize = sizeof(key)});
		if (!NT_STATUS_IS_OK(status)) {
			dbwrap_transaction_cancel(migration->db);
			errno = EIO;
			return -1;
		}
		if (dbwrap_transaction_commit(migration->db) != 0) {
			return -1;
		}
		entry->retired = true;
		migration->counts.tdb_retired++;
		break;
	}
	return 0;
}

static int tc_scan_path(struct tc_migration *migration,
			const char *path,
			bool scan_sidecar)
{
	struct stat st;
	struct stat opened_st;
	char **children = NULL;
	size_t child_count = 0, child_capacity = 0, i;
	int pass;
	DIR *dir = NULL;
	struct dirent *entry;
	int fd = -1;
	int result = 0;

	if (lstat(path, &st) != 0) {
		fprintf(stderr, "lstat failed path=%s error=%s\n", path, strerror(errno));
		return -1;
	}
	if ((uint64_t)st.st_dev != migration->root_dev) {
		/* A volume mounted inside this root belongs to another scan. Its
		 * rows carry its own devid, so skipping it is not an error. */
		migration->counts.boundary_skipped++;
		return 0;
	}
	migration->counts.entries++;
	if (migration->multi && migration->counts.entries % 10000 == 0)
		fprintf(stderr, "scan progress entries=%"PRIu64" path=%s\n", migration->counts.entries, path);
	if (S_ISREG(st.st_mode) || S_ISDIR(st.st_mode)) {
		fd = open(path, O_RDONLY | O_NOFOLLOW);
		if (fd == -1 || fstat(fd, &opened_st) != 0 ||
		    st.st_dev != opened_st.st_dev || st.st_ino != opened_st.st_ino ||
		    (migration->multi ? tc_multi_file(migration, fd, path, &st, scan_sidecar) :
		     tc_migrate_tdb_record(migration, fd, path, &st)) != 0)
		{
			fprintf(stderr, "metadata migration failed path=%s error=%s\n",
				path, strerror(errno));
			result = -1;
		}
		if (!migration->multi && result == 0 && scan_sidecar &&
		    tc_migrate_appledouble(migration, fd, path) != 0)
		{
			result = -1;
		}
		if (!migration->multi && result == 0 && tc_retire_tdb_record(migration, fd, &st) != 0) {
			fprintf(stderr, "metadata retirement failed path=%s error=%s\n",
				path, strerror(errno));
			result = -1;
		}
		if (fd != -1) {
			close(fd);
		}
	}
	if (!S_ISDIR(st.st_mode)) {
		return result;
	}
	dir = opendir(path);
	if (dir == NULL) {
		fprintf(stderr, "opendir failed path=%s error=%s\n", path, strerror(errno));
		return -1;
	}
	/* Snapshot one directory before cleanup removes sidecars from it. A
	 * readdir error is not EOF. Process ._ objects first so their inodes are
	 * visited before their base files can remove verified sidecars. */
	for (;;) {
		char **grown;
		errno = 0;
		entry = readdir(dir);
		if (entry == NULL) {
			if (errno != 0) {
				fprintf(stderr, "readdir failed path=%s error=%s\n", path, strerror(errno));
				result = -1;
			}
			break;
		}
		tc_progress();
		if (ISDOT(entry->d_name) || ISDOTDOT(entry->d_name) ||
		    strcmp(entry->d_name, ".samba4") == 0)
		{
			continue;
		}
		if (child_count == child_capacity) {
			/* Geometric growth avoids repeatedly copying the pointer array
			 * for large Time Machine bands directories. */
			size_t capacity = child_capacity == 0 ? 64 : child_capacity * 2;
			if (capacity < child_capacity || capacity > SIZE_MAX / sizeof(char *)) {
				errno = ENOMEM;
				result = -1;
				break;
			}
			grown = talloc_realloc(migration->mem_ctx, children, char *, capacity);
			if (grown == NULL) {
				result = -1;
				break;
			}
			children = grown;
			child_capacity = capacity;
		}
		children[child_count] = talloc_strdup(children, entry->d_name);
		if (children[child_count] == NULL) {
			result = -1;
			break;
		}
		child_count++;
	}
	if (closedir(dir) != 0) {
		result = -1;
	}
	for (pass = 0; pass < 2; pass++) {
		for (i = 0; i < child_count; i++) {
			TALLOC_CTX *frame = talloc_stackframe();
			bool sidecar_name = strncmp(children[i], "._", 2) == 0;
			char *child;
			if (sidecar_name != (pass == 0)) {
				TALLOC_FREE(frame);
				continue;
			}
			child = talloc_asprintf(frame, "%s/%s", path, children[i]);
			if (child == NULL || tc_scan_path(migration, child, !sidecar_name) != 0) {
				result = -1;
			}
			tc_progress();
			TALLOC_FREE(frame);
		}
	}
	TALLOC_FREE(children);
	return result;
}

static int tc_scan_root(struct tc_migration *migration, const char *path)
{
	struct stat st;
	uint64_t *devs;

	if (!tc_airport_path_is_hfs(path)) {
		fprintf(stderr, "not an HFS volume: %s\n", path);
		errno = EINVAL;
		return -1;
	}
	if (lstat(path, &st) != 0) {
		fprintf(stderr, "lstat failed path=%s error=%s\n", path, strerror(errno));
		return -1;
	}
	migration->root_dev = st.st_dev;
	if (tc_scan_path(migration, path, false) != 0) {
		return -1;
	}
	devs = talloc_realloc(migration->mem_ctx, migration->complete_devs,
			      uint64_t, migration->num_complete_devs + 1);
	if (devs == NULL) {
		errno = ENOMEM;
		return -1;
	}
	migration->complete_devs = devs;
	devs[migration->num_complete_devs++] = st.st_dev;
	return 0;
}

static bool tc_dev_fully_scanned(const struct tc_migration *migration,
				 uint64_t devid)
{
	size_t i;

	for (i = 0; i < migration->num_complete_devs; i++) {
		if (migration->complete_devs[i] == devid) {
			return true;
		}
	}
	return false;
}

/* Legacy keys carry (st_dev, st_ino), never a volume UUID. Only a row whose
 * device was walked completely, with no file claiming it, is proven orphaned.
 * Everything else may belong to a detached disk and stays for a later run. */
static void tc_classify_unmatched_keys(struct tc_migration *migration)
{
	size_t i;

	migration->counts.orphaned = 0;
	migration->counts.unresolved = 0;
	for (i = 0; i < migration->num_tdb_keys; i++) {
		const uint8_t *key = migration->tdb_keys[i].data;
		uint64_t devid;

		if (migration->tdb_keys[i].matched) {
			continue;
		}
		devid = (uint64_t)IVAL(key, 0) | ((uint64_t)IVAL(key, 4) << 32);
		if (tc_dev_fully_scanned(migration, devid)) {
			migration->counts.orphaned++;
		} else {
			migration->counts.unresolved++;
		}
	}
}

/* Move the closed database aside under a name nobody has used. rename() over
 * an existing file would destroy quarantined data, so a taken slot is skipped
 * and a full set of slots is an error that leaves the database in place. The
 * manager serializes migration runs, so the lstat/rename pair has no writer to
 * race against. Durability (review 2, R10): the directory is opened before
 * the rename so an unopenable directory aborts before anything moves; after
 * the rename the directory is fsync'd and a failure is reported with the
 * destination named -- the data is safe under that name, but the run is not
 * reported as durably complete. A filesystem that refuses directory fsync
 * (EINVAL/ENOTSUP) gets a whole-filesystem sync(2) instead. */
static int tc_fsync_directory_or_sync(int dir_fd)
{
	if (fsync(dir_fd) == 0) {
		return 0;
	}
	if (errno == EINVAL || errno == ENOTSUP || errno == EOPNOTSUPP) {
		sync();
		return 0;
	}
	return -1;
}

static int tc_quarantine_tdb(struct tc_migration *migration)
{
	TALLOC_CTX *frame = talloc_stackframe();
	char *destination = NULL;
	char *directory;
	int slot;
	int dir_fd;

	directory = talloc_strdup(frame, migration->tdb_path);
	if (directory == NULL) {
		errno = ENOMEM;
		TALLOC_FREE(frame);
		return -1;
	}
	if (strrchr(directory, '/') != NULL) {
		*strrchr(directory, '/') = '\0';
	} else {
		directory = talloc_strdup(frame, ".");
	}
	dir_fd = open(directory[0] == '\0' ? "/" : directory, O_RDONLY);
	if (dir_fd == -1) {
		fprintf(stderr, "quarantine directory open failed path=%s error=%s\n",
			directory, strerror(errno));
		TALLOC_FREE(frame);
		return -1;
	}
	for (slot = 1; slot <= TC_QUARANTINE_MAX_SLOTS; slot++) {
		struct stat st;

		TALLOC_FREE(destination);
		destination = talloc_asprintf(frame, "%s%s%d", migration->tdb_path,
					      TC_QUARANTINE_SUFFIX, slot);
		if (destination == NULL) {
			errno = ENOMEM;
			close(dir_fd);
			TALLOC_FREE(frame);
			return -1;
		}
		if (lstat(destination, &st) == 0) {
			continue;
		}
		if (errno != ENOENT) {
			fprintf(stderr, "quarantine probe failed path=%s error=%s\n",
				destination, strerror(errno));
			close(dir_fd);
			TALLOC_FREE(frame);
			return -1;
		}
		break;
	}
	if (slot > TC_QUARANTINE_MAX_SLOTS) {
		fprintf(stderr, "quarantine collision: every slot up to %s%s%d exists\n",
			migration->tdb_path, TC_QUARANTINE_SUFFIX, TC_QUARANTINE_MAX_SLOTS);
		errno = EEXIST;
		close(dir_fd);
		TALLOC_FREE(frame);
		return -1;
	}
	if (rename(migration->tdb_path, destination) != 0) {
		fprintf(stderr, "quarantine rename failed path=%s destination=%s error=%s\n",
			migration->tdb_path, destination, strerror(errno));
		close(dir_fd);
		TALLOC_FREE(frame);
		return -1;
	}
	if (tc_fsync_directory_or_sync(dir_fd) != 0) {
		fprintf(stderr, "quarantine sync failed directory=%s error=%s; database is at %s but not known to be durable\n",
			directory, strerror(errno), destination);
		fprintf(migration->multi ? stderr : stdout, "tdb_quarantine_path=%s\n", destination);
		close(dir_fd);
		TALLOC_FREE(frame);
		return -1;
	}
	close(dir_fd);
	fprintf(migration->multi ? stderr : stdout, "tdb_quarantine_path=%s\n", destination);
	migration->counts.tdb_quarantined = 1;
	TALLOC_FREE(frame);
	return 0;
}

/* FNV-1a over the whole file. The manager records this after each run it
 * owns and refuses its checkpoint when the database no longer matches, so a
 * restored or foreign xattr.tdb is rescanned instead of trusted. */
static int tc_print_fingerprint(const char *path)
{
	uint8_t buffer[TC_COPY_SIZE];
	uint64_t hash = TC_FNV64_OFFSET;
	uint64_t size = 0;
	int fd = open(path, O_RDONLY);
	ssize_t got;

	if (fd == -1) {
		fprintf(stderr, "unable to open %s: %s\n", path, strerror(errno));
		return -1;
	}
	while ((got = read(fd, buffer, sizeof(buffer))) > 0) {
		ssize_t i;

		for (i = 0; i < got; i++) {
			hash ^= buffer[i];
			hash *= TC_FNV64_PRIME;
		}
		size += got;
	}
	if (got < 0) {
		fprintf(stderr, "unable to read %s: %s\n", path, strerror(errno));
		close(fd);
		return -1;
	}
	close(fd);
	printf("fingerprint=%"PRIu64"-%016"PRIx64"\n", size, hash);
	return 0;
}

static int tc_scan_roots_file(struct tc_migration *migration,
			      const char *roots_path)
{
	char path[PATH_MAX + 2];
	FILE *roots = fopen(roots_path, "r");
	int result = 0;

	if (roots == NULL) {
		return -1;
	}
	while (fgets(path, sizeof(path), roots) != NULL) {
		size_t length = strlen(path);

		if (length == 0) {
			continue;
		}
		if (path[length - 1] != '\n' && !feof(roots)) {
			fprintf(stderr, "HFS root path exceeds PATH_MAX\n");
			errno = ENAMETOOLONG;
			result = -1;
			break;
		}
		if (path[length - 1] == '\n') {
			path[--length] = '\0';
		}
		if (length != 0 && tc_scan_root(migration, path) != 0) {
			result = -1;
		}
	}
	if (ferror(roots)) {
		result = -1;
	}
	fclose(roots);
	return result;
}

#include "tc_xattr_multi.inc"

static void tc_usage(const char *program)
{
	fprintf(stderr,
		"usage: %s [--stall-seconds SECONDS --log PATH] "
		"multi copy|cleanup|retire\n"
		"       %s [--stall-seconds SECONDS] inspect|inspect-root PATH\n"
		"       %s copy|cleanup TDB_PATH|- stream|netatalk "
		"HFS_ROOT [HFS_ROOT ...]\n"
		"       %s copy|cleanup TDB_PATH|- stream|netatalk "
		"--roots-file PATH\n"
		"       %s fingerprint TDB_PATH\n",
		program,
		program,
		program,
		program,
		program);
}

int main(int argc, char **argv)
{
	TALLOC_CTX *frame = NULL;
	struct tc_migration migration = {0};
	unsigned stall_seconds;
	const char *log_path;
	bool guarded;
	int result = 0;
	int i;

	if (tc_parse_guard_options(&argc, &argv, &stall_seconds, &log_path) != 0) {
		tc_usage(argv[0]);
		return 2;
	}
	guarded = stall_seconds != 0;
	if (guarded &&
	    !(argc == 3 &&
	      ((!strcmp(argv[1], "multi") &&
	        (!strcmp(argv[2], "copy") || !strcmp(argv[2], "cleanup") ||
	         !strcmp(argv[2], "retire"))) ||
	       !strcmp(argv[1], "inspect") || !strcmp(argv[1], "inspect-root"))))
	{
		tc_usage(argv[0]);
		return 2;
	}
	if (guarded && tc_guard_start(stall_seconds) != 0) {
		return 3;
	}
	if (log_path != NULL && tc_guard_log(log_path) != 0) {
		result = 3;
		goto done;
	}
	frame = talloc_stackframe();
	if (frame == NULL) {
		result = 3;
		goto done;
	}
	migration.mem_ctx = frame;
	if (argc == 3 && !strcmp(argv[1], "multi")) {
		result = tc_multi_main(argv[2]);
		goto done;
	}
	if (argc == 3 && (!strcmp(argv[1], "inspect") || !strcmp(argv[1], "inspect-root"))) {
		result = tc_multi_inspect(argv[2], !strcmp(argv[1], "inspect-root"));
		goto done;
	}
	if (argc == 3 && strcmp(argv[1], "fingerprint") == 0) {
		result = tc_print_fingerprint(argv[2]);
		result = result == 0 ? 0 : 3;
		goto done;
	}
	if (argc < 5 ||
	    (strcmp(argv[1], "copy") != 0 && strcmp(argv[1], "cleanup") != 0) ||
	    (strcmp(argv[3], "stream") != 0 && strcmp(argv[3], "netatalk") != 0))
	{
		tc_usage(argv[0]);
		result = 2;
		goto done;
	}
	migration.phase = strcmp(argv[1], "copy") == 0 ?
		TC_PHASE_COPY : TC_PHASE_CLEANUP;
	migration.tdb_path = strcmp(argv[2], "-") == 0 ? NULL : argv[2];
	migration.legacy_metadata = argv[3];
	if (migration.tdb_path != NULL) {
		migration.db = dbwrap_local_open(
			frame, migration.tdb_path, 0, TDB_DEFAULT,
			migration.phase == TC_PHASE_COPY ? O_RDONLY : O_RDWR, 0,
			DBWRAP_LOCK_ORDER_2, DBWRAP_FLAG_NONE);
		if (migration.db == NULL) {
			fprintf(stderr, "unable to open TDB %s: %s\n",
				migration.tdb_path, strerror(errno));
			result = 3;
			goto done;
		}
		if (tc_collect_tdb_keys(&migration) != 0) {
			result = 3;
			goto done;
		}
	}
	if (strcmp(argv[4], "--roots-file") == 0) {
		if (argc != 6 || tc_scan_roots_file(&migration, argv[5]) != 0) {
			migration.counts.errors++;
			result = -1;
		}
	} else {
		for (i = 4; i < argc; i++) {
			if (tc_scan_root(&migration, argv[i]) != 0) {
				migration.counts.errors++;
				result = -1;
			}
		}
	}
	tc_classify_unmatched_keys(&migration);
	if (result == 0 &&
	    migration.phase == TC_PHASE_CLEANUP &&
	    migration.db != NULL &&
	    migration.counts.tdb_retired == migration.counts.tdb_total)
	{
		TALLOC_FREE(migration.db);
		if (unlink(migration.tdb_path) != 0) {
			fprintf(stderr, "unable to remove migrated TDB %s: %s\n",
				migration.tdb_path, strerror(errno));
			migration.counts.errors++;
			result = -1;
		} else {
			migration.counts.tdb_deleted = 1;
		}
	} else if (result == 0 &&
		   migration.phase == TC_PHASE_CLEANUP &&
		   migration.db != NULL &&
		   migration.counts.unresolved == 0 &&
		   migration.counts.orphaned > 0 &&
		   migration.counts.tdb_retired + migration.counts.orphaned ==
		   migration.counts.tdb_total)
	{
		/* Every remaining row is a proven orphan: nothing can ever claim
		 * it, but it is still someone's metadata, so it is set aside
		 * rather than deleted. Unresolved rows keep the database live. */
		TALLOC_FREE(migration.db);
		if (tc_quarantine_tdb(&migration) != 0) {
			migration.counts.errors++;
			result = -1;
		}
	}
	printf("phase=%s entries=%"PRIu64" tdb_records=%"PRIu64
	       " xattrs_written=%"PRIu64" streams_written=%"PRIu64
	       " finderinfo_written=%"PRIu64" resources_written=%"PRIu64
	       " native_kept=%"PRIu64" conflicts=%"PRIu64
	       " sidecars_seen=%"PRIu64" sidecars_deleted=%"PRIu64
	       " tdb_total=%"PRIu64" tdb_matched=%"PRIu64
	       " tdb_orphaned=%"PRIu64" tdb_unresolved=%"PRIu64
	       " tdb_retired=%"PRIu64" tdb_deleted=%"PRIu64
	       " tdb_quarantined=%"PRIu64" boundary_skipped=%"PRIu64
	       " errors=%"PRIu64"\n",
	       migration.phase == TC_PHASE_COPY ? "copy" : "cleanup",
	       migration.counts.entries,
	       migration.counts.tdb_records,
	       migration.counts.xattrs_written,
	       migration.counts.streams_written,
	       migration.counts.finderinfo_written,
	       migration.counts.resources_written,
	       migration.counts.native_kept,
	       migration.counts.conflicts,
	       migration.counts.sidecars_seen,
	       migration.counts.sidecars_deleted,
	       migration.counts.tdb_total,
	       migration.counts.tdb_matched,
	       migration.counts.orphaned,
	       migration.counts.unresolved,
	       migration.counts.tdb_retired,
	       migration.counts.tdb_deleted,
	       migration.counts.tdb_quarantined,
	       migration.counts.boundary_skipped,
	       migration.counts.errors);
	result = result == 0 ? 0 : 4;
done:
	TALLOC_FREE(frame);
	return tc_guard_finish(result);
}
