#include "telemetry.h"
#include "errclass.h"

/* Counters only: they go out in the health object, and nothing decides on
 * them. A static is enough, since every failure happens in this process. */
static struct tc_failure_counts counts;

enum tc_errno_class tc_errno_class(enum tc_op op, int error) {
    if (op == TC_OP_IO) return error == ENOBUFS || error == ENOMEM ? TC_ERR_RESOURCE : TC_ERR_OTHER;
    if (error == EMFILE || error == ENFILE || error == ENOMEM) return TC_ERR_RESOURCE;
    if (op == TC_OP_OPEN) return TC_ERR_OTHER;
    if (error == EAFNOSUPPORT || error == EPROTONOSUPPORT || error == EPFNOSUPPORT) return TC_ERR_FAMILY;
    return TC_ERR_RESOURCE;
}

enum tc_errno_class tc_errno_record(enum tc_op op, int error) {
    enum tc_errno_class class = tc_errno_class(op, error);
    if (op == TC_OP_FORK) {
        if (error == EAGAIN) counts.fork_eagain++;
        else if (error == ENOMEM) counts.fork_enomem++;
        else counts.fork_other++;
    } else if (op == TC_OP_SOCKET && class != TC_ERR_FAMILY) {
        if (error == EMFILE) counts.socket_emfile++;
        else if (error == ENFILE) counts.socket_enfile++;
        else if (error == ENOBUFS || error == ENOMEM) counts.socket_enobufs++;
        else counts.socket_other++;
    } else if (op == TC_OP_IO && class == TC_ERR_RESOURCE) {
        counts.socket_enobufs++;
    }
    return class;
}

void tc_failure_counts(struct tc_failure_counts *out) {
    *out = counts;
}

#ifdef TC_NATIVE_TEST
void tc_failure_counts_set(const struct tc_failure_counts *in) {
    counts = *in;
}

int tc_test_enobufs(const char *call) {
    const char *which = getenv("TC_TEST_ENOBUFS");
    if (!which || strcmp(which, call)) return 0;
    errno = ENOBUFS;
    return 1;
}
#endif
