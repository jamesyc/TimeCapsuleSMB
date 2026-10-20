#include "samba/image.h"
#include "common/worker.h"
#include <assert.h>

/* The device waits: TC_STORAGE_RETRY_MS is 5 s, so 5, 15 and then 60 s. */
#define STEP1 5000
#define STEP2 15000
#define STEP3 60000
#define DAY 86400000LL

int main(void) {
    struct tc_image image = {0};
    int i;

    /* A manager starts with nothing copied, and may copy at once. RAM may
     * still hold an earlier manager's files. */
    assert(image.state == TC_IMAGE_NEEDED && tc_image_wanted(&image) && tc_image_due(&image, 0));
    assert(tc_image_in_ram(&image));
    tc_image_copying(&image);
    tc_image_staged(&image, 0, 0, 1, 100);
    assert(image.state == TC_IMAGE_VERIFIED && !tc_image_wanted(&image) && tc_image_due(&image, 100));

    /* A copy the run did not publish was removed with it. */
    tc_image_copying(&image);
    assert(image.state == TC_IMAGE_NEEDED);
    tc_image_staged(&image, 0, 0, 0, 200);
    assert(image.state == TC_IMAGE_NEEDED && tc_image_due(&image, 200));

    /* Any other staging failure (a disk not ready, a hosts write) adds no
     * wait: staging's own short retry applies, however often it fails. */
    for (i = 0; i < 5; i++) {
        tc_image_staged(&image, -1, 0, 0, 1000 + i);
        assert(image.state == TC_IMAGE_NEEDED && tc_image_due(&image, 1000 + i) && !image.wait.failures);
    }

    /* A damaged disk copy waits 5, 15, then 60 s, and is reported damaged
     * while it is tried again. It never stops being tried. */
    tc_image_staged(&image, TC_COPY_DISK_DAMAGED, 0, 0, 10000);
    assert(image.state == TC_IMAGE_DISK_DAMAGED && tc_image_wanted(&image));
    assert(!tc_image_due(&image, 10000 + STEP1 - 1) && tc_image_due(&image, 10000 + STEP1));
    tc_image_copying(&image);
    assert(image.state == TC_IMAGE_DISK_DAMAGED);
    tc_image_staged(&image, TC_COPY_DISK_DAMAGED, 0, 0, 20000);
    assert(image.wait.at == 20000 + STEP2);
    for (i = 0; i < 10; i++) {
        tc_image_staged(&image, TC_COPY_DISK_DAMAGED, 0, 0, 100000 + i);
        assert(image.wait.at == 100000 + i + STEP3 && tc_image_wanted(&image));
    }
    /* A run that fails without reading other bytes (a read error from the
     * same disk, a hosts write) says nothing new: still damaged, and the wait
     * already earned holds. */
    tc_image_staged(&image, -1, 0, 0, 200000);
    assert(image.state == TC_IMAGE_DISK_DAMAGED && image.wait.at == 100009 + STEP3);
    /* Only bytes that match end it. */
    tc_image_staged(&image, 0, 0, 0, 300000);
    assert(image.state == TC_IMAGE_NEEDED && image.wait.at == 100009 + STEP3);

    /* The payload moved or went away: another file, so no wait or damage
     * carries over. */
    tc_image_staged(&image, TC_COPY_DISK_DAMAGED, 0, 0, 310000);
    tc_image_reset(&image);
    assert(image.state == TC_IMAGE_NEEDED && tc_image_due(&image, 0) && !image.wait.failures);

    /* A launch that refuses a copy, each time after a verified one: the
     * waits grow and go on at 60 s. Verifying a copy does not end them;
     * only serving from it does. */
    {
        static const long long waits[] = {STEP1, STEP2, STEP3, STEP3, STEP3};
        for (i = 0; i < 5; i++) {
            long long now = 400000 + 100000LL * i;
            tc_image_copying(&image);
            tc_image_staged(&image, 0, 0, 1, now);
            assert(image.state == TC_IMAGE_VERIFIED);
            tc_image_refused(&image, now + 10);
            assert(image.state == TC_IMAGE_NEEDED && tc_image_wanted(&image));
            assert(image.wait.at == now + 10 + waits[i]);
        }
    }

    /* Serving clears the wait, however the program later stops: the next
     * refusal waits 5 s again. */
    tc_image_staged(&image, 0, 0, 1, 1000000);
    tc_image_serving(&image);
    assert(image.state == TC_IMAGE_VERIFIED && !image.wait.failures && tc_image_due(&image, 0));
    tc_image_refused(&image, 2000000);
    assert(image.wait.at == 2000000 + STEP1);

    /* Nothing but these functions moves the wait: due reads only the
     * image's own time. */
    assert(!tc_image_due(&image, 2000000 + STEP1 - 1) && tc_image_due(&image, 2000000 + STEP1));

    tc_image_reset(&image);
    tc_image_refused(&image, 2000000);

    /* Whether RAM holds the files is the manager's to say: a reset is about
     * which file to trust and never says they are gone. */
    tc_image_staged(&image, 0, 0, 1, 3000000);
    tc_image_removed(&image);
    assert(!tc_image_in_ram(&image) && image.state == TC_IMAGE_NEEDED && tc_image_wanted(&image));
    assert(image.wait.at == 2000000 + STEP1); /* removal is not damage */
    tc_image_copying(&image);
    assert(tc_image_in_ram(&image));
    tc_image_staged(&image, 0, 0, 1, 3100000);
    tc_image_reset(&image);
    assert(tc_image_in_ram(&image));
    tc_image_staged(&image, TC_COPY_DISK_DAMAGED, 0, 0, 3200000);
    tc_image_removed(&image);
    assert(!tc_image_in_ram(&image) && image.state == TC_IMAGE_DISK_DAMAGED);

    /* A disk that could not return the file waits 5 and 15 s as other damage
     * does, then 60 s doubling each time up to a day, and never stops being
     * tried: each check reads the whole binary from a failing disk. */
    tc_image_reset(&image);
    {
        long long now = 10000000, wait = STEP3;
        tc_image_staged(&image, TC_COPY_DISK_DAMAGED, 1, 0, now);
        assert(image.state == TC_IMAGE_DISK_DAMAGED && image.wait.at == now + STEP1);
        tc_image_staged(&image, TC_COPY_DISK_DAMAGED, 1, 0, now);
        assert(image.wait.at == now + STEP2);
        for (i = 0; i < 20; i++) {
            tc_image_staged(&image, TC_COPY_DISK_DAMAGED, 1, 0, now);
            assert(image.wait.at == now + wait && tc_image_wanted(&image));
            wait = wait * 2 > DAY ? DAY : wait * 2;
        }
        assert(image.wait.at == now + DAY);
        /* A run that read nothing keeps the wait earned. */
        tc_image_staged(&image, -1, 0, 0, now + 1);
        assert(image.state == TC_IMAGE_DISK_DAMAGED && image.wait.at == now + DAY);
        /* Other bytes instead (a reinstall's damage) wait as damage does. */
        tc_image_staged(&image, TC_COPY_DISK_DAMAGED, 0, 0, now);
        assert(image.wait.at == now + STEP3);
        tc_image_staged(&image, TC_COPY_DISK_DAMAGED, 1, 0, now);
        assert(image.wait.at == now + STEP3);
        /* Serving starts it over. */
        tc_image_staged(&image, 0, 0, 1, now);
        tc_image_serving(&image);
        tc_image_staged(&image, TC_COPY_DISK_DAMAGED, 1, 0, now);
        assert(image.wait.at == now + STEP1);
    }
    return 0;
}
