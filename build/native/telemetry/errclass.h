#ifndef TC_TELEMETRY_ERRCLASS_H
#define TC_TELEMETRY_ERRCLASS_H

/* What a failed system call says about the device, by operation: the same
 * errno means different things for fork() and for a non-blocking send().
 *   RESOURCE   the device is short of processes, descriptors or memory
 *              (EAGAIN from fork is a full process table)
 *   FAMILY     the kernel has no such address family: nothing listens there
 *   NETWORK    the path or the far end failed
 *   TRANSIENT  retry at once (EINTR, EAGAIN on a non-blocking socket)
 * Decisions use the class; tc_errno_record also counts fork() and socket()
 * failures for the heartbeat's health object. */
enum tc_op { TC_OP_FORK, TC_OP_PIPE, TC_OP_SOCKET, TC_OP_CONNECT, TC_OP_SEND, TC_OP_RECV };
enum tc_errno_class { TC_ERR_TRANSIENT, TC_ERR_RESOURCE, TC_ERR_FAMILY, TC_ERR_NETWORK };

struct tc_failure_counts {
    unsigned fork_eagain, fork_enomem, fork_other;
    unsigned socket_emfile, socket_enfile, socket_enobufs, socket_other;
};

enum tc_errno_class tc_errno_class(enum tc_op op, int error);
/* tc_errno_class, counting fork() and socket() failures (FAMILY is not a
 * failure of the device and is not counted). */
enum tc_errno_class tc_errno_record(enum tc_op op, int error);
void tc_failure_counts(struct tc_failure_counts *out);
#endif
