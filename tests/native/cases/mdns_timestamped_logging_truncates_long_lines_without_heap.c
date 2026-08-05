#include <string.h>
#include "mdns/mdns.h"

int main(void) {
    char message[5001];
    memset(message, 'A', sizeof(message) - 1);
    message[sizeof(message) - 1] = '\0';
    timestamped_fprintf(stderr, "%s\n", message);
    return 0;
}
