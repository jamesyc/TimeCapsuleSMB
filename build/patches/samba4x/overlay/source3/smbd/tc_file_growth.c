/*
 * Time Capsule (patch 0065): refuse to grow a file on HFS past the volume's
 * available space, before anything is allocated.
 *
 * HFS has no sparse files: a file grows only by allocating every block up to
 * its new end. smbtorture's smb2.rw.invalid writes one byte just below
 * MAXFILESIZE (16 TiB) and accepts success (a sparse file, as on Samba's
 * usual filesystems) or STATUS_DISK_FULL (Windows, where a file that is not
 * sparse cannot outgrow the free space). On the Time Capsules that write
 * never finished (2026-09-28): the NetBSD 6 smbd child ran in the kernel for
 * over 45 minutes while other processes on the disk waited, and the NetBSD 4
 * device rebooted. Answer as Windows does. smbd grows files through writes
 * (synchronous and asynchronous), SET_INFO end-of-file and server-side copies;
 * each calls tc_file_growth_check() with the new end first. Allocation-size
 * requests allocate nothing here (strict allocate = no).
 *
 * Growth of up to TC_GROWTH_UNCHECKED past the size smbd last saw needs no
 * system call, so sequential writes cost nothing extra. Larger growth
 * refreshes the file's size, then compares the growth with the space
 * fstatvfs() reports as available (what dfree.sh reports to clients). Time
 * Machine's sparse bundle bands are 465 MiB on these disks, so a write that
 * starts a band past its beginning costs one fstat and one fstatvfs. A
 * descriptor that is not on HFS, such as a stream's placeholder, keeps
 * upstream behaviour.
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 3 of the License, or
 * (at your option) any later version.
 */

#include "includes.h"
#include "system/filesys.h"
#include "smbd/smbd.h"
#include "smbd/globals.h"
#include <sys/statvfs.h>

/*
 * Eight 8 MiB SMB2 writes. Growth this small costs HFS little even when a
 * full volume refuses it, so it is never checked.
 */
#define TC_GROWTH_UNCHECKED ((off_t)64 * 1024 * 1024)

/* The regression driver replaces the volume query. */
#ifndef TC_GROWTH_FSTATVFS
#define TC_GROWTH_FSTATVFS fstatvfs
#endif
#if !defined(TC_GROWTH_FSTYPE) && defined(__NetBSD__)
#define TC_GROWTH_FSTYPE(sv) ((sv)->f_fstypename)
#endif

/*
 * The bytes still available on fd's volume, or false when fd is not on HFS
 * or its volume cannot be queried. Only NetBSD names the filesystem type;
 * elsewhere this keeps upstream behaviour.
 */
static bool tc_growth_hfs_avail(int fd, uint64_t *avail)
{
#ifdef TC_GROWTH_FSTYPE
	struct statvfs sv;

	if (fd == -1 || TC_GROWTH_FSTATVFS(fd, &sv) != 0) {
		return false;
	}
	if (strcmp(TC_GROWTH_FSTYPE(&sv), "hfs") != 0) {
		return false;
	}
	*avail = (uint64_t)sv.f_bavail *
		 (sv.f_frsize != 0 ? sv.f_frsize : sv.f_bsize);
	return true;
#else
	(void)fd;
	(void)avail;
	return false;
#endif
}

/*
 * Returns 0 when fsp may grow to end bytes, or -1 with errno ENOSPC when
 * the growth exceeds the available space on HFS (errno from fstat when the
 * current size cannot be read). end must be a validated, non-negative
 * offset; callers skip POSIX append writes, whose end is unknown.
 */
int tc_file_growth_check(struct files_struct *fsp, off_t end)
{
	off_t size = fsp->fsp_name->st.st_ex_size;
	uint64_t avail;
	NTSTATUS status;

	if (end <= size || end - size <= TC_GROWTH_UNCHECKED) {
		return 0;
	}

	/*
	 * The cached size does not follow smbd's own writes. A failed
	 * refresh leaves fstat's errno, as vfs_allocate_file_space() expects.
	 * Callers turn errno into the request's result. tevent ignores an
	 * error of 0, which would finish an asynchronous write while it is
	 * still in progress (INVALID_PARAMETER), so never return -1 without one.
	 */
	status = vfs_stat_fsp(fsp);
	if (!NT_STATUS_IS_OK(status)) {
		if (errno == 0) {
			errno = EIO;
		}
		return -1;
	}
	size = fsp->fsp_name->st.st_ex_size;
	if (end <= size || end - size <= TC_GROWTH_UNCHECKED) {
		return 0;
	}

	if (!tc_growth_hfs_avail(fsp_get_io_fd(fsp), &avail)) {
		return 0;
	}
	if ((uint64_t)(end - size) <= avail) {
		return 0;
	}

	DBG_NOTICE("%s: growing from %jd to %jd bytes needs more than the "
		   "%" PRIu64 " bytes available\n",
		   fsp_str_dbg(fsp),
		   (intmax_t)size,
		   (intmax_t)end,
		   avail);
	errno = ENOSPC;
	return -1;
}
