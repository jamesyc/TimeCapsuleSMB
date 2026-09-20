#ifndef TC_SAMBA_STAGING_H
#define TC_SAMBA_STAGING_H
#include "../storage/runtime.h"

/* Prepared files are the actual next configuration, not supervisor state.
 * The manager publishes them only after the job and its generation validate. */
struct tc_samba_settings {
    struct tc_runtime_config config;
    struct tc_samba_identity identity;
    char nt_hash[33];
};
int tc_samba_settings_read(struct tc_samba_settings *);
int tc_samba_stage(const struct tc_storage_snapshot *, const struct tc_samba_settings *, const char *bindings,
                   int copy_smbd, int copy_rsync);
int tc_samba_publish(int rsync);
void tc_samba_discard(void);
/* Call only after ownership audit and the entire old smbd group has exited. */
int tc_samba_clear_locks(void);
#endif
