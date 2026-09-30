/* Execute the real HFS growth check (source3/smbd/tc_file_growth.c, patch 0065).
 * The file's size comes from a controlled fstat and its volume from a controlled
 * fstatvfs, so smb2.rw.invalid's 16 TiB write against a 2 TB volume is decided
 * without allocating anything. The real_volume case asks the real fstat and fstatvfs
 * about a new file in $TMPDIR: on a Time Capsule's HFS disk, growth past the
 * available space is refused, and the check itself never writes. real_resource_fork
 * does the same through a resource fork's own descriptor, which is what an
 * AFP_Resource handle checks.
 *
 * The call_* cases run the four patched callers, cut from this source tree by
 * tests/samba/run.py stage() (tc_file_growth_callers.inc), with the I/O below them
 * replaced: a refused request must fail before any write, truncate or copy starts. */
#include "includes.h"
#include "system/filesys.h"
#include "smbd/smbd.h"
#include "smbd/globals.h"
#include "smbd/fd_handle.h"
#include "lib/util/tevent_unix.h"
#include "lib/util/tevent_ntstatus.h"
#include "librpc/gen_ndr/ndr_ioctl.h"
#include <sys/statvfs.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: %s errno=%d\n", __FILE__, __LINE__, #x, errno); exit(90); } } while (0)

#define MiB ((off_t)1024 * 1024)
#define GiB (1024 * MiB)
#define TiB (1024 * GiB)
/* [MS-FSA] MAXFILESIZE, as in smbd's vfs_valid_allocation_range(). */
#define MAXFILESIZE ((off_t)0xfffffff0000)

/* What the replaced fstat and fstatvfs report, and how often they are asked. */
static off_t disk_size;
static int fstat_error;
/* A failed fstat that leaves errno 0 (a VFS module that forgets to set it). */
static bool fstat_errno_zero;
/* A server-side copy's source and its size. */
static struct files_struct *src_handle;
static off_t src_size;
/* The I/O the callers reach. */
static unsigned pwrite_calls, pwrite_send_calls, ftruncate_calls, fast_copy_calls;
static off_t ftruncate_len;
static const char *fstype = "hfs";
static uint64_t avail_blocks;
static unsigned long frsize = 4096, bsize = 4096;
static int statvfs_error;
static unsigned fstat_calls, statvfs_calls;
static bool real_volume;

static NTSTATUS test_stat_fsp(files_struct *fsp)
{
	struct stat st;

	if (fsp == src_handle) {
		fsp->fsp_name->st.st_ex_size = src_size;
		return NT_STATUS_OK;
	}
	fstat_calls++;
	if (real_volume) {
		if (fstat(fsp_get_io_fd(fsp), &st) != 0) {
			return map_nt_error_from_unix(errno);
		}
		fsp->fsp_name->st.st_ex_size = st.st_size;
		return NT_STATUS_OK;
	}
	if (fstat_error) {
		/* As vfs_stat_fsp(): errno stays fstat's. */
		errno = fstat_error;
		return map_nt_error_from_unix(fstat_error);
	}
	if (fstat_errno_zero) {
		errno = 0;
		return NT_STATUS_UNSUCCESSFUL;
	}
	fsp->fsp_name->st.st_ex_size = disk_size;
	return NT_STATUS_OK;
}

static int test_fstatvfs(int fd, struct statvfs *sv)
{
	statvfs_calls++;
	if (real_volume) {
		return fstatvfs(fd, sv);
	}
	if (statvfs_error) {
		errno = statvfs_error;
		return -1;
	}
	memset(sv, 0, sizeof(*sv));
	sv->f_bavail = avail_blocks;
	sv->f_frsize = frsize;
	sv->f_bsize = bsize;
	return 0;
}

/* Only NetBSD's statvfs names the filesystem; elsewhere a real volume is not HFS. */
static const char *test_fstype(const struct statvfs *sv)
{
	if (!real_volume) {
		return fstype;
	}
#ifdef __NetBSD__
	return sv->f_fstypename;
#else
	(void)sv;
	return "";
#endif
}

#define vfs_stat_fsp test_stat_fsp
#define TC_GROWTH_FSTATVFS test_fstatvfs
#define TC_GROWTH_FSTYPE(sv) test_fstype(sv)
/* smbd_base links the production copy; exercise this one under a distinct name. */
#define tc_file_growth_check t_file_growth_check
#include "smbd/tc_file_growth.c"

/* The I/O below the callers. Each counts its calls: a refused request reaches none. */
static ssize_t test_pwrite_data(struct smb_request *req, files_struct *fsp,
				const char *data, size_t n, off_t pos)
{
	pwrite_calls++;
	return n;
}

struct test_pwrite_state {
	ssize_t n;
};

static struct tevent_req *test_pwrite_send(TALLOC_CTX *mem_ctx, struct tevent_context *ev,
					   files_struct *fsp, const void *data, size_t n, off_t off)
{
	struct tevent_req *req = NULL;
	struct test_pwrite_state *state = NULL;

	pwrite_send_calls++;
	req = tevent_req_create(mem_ctx, &state, struct test_pwrite_state);
	if (req == NULL) {
		return NULL;
	}
	state->n = n;
	tevent_req_done(req);
	return tevent_req_post(req, ev);
}

static ssize_t test_pwrite_recv(struct tevent_req *req, struct vfs_aio_state *aio_state)
{
	struct test_pwrite_state *state = tevent_req_data(req, struct test_pwrite_state);

	ZERO_STRUCTP(aio_state);
	return state->n;
}

static int test_ftruncate(files_struct *fsp, off_t len)
{
	ftruncate_calls++;
	ftruncate_len = len;
	return 0;
}

static NTSTATUS test_fetch_src(struct files_struct **fsp)
{
	*fsp = src_handle;
	return NT_STATUS_OK;
}

#undef SMB_VFS_PWRITE_SEND
#define SMB_VFS_PWRITE_SEND(ctx, ev, fsp, data, n, off) test_pwrite_send(ctx, ev, fsp, data, n, off)
#undef SMB_VFS_PWRITE_RECV
#define SMB_VFS_PWRITE_RECV(req, aio_state) test_pwrite_recv(req, aio_state)
#undef SMB_VFS_FTRUNCATE
#define SMB_VFS_FTRUNCATE(fsp, len) test_ftruncate(fsp, len)
#define vfs_pwrite_data test_pwrite_data
#define vfs_fill_sparse(fsp, len) 0
#define lp_strict_allocate(snum) false
#define lp_strict_sync(snum) false
#define lp_sync_always(snum) false
#define contend_level2_oplocks_begin(fsp, type) do { } while (0)
#define contend_level2_oplocks_end(fsp, type) do { } while (0)
#define notify_fname(conn, action, filter, name, lease) do { } while (0)
#define vfs_offload_token_ctx_init(client, ctx) NT_STATUS_OK
#define vfs_offload_token_db_fetch_fsp(ctx, token, fsp) test_fetch_src(fsp)
#define vfs_offload_token_check_handles(fsctl, src, dst) NT_STATUS_OK
#define change_to_user_and_service_by_fsp(fsp) true
/* smbd_base links the production copies of the non-static ones. */
#define pwrite_fsync_send t_pwrite_fsync_send
#define pwrite_fsync_recv t_pwrite_fsync_recv
#define vfs_set_filelen t_set_filelen
#include "tc_file_growth_callers.inc"

/* Where vfswrap_offload_write_send() hands over to the copy. */
static NTSTATUS vfswrap_offload_fast_copy(struct tevent_req *req, int fsctl)
{
	fast_copy_calls++;
	return NT_STATUS_OK;
}

static NTSTATUS vfswrap_offload_write_loop(struct tevent_req *req)
{
	return NT_STATUS_INTERNAL_ERROR;
}

static void note_done(struct tevent_req *req)
{
	*(bool *)tevent_req_callback_data_void(req) = true;
}

static void note_timeout(struct tevent_context *ev, struct tevent_timer *te,
			 struct timeval now, void *private_data)
{
	*(bool *)private_data = true;
}

/*
 * Wait for req's callback, as smbd does, for at most 5 seconds. tevent also
 * calls it for a request posted while still in progress (an error of 0),
 * which tevent_req_poll() would wait for forever.
 */
static bool finished(struct tevent_req *req, struct tevent_context *ev)
{
	TALLOC_CTX *tmp = talloc_new(NULL);
	bool done = false, timed_out = false;

	CHECK(tmp != NULL);
	CHECK(tevent_add_timer(ev, tmp, timeval_current_ofs(5, 0), note_timeout, &timed_out) != NULL);
	tevent_req_set_callback(req, note_done, &done);
	while (!done && !timed_out && tevent_loop_once(ev) == 0) {
	}
	TALLOC_FREE(tmp);
	return done;
}

static void calls_reset(void)
{
	pwrite_calls = pwrite_send_calls = ftruncate_calls = fast_copy_calls = 0;
	ftruncate_len = -1;
}

struct handle {
	struct files_struct fsp;
};

static void handle_open(TALLOC_CTX *ctx, struct handle *h, int fd, off_t cached_size)
{
	ZERO_STRUCTP(h);
	h->fsp.fh = fd_handle_create(ctx);
	CHECK(h->fsp.fh != NULL);
	fsp_set_fd(&h->fsp, fd);
	h->fsp.fsp_name = synthetic_smb_fname(ctx, "growth.bin", NULL, NULL, 0, 0);
	CHECK(h->fsp.fsp_name != NULL);
	h->fsp.fsp_name->st.st_ex_size = cached_size;
}

static void handle_close(struct handle *h)
{
	fsp_set_fd(&h->fsp, -1); /* fd_handle's destructor insists */
}

/* A volume with this many bytes available, in 4 KiB blocks. */
static void volume(const char *type, uint64_t avail_bytes)
{
	fstype = type;
	frsize = 4096;
	bsize = 4096;
	avail_blocks = avail_bytes / 4096;
	statvfs_error = 0;
	fstat_error = 0;
	fstat_calls = statvfs_calls = 0;
}

/* Decide growth to end; the file is disk_size bytes, smbd last saw cached. */
static int grow(struct handle *h, off_t cached, off_t on_disk, off_t end)
{
	int ret;

	h->fsp.fsp_name->st.st_ex_size = cached;
	disk_size = on_disk;
	errno = 0;
	ret = t_file_growth_check(&h->fsp, end);
	return ret;
}

int main(int argc, char **argv)
{
	TALLOC_CTX *frame = talloc_stackframe();
	struct handle h;
	const char *c;
	bool all;
	/* A stand-in descriptor: only the real_volume case uses it for I/O. */
	int fd = open("/dev/null", O_RDONLY);

	CHECK(argc == 2);
	CHECK(fd != -1);
	all = strcmp(argv[1], "all") == 0;
	c = argv[1];
	handle_open(frame, &h, fd, 0);

	if (all || strcmp(c, "unchecked") == 0) {
		/* Appends, overwrites, shrinks and holes up to 64 MiB ask nothing at all. */
		volume("hfs", 0);
		CHECK(grow(&h, 0, 0, 8 * MiB) == 0);
		CHECK(grow(&h, 5 * GiB, 5 * GiB, 5 * GiB + 8 * MiB) == 0);
		CHECK(grow(&h, 5 * GiB, 5 * GiB, 1 * GiB) == 0);
		CHECK(grow(&h, 5 * GiB, 5 * GiB, 5 * GiB) == 0);
		CHECK(grow(&h, 0, 0, 64 * MiB) == 0);
		CHECK(fstat_calls == 0 && statvfs_calls == 0);
		/* One byte more than the unchecked growth reads the file's size. */
		CHECK(grow(&h, 0, 0, 64 * MiB + 1) == -1 && errno == ENOSPC);
		CHECK(fstat_calls == 1 && statvfs_calls == 1);
	}
	if (all || strcmp(c, "stale_size") == 0) {
		/* smbd's own writes grew the file past the size it cached: refresh it,
		 * and the growth is only the request's. */
		volume("hfs", 0);
		CHECK(grow(&h, 0, 3 * GiB, 3 * GiB + 8 * MiB) == 0);
		CHECK(fstat_calls == 1 && statvfs_calls == 0);
		CHECK(h.fsp.fsp_name->st.st_ex_size == 3 * GiB);
		/* The refreshed size is the next request's starting point. */
		CHECK(grow(&h, h.fsp.fsp_name->st.st_ex_size, 3 * GiB + 8 * MiB,
			   3 * GiB + 16 * MiB) == 0);
		CHECK(fstat_calls == 1 && statvfs_calls == 0);
	}
	if (all || strcmp(c, "fits") == 0) {
		/* A Windows copy sets the end of file to its final size first. */
		volume("hfs", 500 * GiB);
		CHECK(grow(&h, 0, 0, 100 * GiB) == 0);
		CHECK(fstat_calls == 1 && statvfs_calls == 1);
		CHECK(grow(&h, 10 * GiB, 10 * GiB, 510 * GiB) == 0);
		/* Time Machine starts a band (487,854,080 bytes on the devices'
		 * sparse bundles) with a write near its end. */
		volume("hfs", 500 * GiB);
		CHECK(grow(&h, 0, 0, 487854080) == 0);
		CHECK(fstat_calls == 1 && statvfs_calls == 1);
	}
	if (all || strcmp(c, "exceeds") == 0) {
		/* smb2.rw.invalid: 64 KiB written, then one byte at MAXFILESIZE - 1,
		 * on a 2 TB disk with 1.5 TB available. */
		volume("hfs", 1536 * GiB);
		debuglevel_set(10); /* format the refusal's log line too */
		CHECK(grow(&h, 64 * 1024, 64 * 1024, MAXFILESIZE) == -1);
		CHECK(errno == ENOSPC);
		CHECK(fstat_calls == 1 && statvfs_calls == 1);
		debuglevel_set(0);
		/* SET_INFO end-of-file past the available space. */
		volume("hfs", 100 * GiB);
		CHECK(grow(&h, 0, 0, 101 * GiB) == -1 && errno == ENOSPC);
		/* A stale cached size cannot hide the growth. */
		CHECK(grow(&h, 0, 40 * GiB, 141 * GiB) == -1 && errno == ENOSPC);
		CHECK(grow(&h, 0, 40 * GiB, 140 * GiB) == 0);
		/* Nothing available: only growth within the unchecked margin passes. */
		volume("hfs", 0);
		CHECK(grow(&h, 0, 0, 64 * MiB) == 0);
		CHECK(grow(&h, 0, 0, 65 * MiB) == -1 && errno == ENOSPC);
	}
	if (all || strcmp(c, "boundary") == 0) {
		/* Available space is f_bavail fragments of f_frsize bytes. */
		volume("hfs", 1 * GiB);
		CHECK(grow(&h, 0, 0, 1 * GiB) == 0);
		CHECK(grow(&h, 0, 0, 1 * GiB + 1) == -1 && errno == ENOSPC);
		frsize = 512;
		avail_blocks = 2 * 1024 * 1024; /* 1 GiB */
		CHECK(grow(&h, 0, 0, 1 * GiB) == 0);
		CHECK(grow(&h, 0, 0, 1 * GiB + 1) == -1 && errno == ENOSPC);
		/* A filesystem that leaves f_frsize zero counts in f_bsize. */
		frsize = 0;
		bsize = 8192;
		avail_blocks = 128 * 1024; /* 1 GiB */
		CHECK(grow(&h, 0, 0, 1 * GiB) == 0);
		CHECK(grow(&h, 0, 0, 1 * GiB + 1) == -1 && errno == ENOSPC);
		/* The growth counts from the current end, not from zero. */
		volume("hfs", 1 * GiB);
		CHECK(grow(&h, 7 * TiB, 7 * TiB, 7 * TiB + 1 * GiB) == 0);
		CHECK(grow(&h, 7 * TiB, 7 * TiB, 7 * TiB + 1 * GiB + 1) == -1);
	}
	if (all || strcmp(c, "not_hfs") == 0) {
		/* Filesystems with sparse files keep upstream behaviour. */
		volume("ffs", 1 * GiB);
		CHECK(grow(&h, 0, 0, MAXFILESIZE) == 0);
		CHECK(fstat_calls == 1 && statvfs_calls == 1);
		volume("hfsplus", 1 * GiB);
		CHECK(grow(&h, 0, 0, MAXFILESIZE) == 0);
		volume("", 1 * GiB);
		CHECK(grow(&h, 0, 0, MAXFILESIZE) == 0);
	}
	if (all || strcmp(c, "no_volume") == 0) {
		/* A stream's placeholder descriptor (a pipe) has no volume to ask. */
		volume("hfs", 1 * GiB);
		statvfs_error = EINVAL;
		CHECK(grow(&h, 0, 0, MAXFILESIZE) == 0);
		CHECK(statvfs_calls == 1);
		/* Neither has a handle without a descriptor. */
		volume("hfs", 1 * GiB);
		fsp_set_fd(&h.fsp, -1);
		CHECK(grow(&h, 0, 0, MAXFILESIZE) == 0);
		CHECK(fstat_calls == 1 && statvfs_calls == 0);
		fsp_set_fd(&h.fsp, fd);
	}
	if (all || strcmp(c, "fstat_error") == 0) {
		/* Growth whose size cannot be read fails with fstat's error. */
		volume("hfs", 1 * TiB);
		fstat_error = EACCES;
		CHECK(grow(&h, 0, 0, 1 * GiB) == -1 && errno == EACCES);
		CHECK(statvfs_calls == 0);
		/* Small growth never reads it. */
		CHECK(grow(&h, 0, 0, 1 * MiB) == 0);
		fstat_error = 0;
		/* A failure that leaves errno 0 still reports an error. */
		fstat_errno_zero = true;
		CHECK(grow(&h, 0, 0, 1 * GiB) == -1 && errno == EIO);
		fstat_errno_zero = false;
	}
	if (all || strcmp(c, "real_volume") == 0) {
		/* The real system calls on a new file where the driver runs. */
		char dir[PATH_MAX], path[PATH_MAX + 16];
		struct statvfs sv;
		struct stat st;
		struct handle r;
		uint64_t avail;
		bool hfs = false;
		int rfd, ret;

		CHECK(snprintf(dir, sizeof(dir), "%s/tc-file-growth.XXXXXX",
			       getenv("TMPDIR") ? getenv("TMPDIR") : ".") > 0);
		CHECK(mkdtemp(dir) != NULL);
		CHECK(snprintf(path, sizeof(path), "%s/growth.bin", dir) > 0);
		rfd = open(path, O_RDWR | O_CREAT | O_EXCL, 0600);
		CHECK(rfd != -1);
		CHECK(pwrite(rfd, "x", 1, 0) == 1);
		CHECK(fstatvfs(rfd, &sv) == 0);
		avail = (uint64_t)sv.f_bavail * (sv.f_frsize != 0 ? sv.f_frsize : sv.f_bsize);
#ifdef __NetBSD__
		hfs = strcmp(sv.f_fstypename, "hfs") == 0;
		fprintf(stderr, "real_volume: %s, %" PRIu64 " bytes available\n",
			sv.f_fstypename, avail);
#endif
		real_volume = true;
		handle_open(frame, &r, rfd, 0);
		fstat_calls = statvfs_calls = 0;
		/* Past the available space: refused on HFS, allowed elsewhere. */
		errno = 0;
		ret = t_file_growth_check(&r.fsp, 1 + (off_t)avail + 1 * GiB);
		CHECK(hfs ? (ret == -1 && errno == ENOSPC) : ret == 0);
		CHECK(fstat_calls == 1 && statvfs_calls == 1);
		CHECK(r.fsp.fsp_name->st.st_ex_size == 1);
		/* Within it: allowed. */
		if (avail > 128 * MiB) {
			CHECK(t_file_growth_check(&r.fsp, 1 + 128 * MiB) == 0);
		}
		/* The check allocated nothing. */
		CHECK(fstat(rfd, &st) == 0 && st.st_size == 1);
		handle_close(&r);
		real_volume = false;
		CHECK(close(rfd) == 0);
		CHECK(unlink(path) == 0 && rmdir(dir) == 0);
	}
	if (all || strcmp(c, "real_resource_fork") == 0) {
		/* Patch 0056 opens a file's resource fork on HFS as <file>/..namedfork/rsrc
		 * and serves AFP_Resource from that descriptor, so the growth check sees it:
		 * its fstat must give the fork's size, not the file's, and its volume must
		 * be HFS. Elsewhere there is no such path. */
		char dir[PATH_MAX], path[PATH_MAX + 16], fork[PATH_MAX + 32];
		struct statvfs sv;
		struct stat st;
		struct handle r;
		uint64_t avail;
		bool hfs = false;
		int dfd, rfd, ret;

		CHECK(snprintf(dir, sizeof(dir), "%s/tc-file-growth.XXXXXX",
			       getenv("TMPDIR") ? getenv("TMPDIR") : ".") > 0);
		CHECK(mkdtemp(dir) != NULL);
		CHECK(snprintf(path, sizeof(path), "%s/growth.bin", dir) > 0);
		CHECK(snprintf(fork, sizeof(fork), "%s/..namedfork/rsrc", path) > 0);
		dfd = open(path, O_RDWR | O_CREAT | O_EXCL, 0600);
		CHECK(dfd != -1 && pwrite(dfd, "x", 1, 0) == 1);
		CHECK(fstatvfs(dfd, &sv) == 0);
#ifdef __NetBSD__
		hfs = strcmp(sv.f_fstypename, "hfs") == 0;
#endif
		rfd = open(fork, O_RDWR);
		CHECK(hfs == (rfd != -1));
		if (hfs) {
			CHECK(pwrite(rfd, "resource!!", 10, 0) == 10);
			CHECK(fstat(rfd, &st) == 0 && st.st_size == 10);
			CHECK(fstatvfs(rfd, &sv) == 0);
			avail = (uint64_t)sv.f_bavail * (sv.f_frsize != 0 ? sv.f_frsize : sv.f_bsize);
			fprintf(stderr, "real_resource_fork: %" PRIu64 " bytes available\n", avail);
			real_volume = true;
			handle_open(frame, &r, rfd, 0);
			fstat_calls = statvfs_calls = 0;
			errno = 0;
			ret = t_file_growth_check(&r.fsp, 10 + (off_t)avail + 1 * GiB);
			CHECK(ret == -1 && errno == ENOSPC);
			CHECK(fstat_calls == 1 && statvfs_calls == 1);
			CHECK(r.fsp.fsp_name->st.st_ex_size == 10);
			if (avail > 128 * MiB) {
				CHECK(t_file_growth_check(&r.fsp, 10 + 128 * MiB) == 0);
			}
			CHECK(fstat(rfd, &st) == 0 && st.st_size == 10);
			handle_close(&r);
			real_volume = false;
			CHECK(close(rfd) == 0);
		} else {
			fprintf(stderr, "real_resource_fork: not HFS, no resource fork to check\n");
		}
		CHECK(close(dfd) == 0);
		CHECK(unlink(path) == 0 && rmdir(dir) == 0);
	}
	if (all || strcmp(c, "call_write") == 0) {
		/* Synchronous writes, including every stream write (real_write_file). */
		volume("hfs", 1 * GiB);
		calls_reset();
		h.fsp.fsp_name->st.st_ex_size = disk_size = 64 * 1024;
		errno = 0;
		CHECK(real_write_file(NULL, &h.fsp, "x", MAXFILESIZE - 1, 1) == -1 && errno == ENOSPC);
		CHECK(pwrite_calls == 0);
		CHECK(real_write_file(NULL, &h.fsp, "x", 512 * MiB, 1) == 1 && pwrite_calls == 1);
		/* A zero-length write asks nothing, wherever it is. */
		volume("hfs", 0);
		CHECK(real_write_file(NULL, &h.fsp, "", MAXFILESIZE, 0) == 0);
		CHECK(fstat_calls == 0 && statvfs_calls == 0);
		/* A POSIX append has no offset to check. */
		h.fsp.fsp_flags.posix_append = true;
		CHECK(real_write_file(NULL, &h.fsp, "x", VFS_PWRITE_APPEND_OFFSET, 1) == 1);
		CHECK(fstat_calls == 0 && statvfs_calls == 0 && pwrite_calls == 2);
		h.fsp.fsp_flags.posix_append = false;
	}
	if (all || strcmp(c, "call_pwrite_send") == 0) {
		/* Asynchronous writes (aio_fork): the refusal is the request's result. */
		struct tevent_context *ev = tevent_context_init(frame);
		struct tevent_req *req = NULL;
		int err = 0;

		CHECK(ev != NULL);
		volume("hfs", 1 * GiB);
		calls_reset();
		h.fsp.fsp_name->st.st_ex_size = disk_size = 64 * 1024;
		req = t_pwrite_fsync_send(frame, ev, &h.fsp, "x", 1, MAXFILESIZE - 1, false);
		CHECK(req != NULL && finished(req, ev));
		CHECK(t_pwrite_fsync_recv(req, &err) == -1 && err == ENOSPC);
		CHECK(pwrite_send_calls == 0);
		TALLOC_FREE(req);
		req = t_pwrite_fsync_send(frame, ev, &h.fsp, "x", 1, 512 * MiB, false);
		CHECK(req != NULL && finished(req, ev));
		CHECK(t_pwrite_fsync_recv(req, &err) == 1 && pwrite_send_calls == 1);
		TALLOC_FREE(req);
		/* A size that cannot be read, with errno left 0, fails the write with
		 * EIO: tevent ignores an error of 0 and would finish the request while
		 * it is still in progress. */
		fstat_errno_zero = true;
		h.fsp.fsp_name->st.st_ex_size = 0;
		req = t_pwrite_fsync_send(frame, ev, &h.fsp, "x", 1, 512 * MiB, false);
		CHECK(req != NULL && finished(req, ev));
		CHECK(t_pwrite_fsync_recv(req, &err) == -1 && err == EIO);
		CHECK(pwrite_send_calls == 1);
		fstat_errno_zero = false;
		TALLOC_FREE(req);
		TALLOC_FREE(ev);
	}
	if (all || strcmp(c, "call_set_filelen") == 0) {
		/* SET_INFO end-of-file (vfs_set_filelen). */
		volume("hfs", 1 * GiB);
		calls_reset();
		h.fsp.fsp_name->st.st_ex_size = disk_size = 0;
		errno = 0;
		CHECK(t_set_filelen(&h.fsp, 2 * GiB) == -1 && errno == ENOSPC);
		CHECK(ftruncate_calls == 0);
		CHECK(t_set_filelen(&h.fsp, 512 * MiB) == 0);
		CHECK(ftruncate_calls == 1 && ftruncate_len == 512 * MiB);
		/* Shrinking never asks the volume. */
		volume("hfs", 0);
		h.fsp.fsp_name->st.st_ex_size = disk_size = 512 * MiB;
		CHECK(t_set_filelen(&h.fsp, 0) == 0 && ftruncate_len == 0);
		CHECK(fstat_calls == 0 && statvfs_calls == 0);
	}
	if (all || strcmp(c, "call_offload") == 0) {
		/* Server-side copies (vfswrap_offload_write_send). */
		struct tevent_context *ev = tevent_context_init(frame);
		struct smbd_server_connection sconn;
		struct connection_struct conn;
		struct tevent_req *req = NULL;
		DATA_BLOB token = data_blob_null;
		struct handle src;
		NTSTATUS status;

		CHECK(ev != NULL);
		ZERO_STRUCT(sconn);
		ZERO_STRUCT(conn);
		conn.sconn = &sconn;
		handle_open(frame, &src, fd, 0);
		src.fsp.conn = &conn;
		src_handle = &src.fsp;
		src_size = 1 * MiB;
		volume("hfs", 1 * GiB);
		calls_reset();
		h.fsp.fsp_name->st.st_ex_size = disk_size = 0;
		req = vfswrap_offload_write_send(NULL, frame, ev, FSCTL_SRV_COPYCHUNK_WRITE, &token,
						 0, &h.fsp, 2 * GiB, 1);
		CHECK(req != NULL && finished(req, ev));
		CHECK(tevent_req_is_nterror(req, &status) && NT_STATUS_EQUAL(status, NT_STATUS_DISK_FULL));
		CHECK(fast_copy_calls == 0);
		TALLOC_FREE(req);
		req = vfswrap_offload_write_send(NULL, frame, ev, FSCTL_SRV_COPYCHUNK_WRITE, &token,
						 0, &h.fsp, 512 * MiB, 1);
		CHECK(req != NULL && finished(req, ev));
		CHECK(!tevent_req_is_nterror(req, &status) && fast_copy_calls == 1);
		TALLOC_FREE(req);
		/* errno left 0 still fails the copy (map_nt_error_from_unix(0) is
		 * UNSUCCESSFUL, so this path never completed as a success). */
		fstat_errno_zero = true;
		req = vfswrap_offload_write_send(NULL, frame, ev, FSCTL_SRV_COPYCHUNK_WRITE, &token,
						 0, &h.fsp, 512 * MiB, 1);
		CHECK(req != NULL && finished(req, ev));
		CHECK(tevent_req_is_nterror(req, &status) && !NT_STATUS_IS_OK(status));
		CHECK(fast_copy_calls == 1);
		fstat_errno_zero = false;
		TALLOC_FREE(req);
		src_handle = NULL;
		handle_close(&src);
		TALLOC_FREE(ev);
	}

	handle_close(&h);
	close(fd);
	TALLOC_FREE(frame);
	return 0;
}
