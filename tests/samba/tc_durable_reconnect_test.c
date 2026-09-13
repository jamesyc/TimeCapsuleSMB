/* Run the real recreate callback, NDR parsing, record locks and retry loop.
 * Process liveness and waiting are controlled so no test sleeps for seconds. */
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
#include "serverid.h"
#ifdef HAVE_PTHREAD
#error These regressions must exercise the no-pthread appliance build.
#endif

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #x); exit(90); } } while (0)
static unsigned lock_depth, attempts, waits, disconnect_at;
static bool fail_db;
static struct db_context *database;
static TDB_DATA record_key;
/* Rc2 serializes global records with the version-1 schema. */
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

static void controlled_sleep(unsigned milliseconds)
{
	CHECK(milliseconds > 0 && lock_depth == 0);
	/* A generous runaway guard protects the test, without specifying policy. */
	CHECK(++waits < 10000);
	if (disconnect_at && waits == disconnect_at) {
		/* The old owner must be able to acquire this very record during the
		 * wait. dbwrap_store uses the real DB's locking and serialization. */
		server_id_set_disconnected(&record.server_id);
		write_record();
	}
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

#define smb_msleep controlled_sleep
#define dbwrap_do_locked observed_do_locked
#define serverid_exists live_process
#define messaging_server_id current_process
#define smbXsrv_version_global_current() SMBXSRV_VERSION_1
#include "../smbd/smbXsrv_open.c"
#undef smb_msleep
#undef dbwrap_do_locked

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
	bool immediate = false;
	unsigned id;
	CHECK(argc == 2 && frame && table);
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
	disconnect_at = 3;
	if (!strcmp(argv[1], "exhausted")) { disconnect_at = 0; expected = NT_STATUS_FILE_NOT_AVAILABLE; }
	else if (!strcmp(argv[1], "already_disconnected")) { server_id_set_disconnected(&record.server_id); immediate = true; }
	else if (!strcmp(argv[1], "client_mismatch")) { record.client_guid.time_low++; expected = NT_STATUS_OBJECT_NAME_NOT_FOUND; immediate = true; }
	else if (!strcmp(argv[1], "create_mismatch")) { record.create_guid.time_low++; expected = NT_STATUS_OBJECT_NAME_NOT_FOUND; immediate = true; }
	else if (!strcmp(argv[1], "owner_mismatch")) { record.open_owner = global_sid_Anonymous; expected = NT_STATUS_ACCESS_DENIED; immediate = true; }
	else if (!strcmp(argv[1], "not_durable")) { record.durable = false; expected = NT_STATUS_OBJECT_NAME_NOT_FOUND; immediate = true; }
	else if (!strcmp(argv[1], "database_failure")) { fail_db = true; expected = NT_STATUS_INTERNAL_DB_CORRUPTION; immediate = true; }
	write_record();
	status = smb2srv_open_recreate(&conn, &session, &tcon, 7,
		!strcmp(argv[1], "v1_reconnect") ? NULL : &create_guid, &lease_key, 0, &opened);
	CHECK(NT_STATUS_EQUAL(status, expected));
	CHECK(lock_depth == 0);
	if (immediate) CHECK(waits == 0 && attempts == 1);
	else CHECK(waits > 0 && attempts == waits + 1);
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
