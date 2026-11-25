#ifndef TC_TELEMETRY_ERRCLASS_H
#define TC_TELEMETRY_ERRCLASS_H

/* Whether a failed system call means the device is short of processes,
 * descriptors or memory (RESOURCE), by operation:
 *   FORK, PIPE, SOCKET  creating one fails only for want of something local
 *                       (EAGAIN from fork is a full process table, ENOBUFS),
 *                       except EAFNOSUPPORT, EPROTONOSUPPORT and EPFNOSUPPORT
 *                       (FAMILY), which say nothing about the device: a
 *                       socket of a family the kernel lacks. All three
 *                       operations are checked for them, though only
 *                       socket() returns them
 *   OPEN                only EMFILE, ENFILE and ENOMEM; ENOENT, EACCES and
 *                       the rest are about the file (OTHER)
 *   IO                  connect(), send() or recv() on a socket made, and
 *                       SO_ERROR after a connect: only ENOBUFS and ENOMEM
 *                       (a device out of mbufs or memory); refused,
 *                       unreachable, reset and the rest are the network's
 *                       (OTHER)
 * tc_errno_record also counts fork() and socket() failures, and IO ones
 * for want of buffers (with socket()'s ENOBUFS), for the heartbeat's health
 * object. */
enum tc_op { TC_OP_FORK, TC_OP_PIPE, TC_OP_SOCKET, TC_OP_OPEN, TC_OP_IO };
enum tc_errno_class { TC_ERR_OTHER, TC_ERR_RESOURCE, TC_ERR_FAMILY };

struct tc_failure_counts {
    unsigned fork_eagain, fork_enomem, fork_other;
    unsigned socket_emfile, socket_enfile, socket_enobufs, socket_other;
};

enum tc_errno_class tc_errno_class(enum tc_op op, int error);
/* tc_errno_class, counting fork() and socket() failures (FAMILY is not a
 * failure of the device and is not counted). */
enum tc_errno_class tc_errno_record(enum tc_op op, int error);
void tc_failure_counts(struct tc_failure_counts *out);
#ifdef TC_NATIVE_TEST
/* Host tests set the counters directly, e.g. all at their largest. */
void tc_failure_counts_set(const struct tc_failure_counts *in);
/* Host tests stand in for a device out of mbufs: the call named by
 * TC_TEST_ENOBUFS ("connect" or "send") fails with ENOBUFS. */
int tc_test_enobufs(const char *call);
#else
#define tc_test_enobufs(call) 0
#endif
#endif
