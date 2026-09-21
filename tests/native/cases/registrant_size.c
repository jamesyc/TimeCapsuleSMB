#include <stdio.h>
#include "mdns/registrant.h"

int main(void) {
    printf("%lu\n", (unsigned long)sizeof(struct registrant));
    return 0;
}
