/* Native symlinks (patch 0045): declarations for smbd/tc_native_links.c. */

#ifndef _SMBD_TC_NATIVE_LINKS_H_
#define _SMBD_TC_NATIVE_LINKS_H_

#define TC_XSYM_FILE_SIZE 1067

struct tc_xsym_candidate {
	char *target;
	SMB_DEV_T dev;
	SMB_INO_T ino;
	off_t size;
	char *aside;	/* taken aside; removed by close_finish */
};

bool tc_native_links_enabled(const struct connection_struct *conn);
bool tc_client_resolves_links(const struct connection_struct *conn);
NTSTATUS tc_native_links_read_xsym(struct files_struct *fsp,
				   TALLOC_CTX *mem_ctx,
				   DATA_BLOB *out,
				   off_t offset,
				   size_t length);
NTSTATUS tc_native_links_write_xsym(struct files_struct *fsp,
				    const uint8_t *data,
				    size_t length,
				    off_t offset);
bool tc_native_links_close_prepare(TALLOC_CTX *mem_ctx,
				   struct files_struct *fsp,
				   enum file_close_type close_type,
				   struct tc_xsym_candidate *cand);
struct share_mode_lock;
bool tc_native_links_sole_open(struct share_mode_lock *lck,
			       struct files_struct *fsp);
bool tc_native_links_close_commit(struct files_struct *fsp,
				  struct tc_xsym_candidate *cand);
void tc_native_links_close_finish(struct files_struct *fsp,
				  struct tc_xsym_candidate *cand);
uint32_t tc_native_links_dos_mode(struct files_struct *fsp, uint32_t dosmode);
uint32_t tc_native_links_fs_capabilities(const struct connection_struct *conn);
NTSTATUS tc_native_links_map_target(struct connection_struct *conn,
				    TALLOC_CTX *mem_ctx,
				    char **_target,
				    enum vfs_translate_direction direction);
NTSTATUS tc_native_links_check_set(struct files_struct *fsp,
				   uint32_t reparse_tag,
				   const uint8_t *data,
				   size_t len);
NTSTATUS tc_native_links_stream_base(struct connection_struct *conn,
				     struct files_struct *dirfsp,
				     struct smb_filename *smb_fname_base);

#endif /* _SMBD_TC_NATIVE_LINKS_H_ */
