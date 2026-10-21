#include "image.h"
#include "../common/worker.h"

void tc_image_reset(struct tc_image *image) {
    image->state = TC_IMAGE_NEEDED;
    tc_storage_retry_finish(&image->wait, 0, 0);
    image->unreadable_wait = 0;
}
/* After the usual 1x and 3x, each wait for a disk that could not return the
 * file doubles from 12x TC_STORAGE_RETRY_MS up to TC_IMAGE_UNREADABLE_MAX_MS:
 * every check reads the whole binary, which keeps a failing disk busy while
 * its files wait to be copied off, and a disk that recovers is still found. */
static void wait_unreadable(struct tc_image *image, long long now) {
    if (image->wait.failures < 2) {
        tc_storage_retry_finish(&image->wait, now, 1);
        return;
    }
    image->unreadable_wait = image->unreadable_wait ? 2 * image->unreadable_wait : 12LL * TC_STORAGE_RETRY_MS;
    if (image->unreadable_wait > TC_IMAGE_UNREADABLE_MAX_MS)
        image->unreadable_wait = TC_IMAGE_UNREADABLE_MAX_MS;
    image->wait.at = now + image->unreadable_wait;
}
void tc_image_copying(struct tc_image *image) {
    image->removed = 0;
    /* A damaged disk copy stays reported while it is tried again. */
    if (image->state == TC_IMAGE_VERIFIED)
        image->state = TC_IMAGE_NEEDED;
}
void tc_image_staged(struct tc_image *image, int copy_result, int unreadable, int published, long long now) {
    if (copy_result == TC_COPY_DISK_DAMAGED && unreadable) {
        image->state = TC_IMAGE_DISK_DAMAGED;
        wait_unreadable(image, now);
    } else if (copy_result == TC_COPY_DISK_DAMAGED) {
        image->state = TC_IMAGE_DISK_DAMAGED;
        tc_storage_retry_finish(&image->wait, now, 1);
        image->unreadable_wait = 0;
    } else if (!copy_result) {
        /* Its bytes matched, so the disk copy is good. A verified copy keeps
         * the wait: only serving shows it stays good. A copy the run did not
         * publish was removed with the run. */
        image->state = published ? TC_IMAGE_VERIFIED : TC_IMAGE_NEEDED;
    } else if (image->state != TC_IMAGE_DISK_DAMAGED) {
        image->state = TC_IMAGE_NEEDED;
    }
}
void tc_image_refused(struct tc_image *image, long long now) {
    image->state = TC_IMAGE_NEEDED;
    tc_storage_retry_finish(&image->wait, now, 1);
}
void tc_image_serving(struct tc_image *image) {
    tc_storage_retry_finish(&image->wait, 0, 0);
    image->unreadable_wait = 0;
}
void tc_image_removed(struct tc_image *image) {
    image->removed = 1;
    /* Damage on the disk is still there to report if the program comes back. */
    if (image->state == TC_IMAGE_VERIFIED)
        image->state = TC_IMAGE_NEEDED;
}
int tc_image_wanted(const struct tc_image *image) {
    return image->state != TC_IMAGE_VERIFIED;
}
int tc_image_due(const struct tc_image *image, long long now) {
    return now >= image->wait.at;
}
int tc_image_in_ram(const struct tc_image *image) {
    return !image->removed;
}
