#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <sys/socket.h>
#include <sys/types.h>

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len);

#include "nbns/nbns.h"

static int sendto_call_count = 0;

ssize_t fake_sendto(int sockfd, const void *buf, size_t len, int flags,
                    const struct sockaddr *dest, socklen_t dest_len) {
    (void)sockfd;
    (void)buf;
    (void)flags;
    (void)dest;
    (void)dest_len;

    sendto_call_count++;
    if (sendto_call_count == 1) {
        errno = EINTR;
        return -1;
    }
    return (ssize_t)len;
}

int main(void) {
    struct sockaddr_in dest;
    unsigned char packet[4] = {1, 2, 3, 4};
    ssize_t sent;

    memset(&dest, 0, sizeof(dest));
    sent = sendto_retry(1, packet, sizeof(packet), 0, (const struct sockaddr *)&dest, sizeof(dest));
    if (sent != (ssize_t)sizeof(packet)) {
        return 1;
    }
    if (sendto_call_count != 2) {
        return 2;
    }
    return 0;
}
