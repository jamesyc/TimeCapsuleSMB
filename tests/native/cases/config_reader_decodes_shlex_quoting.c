/* config_decode_assignment_value() must round-trip shlex.quote() output.
 * Usage: <case> <file> <key> -> prints "ok:<value>" or "unavailable". */
#include "common/config.h"
int main(int argc, char **argv) {
    char value[TC_CONFIG_VALUE_MAX];
    if (argc != 3) return 2;
    if (config_read_value(argv[1], argv[2], value, sizeof(value)) == 0) printf("ok:%s\n", value);
    else puts("unavailable");
    return 0;
}
