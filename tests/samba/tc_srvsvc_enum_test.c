/* Run the real NetShareEnumAll on the real loadparm table. Patch 0005 serves
 * srvsvc in the worker that holds the client's trees, so an enumeration must
 * leave what modules set on that table at tree connect. */
#include "includes.h"
#include "ntdomain.h"
#include "librpc/rpc/dcesrv_core.h"
#include "librpc/gen_ndr/ndr_srvsvc.h"
#include "librpc/gen_ndr/ndr_srvsvc_scompat.h"
#include "rpc_server/rpc_pipes.h"
#include "auth.h"
#include "smbd/smbd.h"
#include "lib/tsocket/tsocket.h"
#include "dynconfig/dynconfig.h"

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #x); exit(90); } } while (0)
static char directory[128], config_path[160];

static void write_config(bool extra)
{
	FILE *f = fopen(config_path, "w");
	CHECK(f);
	fprintf(f, "[global]\nworkgroup = WORKGROUP\n[Disk]\npath = %s\n[Other]\npath = %s\n", directory, directory);
	if (extra) fprintf(f, "[Extra]\npath = %s\n", directory);
	CHECK(!fclose(f));
}

static bool listed(struct srvsvc_NetShareCtr1 *ctr, const char *name)
{
	uint32_t i;
	for (i = 0; i < ctr->count; i++) if (!strcmp(ctr->array[i].name, name)) return true;
	return false;
}

/* What smbd has for a pipe call once the session is authenticated. */
static struct srvsvc_NetShareCtr1 *enumerate(TALLOC_CTX *mem)
{
	struct dcesrv_connection *conn = talloc_zero(mem, struct dcesrv_connection);
	struct dcesrv_auth *auth = talloc_zero(mem, struct dcesrv_auth);
	struct dcesrv_call_state *call = talloc_zero(mem, struct dcesrv_call_state);
	struct pipes_struct *p = talloc_zero(mem, struct pipes_struct);
	struct auth_session_info *session = talloc_zero(mem, struct auth_session_info);
	struct srvsvc_NetShareEnumAll r = {};
	struct srvsvc_NetShareInfoCtr info = {.level = 1};
	struct srvsvc_NetShareCtr1 *ctr = talloc_zero(mem, struct srvsvc_NetShareCtr1);
	struct tsocket_address *local = NULL;
	uint32_t total = 0;

	CHECK(conn && auth && call && p && session && ctr);
	CHECK(!tsocket_address_inet_from_strings(conn, "ip", "127.0.0.1", 445, &local));
	conn->local_address = local;
	session->unix_info = talloc_zero(session, struct auth_user_info_unix); CHECK(session->unix_info);
	session->unix_info->unix_name = talloc_strdup(session, "root"); CHECK(session->unix_info->unix_name);
	session->unix_token = talloc_zero(session, struct security_unix_token); CHECK(session->unix_token);
	session->security_token = talloc_zero(session, struct security_token); CHECK(session->security_token);
	auth->auth_finished = true;
	auth->session_info = session;
	call->conn = conn;
	call->auth_state = auth;
	p->dce_call = call;
	p->mem_ctx = mem;
	info.ctr.ctr1 = ctr;
	r.in.info_ctr = &info;
	r.out.info_ctr = &info;
	r.in.max_buffer = (uint32_t)-1;
	r.out.totalentries = &total;
	CHECK(W_ERROR_IS_OK(_srvsvc_NetShareEnumAll(p, &r)));
	CHECK(total == info.ctr.ctr1->count);
	return info.ctr.ctr1;
}

static void test_options(TALLOC_CTX *mem)
{
	int disk, other;
	struct share_params params = {};
	struct srvsvc_NetShareCtr1 *ctr;

	write_config(false);
	CHECK(lp_load_with_shares(config_path));
	disk = lp_servicenumber("Disk");
	other = lp_servicenumber("Other");
	CHECK(disk >= 0 && other >= 0);
	params.service = disk;
	/* As fruit and acl_xattr do at tree connect. */
	CHECK(lp_do_parameter(disk, "posix locking", "no"));
	CHECK(lp_do_parameter(disk, "map archive", "no"));
	CHECK(!lp_posix_locking(&params));

	ctr = enumerate(mem);
	CHECK(listed(ctr, "Disk") && listed(ctr, "Other") && listed(ctr, "IPC$"));
	CHECK(!lp_posix_locking(&params));
	CHECK(!lp_map_archive(disk));
	/* Open trees name a share by this number. */
	CHECK(lp_servicenumber("Disk") == disk && lp_servicenumber("Other") == other);
}

/* The worker follows smb.conf through reload_services(); an enumeration lists
 * what that loaded, and a share nothing has loaded yet is not its to find. */
static void test_table(TALLOC_CTX *mem)
{
	struct srvsvc_NetShareCtr1 *ctr;

	write_config(false);
	CHECK(lp_load_with_shares(config_path));
	write_config(true);
	ctr = enumerate(mem);
	CHECK(listed(ctr, "Disk") && !listed(ctr, "Extra"));
	CHECK(lp_load_with_shares(config_path));
	ctr = enumerate(mem);
	CHECK(listed(ctr, "Disk") && listed(ctr, "Extra"));
}

int main(int argc, char **argv)
{
	TALLOC_CTX *frame = talloc_stackframe();
	bool all;

	setup_logging(argv[0], DEBUG_STDERR);
	CHECK(argc == 2); alarm(30);
	smb_init_locale();
	init_sec_ctx();
	snprintf(directory, sizeof(directory), "tc-srvsvc-enum-%ld", (long)getpid());
	CHECK(!mkdir(directory, 0700));
	snprintf(config_path, sizeof(config_path), "%s/smb.conf", directory);
	set_dyn_CONFIGFILE(config_path);
	all = !strcmp(argv[1], "all");
	CHECK(all || !strcmp(argv[1], "options") || !strcmp(argv[1], "table"));
	if (all || !strcmp(argv[1], "options")) test_options(frame);
	if (all || !strcmp(argv[1], "table")) test_table(frame);
	CHECK(!unlink(config_path) && !rmdir(directory));
	TALLOC_FREE(frame);
	return 0;
}
