#include "image.h"
#include "../common/worker.h"

void tc_image_reset(struct tc_image *image) {
    image->state = TC_IMAGE_NEEDED;
    tc_storage_retry_finish(&image->wait, 0, 0);
}
void tc_image_copying(struct tc_image *image) {
    image->removed = 0;
    /* A damaged disk copy stays reported while it is tried again. */
    if (image->state == TC_IMAGE_VERIFIED)
        image->state = TC_IMAGE_NEEDED;
}
void tc_image_staged(struct tc_image *image, int copy_result, int published, long long now) {
    if (copy_result == TC_COPY_DISK_DAMAGED) {
        image->state = TC_IMAGE_DISK_DAMAGED;
        tc_storage_retry_finish(&image->wait, now, 1);
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
