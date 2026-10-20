#ifndef TC_SAMBA_STAGING_H
#define TC_SAMBA_STAGING_H
#include "../common/worker.h"
#include "../storage/runtime.h"

/* Prepared files are the actual next configuration, not supervisor state.
 * The manager publishes them only after the job and its generation validate. */
struct tc_samba_settings {
    struct tc_runtime_config config;
    struct tc_samba_identity identity;
    char nt_hash[33];
};
int tc_samba_settings_read(struct tc_samba_settings *, const char *hostname);
/* How a run treats its program's binary: whether to copy it; whether to
 * check the disk copy where it is first (it was damaged: one that has not
 * changed is neither copied into RAM nor logged again); what that damage
 * read last (tc_stage_result.read); and which files the last run found
 * missing from the payload (tc_stage_result.missing), not logged again. */
struct tc_stage_copy {
    int copy, probe, missing;
    long long seen;
};
/* What a run did with its binary: tc_copy_file's result (worker.h), -1 when
 * the run did not get that far; the CRC-32 its payload's checksums file
 * records, -1 for none, which each launch checks the RAM copy against; and,
 * on damage, what it read: the binary's CRC-32, TC_COPY_UNREADABLE,
 * TC_COPY_STALLED, TC_STAGE_BAD_CHECKSUMS for a checksums file without the
 * binary's entry, or TC_STAGE_UNREADABLE_CHECKSUMS for one the disk could not
 * read; else -1. */
#define TC_STAGE_BAD_CHECKSUMS -3
#define TC_STAGE_UNREADABLE_CHECKSUMS -5
/* Whether what damage read says the disk could not return the payload, or
 * not in time: a failing disk, which a reinstall does not fix. */
int tc_stage_disk_unreadable(long long read);
/* missing: the files the run needs that the payload lacks, a bit for each
 * (only a reinstall puts them back: the title's NAME=missing), 0 for none,
 * -1 when the run did not get that far. A run whose disk did not stay the
 * payload's reports neither missing files nor damage. */
/* copy_ms: how long a copy that went through took to read, -1 for none. */
struct tc_stage_result {
    int copy, missing;
    long long crc32, read, copy_ms;
};
#define TC_STAGE_RESULT_NONE {-1, -1, -1, -1, -1}
/* Samba's run: its directories, smbd and its three files. rsync's run:
 * ShareRoot, rsyncd.conf and rsync. Each stages and publishes alone, so
 * neither program waits on the other. */
int tc_samba_stage(const struct tc_storage_snapshot *, const struct tc_samba_settings *,
                   const struct tc_stage_copy *, struct tc_stage_result *);
int tc_samba_publish(void);
void tc_samba_discard(void);
int tc_rsync_stage(const struct tc_storage_snapshot *, const struct tc_stage_copy *, struct tc_stage_result *);
int tc_rsync_publish(void);
void tc_rsync_discard(void);
/* Call only after ownership audit and the entire old smbd group has exited. */
int tc_samba_clear_locks(void);
/* The space the files in the lock directory take, msg.lock's included, or
 * -1 when it cannot be read. */
long long tc_samba_locks_bytes(void);
#endif
