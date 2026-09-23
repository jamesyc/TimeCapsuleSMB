/* Behavioral tests of the final patched VFS, using real talloc, tevent,
 * socketpairs, mappings and workers. Only syscall failures are injected. */
#include "includes.h"
#include "smbd/smbd.h"
#include "smbd/globals.h"
#include "lib/async_req/async_sock.h"
#include "lib/util/sys_rw.h"
#include "lib/util/sys_rw_data.h"
#include "lib/global_contexts.h"
#include <sys/wait.h>
#ifdef HAVE_PTHREAD
#error These regressions must exercise the no-pthread appliance build.
#endif

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #x); exit(90); } } while (0)

static struct tevent_context *event;
static int io_fd, fail_fork, fail_send, fail_read, fail_allocation, io_error;
static bool oversized_reply;
static pid_t workers[128];
static size_t worker_count;
static unsigned destructors;
static unsigned inherited_destructors;

static pid_t controlled_fork(void)
{
	pid_t pid;
	if (fail_fork) { errno = EAGAIN; return -1; }
	pid = fork();
	if (pid > 0) { CHECK(worker_count < ARRAY_SIZE(workers)); workers[worker_count++] = pid; }
	return pid;
}

static int mark_destroyed(int *marker)
{
	(void)marker;
	destructors++;
	return 0;
}

static int mark_inherited_destroyed(int *marker)
{
	(void)marker;
	inherited_destructors++;
	return 0;
}

static struct files_struct *check_child_stack(struct smbd_server_connection *sconn,
	struct files_struct *(*fn)(struct files_struct *, void *), void *data)
{
	TALLOC_CTX *frame;
	(void)sconn; (void)fn; (void)data;
	alarm(15);
	/* Called by create_aio_child after its actual fork/reset path. */
	CHECK(!talloc_stackframe_exists());
	CHECK(destructors == 0);
	frame = talloc_stackframe();
	CHECK(talloc_stackframe_exists());
	TALLOC_FREE(frame);
	CHECK(!talloc_stackframe_exists() && destructors == 0);
	return NULL;
}

static ssize_t controlled_pread(int fd, void *buf, size_t count, off_t offset)
{
	if (io_error) { errno = io_error; return -1; }
	if (oversized_reply) return count + 1;
	return sys_pread_full(fd, buf, count, offset);
}

static ssize_t controlled_pwrite(int fd, const void *buf, size_t count, off_t offset)
{
	if (io_error) { errno = io_error; return -1; }
	return sys_pwrite_full(fd, buf, count, offset);
}

static ssize_t controlled_write(int fd, const void *buf, size_t count)
{
	if (io_error) { errno = io_error; return -1; }
	return sys_write_full(fd, buf, count);
}

static int controlled_fsync(int fd)
{
	if (io_error) { errno = io_error; return -1; }
	return fsync(fd);
}

static ssize_t controlled_sendmsg(int fd, const struct msghdr *msg, int flags)
{
	if (fail_send) { fail_send--; errno = EPIPE; return -1; }
	return sendmsg(fd, msg, flags);
}

static struct tevent_req *controlled_packet_send(TALLOC_CTX *ctx,
	struct tevent_context *ev, int fd, size_t initial,
	ssize_t (*more)(uint8_t *, size_t, void *), void *data)
{
	if (fail_allocation) { fail_allocation--; return NULL; }
	return read_packet_send(ctx, ev, fd, initial, more, data);
}

static ssize_t controlled_packet_recv(struct tevent_req *req, TALLOC_CTX *ctx,
	uint8_t **buf, int *error)
{
	if (fail_read) { fail_read--; *error = EIO; return -1; }
	return read_packet_recv(req, ctx, buf, error);
}

#define fork controlled_fork
#define files_forall check_child_stack
#define fsp_get_io_fd(fsp) io_fd
#define global_event_context() event
#define sys_pread_full controlled_pread
#define sys_pwrite_full controlled_pwrite
#define sys_write_full controlled_write
#define fsync controlled_fsync
#define sendmsg controlled_sendmsg
#define read_packet_send controlled_packet_send
#define read_packet_recv controlled_packet_recv
#undef vfs_aio_fork_init
#define vfs_aio_fork_init regression_vfs_aio_fork_init
NTSTATUS vfs_aio_fork_init(TALLOC_CTX *ctx);
#include "vfs_aio_fork.c"
#undef fork
#undef fsync
#undef sys_pread_full
#undef sys_pwrite_full
#undef sys_write_full

/* Exercise the production connection-child cleanup with real talloc and
 * tevent objects. server.c itself owns main(), so stage only this helper. */
struct smbd_open_socket;
struct smbd_parent_context {
	struct smbd_open_socket *sockets;
};
struct smbd_open_socket {
	struct smbd_open_socket *prev, *next;
	struct smbd_parent_context *parent;
	int fd;
	struct tevent_fd *fde;
};
#include "tc_smbd_child_detach_parent.inc"

static unsigned listener_parent_frees, listener_closes;

static int listener_parent_destructor(struct smbd_parent_context *parent)
{
	(void)parent;
	listener_parent_frees++;
	return 0;
}

static void listener_close(struct tevent_context *ev, struct tevent_fd *fde,
			   int fd, void *private_data)
{
	(void)ev; (void)fde; (void)private_data;
	listener_closes++;
	close(fd);
}

static void listener_signal(struct tevent_context *ev, struct tevent_signal *se,
			    int signum, int count, void *siginfo, void *private_data)
{
	(void)ev; (void)se; (void)signum; (void)count;
	(void)siginfo; (void)private_data;
}

struct listener_handoff {
	struct smbd_parent_context *parent;
	int listeners[2];
	int client;
	int done;
	struct sigaction original_signal;
};

static void listener_accept(struct tevent_context *ev, struct tevent_fd *fde,
			    uint16_t flags, void *private_data)
{
	struct listener_handoff *test = private_data;
	struct sigaction action;
	int accepted, status;
	pid_t pid;
	char reply[2];
	(void)fde; (void)flags;
	accepted = accept(test->listeners[0], NULL, NULL);
	CHECK(accepted >= 0);
	pid = fork();
	CHECK(pid >= 0);
	if (pid == 0) {
		alarm(15);
		smbd_child_detach_parent(test->parent);
		CHECK(listener_parent_frees == 0 && listener_closes == 0);
		CHECK(fcntl(test->listeners[0], F_GETFD) == -1 && errno == EBADF);
		CHECK(fcntl(test->listeners[1], F_GETFD) == -1 && errno == EBADF);
		CHECK(fcntl(accepted, F_GETFD) >= 0);
		CHECK(tevent_re_initialise(ev) == 0);
		CHECK(sigaction(SIGUSR1, NULL, &action) == 0);
		CHECK(action.sa_handler == test->original_signal.sa_handler);
		TALLOC_FREE(ev);
		CHECK(listener_parent_frees == 0 && listener_closes == 0);
		CHECK(write_data(accepted, "ok", 2) == 2);
		close(accepted);
		_exit(0);
	}
	CHECK(waitpid(pid, &status, 0) == pid);
	CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0);
	CHECK(read_data(test->client, reply, sizeof(reply)) == sizeof(reply));
	CHECK(memcmp(reply, "ok", sizeof(reply)) == 0);
	CHECK(fcntl(test->listeners[0], F_GETFD) >= 0);
	CHECK(fcntl(test->listeners[1], F_GETFD) >= 0);
	CHECK(listener_parent_frees == 0 && listener_closes == 0);
	close(accepted);
	test->done = 1;
}

static void test_listener_handoff(void)
{
	struct listener_handoff test = {0};
	struct sockaddr_in address = {0};
	struct sockaddr_in bind_address = {0};
	socklen_t address_size = sizeof(address);
	int i;
	CHECK(sigaction(SIGUSR1, NULL, &test.original_signal) == 0);
	test.parent = talloc_zero(event, struct smbd_parent_context);
	CHECK(test.parent != NULL);
	talloc_set_destructor(test.parent, listener_parent_destructor);
	CHECK(tevent_add_signal(event, test.parent, SIGUSR1, 0,
				listener_signal, NULL) != NULL);
	for (i = 0; i < 2; i++) {
		struct smbd_open_socket *listener = talloc_zero(
			test.parent, struct smbd_open_socket);
		CHECK(listener != NULL);
		listener->parent = test.parent;
		listener->fd = socket(AF_INET, SOCK_STREAM, 0);
		CHECK(listener->fd >= 0);
		bind_address.sin_family = AF_INET;
		bind_address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
		bind_address.sin_port = 0;
		CHECK(bind(listener->fd, (struct sockaddr *)&bind_address,
			   sizeof(bind_address)) == 0);
		CHECK(listen(listener->fd, 2) == 0);
		test.listeners[i] = listener->fd;
		listener->fde = tevent_add_fd(event, listener, listener->fd,
			TEVENT_FD_READ, listener_accept, &test);
		CHECK(listener->fde != NULL);
		tevent_fd_set_close_fn(listener->fde, listener_close);
		DLIST_ADD_END(test.parent->sockets, listener);
		if (i == 0) {
			CHECK(getsockname(listener->fd, (struct sockaddr *)&address,
				&address_size) == 0);
		}
	}
	test.client = socket(AF_INET, SOCK_STREAM, 0);
	CHECK(test.client >= 0);
	CHECK(connect(test.client, (struct sockaddr *)&address, address_size) == 0);
	CHECK(tevent_loop_once(event) == 0 && test.done);
	close(test.client);
	TALLOC_FREE(test.parent);
	CHECK(listener_parent_frees == 1 && listener_closes == 2);
}

static struct vfs_handle_struct *share(TALLOC_CTX *ctx, unsigned limit)
{
	struct vfs_handle_struct *h = talloc_zero(ctx, struct vfs_handle_struct);
	struct aio_fork_config *config = talloc_zero(h, struct aio_fork_config);
	CHECK(h && config);
	h->conn = talloc_zero(h, struct connection_struct);
	CHECK(h->conn);
	h->data = config;
	config->max_children = limit;
	return h;
}

static struct aio_child_list *pool(struct vfs_handle_struct *h)
{
	return ((struct aio_fork_config *)h->data)->children;
}

static struct aio_fork_state *request_state(struct tevent_req *req)
{
	return tevent_req_data(req, struct aio_fork_state);
}

static void completed(struct tevent_req *req);

static struct tevent_req *submit(struct vfs_handle_struct *h, char *buf,
	size_t size, off_t offset, enum cmd_type cmd)
{
	struct tevent_req *req = aio_fork_send(h, event, event, NULL, buf, size, offset, cmd);
	unsigned *calls;
	CHECK(req);
	calls = talloc_zero(req, unsigned);
	CHECK(calls);
	tevent_req_set_callback(req, completed, calls);
	return req;
}

static void completed(struct tevent_req *req)
{
	unsigned *calls = tevent_req_callback_data(req, unsigned);
	(*calls)++;
}

static void finish(struct tevent_req *req, ssize_t expected, int error)
{
	struct vfs_aio_state state = {0};
	unsigned *calls = tevent_req_callback_data(req, unsigned);
	while (!*calls) CHECK(tevent_loop_once(event) == 0);
	CHECK(*calls == 1);
	CHECK(aio_fork_recv(req, &state) == expected);
	CHECK(state.error == error);
	TALLOC_FREE(req);
}

static void test_io(struct vfs_handle_struct *h, const char *scenario)
{
	struct tevent_req *req;
	char buffer[16] = "untouched";
	enum cmd_type cmd = READ_CMD;
	ssize_t expected = 4;
	size_t size = !strcmp(scenario, "short") ? 8 : 4;
	int error = 0;
	if (!strncmp(scenario, "sync_", 5)) fail_fork = 1;
	if (strstr(scenario, "pwrite")) cmd = PWRITE_CMD;
	else if (strstr(scenario, "append")) { cmd = WRITE_CMD; CHECK(lseek(io_fd, 0, SEEK_END) == 4); }
	else if (strstr(scenario, "fsync")) { cmd = FSYNC_CMD; expected = 0; }
	if (strstr(scenario, "error")) { io_error = EBADF; error = EBADF; expected = -1; }
	if (!strcmp(scenario, "oversized")) { oversized_reply = true; error = EIO; expected = -1; }
	if (!strcmp(scenario, "empty")) expected = 0;
	if (!strcmp(scenario, "zero")) { size = 0; expected = 0; }
	req = submit(h, buffer, size, !strcmp(scenario, "empty") ? 50 : 0, cmd);
	finish(req, expected, error);
	if (cmd == READ_CMD) CHECK(!memcmp(buffer, expected > 0 ? "data" : "unto", 4));
	if (cmd == READ_CMD) CHECK(!memcmp(buffer + 4, "uched", 6));
	if (!error && (cmd == WRITE_CMD || cmd == PWRITE_CMD)) {
		char actual[4];
		CHECK(pread(io_fd, actual, 4, cmd == WRITE_CMD ? 4 : 0) == 4);
		CHECK(!memcmp(actual, "unto", 4));
	}
	if (!fail_fork) {
		CHECK(pool(h)->num_children == 1 && !pool(h)->children->busy);
		/* The same worker is reusable after success or a worker errno. */
		finish(submit(h, buffer, 0, 0, FSYNC_CMD), io_error ? -1 : 0, io_error);
		CHECK(worker_count == 1);
	}
}

static void test_queue(struct vfs_handle_struct *h, const char *scenario)
{
	struct tevent_req *requests[AIO_FORK_MAX_PENDING + 3];
	char buffers[AIO_FORK_MAX_PENDING + 3][4];
	unsigned i, count = ARRAY_SIZE(requests);
	struct aio_child_list *list;
	for (i = 0; i < count; i++) {
		requests[i] = submit(h, buffers[i], 4, 0, READ_CMD);
	}
	list = pool(h);
	CHECK(list->num_children == 1 && worker_count == 1);
	CHECK(list->num_pending == AIO_FORK_MAX_PENDING);
	CHECK(request_state(requests[0])->active);
	for (i = 1; i <= AIO_FORK_MAX_PENDING; i++) {
		CHECK(request_state(requests[i])->queued);
	}
	CHECK(!tevent_req_is_in_progress(requests[count - 1]));
	if (!strcmp(scenario, "cancel_queued")) {
		TALLOC_FREE(requests[2]);
		CHECK(list->num_pending == AIO_FORK_MAX_PENDING - 1);
	}
	if (!strcmp(scenario, "cancel_active") || !strcmp(scenario, "queued_fork_failure")) {
		if (!strcmp(scenario, "queued_fork_failure")) fail_fork = 1;
		TALLOC_FREE(requests[0]);
		CHECK(list->num_children <= 1);
	}
	for (i = 0; i < count; i++) {
		if (!requests[i]) continue;
		if (i > 0 && i <= AIO_FORK_MAX_PENDING && !fail_fork) {
			/* Completion of each preceding request must dispatch the FIFO head. */
			CHECK(request_state(requests[i])->active);
		}
		finish(requests[i], 4, 0);
		CHECK(!memcmp(buffers[i], "data", 4));
	}
	CHECK(list->num_pending == 0 && list->pending == NULL);
	fail_fork = 0;
	finish(submit(h, buffers[0], 4, 0, READ_CMD), 4, 0);
	CHECK(list->num_children == 1 && !list->children->busy);
}

static void test_failures(struct vfs_handle_struct *h, const char *scenario)
{
	char buf[4];
	struct tevent_req *first, *next;
	if (!strcmp(scenario, "dispatch_failure")) fail_send = 1;
	if (!strcmp(scenario, "allocation_failure")) fail_allocation = 1;
	if (!strcmp(scenario, "response_failure")) fail_read = 1;
	first = submit(h, buf, 4, 0, READ_CMD);
	next = submit(h, buf, 4, 0, READ_CMD);
	finish(first, -1, !strcmp(scenario, "dispatch_failure") ? EPIPE :
		!strcmp(scenario, "allocation_failure") ? ENOMEM : EIO);
	finish(next, 4, 0);
	CHECK(pool(h)->num_pending == 0 && pool(h)->num_children == 1);
	finish(submit(h, buf, 4, 0, READ_CMD), 4, 0);
}

static void test_limits(TALLOC_CTX *ctx, struct vfs_handle_struct *h, bool unlimited)
{
	char buffers[4][4];
	unsigned i;
	struct tevent_req *req[4];
	struct vfs_handle_struct *other = share(ctx, 1);
	((struct aio_fork_config *)h->data)->max_children = unlimited ? 0 : 2;
	req[0] = submit(h, buffers[0], 4, 0, READ_CMD);
	req[1] = submit(h, buffers[1], 4, 0, READ_CMD);
	req[2] = submit(h, buffers[2], 4, 0, READ_CMD);
	req[3] = submit(other, buffers[3], 4, 0, READ_CMD);
	CHECK(pool(h)->num_children == (unlimited ? 3 : 2));
	CHECK(pool(h)->num_pending == (unlimited ? 0 : 1));
	CHECK(pool(other)->num_children == 1 && pool(other)->num_pending == 0);
	for (i = 0; i < 4; i++) finish(req[i], 4, 0);
	TALLOC_FREE(other);
}

static void test_cleanup(struct vfs_handle_struct *h)
{
	char buf[4];
	struct tevent_req *req = submit(h, buf, 4, 0, READ_CMD);
	struct aio_child_list *list = pool(h);
	struct timeval now = timeval_current();
	aio_child_cleanup(event, NULL, now, list);
	CHECK(list->num_children == 1 && list->children->busy);
	finish(req, 4, 0);
	aio_child_cleanup(event, NULL, now, list);
	CHECK(list->num_children == 1 && !list->children->dont_delete);
	aio_child_cleanup(event, NULL, now, list);
	CHECK(list->num_children == 0 && list->cleanup_event == NULL);
	finish(submit(h, buf, 4, 0, READ_CMD), 4, 0);
	CHECK(list->num_children == 1 && list->cleanup_event != NULL);
}

static void test_fork_stackframes(TALLOC_CTX *root)
{
	TALLOC_CTX *inherited = talloc_stackframe();
	int *marker = talloc_zero(inherited, int);
	int mode;

	CHECK(inherited != NULL && marker != NULL);
	talloc_set_destructor(marker, mark_inherited_destroyed);
	for (mode = 0; mode < 2; mode++) {
		pid_t pid = fork();
		int status;

		CHECK(pid >= 0);
		if (pid == 0) {
			TALLOC_CTX *fresh = NULL;
			TALLOC_CTX *nested = NULL;

			alarm(15);
			talloc_stackframe_reinit_after_fork();
			CHECK(!talloc_stackframe_exists());
			if (mode != 0) {
				fresh = talloc_stackframe();
				nested = talloc_stackframe();
				CHECK(talloc_tos() == nested);
			}
			TALLOC_FREE(inherited);
			CHECK(inherited_destructors == 1);
			if (mode != 0) {
				CHECK(talloc_tos() == nested);
				TALLOC_FREE(nested);
				CHECK(talloc_tos() == fresh);
				TALLOC_FREE(fresh);
			}
			CHECK(!talloc_stackframe_exists());
			_exit(0);
		}
		CHECK(waitpid(pid, &status, 0) == pid);
		CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0);
		CHECK(inherited_destructors == 0 && talloc_tos() == inherited);
	}
	TALLOC_FREE(inherited);
	CHECK(inherited_destructors == 1 && talloc_tos() == root);
}

int main(int argc, char **argv)
{
	char path[] = "/tmp/tc-aio-test.XXXXXX";
	TALLOC_CTX *frame;
	struct vfs_handle_struct *h;
	int *marker, status;
	size_t i;
	CHECK(argc == 2);
	setup_logging(argv[0], DEBUG_STDERR);
	alarm(15);
	signal(SIGPIPE, SIG_IGN);
	frame = talloc_stackframe();
	marker = talloc_zero(frame, int);
	CHECK(marker);
	talloc_set_destructor(marker, mark_destroyed);
	event = tevent_context_init(frame);
	CHECK(event);
	io_fd = mkstemp(path);
	CHECK(io_fd >= 0 && unlink(path) == 0);
	CHECK(write(io_fd, "data", 4) == 4);
	h = share(frame, 1);
	if (!strcmp(argv[1], "queue") || !strncmp(argv[1], "cancel_", 7) ||
	    !strcmp(argv[1], "queued_fork_failure")) test_queue(h, argv[1]);
	else if (strstr(argv[1], "failure")) test_failures(h, argv[1]);
	else if (!strcmp(argv[1], "limits") || !strcmp(argv[1], "unlimited")) test_limits(frame, h, !strcmp(argv[1], "unlimited"));
	else if (!strcmp(argv[1], "cleanup")) test_cleanup(h);
	else if (!strcmp(argv[1], "fork_stack")) test_fork_stackframes(frame);
	else if (!strcmp(argv[1], "listener_handoff")) test_listener_handoff();
	else test_io(h, argv[1]);
	CHECK(destructors == 0 && talloc_tos() == frame);
	TALLOC_FREE(h);
	for (i = 0; i < worker_count; i++) {
		CHECK(waitpid(workers[i], &status, 0) == workers[i]);
		/* Retirement can close the socket while a worker is replying (2),
		 * or send the intentional short shutdown message (1). */
		CHECK(WIFEXITED(status) && (WEXITSTATUS(status) == 1 || WEXITSTATUS(status) == 2));
	}
	close(io_fd);
	TALLOC_FREE(frame);
	CHECK(destructors == 1 && !talloc_stackframe_exists());
	printf("PASS %s\n", argv[1]);
	return 0;
}
