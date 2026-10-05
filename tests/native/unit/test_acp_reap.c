#include "acp.h"
#include <assert.h>
#include <signal.h>
#include <sys/select.h>
#include <sys/wait.h>
#include <unistd.h>

/* "exit": the child has exited before the collector reads its output. The
 * pump that reads EOF must reap it and finish the key, not leave that to a
 * later poll. (A loaded macOS host can release an exited child's descriptors
 * late, so reads may see no data and no EOF for a while; those pumps do not
 * count.) "closed": the child closes its output but keeps running. The
 * collector cannot wait on a descriptor, so it polls closely at first and
 * then every 100 ms. The deadlines are read at chosen times after the close:
 * a deadline read at the real time would race the scheduler. */
int main(int argc, char **argv) {
    struct acp_request request;
    struct acp_collector collector;
    char buffer[64];
    int rc, pending_after_eof = 0;
    if (argc != 2) return 2;
    memset(&request, 0, sizeof(request));
    request.key = "syAP";
    request.trim_whitespace = 1;
    request.output = buffer;
    request.capacity = sizeof(buffer);
    rc = acp_collect_begin(&collector, &request, 1, 5000, 5000);
    assert(rc == 0 && collector.active);
    if (!strcmp(argv[1], "exit")) {
        siginfo_t info;
        /* Wait for the exit without reaping: all output is then in the pipe. */
        assert(waitid(P_PID, collector.child, &info, WEXITED | WNOWAIT) == 0);
        while (!rc) {
            rc = acp_collect_pump(&collector);
            if (!rc && collector.eof)
                pending_after_eof++;
        }
        printf("pending_after_eof=%d rc=%d status=%d value=%s\n", pending_after_eof, rc, request.status, buffer);
        return 0;
    }
    if (!strcmp(argv[1], "closed")) {
        long long closed, last, soon, edge, later, capped, before, real, after;
        while (!collector.eof) {
            fd_set reads;
            int fd = acp_collect_fd(&collector);
            FD_ZERO(&reads);
            FD_SET(fd, &reads);
            assert(select(fd + 1, &reads, NULL, NULL, NULL) == 1);
            assert(acp_collect_pump(&collector) == 0);
        }
        /* Past the close-poll window the child is still running: still pending. */
        usleep(150000);
        assert(acp_collect_pump(&collector) == 0);
        /* The real-clock form reads a time between these two readings. */
        before = acp_monotonic_ms();
        real = acp_collect_deadline_ms(&collector);
        after = acp_monotonic_ms();
        closed = collector.eof_ms;
        last = collector.child_deadline_ms;
        soon = acp_collect_deadline_at(&collector, closed) - closed;
        edge = acp_collect_deadline_at(&collector, closed + 99) - (closed + 99);
        later = acp_collect_deadline_at(&collector, closed + 100) - (closed + 100);
        capped = acp_collect_deadline_at(&collector, last - 50) - (last - 50);
        printf("fd=%d soon=%lld edge=%lld later=%lld capped=%lld real_min=%lld real_max=%lld\n",
               acp_collect_fd(&collector), soon, edge, later, capped, real - after, real - before);
        acp_collect_cancel(&collector);
        return 0;
    }
    return 2;
}
