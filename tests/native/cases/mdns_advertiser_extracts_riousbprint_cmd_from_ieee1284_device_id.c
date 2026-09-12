#include <stdio.h>
#include <string.h>
#include "mdns/mdns.h"

int main(void) {
    const char *device_id = "MFG:Canon;MDL:MP490 series;CMD:BJL,BJRaster3,BSCCe,IVEC,IVECPLI;";
    unsigned char buf[256];
    char cmd[MAX_TXT_STRING + 1];
    size_t len = strlen(device_id) + 2;

    memset(buf, 0, sizeof(buf));
    buf[0] = (unsigned char)((len >> 8) & 0xff);
    buf[1] = (unsigned char)(len & 0xff);
    memcpy(buf + 2, device_id, strlen(device_id));

    if (extract_cmd_from_ieee1284_device_id(cmd, sizeof(cmd), buf, len) != 0) {
        return 1;
    }
    if (strcmp(cmd, "BJL,BJRaster3,BSCCe,IVEC,IVECPLI") != 0) {
        return 2;
    }
    printf("%s\n", cmd);
    return 0;
}
