/* Run the real recreate callback, NDR parsing and record locks. Process
 * liveness is controlled. The wait for a live open is the create's deferral
 * (smb2_create.c), so recreate itself must answer at once. */
/* Give the included translation unit private export names: the linked Samba
 * server library also contains its ordinary, uninstrumented copy. */
#define smbXsrv_open_global_parse_record regression_smbXsrv_open_global_parse_record
#define smbXsrv_open_global_lookup regression_smbXsrv_open_global_lookup
#define smbXsrv_open_global_traverse_per_rec_persistent_read regression_smbXsrv_open_global_traverse_per_rec_persistent_read
#define smbXsrv_open_global_wipe regression_smbXsrv_open_global_wipe
#define smbXsrv_open_global_init regression_open_global_init
#define smbXsrv_open_create regression_open_create
#define smbXsrv_open_update regression_open_update
#define smbXsrv_open_close regression_open_close
#define smb1srv_open_table_init regression_smb1_table_init
#define smb1srv_open_lookup regression_smb1_lookup
#define smb2srv_open_table_init regression_smb2_table_init
#define smb2srv_open_lookup regression_smb2_lookup
#define smbXsrv_open_purge_replay_cache regression_purge_replay_cache
#define smb2srv_open_lookup_replay_cache regression_lookup_replay_cache
#define smb2srv_open_recreate regression_open_recreate
#define smbXsrv_open_global_traverse regression_open_global_traverse
#define smbXsrv_open_cleanup regression_open_cleanup
#define smbXsrv_replay_cleanup regression_replay_cleanup
#define rename_open_files_below regression_rename_open_files_below
#define msg_file_was_renamed regression_msg_file_was_renamed
#include "includes.h"
#include "smbd/smbd.h"
#include "smbd/globals.h"
#include "dbwrap/dbwrap.h"
#include "dbwrap/dbwrap_rbt.h"
#include "lib/util/server_id.h"
#include "lib/util/idtree.h"
#include "lib/util/util_tdb.h"
#include "lib/util/time_basic.h"
#include "librpc/gen_ndr/ndr_smbXsrv.h"
#include "libcli/security/security.h"
#include "messages.h"
#include "librpc/gen_ndr/ndr_open_files.h"
#include "serverid.h"
#ifdef HAVE_PTHREAD
#error These regressions must exercise the no-pthread appliance build.
#endif

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #x); exit(90); } } while (0)
static unsigned lock_depth, attempts;
static bool fail_db;
static struct db_context *database;
static TDB_DATA record_key;
/* 4.25 serializes global records with the version-1 schema. */
static struct smbXsrv_open_global record;

static void write_record(void)
{
	DATA_BLOB blob = data_blob_null;
	struct smbXsrv_open_globalB value = {.version = SMBXSRV_VERSION_1};
	value.info.info1 = &record;
	CHECK(NDR_ERR_CODE_IS_SUCCESS(ndr_push_struct_blob(&blob, NULL, &value,
		(ndr_push_flags_fn_t)ndr_push_smbXsrv_open_globalB)));
	CHECK(NT_STATUS_IS_OK(dbwrap_store(database, record_key,
		make_tdb_data(blob.data, blob.length), TDB_REPLACE)));
	TALLOC_FREE(blob.data);
}

static NTSTATUS observed_do_locked(struct db_context *db, TDB_DATA key,
	void (*fn)(struct db_record *, TDB_DATA, void *), void *data)
{
	NTSTATUS status;
	attempts++;
	if (fail_db) return NT_STATUS_INTERNAL_DB_CORRUPTION;
	CHECK(lock_depth == 0);
	lock_depth++;
	status = dbwrap_do_locked(db, key, fn, data);
	lock_depth--;
	return status;
}

static bool live_process(const struct server_id *id)
{
	return id->pid == 123;
}

static struct server_id current_process(const struct messaging_context *ctx)
{
	struct server_id result = {.pid = 456};
	(void)ctx;
	return result;
}

#define dbwrap_do_locked observed_do_locked
#define serverid_exists live_process
#define messaging_server_id current_process
#define smbXsrv_version_global_current() SMBXSRV_VERSION_1
#include "../smbd/smbXsrv_open.c"
#undef dbwrap_do_locked
#undef messaging_server_id

/*
 * The smbd parent's wait for a new scavenger's hello (patch 0075), cut from
 * the patched scavenger.c (tests/samba/run.py stage): it must not block the
 * parent for good, so a child that never says hello is killed after the
 * timeout. Each case sets the limit: a slow host or device only makes the
 * silent case longer, and the others get a limit they never come near, so
 * no case depends on how fast the child is scheduled.
 */
static int tc_hello_seconds;
#define TC_SCAVENGER_HELLO_SECONDS tc_hello_seconds
#include "../../lib/util/select.h"
#include "tc_scavenger_hello.inc"

static int scavenger_hello(const char *name)
{
	struct server_id sent = {.pid = 4321, .task_id = 7}, got = {0};
	struct timeval start;
	int fds[2], status;
	double took;
	bool ok;
	pid_t pid;

	tc_hello_seconds = strcmp(name, "scavenger_hello_silent") ? 120 : 3;
	CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, fds) == 0);
	pid = fork();
	CHECK(pid >= 0);
	if (pid == 0) {
		close(fds[0]);
		if (!strcmp(name, "scavenger_hello_sent")) {
			_exit(write(fds[1], &sent, sizeof(sent)) == sizeof(sent) ? 0 : 1);
		}
		if (!strcmp(name, "scavenger_hello_silent")) {
			for (;;) pause();	/* holds its end open and says nothing */
		}
		_exit(0);			/* scavenger_hello_exited: no hello */
	}
	close(fds[1]);
	GetTimeOfDay(&start);
	ok = scavenger_wait_hello(fds[0], pid, &got);
	took = timeval_elapsed(&start);
	CHECK(waitpid(pid, &status, 0) == pid);
	close(fds[0]);
	if (!strcmp(name, "scavenger_hello_sent")) {
		CHECK(ok && server_id_equal(&got, &sent));
		CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0);
	} else if (!strcmp(name, "scavenger_hello_silent")) {
		/* Given up after the timeout, and the child killed. */
		CHECK(!ok && took >= tc_hello_seconds - 0.1);
		CHECK(WIFSIGNALED(status) && WTERMSIG(status) == SIGKILL);
	} else {
		/* The child's end closed: no wait for the timeout. */
		CHECK(!strcmp(name, "scavenger_hello_exited"));
		CHECK(!ok && took < tc_hello_seconds / 2);
		CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0);
	}
	printf("PASS %s\n", name);
	return 0;
}

/*
 * The scavenger's walk after an unclean exit (patch 0077), cut from the
 * patched scavenger.c (tests/samba/run.py stage), over the real
 * smbXsrv_open.c traverse callback. It must release exactly the opens whose
 * smbd is gone (pid 789; 123 is live), and leave live, disconnected and
 * persistent opens and replay records alone, and count only the opens
 * scavenger_cleanup() removed. scavenger_cleanup() is recorded
 * here; the cleanup_* cases run the real smbXsrv_open_cleanup() it calls.
 */
struct scavenger_message;
static bool rescan_cleanup(const struct scavenger_message *msg);
static NTSTATUS rescan_traverse(
	int (*fn)(struct db_record *rec, struct smbXsrv_open_global *global,
		  TDB_DATA *rc_open_global_key, void *private_data),
	void *private_data)
{
	struct smbXsrv_open_global_traverse_state state = {.fn = fn, .private_data = private_data};
	int count = 0;
	return dbwrap_traverse_read(database, smbXsrv_open_global_traverse_fn, &state, &count);
}
#define scavenger_cleanup rescan_cleanup
#undef smbXsrv_open_global_traverse
#define smbXsrv_open_global_traverse rescan_traverse
#include "tc_scavenger_rescan.inc"
#undef smbXsrv_open_global_traverse
#undef scavenger_cleanup

static uint64_t rescan_cleaned[8];
static unsigned rescan_cleaned_count;
/* An open whose cleanup finds nothing to remove: its smbd exited cleanly
 * after the walk, or a timer removed it meanwhile. */
static uint64_t rescan_already_gone;

static bool rescan_cleanup(const struct scavenger_message *msg)
{
	CHECK(rescan_cleaned_count < 8);
	rescan_cleaned[rescan_cleaned_count++] = msg->open_persistent_id;
	return msg->open_persistent_id != rescan_already_gone;
}

static int compare_ids(const void *a, const void *b)
{
	uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
	return (x > y) - (x < y);
}

/* Store an open record with id and owner, and its replay record. */
static void store_open(uint32_t id, uint64_t pid, bool disconnected, bool persistent)
{
	struct smbXsrv_open_global_key_buf key_buf;
	struct smbXsrv_open_replay_cache_key_buf rc_buf;
	struct GUID create_guid = {.time_low = id};
	TDB_DATA rc_key, value;

	record = (struct smbXsrv_open_global) {
		.open_global_id = id, .open_persistent_id = id, .durable = true,
		.persistent = persistent, .server_id = {.pid = pid},
		.client_guid = {.time_low = 11}, .create_guid = create_guid,
		.open_owner = global_sid_System, .durable_timeout_msec = 300000,
	};
	if (disconnected) {
		server_id_set_disconnected(&record.server_id);
		record.disconnect_time = timeval_to_nttime(&(struct timeval){.tv_sec = time(NULL)});
	}
	record_key = smbXsrv_open_global_id_to_key(id, &key_buf);
	write_record();
	rc_key = smbXsrv_open_replay_cache_key(&record.client_guid, &create_guid, &rc_buf);
	value = smbXsrv_open_global_id_to_key(id, &key_buf);
	CHECK(NT_STATUS_IS_OK(dbwrap_store(database, rc_key, value, TDB_REPLACE)));
}

static bool open_stored(uint32_t id)
{
	struct smbXsrv_open_global_key_buf key_buf;
	return dbwrap_exists(database, smbXsrv_open_global_id_to_key(id, &key_buf));
}

static bool replay_stored(uint32_t id)
{
	struct smbXsrv_open_replay_cache_key_buf rc_buf;
	struct GUID client_guid = {.time_low = 11}, create_guid = {.time_low = id};
	return dbwrap_exists(database, smbXsrv_open_replay_cache_key(&client_guid, &create_guid, &rc_buf));
}

static int rescan_case(const char *name)
{
	TALLOC_CTX *frame = talloc_stackframe();
	NTSTATUS status;

	setup_logging("tc_durable_reconnect_test", DEBUG_STDERR);
	alarm(10);
	database = db_open_rbt(frame);
	CHECK(database != NULL);
	smbXsrv_open_global_db_ctx = database;
	if (!strcmp(name, "rescan_dead_owners")) {
		store_open(7, 123, false, false);	/* live owner */
		store_open(8, 789, false, false);	/* its smbd died */
		store_open(12, 789, false, false);	/* the same smbd's next open */
		store_open(13, 123, false, false);	/* the live owner's next open */
		store_open(9, 0, true, false);		/* disconnected, left to its timer */
		store_open(10, 789, false, true);	/* persistent: cleanup_ph()'s */
		store_open(11, 790, false, false);	/* another dead smbd */
		rescan_already_gone = 12;
		CHECK(scavenger_rescan() == 2);
		CHECK(rescan_cleaned_count == 3);
		qsort(rescan_cleaned, rescan_cleaned_count, sizeof(rescan_cleaned[0]), compare_ids);
		CHECK(rescan_cleaned[0] == 8 && rescan_cleaned[1] == 11 && rescan_cleaned[2] == 12);
	} else if (!strcmp(name, "rescan_nothing_dead")) {
		store_open(7, 123, false, false);
		store_open(9, 0, true, false);
		CHECK(scavenger_rescan() == 0);
		CHECK(rescan_cleaned_count == 0);
	} else if (!strcmp(name, "cleanup_dead_owner")) {
		/* What the rescan's scavenger_cleanup() does to such an open. */
		store_open(8, 789, false, false);
		status = smbXsrv_open_cleanup(8);
		CHECK(NT_STATUS_IS_OK(status));
		CHECK(!open_stored(8) && !replay_stored(8));
	} else {
		/* A live open answers CANNOT_DELETE and keeps everything. */
		CHECK(!strcmp(name, "cleanup_live_owner"));
		store_open(7, 123, false, false);
		status = smbXsrv_open_cleanup(7);
		CHECK(NT_STATUS_EQUAL(status, NT_STATUS_CANNOT_DELETE));
		CHECK(open_stored(7) && replay_stored(7));
	}
	CHECK(lock_depth == 0);
	smbXsrv_open_global_db_ctx = NULL;
	TALLOC_FREE(frame);
	printf("PASS %s\n", name);
	return 0;
}

/* Execute both production rename helpers with real fsp names and file IDs.
 * Only share-mode locking/notifications are observed; name hashes and stat
 * preservation use Samba's own fsp_set_smb_fname(). */
static bool rename_lock_held, fail_rename_copy, fail_rename_stat;
static unsigned renamed_records;
static const char *renamed_prefix = "dest space/";
static int rename_unlock(void *p)
{
	(void)p; CHECK(rename_lock_held); rename_lock_held = false; return 0;
}
static struct share_mode_lock *rename_lock(TALLOC_CTX *ctx, struct file_id id)
{
	void *p;
	CHECK(!rename_lock_held);
	if (id.inode == 9) return NULL; /* internal pathref */
	p = talloc_new(ctx); CHECK(p);
	rename_lock_held = true;
	talloc_set_destructor(p, rename_unlock);
	return (struct share_mode_lock *)p;
}
static bool rename_record(struct messaging_context *msg, struct share_mode_lock *lck,
	struct file_id id, const char *service, uint32_t old_hash, uint32_t new_hash,
	const struct smb_filename *name)
{
	(void)msg; (void)lck; (void)service;
	CHECK(rename_lock_held && old_hash != new_hash && new_hash != 0);
	CHECK(id.inode >= 1 && id.inode <= 3);
	CHECK(strncmp(name->base_name, renamed_prefix, strlen(renamed_prefix)) == 0);
	renamed_records++;
	return true;
}
static struct smb_filename *rename_copy(TALLOC_CTX *ctx, const struct smb_filename *name)
{
	return fail_rename_copy ? NULL : cp_smb_filename(ctx, name);
}
/* Destination lookup is an independent filesystem observation: a freshly
 * opened replacement may have the same old pathname, but a different inode. */
static int rename_stat(connection_struct *conn, const files_struct *dir,
	const struct smb_filename *name, SMB_STRUCT_STAT *st, int flags)
{
	const char *suffix = name->base_name + strlen(renamed_prefix);
	CHECK(dir == conn->cwd_fsp && name->stream_name == NULL && flags == 0);
	if (fail_rename_stat) { errno = EIO; return -1; }
	ZERO_STRUCTP(st);
	if (!strcmp(suffix, "sub/band")) st->st_ex_ino = 1;
	else if (!strcmp(suffix, "sub")) st->st_ex_ino = 3;
	else { CHECK(!strcmp(suffix, "ref")); st->st_ex_ino = 9; }
	return 0;
}
static struct file_id rename_stat_id(connection_struct *conn, const SMB_STRUCT_STAT *st)
{
	(void)conn;
	return (struct file_id){.inode = st->st_ex_ino};
}
#undef SMB_VFS_FSTATAT
#define SMB_VFS_FSTATAT rename_stat
#define vfs_file_id_from_sbuf rename_stat_id
#define get_existing_share_mode_lock rename_lock
#define rename_share_filename rename_record
#define cp_smb_filename rename_copy
#include "tc_rename_open_files.inc"
#include "tc_rename_message.inc"
#undef get_existing_share_mode_lock
#undef rename_share_filename
#undef cp_smb_filename

static files_struct *rename_fsp(connection_struct *conn, const char *name,
	const char *stream, unsigned inode)
{
	files_struct *fsp = talloc_zero(conn, files_struct);
	struct smb_filename *fname = synthetic_smb_fname(fsp, name, stream, NULL, 0, 0);
	CHECK(fsp && fname);
	fsp->conn = conn;
	fsp->file_id.inode = inode;
	CHECK(fsp_set_smb_fname(fsp, fname));
	fsp->fsp_name->st.st_ex_size = 123;
	fsp->next = conn->sconn->files;
	conn->sconn->files = fsp;
	return fsp;
}

static int rename_descendants(const char *which)
{
	TALLOC_CTX *frame = talloc_stackframe();
	struct smbd_server_connection *sconn = talloc_zero(frame, struct smbd_server_connection);
	connection_struct *conn = talloc_zero(frame, connection_struct);
	connection_struct *other = talloc_zero(frame, connection_struct);
	struct smb_filename src = {.base_name = "./SOURCE"}, dst = {.base_name = "./dest space"};
	files_struct *band, *duplicate, *stream, *folder, *neighbor, *foreign, *ref, *alias;
	files_struct *replacement = NULL;
	uint32_t original_hash;
	const char *prefix = "source", *destination = "dest space";
	char *band_name, *folder_name, *ref_name, *new_band, *new_folder, *new_ref;
	bool no_change = !strcmp(which, "rename_case_sensitive") ||
		!strcmp(which, "rename_message_other_share") || !strcmp(which, "rename_stat_failure");
	char work[] = "tc-rename-XXXXXX", data[5] = {0};
	int here = open(".", O_RDONLY), fd;
	struct stat st;
	NTSTATUS status;
	CHECK(frame && sconn && conn && other && here != -1);
	conn->sconn = other->sconn = sconn;
	sconn->connections = conn;
	conn->next = other;
	if (!strcmp(which, "rename_unicode")) prefix = "café";
	if (!strcmp(which, "rename_emoji")) prefix = "folder😀";
	if (strcmp(prefix, "source") != 0) src.base_name = talloc_asprintf(frame, "./%s", prefix);
	if (!strcmp(which, "rename_case_only")) {
		src.base_name = "./source";
		dst.base_name = "./SOURCE";
		destination = "SOURCE";
		renamed_prefix = "SOURCE/";
	}
	new_band = talloc_asprintf(frame, "%s/sub/band", destination);
	new_folder = talloc_asprintf(frame, "%s/sub", destination);
	new_ref = talloc_asprintf(frame, "%s/ref", destination);
	CHECK(new_band && new_folder && new_ref);
	band_name = talloc_asprintf(frame, "%s/sub/band", prefix);
	folder_name = talloc_asprintf(frame, "%s/sub", prefix);
	ref_name = talloc_asprintf(frame, "./%s/ref", prefix);
	CHECK(band_name && folder_name && ref_name && src.base_name);
	conn->case_sensitive = !strncmp(which, "rename_case_sensitive", 21);
	if (!strcmp(which, "rename_case_sensitive_match")) src.base_name = "./source";
	conn->connectpath = talloc_strdup(conn, "/share");
	other->connectpath = talloc_strdup(other, "/elsewhere");
	band = rename_fsp(conn, band_name, NULL, 1);
	duplicate = rename_fsp(conn, band_name, NULL, 1);
	alias = rename_fsp(conn, "outside-link", NULL, 1);
	stream = rename_fsp(conn, band_name, ":AFP_AfpInfo", 2);
	folder = rename_fsp(conn, folder_name, NULL, 3);
	neighbor = rename_fsp(conn, "source-other/band", NULL, 4);
	foreign = rename_fsp(other, band_name, NULL, 5);
	ref = rename_fsp(conn, ref_name, NULL, 9);
	stream->base_fsp = band;
	if (!strcmp(which, "rename_message_reused_path"))
		replacement = rename_fsp(conn, band_name, NULL, 44);
	fail_rename_stat = !strcmp(which, "rename_stat_failure");
	original_hash = band->name_hash;
	fail_rename_copy = !strcmp(which, "rename_allocation_failure");
	if (!strncmp(which, "rename_message", 14)) {
		struct file_rename_message msg = {
			.servicepath = no_change ? "/missing" : conn->connectpath,
			.base_name = dst.base_name,
			.old_base_name = src.base_name,
		};
		DATA_BLOB blob = data_blob_null;
		CHECK(NDR_ERR_CODE_IS_SUCCESS(ndr_push_struct_blob(&blob, frame, &msg,
			(ndr_push_flags_fn_t)ndr_push_file_rename_message)));
		msg_file_was_renamed(NULL, sconn, MSG_SMB_FILE_RENAME,
			(struct server_id){.pid = 999}, &blob);
		status = NT_STATUS_OK;
	} else {
		status = rename_open_files_below(conn, &src, &dst);
	}
	if (fail_rename_copy || no_change) {
		CHECK(NT_STATUS_EQUAL(status, fail_rename_copy ? NT_STATUS_NO_MEMORY : NT_STATUS_OK));
		CHECK(!strcmp(band->fsp_name->base_name, band_name));
		CHECK(renamed_records == 0 && !rename_lock_held);
	} else {
		CHECK(NT_STATUS_IS_OK(status));
		CHECK(!strcmp(band->fsp_name->base_name, new_band));
		CHECK(!strcmp(duplicate->fsp_name->base_name, band->fsp_name->base_name));
		CHECK(!strcmp(stream->fsp_name->base_name, band->fsp_name->base_name));
		CHECK(!strcmp(stream->fsp_name->stream_name, ":AFP_AfpInfo"));
		CHECK(!strcmp(folder->fsp_name->base_name, new_folder));
		CHECK(!strcmp(ref->fsp_name->base_name, new_ref));
		CHECK(band->name_hash != original_hash && band->name_hash == duplicate->name_hash);
		CHECK(band->fsp_name->st.st_ex_size == 123 && duplicate->fsp_name->st.st_ex_size == 123);
		CHECK(renamed_records == 3 && !rename_lock_held);
		/* The name still locates the same file after closing its descriptor,
		 * exactly what the appliance's post-close durable stat needs. */
		CHECK(mkdtemp(work) != NULL && chdir(work) == 0);
		CHECK(mkdir(prefix, 0700) == 0 && mkdir(folder_name, 0700) == 0);
		fd = open(band_name, O_CREAT|O_RDWR, 0600); CHECK(fd != -1);
		CHECK(write(fd, "band", 4) == 4 && fstat(fd, &st) == 0);
		CHECK(rename(prefix, destination) == 0 && close(fd) == 0);
		fd = open(band->fsp_name->base_name, O_RDONLY); CHECK(fd != -1);
		CHECK(read(fd, data, 4) == 4 && !strcmp(data, "band"));
		CHECK(close(fd) == 0 && unlink(band->fsp_name->base_name) == 0);
		CHECK(rmdir(new_folder) == 0 && rmdir(destination) == 0);
		CHECK(fchdir(here) == 0 && rmdir(work) == 0);
	}
	CHECK(!strcmp(neighbor->fsp_name->base_name, "source-other/band"));
	CHECK(!strcmp(foreign->fsp_name->base_name, band_name));
	CHECK(!strcmp(alias->fsp_name->base_name, "outside-link"));
	if (replacement != NULL) CHECK(!strcmp(replacement->fsp_name->base_name, band_name));
	close(here);
	TALLOC_FREE(frame);
	printf("PASS %s\n", which);
	return 0;
}

int main(int argc, char **argv)
{
	TALLOC_CTX *frame = talloc_stackframe();
	struct smbXsrv_open_table *table = talloc_zero(frame, struct smbXsrv_open_table);
	struct smbXsrv_client_global client_global = {0};
	struct smbXsrv_client client = {.global = &client_global, .open_table = table};
	struct smbXsrv_connection conn = {.client = &client};
	struct smbXsrv_session_global session_global = {0};
	struct smbXsrv_session session = {.global = &session_global};
	struct smbXsrv_tcon_global tcon_global = {0};
	struct smbXsrv_tcon tcon = {.global = &tcon_global};
	struct auth_session_info auth = {0};
	struct security_token token = {0};
	struct dom_sid owner = global_sid_System;
	struct GUID create_guid = {.time_low = 22};
	struct smb2_lease_key lease_key = {0};
	struct smbXsrv_open *opened = NULL;
	struct smbXsrv_open_global_key_buf key_buf;
	NTSTATUS status, expected = NT_STATUS_OK;
	unsigned id, expected_attempts = 1;
	const struct GUID *guid = NULL;
	CHECK(argc == 2 && frame && table);
	if (!strncmp(argv[1], "rename_", 7)) return rename_descendants(argv[1]);
	if (!strncmp(argv[1], "scavenger_hello_", 16)) {
		return scavenger_hello(argv[1]);
	}
	if (!strncmp(argv[1], "rescan_", 7) || !strncmp(argv[1], "cleanup_", 8)) {
		return rescan_case(argv[1]);
	}
	/* A v1 reconnect names no create GUID. */
	if (strncmp(argv[1], "v1_", 3)) guid = &create_guid;
	setup_logging(argv[0], DEBUG_STDERR);
	alarm(10);
	table->local.idr = idr_init(table);
	table->local.lowest_id = 1;
	table->local.highest_id = 100;
	table->local.max_opens = 100;
	database = db_open_rbt(table);
	CHECK(database && table->local.idr);
	table->global.db_ctx = database;
	record_key = smbXsrv_open_global_id_to_key(7, &key_buf);
	client_global.client_guid.time_low = 11;
	token.num_sids = 1;
	token.sids = &owner;
	auth.security_token = &token;
	session_global.auth_session_info = &auth;
	session_global.session_global_id = 31;
	tcon_global.tcon_global_id = 41;
	record = (struct smbXsrv_open_global) {
		.open_global_id = 7, .open_persistent_id = 7, .durable = true,
		.server_id = {.pid = 123}, .client_guid = client_global.client_guid,
		.create_guid = create_guid, .open_owner = owner,
	};
	/* A live open is only "still busy": FILE_NOT_AVAILABLE, never taken over.
	 * The create defers and asks again; the client never sees this status. */
	if (!strcmp(argv[1], "live")) expected = NT_STATUS_FILE_NOT_AVAILABLE;
	else if (!strcmp(argv[1], "already_disconnected") || !strcmp(argv[1], "v1_reconnect"))
		server_id_set_disconnected(&record.server_id);
	else if (!strcmp(argv[1], "client_mismatch")) { record.client_guid.time_low++; expected = NT_STATUS_OBJECT_NAME_NOT_FOUND; }
	else if (!strcmp(argv[1], "create_mismatch")) { record.create_guid.time_low++; expected = NT_STATUS_OBJECT_NAME_NOT_FOUND; }
	else if (!strcmp(argv[1], "owner_mismatch")) { record.open_owner = global_sid_Anonymous; expected = NT_STATUS_ACCESS_DENIED; }
	else if (!strcmp(argv[1], "not_durable")) { record.durable = false; expected = NT_STATUS_OBJECT_NAME_NOT_FOUND; }
	else if (!strcmp(argv[1], "database_failure")) { fail_db = true; expected = NT_STATUS_INTERNAL_DB_CORRUPTION; }
	write_record();
	if (!strcmp(argv[1], "transition") || !strcmp(argv[1], "v1_transition")) {
		/* Busy while the old smbd holds the open; the next try, after the
		 * old smbd marked it disconnected, takes it over. */
		status = smb2srv_open_recreate(&conn, &session, &tcon, 7, guid, &lease_key, 0, &opened);
		CHECK(NT_STATUS_EQUAL(status, NT_STATUS_FILE_NOT_AVAILABLE) && opened == NULL);
		CHECK(table->local.num_opens == 0);
		server_id_set_disconnected(&record.server_id);
		write_record();
		expected_attempts = 2;
	}
	status = smb2srv_open_recreate(&conn, &session, &tcon, 7, guid, &lease_key, 0, &opened);
	CHECK(NT_STATUS_EQUAL(status, expected));
	CHECK(lock_depth == 0);
	CHECK(attempts == expected_attempts);
	if (!strcmp(argv[1], "live")) {
		/* The old smbd still owns the stored record. */
		TDB_DATA stored;
		struct smbXsrv_open_global *decoded = NULL;
		CHECK(NT_STATUS_IS_OK(dbwrap_fetch(database, frame, record_key, &stored)));
		CHECK(NT_STATUS_IS_OK(smbXsrv_open_global_parse_record(frame, record_key, stored, &decoded)));
		CHECK(decoded->server_id.pid == 123);
	}
	if (NT_STATUS_IS_OK(status)) {
		TDB_DATA stored;
		struct smbXsrv_open_global *decoded = NULL;
		CHECK(opened && table->local.num_opens == 1);
		CHECK(opened->session == &session && opened->tcon == &tcon);
		CHECK(opened->global->server_id.pid == 456);
		CHECK(NT_STATUS_IS_OK(dbwrap_fetch(database, frame, record_key, &stored)));
		CHECK(NT_STATUS_IS_OK(smbXsrv_open_global_parse_record(frame, record_key, stored, &decoded)));
		CHECK(decoded->server_id.pid == 456 && decoded->open_volatile_id == opened->local_id);
		/* No live smbd session exists in this fixture to run the unrelated
		 * full server-close destructor during test teardown. */
		talloc_set_destructor(opened, NULL);
	} else {
		CHECK(opened == NULL && table->local.num_opens == 0);
		/* An unsuccessful attempt must free its local slot. */
		for (id = table->local.lowest_id; id <= table->local.highest_id; id++) {
			CHECK(idr_find(table->local.idr, id) == NULL);
		}
	}
	TALLOC_FREE(frame);
	printf("PASS %s\n", argv[1]);
	return 0;
}
