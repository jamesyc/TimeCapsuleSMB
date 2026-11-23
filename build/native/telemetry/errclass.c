#include "telemetry.h"
#include "errclass.h"

/* Counters only: they go out in the health object, and nothing decides on
 * them. A static is enough, since every failure happens in this process. */
static struct tc_failure_counts counts;

enum tc_errno_class tc_errno_class(enum tc_op op, int error) {
    if (error == EINTR) return TC_ERR_TRANSIENT;
    if (error == EMFILE || error == ENFILE || error == ENOMEM) return TC_ERR_RESOURCE;
    if (error == EAFNOSUPPORT || error == EPROTONOSUPPORT || error == EPFNOSUPPORT) return TC_ERR_FAMILY;
    switch (op) {
    case TC_OP_FORK:
    case TC_OP_PIPE:
    case TC_OP_SOCKET:
        /* Creating a process, pipe or socket fails only for want of
         * something local: EAGAIN (a full process table), ENOBUFS. */
        return TC_ERR_RESOURCE;
    case TC_OP_CONNECT:
    case TC_OP_SEND:
    case TC_OP_RECV:
        if (error == EAGAIN || error == EWOULDBLOCK || error == EINPROGRESS) return TC_ERR_TRANSIENT;
        return TC_ERR_NETWORK;
    }
    return TC_ERR_NETWORK;
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
    }
    return class;
}

void tc_failure_counts(struct tc_failure_counts *out) {
    *out = counts;
}
