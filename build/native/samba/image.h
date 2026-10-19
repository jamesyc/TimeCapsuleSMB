#ifndef TC_SAMBA_IMAGE_H
#define TC_SAMBA_IMAGE_H
#include "../storage/settle.h"

/* A binary staging copies from the payload to RAM (smbd, rsync): whether the
 * RAM copy is the one deploy wrote, whether RAM may hold its files at all,
 * and when the next copy may be made. Only these functions change it; the
 * manager feeds them what staging, launches and the payload did, and reads
 * the result.
 *
 *   needed        no verified copy for the current payload
 *   verified      copied, matched the CRC-32 deploy recorded, and published
 *   disk_damaged  the bytes read from the disk did not match it
 *
 * Damage, the disk's at staging or the RAM copy's at a launch, makes the next
 * copy wait 1x, 3x, then 12x TC_STORAGE_RETRY_MS, and copies go on at 12x for
 * as long as it lasts. Disk damage stays reported until a copy's bytes match
 * or the payload changes: a run that fails without reading other bytes says
 * nothing new. Any other staging failure leaves the wait alone: the unit's
 * own retry paces runs (manager.c), and a run that only restages the
 * program's files, without copying it, neither waits for nor touches the
 * image. The wait starts over once the program serves from a copy, or when
 * the payload changes.
 *
 * Whether RAM holds the files is apart from all that: another payload is
 * another file to trust, but the old one's files stay in RAM until removed. */
enum tc_image_state { TC_IMAGE_NEEDED, TC_IMAGE_VERIFIED, TC_IMAGE_DISK_DAMAGED };
struct tc_image {
    enum tc_image_state state;
    /* at: no copy before it (0 for none); failures: how far the wait has grown. */
    struct tc_storage_retry wait;
    /* 1 once the manager removed the RAM files, until the next copy; 0, as
     * at the start, when RAM may hold them (an earlier manager's too). */
    int removed;
};
/* The payload moved or went away. */
void tc_image_reset(struct tc_image *);
/* A copy is starting and replaces the RAM file. */
void tc_image_copying(struct tc_image *);
/* A staging run that copied this image ended: tc_copy_file's result for it,
 * -1 when the run failed before reading it, and whether the run published it. */
void tc_image_staged(struct tc_image *, int copy_result, int published, long long now);
/* The launch found the RAM copy damaged or unreadable. */
void tc_image_refused(struct tc_image *, long long now);
/* The program is serving from this copy. */
void tc_image_serving(struct tc_image *);
/* The manager removed the image's files from RAM. */
void tc_image_removed(struct tc_image *);
/* Whether staging must copy it, whether that may happen now, and whether RAM
 * may hold its files. */
int tc_image_wanted(const struct tc_image *);
int tc_image_due(const struct tc_image *, long long now);
int tc_image_in_ram(const struct tc_image *);
#endif
