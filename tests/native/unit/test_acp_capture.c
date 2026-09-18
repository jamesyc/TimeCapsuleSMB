#include "acp.h"
#include <assert.h>

int main(int argc, char **argv) {
    struct acp_request request;
    struct acp_collector collector;
    char *buffer;
    int rc;
    assert(argc == 6);
    memset(&request, 0, sizeof(request));
    request.key = "MaSt";
    request.form = ACP_ARRAY;
    request.multiline = atoi(argv[1]);
    request.trim_whitespace = atoi(argv[2]);
    request.capacity = (size_t)atoi(argv[3]);
    buffer = malloc(request.capacity ? request.capacity : 1);
    assert(buffer);
    request.output = buffer;
    if (atoi(argv[4])) {
        rc = acp_collect_begin(&collector, &request, 1, 5000, atoi(argv[5]));
        while (!rc) {
            fd_set reads;
            struct timeval delay = {0, 10000};
            int fd = acp_collect_fd(&collector);
            FD_ZERO(&reads);
            if (fd >= 0) FD_SET(fd, &reads);
            (void)select(fd + 1, &reads, NULL, NULL, &delay);
            rc = acp_collect_pump(&collector);
        }
    } else {
        rc = acp_collect_run(&request, 1, 5000, atoi(argv[5]));
    }
    printf("%d %lu %d %d\n", request.status, (unsigned long)request.length, request.exit_status, rc);
    fwrite(buffer, 1, request.length, stdout);
    free(buffer);
    return 0;
}
