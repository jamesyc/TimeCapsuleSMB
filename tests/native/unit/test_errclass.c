#include "telemetry.h"
#include "errclass.h"
#include <assert.h>

int main(void) {
    struct tc_failure_counts c;
    /* fork: EAGAIN is the full process table, not a retry. */
    assert(tc_errno_class(TC_OP_FORK, EAGAIN) == TC_ERR_RESOURCE);
    assert(tc_errno_class(TC_OP_FORK, ENOMEM) == TC_ERR_RESOURCE);
    assert(tc_errno_class(TC_OP_PIPE, EMFILE) == TC_ERR_RESOURCE);
    /* socket(): descriptors, buffers or memory; a missing family is not
     * the device running short. */
    assert(tc_errno_class(TC_OP_SOCKET, EMFILE) == TC_ERR_RESOURCE);
    assert(tc_errno_class(TC_OP_SOCKET, ENFILE) == TC_ERR_RESOURCE);
    assert(tc_errno_class(TC_OP_SOCKET, ENOBUFS) == TC_ERR_RESOURCE);
    assert(tc_errno_class(TC_OP_SOCKET, EAFNOSUPPORT) == TC_ERR_FAMILY);
    assert(tc_errno_class(TC_OP_SOCKET, EPROTONOSUPPORT) == TC_ERR_FAMILY);
    /* A non-blocking socket's EAGAIN means wait; refusals and timeouts are
     * the network. */
    assert(tc_errno_class(TC_OP_SEND, EAGAIN) == TC_ERR_TRANSIENT);
    assert(tc_errno_class(TC_OP_RECV, EWOULDBLOCK) == TC_ERR_TRANSIENT);
    assert(tc_errno_class(TC_OP_CONNECT, EINPROGRESS) == TC_ERR_TRANSIENT);
    assert(tc_errno_class(TC_OP_CONNECT, ECONNREFUSED) == TC_ERR_NETWORK);
    assert(tc_errno_class(TC_OP_CONNECT, ETIMEDOUT) == TC_ERR_NETWORK);
    assert(tc_errno_class(TC_OP_CONNECT, ENETUNREACH) == TC_ERR_NETWORK);
    assert(tc_errno_class(TC_OP_SEND, EPIPE) == TC_ERR_NETWORK);
    assert(tc_errno_class(TC_OP_SEND, EMFILE) == TC_ERR_RESOURCE);
    assert(tc_errno_class(TC_OP_RECV, EINTR) == TC_ERR_TRANSIENT);

    /* Only fork() and socket() failures are counted; a missing family is not. */
    tc_failure_counts(&c);
    assert(!c.fork_eagain && !c.socket_emfile);
    assert(tc_errno_record(TC_OP_FORK, EAGAIN) == TC_ERR_RESOURCE);
    assert(tc_errno_record(TC_OP_FORK, ENOMEM) == TC_ERR_RESOURCE);
    assert(tc_errno_record(TC_OP_FORK, EPERM) == TC_ERR_RESOURCE);
    assert(tc_errno_record(TC_OP_SOCKET, EMFILE) == TC_ERR_RESOURCE);
    assert(tc_errno_record(TC_OP_SOCKET, ENFILE) == TC_ERR_RESOURCE);
    assert(tc_errno_record(TC_OP_SOCKET, ENOBUFS) == TC_ERR_RESOURCE);
    assert(tc_errno_record(TC_OP_SOCKET, EACCES) == TC_ERR_RESOURCE);
    assert(tc_errno_record(TC_OP_SOCKET, EAFNOSUPPORT) == TC_ERR_FAMILY);
    assert(tc_errno_record(TC_OP_CONNECT, ECONNREFUSED) == TC_ERR_NETWORK);
    assert(tc_errno_record(TC_OP_PIPE, EMFILE) == TC_ERR_RESOURCE);
    tc_failure_counts(&c);
    assert(c.fork_eagain == 1 && c.fork_enomem == 1 && c.fork_other == 1);
    assert(c.socket_emfile == 1 && c.socket_enfile == 1 && c.socket_enobufs == 1 && c.socket_other == 1);
    return 0;
}
