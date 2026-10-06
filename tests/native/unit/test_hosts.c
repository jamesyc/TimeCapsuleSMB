#include "service/service.h"
#include <assert.h>
/* test_hosts PATH NAME: prints tc_hosts_update's result, and its errno text
 * on stderr when it fails. test_hosts --hostname: prints tc_hostname_read. */
int main(int argc, char **argv) {
    int result;
    if (argc == 2 && !strcmp(argv[1], "--hostname")) {
        char name[256];
        tc_hostname_read(name, sizeof(name));
        printf("%s\n", name);
        return 0;
    }
    assert(argc == 3);
    result = tc_hosts_update(argv[1], argv[2]);
    printf("result=%d\n", result);
    if (result < 0)
        fprintf(stderr, "%s\n", strerror(errno));
    return 0;
}
