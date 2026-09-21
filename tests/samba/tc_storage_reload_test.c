/* Exercise the patched connection code, real loadparm, descriptors and tevent
 * AIO drain. Only revocation/error observations and final protocol teardown
 * are controlled; this is not a parallel model of the production predicate. */
#define conn_idle_all regression_conn_idle_all
#define conn_force_tdis regression_conn_force_tdis
#define conn_refresh_bindings regression_conn_refresh_bindings
#define conn_record_bindings regression_conn_record_bindings
#include "includes.h"
#include "system/filesys.h"
#include "smbd/smbd.h"
#include "smbd/globals.h"
#include "smbd/fd_handle.h"
#include "lib/util/tevent_ntstatus.h"
#include "messages.h"
#include "lib/global_contexts.h"

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: %s errno=%d\n", __FILE__, __LINE__, #x, errno); exit(90); } } while (0)
static int revoked_fd = -1, fd_error, path_error;
static unsigned resets, disconnects, reloads, forwarded, guest_reinits;
static bool reload_valid = true;
static connection_struct *trees[2];
static char directory[128], config_path[160];

static int observed_fstat(int fd, struct stat *st)
{
	if (fd == revoked_fd && fd_error) { errno = fd_error; return -1; }
	return fstat(fd, st);
}
static int observed_stat(const char *path, struct stat *st)
{
	if (path_error) { errno = path_error; return -1; }
	return stat(path, st);
}
static void observed_reset(void) { resets++; }
static void observed_root_user(void) {}
static bool observed_smb2(struct smbd_server_connection *sconn) { (void)sconn; return true; }
static bool observed_reload(struct smbd_server_connection *sconn,
	bool (*used)(struct smbd_server_connection *, int), bool test)
{
	(void)sconn; (void)used; CHECK(!test); reloads++; return reload_valid;
}
static NTSTATUS observed_disconnect(struct smbXsrv_tcon *tcon, uint64_t vuid)
{
	unsigned i;
	(void)vuid;
	for (i = 0; i < ARRAY_SIZE(trees); i++) if (trees[i] && trees[i]->tcon == tcon) trees[i]->tcon = NULL;
	disconnects++;
	return NT_STATUS_OK;
}
#define fstat observed_fstat
#define stat(path, output) observed_stat(path, output)
#define reset_chdir_lastconn_cache observed_reset
#define change_to_root_user observed_root_user
#define conn_using_smb2 observed_smb2
#define reload_services observed_reload
#define smbXsrv_tcon_disconnect observed_disconnect
#include "../smbd/conn_idle.c"
#undef fstat
#undef stat

/* The runner extracts these unchanged callback bodies from the patched source
 * being built. This reaches the production HUP/message routing without running
 * smbd's main or replacing its listener sockets in a unit test. */
struct smbd_parent_context { struct messaging_context *msg_ctx; };
static struct server_id observed_server_id(struct messaging_context *msg)
{
	struct server_id id = {0}; (void)msg; id.pid = 123; return id;
}
static bool observed_guest(void *unused) { (void)unused; guest_reinits++; return true; }
static NTSTATUS messaging_send_to_children(struct messaging_context *msg, uint32_t type, DATA_BLOB *data)
{
	CHECK(msg == (struct messaging_context *)1 && type == MSG_SMB_CONF_UPDATED && data == NULL);
	forwarded++;
	return NT_STATUS_OK;
}
#define messaging_server_id observed_server_id
#define reinit_guest_session_info observed_guest
#include "tc_storage_reload_callbacks.inc"

static void load_config_root(const char *first_uuid, bool include_first, bool narrow, bool rename_share)
{
	FILE *f = fopen(config_path, "w");
	char path[160];
	snprintf(path, sizeof(path), "%s%s", directory, narrow ? "/ShareRoot" : "");
	CHECK(f);
	fprintf(f, "[global]\nworkgroup = WORKGROUP\ntc:volume dk3 = second|%s\n", directory);
	if (include_first) fprintf(f, "tc:volume dk2 = %s|%s\n", first_uuid, path);
	fprintf(f, "[%s]\npath = %s\ntc:volume uuid = first\ntc:volume device = dk2\n", rename_share ? "Renamed" : "Data", path);
	fprintf(f, "[USB]\npath = %s\ntc:volume uuid = second\ntc:volume device = dk3\n", directory);
	CHECK(!fclose(f) && lp_load_with_shares(config_path));
}
static void load_config(const char *uuid, bool present)
{
    load_config_root(uuid, present, false, false);
}
static files_struct *file_for(connection_struct *conn, int fd, mode_t mode)
{
	files_struct *fsp = talloc_zero(conn, files_struct);
	CHECK(fsp);
	fsp->conn = conn; fsp->fh = fd_handle_create(fsp); CHECK(fsp->fh);
	fsp_set_fd(fsp, fd);
	fsp->fsp_name = talloc_zero(fsp, struct smb_filename); CHECK(fsp->fsp_name);
	fsp->fsp_name->st.st_ex_mode = mode;
	fsp->next = conn->sconn->files; conn->sconn->files = fsp;
	return fsp;
}
static connection_struct *tree_for(struct smbd_server_connection *sconn, const char *name, unsigned index)
{
	struct stat st;
	connection_struct *conn = talloc_zero(sconn, connection_struct);
	CHECK(conn);
	conn->sconn = sconn;
	conn->params = talloc_zero(conn, struct share_params); CHECK(conn->params);
	conn->params->service = lp_servicenumber(name); CHECK(conn->params->service >= 0);
	conn->connectpath = lp_path(conn, loadparm_s3_global_substitution(), SNUM(conn));
	CHECK(conn->connectpath && !stat(conn->connectpath, &st));
	conn->base_share_dev = st.st_dev;
	conn->cwd_fsp = file_for(conn, AT_FDCWD, S_IFDIR | 0700);
	conn->cwd_fsp->fsp_name->st.st_ex_ino = st.st_ino;
	CHECK(conn_record_bindings(conn));
	conn->tcon = talloc_zero(conn, struct smbXsrv_tcon); CHECK(conn->tcon);
	conn->tcon->global = talloc_zero_size(conn->tcon, sizeof(*conn->tcon->global)); CHECK(conn->tcon->global);
	conn->tcon->global->share_name = talloc_strdup(conn->tcon->global, name);
	conn->tcon->status = NT_STATUS_OK;
	conn->next = sconn->connections; sconn->connections = conn;
	trees[index] = conn;
	return conn;
}
static void drain(struct tevent_context *ev)
{
	unsigned i = 0;
	while (!disconnects && i++ < 20) CHECK(tevent_loop_once(ev) == 0);
	CHECK(disconnects == 1);
}
static void run_case(const char *name)
{
	TALLOC_CTX *ctx = talloc_new(NULL);
	struct smbd_server_connection *sconn = talloc_zero(ctx, struct smbd_server_connection);
	connection_struct *first, *second;
	files_struct *file;
	bool valid = true;
	int fd;
	CHECK(ctx && sconn);
	ZERO_ARRAY(trees); resets = disconnects = reloads = forwarded = guest_reinits = 0;
	revoked_fd = -1; fd_error = path_error = 0; reload_valid = true;
	load_config_root("first", true, !strcmp(name, "root_widen"), false);
	sconn->ev_ctx = tevent_context_init(sconn); CHECK(sconn->ev_ctx);
	first = tree_for(sconn, "Data", 0); second = tree_for(sconn, "USB", 1);
	fd = open(directory, O_RDONLY); CHECK(fd >= 0);
	file = file_for(first, fd, S_IFDIR | 0700);
	CHECK(!tc_stale_disk_tree(first, &valid) && !tc_stale_disk_tree(second, &valid));
	if (strcmp(name, "descriptors") == 0) {
		int errors[] = {EBADF, ESTALE, ENODEV, ENXIO, EACCES, EIO, ENOSYS};
		unsigned i;
		revoked_fd = fd;
		for (i = 0; i < ARRAY_SIZE(errors); i++) {
			fd_error = errors[i]; CHECK(tc_stale_disk_tree(first, &valid) == (i < 4));
			CHECK(!tc_stale_disk_tree(second, &valid));
		}
		/* Revoked regular/resource/ADS files use the same real descriptor
		 * path as directories. A closed descriptor is different: F_GETFD fails. */
		file->fsp_name->st.st_ex_mode = S_IFREG | 0600; fd_error = EBADF;
		CHECK(tc_stale_disk_tree(first, &valid));
		CHECK(!close(fd)); CHECK(!tc_stale_disk_tree(first, &valid)); fd = -1;
	} else if (strcmp(name, "sentinels") == 0) {
		revoked_fd = fd; fd_error = EBADF;
		file->fsp_flags.closing = true; CHECK(!tc_stale_disk_tree(first, &valid));
		file->fsp_flags.closing = false; file->fake_file_handle = (void *)1;
		CHECK(!tc_stale_disk_tree(first, &valid)); file->fake_file_handle = NULL;
		file->fsp_name->st.st_ex_mode = S_IFIFO; CHECK(!tc_stale_disk_tree(first, &valid));
		file->fsp_name->st.st_ex_mode = S_IFREG; first->ipc = true;
		CHECK(!tc_stale_disk_tree(first, &valid)); first->ipc = false; first->printer = true;
		CHECK(!tc_stale_disk_tree(first, &valid)); first->printer = false;
		fsp_set_fd(file, -1); CHECK(!tc_stale_disk_tree(first, &valid));
		fsp_set_fd(file, AT_FDCWD); CHECK(!tc_stale_disk_tree(first, &valid));
	} else if (strcmp(name, "identity") == 0) {
		/* Even a tree with only AT_FDCWD must notice UUID replacement or
		 * removal. Failed config reloads must not use a partial global map. */
		fsp_set_fd(file, -1);
		load_config("replacement", true);
		CHECK(!strcmp(first->tc_volume_binding, talloc_asprintf(ctx, "first|%s", directory)) && tc_stale_disk_tree(first, &valid));
		valid = false; CHECK(!tc_stale_disk_tree(first, &valid)); valid = true;
		load_config("first", false); CHECK(tc_stale_disk_tree(first, &valid));
		load_config("first", true); first->tc_root_ino++;
		CHECK(tc_stale_disk_tree(first, &valid)); first->tc_root_ino--;
		path_error = ENOENT; CHECK(tc_stale_disk_tree(first, &valid));
		path_error = EACCES; CHECK(!tc_stale_disk_tree(first, &valid)); path_error = 0;
	} else if (strncmp(name, "root", 4) == 0) {
        bool aio = !strcmp(name, "root_aio");
        bool unchanged = !strcmp(name, "root_unchanged");
        bool failed = !strcmp(name, "root_failed");
        /* Both old directories still exist with the same device/inode. A
         * retained old share definition must not hide the current root map. */
        load_config_root("first", true, strcmp(name, "root_widen") && !unchanged,
                         !strcmp(name, "root_rename"));
        if (!strcmp(name, "root_no_fds")) fsp_set_fd(file, -1);
        if (aio) {
            file->aio_requests = talloc_zero_array(file, struct tevent_req *, 1);
            CHECK(file->aio_requests); file->num_aio_requests = 1;
        }
        conn_refresh_bindings(sconn, !failed);
        CHECK(NT_STATUS_IS_OK(second->tcon->status));
        if (unchanged || failed) {
            CHECK(NT_STATUS_IS_OK(first->tcon->status) && disconnects == 0);
        } else {
            CHECK(NT_STATUS_EQUAL(first->tcon->status, NT_STATUS_NETWORK_NAME_DELETED));
            conn_refresh_bindings(sconn, true);
            if (aio) {
                CHECK(disconnects == 0 && file->fsp_flags.closing);
                TALLOC_FREE(file->aio_requests); file->num_aio_requests = 0;
            }
            drain(sconn->ev_ctx);
            CHECK(first->tcon == NULL && NT_STATUS_IS_OK(second->tcon->status));
        }
	} else if (strcmp(name, "aio") == 0) {
		files_struct *healthy = file_for(second, fd, S_IFDIR | 0700);
		/* A shared descriptor can back aliases on the same tree. Only the
		 * selected tree is marked closing while actual tevent AIO waiters drain. */
		file_for(first, fd, S_IFREG | 0600);
		first->tc_root_ino++;
		file->aio_requests = talloc_zero_array(file, struct tevent_req *, 1); CHECK(file->aio_requests);
		file->num_aio_requests = 1;
		conn_refresh_bindings(sconn, true);
		CHECK(resets == 1 && NT_STATUS_EQUAL(first->tcon->status, NT_STATUS_NETWORK_NAME_DELETED));
		CHECK(NT_STATUS_IS_OK(second->tcon->status) && !healthy->fsp_flags.closing);
		CHECK(file->fsp_flags.closing && disconnects == 0);
		conn_refresh_bindings(sconn, true); CHECK(resets == 2 && disconnects == 0);
		TALLOC_FREE(file->aio_requests); file->num_aio_requests = 0;
		drain(sconn->ev_ctx);
		CHECK(first->tcon == NULL && NT_STATUS_IS_OK(second->tcon->status));
	} else if (strcmp(name, "callbacks") == 0) {
		struct smbd_parent_context *parent = talloc_zero(ctx, struct smbd_parent_context);
		struct server_id id = {0};
		CHECK(parent); parent->msg_ctx = (struct messaging_context *)1;
		smbd_parent_sig_hup_handler(NULL, NULL, SIGHUP, 1, NULL, parent);
		CHECK(reloads == 1 && forwarded == 1 && guest_reinits == 1);
		smbd_parent_conf_updated(parent->msg_ctx, parent, MSG_SMB_CONF_UPDATED, id, NULL);
		CHECK(reloads == 2 && forwarded == 2 && guest_reinits == 2);
		smbd_sig_hup_handler(NULL, NULL, SIGHUP, 1, NULL, sconn);
		smbd_conf_updated(NULL, sconn, MSG_SMB_CONF_UPDATED, id, NULL);
		CHECK(reloads == 4 && resets == 2 && disconnects == 0);
		load_config("replacement", true); reload_valid = false;
		smbd_conf_updated(NULL, sconn, MSG_SMB_CONF_UPDATED, id, NULL);
		CHECK(NT_STATUS_IS_OK(first->tcon->status) && resets == 3);
		reload_valid = true;
		smbd_conf_updated(NULL, sconn, MSG_SMB_CONF_UPDATED, id, NULL);
		CHECK(NT_STATUS_EQUAL(first->tcon->status, NT_STATUS_NETWORK_NAME_DELETED));
		drain(sconn->ev_ctx); CHECK(NT_STATUS_IS_OK(second->tcon->status));
	} else CHECK(false);
	if (fd >= 0) CHECK(!close(fd));
    /* The fixture closed its shared real descriptor above. Samba's fd_handle
     * destructor requires its bookkeeping to be cleared before freeing fsps. */
    for (file = sconn->files; file != NULL; file = file->next) fsp_set_fd(file, -1);
	TALLOC_FREE(ctx);
}
int main(int argc, char **argv)
{
    TALLOC_CTX *frame = talloc_stackframe();
    setup_logging(argv[0], DEBUG_STDERR);
	const char *cases[] = {"descriptors", "sentinels", "identity", "aio", "callbacks", "root", "root_widen", "root_rename", "root_no_fds", "root_aio", "root_failed", "root_unchanged"};
	unsigned i;
    char narrow[160];
	CHECK(argc == 2); alarm(30);
	snprintf(directory, sizeof(directory), "tc-storage-reload-%ld", (long)getpid());
	CHECK(!mkdir(directory, 0700));
    snprintf(narrow, sizeof(narrow), "%s/ShareRoot", directory);
    CHECK(!mkdir(narrow, 0700));
	snprintf(config_path, sizeof(config_path), "%s/smb.conf", directory);
	for (i = 0; i < ARRAY_SIZE(cases); i++) {
		if (!strcmp(argv[1], "all") || !strcmp(argv[1], cases[i])) run_case(cases[i]);
	}
	CHECK(!unlink(config_path) && !rmdir(narrow) && !rmdir(directory));
    TALLOC_FREE(frame);
	return 0;
}
