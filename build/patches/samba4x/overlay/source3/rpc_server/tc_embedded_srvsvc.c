/* Included by srv_pipe_hnd.c (patch 0005) for TC_SAMBA4X_EMBEDDED_SRVSVC. */

#include "libcli/named_pipe_auth/npa_tstream.h"
#include "libcli/smb/smb_constants.h"
#include "librpc/gen_ndr/ndr_srvsvc_scompat.h"
#include "librpc/rpc/dcesrv_core.h"

static NTSTATUS tc_smbd_init_embedded_srvsvc(struct dcesrv_context **_dce_ctx)
{
	struct dcesrv_context *dce_ctx = *_dce_ctx;
	const struct dcesrv_endpoint_server *ep_server = srvsvc_get_ep_server();
	static bool ep_server_registered;
	static struct dcesrv_context *initialized_dce_ctx;
	NTSTATUS status;

	/*
	 * Time Capsule appliance builds stage only smbd on the RAM disk. Finder
	 * and smbclient -L still need IPC$ -> \PIPE\srvsvc for share
	 * enumeration, so smbd hosts exactly srvsvc in-process instead of
	 * starting samba-dcerpcd/rpcd_classic from the unmountable HFS disk.
	 */
	if (dce_ctx == NULL) {
		dce_ctx = global_dcesrv_context();
	}

	if (initialized_dce_ctx == dce_ctx) {
		*_dce_ctx = dce_ctx;
		return NT_STATUS_OK;
	}

	if (!ep_server_registered) {
		status = dcerpc_register_ep_server(ep_server);
		if (!NT_STATUS_IS_OK(status) &&
		    !NT_STATUS_EQUAL(status, NT_STATUS_OBJECT_NAME_COLLISION)) {
			DBG_ERR("Failed to register embedded srvsvc endpoint: "
				"%s\n",
				nt_errstr(status));
			return status;
		}
		ep_server_registered = true;
	}

	status = dcesrv_init_ep_server(dce_ctx, ep_server->name);
	if (!NT_STATUS_IS_OK(status)) {
		DBG_ERR("Failed to init embedded srvsvc endpoint: %s\n",
			nt_errstr(status));
		return status;
	}

	if (dcesrv_auth_type_principal_find(dce_ctx,
					    DCERPC_AUTH_TYPE_NTLMSSP) == NULL) {
		status = dcesrv_register_default_auth_types_machine_principal(
			dce_ctx);
		if (!NT_STATUS_IS_OK(status)) {
			DBG_ERR("Failed to register embedded srvsvc auth types: "
				"%s\n",
				nt_errstr(status));
			return status;
		}
	}

	initialized_dce_ctx = dce_ctx;
	*_dce_ctx = dce_ctx;
	return NT_STATUS_OK;
}

static NTSTATUS tc_smbd_open_embedded_srvsvc_np(
	const char *name,
	const struct tsocket_address *remote_client_address,
	const struct tsocket_address *local_server_address,
	struct auth_session_info *session_info,
	struct tevent_context *ev_ctx,
	struct messaging_context *msg_ctx,
	struct dcesrv_context *dce_ctx,
	struct npa_state *npa)
{
	struct dcesrv_endpoint *ep = NULL;
	struct dcerpc_ncacn_conn *ncacn_conn = NULL;
	struct dcesrv_connection *dcesrv_conn = NULL;
	struct tstream_context *client_transport = NULL;
	struct tstream_context *server_transport = NULL;
	struct tstream_context *server_stream = NULL;
	NTSTATUS status;
	int ret;

	if (!strequal(name, "srvsvc")) {
		return NT_STATUS_OBJECT_NAME_NOT_FOUND;
	}

	status = tc_smbd_init_embedded_srvsvc(&dce_ctx);
	if (!NT_STATUS_IS_OK(status)) {
		return status;
	}

	status = dcesrv_endpoint_by_ncacn_np_name(dce_ctx, name, &ep);
	if (!NT_STATUS_IS_OK(status)) {
		DBG_ERR("Embedded srvsvc endpoint lookup failed: %s\n",
			nt_errstr(status));
		return status;
	}

	ncacn_conn = talloc_zero(npa, struct dcerpc_ncacn_conn);
	if (ncacn_conn == NULL) {
		return NT_STATUS_NO_MEMORY;
	}
	/* Rc2 keeps the endpoint on dcesrv_conn rather than ncacn_conn. */
	ncacn_conn->p.msg_ctx = msg_ctx;
	ncacn_conn->p.transport = NCACN_NP;

	/*
	 * Use the same NPA framing that the external local_np path exposes to
	 * smbd, but connect both ends inside this process. The client side is
	 * stored in npa->stream for the normal SMB2 pipe read/write code; the
	 * server side is handed to the DCE/RPC srvsvc event loop below.
	 */
	ret = tstream_unix_socketpair(npa,
				      &client_transport,
				      ncacn_conn,
				      &server_transport);
	if (ret == -1) {
		status = map_nt_error_from_unix(errno);
		TALLOC_FREE(ncacn_conn);
		return status;
	}

	ret = tstream_npa_existing_stream(npa,
					  &client_transport,
					  FILE_TYPE_MESSAGE_MODE_PIPE,
					  &npa->stream);
	if (ret == -1) {
		status = map_nt_error_from_unix(errno);
		TALLOC_FREE(ncacn_conn);
		return status;
	}

	ret = tstream_npa_existing_stream(ncacn_conn,
					  &server_transport,
					  FILE_TYPE_MESSAGE_MODE_PIPE,
					  &server_stream);
	if (ret == -1) {
		status = map_nt_error_from_unix(errno);
		TALLOC_FREE(ncacn_conn);
		return status;
	}

	status = dcesrv_endpoint_connect(dce_ctx,
					 ncacn_conn,
					 ep,
					 session_info,
					 ev_ctx,
					 DCESRV_CALL_STATE_FLAG_MAY_ASYNC,
					 &dcesrv_conn);
	if (!NT_STATUS_IS_OK(status)) {
		DBG_DEBUG("Embedded srvsvc connect failed: %s\n",
			  nt_errstr(status));
		TALLOC_FREE(ncacn_conn);
		return status;
	}

	ncacn_conn->dcesrv_conn = dcesrv_conn;
	dcesrv_conn->transport.private_data = ncacn_conn;
	dcesrv_conn->transport.report_output_data =
		dcesrv_sock_report_output_data;
	dcesrv_conn->transport.terminate_connection =
		dcesrv_transport_terminate_connection;

	dcesrv_conn->send_queue = tevent_queue_create(
		dcesrv_conn, "embedded srvsvc send queue");
	if (dcesrv_conn->send_queue == NULL) {
		TALLOC_FREE(ncacn_conn);
		return NT_STATUS_NO_MEMORY;
	}

	dcesrv_conn->stream = talloc_move(dcesrv_conn, &server_stream);
	dcesrv_conn->local_address =
		tsocket_address_copy(local_server_address, dcesrv_conn);
	dcesrv_conn->remote_address =
		tsocket_address_copy(remote_client_address, dcesrv_conn);
	if (dcesrv_conn->local_address == NULL ||
	    dcesrv_conn->remote_address == NULL) {
		TALLOC_FREE(ncacn_conn);
		return NT_STATUS_NO_MEMORY;
	}

	status = dcesrv_connection_loop_start(dcesrv_conn);
	if (!NT_STATUS_IS_OK(status)) {
		TALLOC_FREE(ncacn_conn);
		return status;
	}

	return NT_STATUS_OK;
}
