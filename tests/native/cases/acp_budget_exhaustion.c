/* A collection whose budget is already spent must report every unread key
 * as ACP_ABORT, not ACP_UNAVAILABLE: "unavailable" is acp's own answer
 * that a key is not set and the planner acts on it (review finding 1).
 * No acp child is started, so this does not depend on the fixture binary. */
#include "common/acp.h"

int main(void) {
    static const char *const keys[3] = { "raNA", "laIP", "gnRo" };
    struct acp_value values[3];
    struct acp_collector c;
    struct acp_request requests[3];
    size_t i;
    int rc;
    memset(requests, 0, sizeof(requests));
    for (i = 0; i < 3; i++) {
        requests[i].key = keys[i];
        requests[i].output = values[i].text;
        requests[i].capacity = sizeof(values[i].text);
    }
    rc = acp_collect_begin(&c, requests, 3, 1000, 0);

    if (rc != -1) {
        printf("expected the collection to finish immediately, rc=%d\n", rc);
        return 1;
    }
    for (i = 0; i < 3; i++) {
        if (requests[i].status != ACP_ABORT || values[i].text[0] != '\0') {
            printf("%s: status=%d text='%s'\n", keys[i], requests[i].status, values[i].text);
            return 1;
        }
        values[i].status = requests[i].status;
        if (acp_ipv4(&values[i]).available || acp_bool(&values[i]).available) {
            printf("%s: decoded as available\n", keys[i]);
            return 1;
        }
    }
    printf("budget exhaustion marks %u keys aborted\n", 3u);
    return 0;
}
