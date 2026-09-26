/* One-shot migration of xattr_tdb metadata into AirPort HFS storage.
 * This is a standalone deployment helper, not part of the resident smbd. */
#include "includes.h"
#include "system/filesys.h"
#include "lib/dbwrap/dbwrap.h"
#include "lib/dbwrap/dbwrap_rbt.h"
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
		    strcmp(name, TC_RESOURCEFORK_XATTR) != 0)
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

/* Deploy-only multi-TDB migration. Each invocation walks one volume;
 * Python owns durable per-volume JSON receipts and never needs a roots file.
 * Source databases stay read-only until whole-file retirement in rank order. */
#define TC_MULTI_VERSION 1
#define TC_MULTI_MAX_SOURCES 32
#define TC_MULTI_MAX_KEYS 262144
#define TC_MULTI_LINE_MAX (PATH_MAX * 4 + 1024)

struct tc_source_stat {
	uint64_t dev, inode, size, hash;
	int64_t mtime;
	long nsec;
};
struct tc_multi_source {
	unsigned index;
	char uuid[37];
	char *relative, *path;
	char **aliases;
	size_t alias_count;
	const char *mode;
	struct tc_source_stat stat;
	struct tc_migration scan;
	uint8_t *coverage; /* 0 unresolved, 1 verified value, 2 proven orphan */
	unsigned retired;
};
struct tc_multi {
	TALLOC_CTX *ctx;
	struct tc_multi_source sources[TC_MULTI_MAX_SOURCES];
	size_t count, total_keys;
	char root_uuid[37];
	char *root;
	uint64_t root_dev, root_inode;
	enum tc_phase phase;
	bool retire;
};

static int tc_source_stat_read(const char *path, struct tc_source_stat *out)
{
	struct stat before, after;
	uint8_t buffer[TC_COPY_SIZE];
	ssize_t got;
	uint64_t bytes = 0, hash = TC_FNV64_OFFSET;
	int fd = open(path, O_RDONLY);
	if (fd < 0) return -1;
	if (fstat(fd, &before) || !S_ISREG(before.st_mode)) { close(fd); return -1; }
	while ((got = read(fd, buffer, sizeof(buffer))) != 0) {
		ssize_t i;
		if (got < 0 && errno == EINTR) continue;
		if (got < 0) { close(fd); return -1; }
		for (i = 0; i < got; i++) { hash ^= buffer[i]; hash *= TC_FNV64_PRIME; }
		bytes += got;
		tc_progress();
	}
	if (fstat(fd, &after) || before.st_dev != after.st_dev || before.st_ino != after.st_ino ||
		before.st_size != after.st_size || before.st_mtime != after.st_mtime || bytes != (uint64_t)after.st_size) {
		close(fd); errno = ESTALE; return -1;
	}
	close(fd);
	out->dev = before.st_dev; out->inode = before.st_ino; out->size = bytes;
	out->mtime = before.st_mtime; out->hash = hash;
#if defined(__NetBSD__) || defined(__APPLE__)
	out->nsec = before.st_mtimespec.tv_nsec;
	if (out->nsec != after.st_mtimespec.tv_nsec) { errno = ESTALE; return -1; }
#else
	out->nsec = before.st_mtim.tv_nsec;
	if (out->nsec != after.st_mtim.tv_nsec) { errno = ESTALE; return -1; }
#endif
	return 0;
}
static bool tc_source_stat_same(const struct tc_source_stat *a, const struct tc_source_stat *b)
{
	return a->dev == b->dev && a->inode == b->inode && a->size == b->size &&
		a->hash == b->hash && a->mtime == b->mtime && a->nsec == b->nsec;
}
static void tc_hex_print(FILE *file, const uint8_t *bytes, size_t size)
{
	size_t i;
	for (i = 0; i < size; i++) fprintf(file, "%02x", (unsigned)bytes[i]);
}
static int tc_hex_value(unsigned char ch)
{
	if (ch >= '0' && ch <= '9') return ch - '0';
	if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
	return -1;
}
static int tc_hex_decode(const char *text, uint8_t *out, size_t size)
{
	size_t i;
	if (strlen(text) != 2 * size) return -1;
	for (i = 0; i < size; i++) {
		int hi = tc_hex_value(text[2*i]), lo = tc_hex_value(text[2*i+1]);
		if (hi < 0 || lo < 0) return -1;
		out[i] = (hi << 4) | lo;
	}
	return 0;
}
static char *tc_hex_path(TALLOC_CTX *ctx, const char *text)
{
	size_t size = strlen(text) / 2;
	char *path;
	if (!size || size >= PATH_MAX || strlen(text) != size * 2) return NULL;
	path = talloc_array(ctx, char, size + 1);
	if (path == NULL) return NULL;
	if (tc_hex_decode(text, (uint8_t *)path, size) || memchr(path, 0, size)) { TALLOC_FREE(path); return NULL; }
	path[size] = 0;
	return path;
}
static bool tc_uuid_valid(const char *uuid)
{
	size_t i;
	if (strlen(uuid) != 36) return false;
	for (i = 0; i < 36; i++) {
		if (i == 8 || i == 13 || i == 18 || i == 23) { if (uuid[i] != '-') return false; }
		else if (tc_hex_value(uuid[i]) < 0) return false;
	}
	return true;
}
static int tc_key_compare(const void *a, const void *b) { return memcmp(a, b, 16); }
static struct tc_tdb_key *tc_source_key(struct tc_multi_source *source, const uint8_t key[16])
{
	return bsearch(key, source->scan.tdb_keys, source->scan.num_tdb_keys,
				   sizeof(source->scan.tdb_keys[0]), tc_key_compare);
}
static int tc_rank_compare(const void *a, const void *b)
{
	const struct tc_multi_source *x = *(const struct tc_multi_source *const *)a;
	const struct tc_multi_source *y = *(const struct tc_multi_source *const *)b;
	int result;
	if (x->stat.mtime != y->stat.mtime) return x->stat.mtime < y->stat.mtime ? -1 : 1;
	if (x->stat.nsec != y->stat.nsec) return x->stat.nsec < y->stat.nsec ? -1 : 1;
	result = strcmp(x->uuid, y->uuid);
	return result ? result : strcmp(x->relative, y->relative);
}
static int tc_multi_validate_sources(struct tc_multi *multi)
{
	size_t i;
	for (i = 0; i < multi->count; i++) {
		struct tc_source_stat current;
		if (tc_source_stat_read(multi->sources[i].path, &current) ||
			!tc_source_stat_same(&current, &multi->sources[i].stat)) {
			fprintf(stderr, "source changed db=%zu/%zu path=%s\n", i+1, multi->count, multi->sources[i].path);
			errno = ESTALE; return -1;
		}
		tc_progress();
	}
	return 0;
}

/* A source contributes a whole logical value. In particular, stream extents
 * and the competing FinderInfo representations never come from different DBs.
 * This temporary red-black tree holds one file's merged record, not a second
 * filesystem index or a persistent/intermediate metadata database. */
static char *tc_logical_name(TALLOC_CTX *ctx, const char *name)
{
	char *native;
	if (!strcmp(name, TC_AFPINFO_XATTR) || !strcmp(name, TC_NETATALK_META_XATTR) ||
		!strcmp(name, TC_FINDERINFO_XATTR)) return talloc_strdup(ctx, TC_FINDERINFO_XATTR);
	if (tc_stream_extent(name)) return NULL;
	native = tc_stream_anchor(name) ? tc_apple_native_name(ctx, name) : NULL;
	return native ? native : talloc_strdup(ctx, name);
}
static int tc_merge_attribute(struct db_context *target, struct tc_multi_source *source,
							  TALLOC_CTX *ctx, const struct file_id *id, const char *name)
{
	DATA_BLOB value = data_blob_null;
	if (xattr_tdb_getattr(source->scan.db, ctx, id, name, &value) < 0 ||
		xattr_tdb_setattr(target, id, name, value.data, value.length, 0) != 0) return -1;
	if (tc_stream_anchor(name)) {
		uint8_t extent, count;
		if (!value.length) { errno = EINVAL; return -1; }
		count = value.data[value.length-1];
		if (count >= TC_HFS_STREAM_XATTRS) { errno = EOVERFLOW; return -1; }
		for (extent = 1; extent <= count; extent++) {
			char *part = talloc_asprintf(ctx, "%s%u.%s", TC_EXT_PREFIX, extent, name + strlen(TC_STREAM_PREFIX));
			DATA_BLOB data = data_blob_null;
			if (part == NULL || xattr_tdb_getattr(source->scan.db, ctx, id, part, &data) < 0 ||
				xattr_tdb_setattr(target, id, part, data.data, data.length, 0) != 0) return -1;
		}
	}
	return 0;
}
static int tc_multi_file(struct tc_migration *migration, int fd, const char *path,
						 const struct stat *st, bool sidecar)
{
	struct tc_multi *multi = migration->multi;
	TALLOC_CTX *frame = talloc_stackframe();
	struct tc_migration file = *migration;
	struct tc_multi_source *ordered[TC_MULTI_MAX_SOURCES];
	struct tc_tdb_key one = {0};
	struct file_id id = tc_file_id(st);
	char **names = NULL;
	unsigned *owners = NULL;
	size_t name_count = 0, i;
	int result = -1;
	bool have_record = false;
	push_file_id_16(one.data, &id);
	file.multi = NULL;
	file.legacy_metadata = "stream"; /* Records need not contain FinderInfo. */
	file.db = db_open_rbt(frame);
	file.mem_ctx = frame; file.tdb_keys = &one; file.num_tdb_keys = 1;
	if (file.db == NULL) goto done;
	for (i = 0; i < multi->count; i++) ordered[i] = &multi->sources[i];
	qsort(ordered, multi->count, sizeof(ordered[0]), tc_rank_compare);
	/* Descending precedence: only the winning source can claim a name. */
	for (i = multi->count; i > 0; i--) {
		struct tc_multi_source *source = ordered[i-1];
		struct tc_tdb_key *key = tc_source_key(source, one.data);
		char *list;
		ssize_t size, got;
		size_t offset = 0;
		if (key == NULL) continue;
		have_record = true;
		size = xattr_tdb_listattr(source->scan.db, &id, NULL, 0);
		if (size < 0 || size > 4 * 1024 * 1024) goto done;
		list = talloc_array(frame, char, size ? size : 1);
		if (list == NULL) goto done;
		got = xattr_tdb_listattr(source->scan.db, &id, list, size);
		if (got != size) goto done;
		while (offset < (size_t)size) {
			const char *name = list + offset;
			size_t length = strnlen(name, size - offset), j;
			char *logical;
			if (length == (size_t)size - offset) { errno = EINVAL; goto done; }
			offset += length + 1;
			tc_progress();
			if (tc_stream_extent(name)) continue;
			logical = tc_logical_name(frame, name);
			if (logical == NULL) goto done;
			for (j = 0; j < name_count; j++) if (!strcmp(names[j], logical)) break;
			if (j < name_count && owners[j] != source->index) continue;
			if (j == name_count) {
				names = talloc_realloc(frame, names, char *, name_count + 1);
				owners = talloc_realloc(frame, owners, unsigned, name_count + 1);
				if (names == NULL || owners == NULL) goto done;
				names[name_count] = logical; owners[name_count++] = source->index;
				if (!strcmp(logical, TC_FINDERINFO_XATTR)) file.legacy_metadata = source->mode;
			}
			if (tc_merge_attribute(file.db, source, frame, &id, name)) goto done;
		}
	}
	if (!have_record) { TALLOC_FREE(file.db); file.num_tdb_keys = 0; }
	if (tc_migrate_tdb_record(&file, fd, path, st) ||
		(sidecar && tc_migrate_appledouble(&file, fd, path))) goto done;
	/* Do not call legacy per-record retirement: source contents and mtimes
	 * must stay identical until whole DBs can be retired oldest-first. */
	if (have_record && multi->phase == TC_PHASE_CLEANUP && fsync(fd)) goto done;
	for (i = 0; i < multi->count; i++) {
		struct tc_multi_source *source = &multi->sources[i];
		struct tc_tdb_key *key = tc_source_key(source, one.data);
		if (key == NULL) continue;
		if (!key->matched) source->scan.counts.tdb_matched++;
		key->matched = true;
		if (multi->phase == TC_PHASE_CLEANUP) {
			key->retired = true;
			source->coverage[key - source->scan.tdb_keys] = 1;
		}
	}
	migration->counts = file.counts;
	tc_progress();
	result = 0;
done:
	TALLOC_FREE(frame);
	return result;
}

static int tc_multi_open(struct tc_multi *multi, struct tc_multi_source *source)
{
	fprintf(stderr, "source db=%u/%zu uuid=%s path=%s mtime=%"PRId64".%09ld decoder=%s\n", source->index + 1, multi->count, source->uuid, source->path, source->stat.mtime, source->stat.nsec, source->mode);
	source->scan.mem_ctx = multi->ctx;
	source->scan.multi = multi;
	source->scan.tdb_path = source->path;
	source->scan.legacy_metadata = source->mode;
	source->scan.db = dbwrap_local_open(multi->ctx, source->path, 0,
		TDB_DEFAULT, O_RDONLY, 0, DBWRAP_LOCK_ORDER_2, DBWRAP_FLAG_NONE);
	if (source->scan.db == NULL || tc_collect_tdb_keys(&source->scan)) return -1;
	multi->total_keys += source->scan.num_tdb_keys;
	if (multi->total_keys > TC_MULTI_MAX_KEYS) { errno = E2BIG; return -1; }
	qsort(source->scan.tdb_keys, source->scan.num_tdb_keys,
		  sizeof(source->scan.tdb_keys[0]), tc_key_compare);
	source->coverage = talloc_zero_array(multi->ctx, uint8_t, source->scan.num_tdb_keys + 1);
	return source->coverage == NULL ? -1 : 0;
}
static int tc_number(const char *text, uint64_t *out, int base)
{
	char *end;
	unsigned long long value;
	if (!text[0] || text[0] == '-' || text[0] == '+') return -1;
	errno = 0; value = strtoull(text, &end, base);
	if (errno || *end) return -1;
	*out = value; return 0;
}
/* Stdin has bounded ASCII fields; filesystem names are hex, never shell code
 * or log-delimited records. Python parses only the separate JSON stdout. */
static int tc_multi_read(struct tc_multi *multi, FILE *input)
{
	char line[TC_MULTI_LINE_MAX], *words[16], *save, *word;
	size_t line_number = 0, coverage_count = 0;
	bool opened = false;
	while (fgets(line, sizeof(line), input)) {
		size_t length = strlen(line), count = 0, i;
		uint64_t number;
		if (!length || line[length-1] != '\n') goto invalid;
		line[length-1] = 0;
		if (!line_number++) { if (strcmp(line, "TCMIGRATE1")) goto invalid; continue; }
		for (word = strtok_r(line, " ", &save); word; word = strtok_r(NULL, " ", &save)) {
			if (count == ARRAY_SIZE(words)) goto invalid;
			words[count++] = word;
		}
		if (!count) goto invalid;
		if (!strcmp(words[0], "S")) {
			struct tc_multi_source *s;
			char *end;
			if (opened || count != 12 || multi->count == TC_MULTI_MAX_SOURCES ||
				tc_number(words[1], &number, 10) || number != multi->count || !tc_uuid_valid(words[2])) goto invalid;
			s = &multi->sources[multi->count]; s->index = number;
			memcpy(s->uuid, words[2], sizeof(s->uuid));
			s->relative = tc_hex_path(multi->ctx, words[3]); s->path = tc_hex_path(multi->ctx, words[4]);
			if (!s->relative || s->relative[0] == '/' || !s->path || s->path[0] != '/' ||
				(strcmp(words[5], "stream") && strcmp(words[5], "netatalk"))) goto invalid;
			s->mode = talloc_strdup(multi->ctx, words[5]);
			if (tc_number(words[6], &s->stat.dev, 10) || tc_number(words[7], &s->stat.inode, 10) ||
				tc_number(words[8], &s->stat.size, 10)) goto invalid;
			errno = 0; s->stat.mtime = strtoll(words[9], &end, 10);
			if (errno || !words[9][0] || *end || tc_number(words[10], &number, 10) || number >= 1000000000) goto invalid;
			s->stat.nsec = number;
			if (tc_number(words[11], &s->stat.hash, 16)) goto invalid;
			for (i = 0; i < multi->count; i++) {
				struct tc_multi_source *other = &multi->sources[i], *current = s;
				/* Deduplicate aliases in Python first, retaining every name for
				 * retirement. Ambiguous priorities must not depend on dk order. */
				if ((other->stat.dev == s->stat.dev && other->stat.inode == s->stat.inode) ||
					!tc_rank_compare(&other, &current)) goto invalid;
			}
			multi->count++;
		} else if (!strcmp(words[0], "A")) {
			struct tc_multi_source *s;
			char *path;
			if (opened || count != 3 || tc_number(words[1], &number, 10) || number >= multi->count) goto invalid;
			s = &multi->sources[number]; path = tc_hex_path(multi->ctx, words[2]);
			if (!path || path[0] != '/' || s->alias_count >= TC_MULTI_MAX_SOURCES) goto invalid;
			s->aliases = talloc_realloc(multi->ctx, s->aliases, char *, s->alias_count + 1);
			if (!s->aliases) return -1;
			s->aliases[s->alias_count++] = path;
		} else if (!strcmp(words[0], "R")) {
			if (multi->retire || count != 5 || multi->root || !tc_uuid_valid(words[1])) goto invalid;
			memcpy(multi->root_uuid, words[1], sizeof(multi->root_uuid));
			multi->root = tc_hex_path(multi->ctx, words[2]);
			if (!multi->root || multi->root[0] != '/' || tc_number(words[3], &multi->root_dev, 10) ||
				tc_number(words[4], &multi->root_inode, 10)) goto invalid;
		} else if (!strcmp(words[0], "K") || !strcmp(words[0], "E")) {
			if (!opened) {
				if (!multi->count || tc_multi_validate_sources(multi)) goto invalid;
				for (i = 0; i < multi->count; i++) if (tc_multi_open(multi, &multi->sources[i])) return -1;
				opened = true;
			}
			if (!strcmp(words[0], "E")) {
				if (count != 1 || (!multi->retire && !multi->root) || fgetc(input) != EOF || ferror(input)) goto invalid;
				tc_progress();
				return 0;
			} else {
				struct tc_multi_source *s;
				struct tc_tdb_key *key;
				uint8_t bytes[16];
				if (!multi->retire || count != 4 || tc_number(words[1], &number, 10) || number >= multi->count ||
					(strcmp(words[2], "M") && strcmp(words[2], "O")) || tc_hex_decode(words[3], bytes, 16) ||
					++coverage_count > TC_MULTI_MAX_KEYS) goto invalid;
				s = &multi->sources[number]; key = tc_source_key(s, bytes);
				if (!key || s->coverage[key-s->scan.tdb_keys]) goto invalid;
				s->coverage[key-s->scan.tdb_keys] = !strcmp(words[2], "M") ? 1 : 2;
			}
		} else goto invalid;
		tc_progress();
	}
invalid:
	fprintf(stderr, "invalid multi migration input line=%zu\n", line_number);
	errno = EINVAL; return -1;
}
static int tc_multi_root_valid(struct tc_multi *multi, struct stat *out)
{
	if (lstat(multi->root, out) || !S_ISDIR(out->st_mode) ||
		(uint64_t)out->st_dev != multi->root_dev || (uint64_t)out->st_ino != multi->root_inode ||
		!tc_airport_path_is_hfs(multi->root)) { errno = ESTALE; return -1; }
	return 0;
}
static int tc_multi_scan(struct tc_multi *multi, struct tc_counts *counts)
{
	struct tc_migration scan = {.mem_ctx = multi->ctx, .multi = multi, .phase = multi->phase};
	struct stat st;
	size_t i, k;
	int fd, result;
	if (tc_multi_root_valid(multi, &st)) return -1;
	fd = open(multi->root, O_RDONLY | O_NOFOLLOW);
	if (fd < 0) return -1;
	fprintf(stderr, "volume phase=%s uuid=%s root=%s sources=%zu start\n",
		multi->phase == TC_PHASE_COPY ? "copy" : "cleanup", multi->root_uuid, multi->root, multi->count);
	result = tc_scan_root(&scan, multi->root);
	/* Apple's HFS may disappear while a walk is in flight. Both the original
	 * root descriptor and its current pathname must still identify this mount. */
	if (fstat(fd, &st) || (uint64_t)st.st_dev != multi->root_dev ||
		(uint64_t)st.st_ino != multi->root_inode || tc_multi_root_valid(multi, &st) ||
		tc_fsync_directory_or_sync(fd) || tc_multi_validate_sources(multi)) result = -1;
	close(fd);
	if (result) return -1;
	if (multi->phase == TC_PHASE_CLEANUP) {
		for (i = 0; i < multi->count; i++) {
			struct tc_multi_source *s = &multi->sources[i];
			for (k = 0; k < s->scan.num_tdb_keys; k++) {
				const uint8_t *key = s->scan.tdb_keys[k].data;
				uint64_t dev = (uint64_t)IVAL(key, 0) | ((uint64_t)IVAL(key, 4) << 32);
				if (!s->coverage[k] && dev == multi->root_dev) s->coverage[k] = 2;
				tc_progress();
			}
		}
	}
	*counts = scan.counts;
	fprintf(stderr, "volume uuid=%s complete entries=%"PRIu64" sidecars_deleted=%"PRIu64"\n",
			multi->root_uuid, counts->entries, counts->sidecars_deleted);
	return 0;
}
static int tc_multi_retire_path(struct tc_multi_source *source, const char *path, bool orphan)
{
	struct tc_source_stat current;
	struct stat alias;
	struct tc_migration temporary = {.tdb_path = path, .multi = source->scan.multi};
	char *directory = talloc_strdup(source->scan.mem_ctx, path), *slash;
	int fd, result;
	if (!directory || !(slash = strrchr(directory, '/'))) return -1;
	*slash = 0;
	if (tc_source_stat_read(path, &current) || !tc_source_stat_same(&current, &source->stat)) return -1;
	if (lstat(path, &alias)) return -1;
	if (orphan && !S_ISLNK(alias.st_mode)) return tc_quarantine_tdb(&temporary);
	fd = open(directory[0] ? directory : "/", O_RDONLY);
	if (fd < 0) return -1;
	result = unlink(path);
	if (!result) result = tc_fsync_directory_or_sync(fd);
	close(fd);
	return result;
}
static int tc_multi_retire(struct tc_multi *multi)
{
	struct tc_multi_source *ordered[TC_MULTI_MAX_SOURCES];
	size_t i, k;
	if (tc_multi_validate_sources(multi)) return -1;
	for (i = 0; i < multi->count; i++) ordered[i] = &multi->sources[i];
	qsort(ordered, multi->count, sizeof(ordered[0]), tc_rank_compare);
	/* A surviving receipt describes the whole cohort. Retiring an earlier DB
	 * while a later DB is unresolved would make that receipt incompatible on
	 * the next deploy and permit stale metadata replay. Preflight the entire
	 * cohort before unlinking any source. */
	for (i = 0; i < multi->count; i++) {
		struct tc_multi_source *s = ordered[i];
		for (k = 0; k < s->scan.num_tdb_keys; k++) {
			if (!s->coverage[k]) {
				fprintf(stderr, "retirement deferred db=%u path=%s; whole cohort retained for unresolved records\n", s->index, s->path);
				return 0;
			}
			tc_progress();
		}
	}
	for (i = 0; i < multi->count; i++) {
		struct tc_multi_source *s = ordered[i];
		bool orphan = false;
		for (k = 0; k < s->scan.num_tdb_keys; k++) {
			if (s->coverage[k] == 2) orphan = true;
			tc_progress();
		}
		TALLOC_FREE(s->scan.db);
		/* Aliases go first; the ranked primary remains authoritative if this
		 * is interrupted. No source row or original mtime is ever rewritten. */
		for (k = 0; k < s->alias_count; k++)
			if (tc_multi_retire_path(s, s->aliases[k], orphan)) return -1;
			else tc_progress();
		if (tc_multi_retire_path(s, s->path, orphan)) return -1;
		tc_progress();
		s->retired = orphan ? 2 : 1;
		fprintf(stderr, "retired db=%u path=%s outcome=%s\n", s->index, s->path, orphan ? "quarantined" : "deleted");
	}
	return 0;
}
static void tc_multi_report(struct tc_multi *multi, const struct tc_counts *counts)
{
	size_t i, k;
	printf("{\"version\":%d,\"entries\":%"PRIu64",\"sources\":[", TC_MULTI_VERSION, counts->entries);
	for (i = 0; i < multi->count; i++) {
		struct tc_multi_source *s = &multi->sources[i];
		bool comma = false;
		printf("%s{\"index\":%u,\"total\":%zu,\"retired\":%u,\"coverage\":[", i ? "," : "", s->index, s->scan.num_tdb_keys, s->retired);
		for (k = 0; k < s->scan.num_tdb_keys; k++) {
			if (!s->coverage[k]) continue;
			printf("%s[\"%c\",\"", comma ? "," : "", s->coverage[k] == 1 ? 'M' : 'O');
			tc_hex_print(stdout, s->scan.tdb_keys[k].data, 16); printf("\"]"); comma = true;
			tc_progress();
		}
		printf("]}");
		tc_progress();
	}
	printf("]}\n");
}
static int tc_multi_main(const char *phase)
{
	TALLOC_CTX *frame = talloc_stackframe();
	struct tc_multi multi = {.ctx = frame};
	struct tc_counts counts = {0};
	int result;
	if (strcmp(phase, "copy") && strcmp(phase, "cleanup") && strcmp(phase, "retire")) {
		TALLOC_FREE(frame); return 2;
	}
	multi.phase = !strcmp(phase, "copy") ? TC_PHASE_COPY : TC_PHASE_CLEANUP;
	multi.retire = !strcmp(phase, "retire");
	result = tc_multi_read(&multi, stdin);
	if (!result) result = multi.retire ? tc_multi_retire(&multi) : tc_multi_scan(&multi, &counts);
	if (!result) tc_multi_report(&multi, &counts);
	else fprintf(stderr, "multi migration failed phase=%s error=%s\n", phase, strerror(errno));
	TALLOC_FREE(frame);
	return result ? 4 : 0;
}
static int tc_multi_inspect(const char *path, bool root)
{
	struct tc_source_stat s = {0};
	struct stat st;
	char canonical[PATH_MAX];
	if (!realpath(path, canonical)) return 3;
	if (root) {
		if (stat(canonical, &st) || !S_ISDIR(st.st_mode) || !tc_airport_path_is_hfs(canonical)) return 3;
		s.dev = st.st_dev; s.inode = st.st_ino;
	} else if (tc_source_stat_read(canonical, &s)) return 3;
	printf("{\"path_hex\":\""); tc_hex_print(stdout, (const uint8_t *)canonical, strlen(canonical));
	printf("\",\"dev\":%"PRIu64",\"inode\":%"PRIu64",\"size\":%"PRIu64
		   ",\"mtime\":%"PRId64",\"nsec\":%ld,\"hash\":\"%016"PRIx64"\"}\n",
		   s.dev, s.inode, s.size, s.mtime, s.nsec, s.hash);
	return 0;
}

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
