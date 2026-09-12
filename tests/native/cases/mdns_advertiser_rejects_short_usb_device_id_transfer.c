#include <stdio.h>
#include <string.h>
#include "mdns/mdns.h"

int main(void) {
    unsigned char buf[64];
    int actual_len = 99;

    memset(buf, 'X', sizeof(buf));
    buf[0] = 0;
    buf[1] = 32;
    memcpy(buf + 2, "CMD:LEAK;", 9);

    if (sanitize_usb_printer_device_id_transfer(buf, sizeof(buf), 2, &actual_len) == 0) {
        return 1;
    }
    if (actual_len != 0) {
        return 2;
    }
    if (buf[0] != 0 || buf[1] != 32 || buf[2] != 0 || buf[10] != 0 || buf[63] != 0) {
        return 3;
    }

    memset(buf, 'Y', sizeof(buf));
    if (sanitize_usb_printer_device_id_transfer(buf, sizeof(buf), 8, &actual_len) != 0) {
        return 4;
    }
    if (actual_len != 8 || buf[7] != 'Y' || buf[8] != 0 || buf[63] != 0) {
        return 5;
    }

    memset(buf, 'Z', sizeof(buf));
    if (sanitize_usb_printer_device_id_transfer(buf, sizeof(buf), 65, &actual_len) == 0) {
        return 6;
    }
    if (buf[0] != 0 || buf[63] != 0) {
        return 7;
    }

    printf("ok\n");
    return 0;
}
