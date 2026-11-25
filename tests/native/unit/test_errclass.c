#include "telemetry.h"
#include "errclass.h"
#include <assert.h>

static unsigned total(const struct tc_failure_counts *c) {
    return c->fork_eagain + c->fork_enomem + c->fork_other + c->socket_emfile + c->socket_enfile +
           c->socket_enobufs + c->socket_other;
}

int main(void) {
    struct tc_failure_counts c;
    /* fork and pipe: EAGAIN is the full process table, not a retry; any
     * failure to create one is the device running short. */
    assert(tc_errno_class(TC_OP_FORK, EAGAIN) == TC_ERR_RESOURCE);
    assert(tc_errno_class(TC_OP_FORK, ENOMEM) == TC_ERR_RESOURCE);
    assert(tc_errno_class(TC_OP_PIPE, EMFILE) == TC_ERR_RESOURCE);
    assert(tc_errno_class(TC_OP_PIPE, ENFILE) == TC_ERR_RESOURCE);
    /* socket(): descriptors, buffers or memory; a missing family is not
     * the device running short. */
    assert(tc_errno_class(TC_OP_SOCKET, EMFILE) == TC_ERR_RESOURCE);
    assert(tc_errno_class(TC_OP_SOCKET, ENFILE) == TC_ERR_RESOURCE);
    assert(tc_errno_class(TC_OP_SOCKET, ENOBUFS) == TC_ERR_RESOURCE);
    assert(tc_errno_class(TC_OP_SOCKET, EAFNOSUPPORT) == TC_ERR_FAMILY);
    assert(tc_errno_class(TC_OP_SOCKET, EPROTONOSUPPORT) == TC_ERR_FAMILY);
    /* Opening a file (resolv.conf): only a shortage of descriptors or
     * memory is the device's; a missing or unreadable file is not. */
    assert(tc_errno_class(TC_OP_OPEN, EMFILE) == TC_ERR_RESOURCE);
    assert(tc_errno_class(TC_OP_OPEN, ENFILE) == TC_ERR_RESOURCE);
    assert(tc_errno_class(TC_OP_OPEN, ENOMEM) == TC_ERR_RESOURCE);
    assert(tc_errno_class(TC_OP_OPEN, ENOENT) == TC_ERR_OTHER);
    assert(tc_errno_class(TC_OP_OPEN, EACCES) == TC_ERR_OTHER);
    assert(tc_errno_class(TC_OP_OPEN, EAFNOSUPPORT) == TC_ERR_OTHER);

    /* Traffic on a socket made (connect, send, recv, SO_ERROR): only a
     * shortage of buffers or memory is the device's; the rest is the
     * network's, and a socket call never runs out of descriptors there. */
    assert(tc_errno_class(TC_OP_IO, ENOBUFS) == TC_ERR_RESOURCE);
    assert(tc_errno_class(TC_OP_IO, ENOMEM) == TC_ERR_RESOURCE);
    assert(tc_errno_class(TC_OP_IO, ECONNREFUSED) == TC_ERR_OTHER);
    assert(tc_errno_class(TC_OP_IO, ECONNRESET) == TC_ERR_OTHER);
    assert(tc_errno_class(TC_OP_IO, EHOSTUNREACH) == TC_ERR_OTHER);
    assert(tc_errno_class(TC_OP_IO, ETIMEDOUT) == TC_ERR_OTHER);
    assert(tc_errno_class(TC_OP_IO, EMFILE) == TC_ERR_OTHER);

    /* Classifying counts nothing. */
    tc_failure_counts(&c);
    assert(total(&c) == 0);

    /* Recording counts fork() and socket() failures by cause. */
    assert(tc_errno_record(TC_OP_FORK, EAGAIN) == TC_ERR_RESOURCE);
    assert(tc_errno_record(TC_OP_FORK, ENOMEM) == TC_ERR_RESOURCE);
    assert(tc_errno_record(TC_OP_FORK, EPERM) == TC_ERR_RESOURCE);
    assert(tc_errno_record(TC_OP_SOCKET, EMFILE) == TC_ERR_RESOURCE);
    assert(tc_errno_record(TC_OP_SOCKET, ENFILE) == TC_ERR_RESOURCE);
    assert(tc_errno_record(TC_OP_SOCKET, ENOBUFS) == TC_ERR_RESOURCE);
    assert(tc_errno_record(TC_OP_SOCKET, ENOMEM) == TC_ERR_RESOURCE);
    assert(tc_errno_record(TC_OP_SOCKET, EACCES) == TC_ERR_RESOURCE);
    tc_failure_counts(&c);
    assert(c.fork_eagain == 1 && c.fork_enomem == 1 && c.fork_other == 1);
    assert(c.socket_emfile == 1 && c.socket_enfile == 1 && c.socket_enobufs == 2 && c.socket_other == 1);

    /* A missing family, a pipe and a file are classified but not counted. */
    assert(tc_errno_record(TC_OP_SOCKET, EAFNOSUPPORT) == TC_ERR_FAMILY);
    assert(tc_errno_record(TC_OP_PIPE, EMFILE) == TC_ERR_RESOURCE);
    assert(tc_errno_record(TC_OP_OPEN, EMFILE) == TC_ERR_RESOURCE);
    assert(tc_errno_record(TC_OP_OPEN, ENOENT) == TC_ERR_OTHER);
    tc_failure_counts(&c);
    assert(total(&c) == 8);

    /* Traffic failures count only for want of buffers or memory, with
     * socket()'s ENOBUFS. */
    assert(tc_errno_record(TC_OP_IO, ENOBUFS) == TC_ERR_RESOURCE);
    assert(tc_errno_record(TC_OP_IO, ENOMEM) == TC_ERR_RESOURCE);
    assert(tc_errno_record(TC_OP_IO, ECONNREFUSED) == TC_ERR_OTHER);
    tc_failure_counts(&c);
    assert(total(&c) == 10 && c.socket_enobufs == 4);
    return 0;
}
