#include "service/service.h"
#include <assert.h>
int main(int argc, char **argv) {
    assert(argc == 3);
    return tc_hosts_ensure(argv[1], argv[2]) ? 1 : 0;
}
