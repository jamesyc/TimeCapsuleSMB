/*
 * symlink-converter -- convert symlinks on a Time Capsule share between
 * POSIX symlinks and Minshall+French ("XSym") objects.
 *
 * The two representations resolve their target in different namespaces:
 * a POSIX symlink is resolved by smbd, while an XSym object is an ordinary
 * 1067-byte file that the macOS client parses and resolves itself. Measured
 * on the device, a POSIX symlink reaches a macOS client as a plain copy of
 * its target -- readlink(2) fails and the link is gone -- while the same
 * object stored as XSym arrives as a working symlink. Converting therefore
 * restores behaviour that the original AFP firmware provided, and the
 * reverse direction exists so a share can be moved back.
 *
 * The XSym layout is fixed at 1067 bytes:
 *
 *     off   len  content
 *       0     4  "XSym"
 *       4     1  '\n'
 *       5     4  target length in bytes, %04u
 *       9     1  '\n'
 *      10    32  MD5 of the raw target bytes, lowercase hex
 *      42     1  '\n'
 *      43   len  target bytes, verbatim
 *   43+len    1  '\n', only when 43+len < 1067
 *   44+len    *  ' ' (0x20) padding to byte 1066
 *
 * The padding is spaces, not NUL, and the trailing newline is part of that
 * padding: a target of exactly 1024 bytes leaves room for neither. macOS
 * only inspects a file at all when its size is exactly 1067.
 */

#include <sys/stat.h>
#include <sys/time.h>
#include <sys/types.h>

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>
#include <unistd.h>

/*
 * MD5 comes from the base system on the device, where <md5.h> is part of
 * libc. Elsewhere that header does not exist, so take the platform's own
 * digest and give it the NetBSD spelling: CommonCrypto on macOS, OpenSSL
 * otherwise. The algorithm is the same either way -- this only reconciles
 * three names for it -- and it lets the file compile, and be tested, on a
 * host without carrying a digest implementation of its own.
 *
 * The MD5 here is the XSym format's checksum over a symlink target, not a
 * security primitive: it is what tells a real symlink object apart from a
 * file that merely happens to be 1067 bytes long.
 */
#if defined(__NetBSD__)
#include <md5.h>
#elif defined(__APPLE__)
#include <CommonCrypto/CommonDigest.h>
#define MD5_DIGEST_LENGTH CC_MD5_DIGEST_LENGTH
typedef CC_MD5_CTX MD5_CTX;
#define MD5Init(ctx) CC_MD5_Init(ctx)
#define MD5Update(ctx, data, len) CC_MD5_Update((ctx), (data), (CC_LONG)(len))
#define MD5Final(digest, ctx) CC_MD5_Final((digest), (ctx))
#else
#include <openssl/md5.h>
#define MD5Init(ctx) MD5_Init(ctx)
#define MD5Update(ctx, data, len) MD5_Update((ctx), (data), (size_t)(len))
#define MD5Final(digest, ctx) MD5_Final((digest), (ctx))
#endif

#define XSYM_MAGIC "XSym\n"
#define XSYM_MAGIC_LEN 5
#define XSYM_LEN_OFFSET 5
#define XSYM_MD5_OFFSET 10
#define XSYM_HEADER_LEN 43
#define XSYM_MAX_TARGET 1024
#define XSYM_FILE_SIZE (XSYM_HEADER_LEN + XSYM_MAX_TARGET)
#define XSYM_PAD_BYTE ' '

#define MODE_INVENTORY 0
#define MODE_POSIX_TO_XSYM 1
#define MODE_XSYM_TO_POSIX 2

#define TEMP_PREFIX ".tcmig."

#define SMB_CONF_DEFAULT "/mnt/Memory/samba4/etc/smb.conf"

/*
 * Outcomes. Every object the walk yields is reported as exactly one of
 * these, so a run accounts for the whole tree rather than only the objects
 * it changed.
 */
enum outcome {
	OUT_CONVERTED,
	OUT_WOULD_CONVERT,
	OUT_ALREADY,
	OUT_NOT_APPLICABLE,
	OUT_TARGET_TOO_LONG,
	OUT_EMPTY_TARGET,
	OUT_BAD_MAGIC,
	OUT_BAD_LENGTH,
	OUT_BAD_MD5,
	OUT_NOT_XSYM_SIZE,
	OUT_PERMISSION_DENIED,
	OUT_DIR_NOT_WRITABLE,
	OUT_HARDLINKED,
	OUT_IO_ERROR,
	OUT_FAILED,
	OUT__COUNT
};

static const char *const outcome_names[OUT__COUNT] = {
	"converted",
	"would_convert",
	"already",
	"not_applicable",
	"target_too_long",
	"empty_target",
	"bad_magic",
	"bad_length",
	"bad_md5",
	"not_xsym_size",
	"permission_denied",
	"dir_not_writable",
	"hardlinked",
	"io_error",
	"failed",
};

/* Reported alongside the outcome; these never decide what happens. */
enum note {
	NOTE_NONE = 0,
	NOTE_TARGET_DIR = 1 << 0,
	NOTE_TARGET_MISSING = 1 << 1,
	NOTE_SELFREF = 1 << 2,
	NOTE_CHAIN = 1 << 3,
	NOTE_HARDLINK = 1 << 4,
	NOTE_ABSOLUTE = 1 << 5,
	NOTE_REWRITTEN = 1 << 6
};

struct options {
	int mode;
	int apply;
	const char *root;
	const char *only;
	int no_share_check;
	FILE *journal;
};

struct counters {
	unsigned long outcomes[OUT__COUNT];
	unsigned long scanned_dirs;
	unsigned long scanned_entries;
	unsigned long skipped_excluded;
	unsigned long notes_target_dir;
	unsigned long notes_target_missing;
	unsigned long notes_selfref;
	unsigned long notes_chain;
	unsigned long notes_hardlink;
	unsigned long notes_absolute;
	unsigned long notes_rewritten;
};

/*
 * Directory names and suffixes that must never be entered. These mirror
 * repair_xattrs.py, minus its include_hidden flag: hidden objects are
 * ordinary migration candidates here, and a measured share carries a hidden
 * symlink (.fcpcache) that must not be skipped.
 *
 * Matching is case-insensitive because the share is HFS+, where ".Samba4"
 * and ".samba4" are the same directory. A byte-exact comparison would let
 * a differently-cased Time Machine directory through, which is the unsafe
 * direction to be wrong in.
 */
static const char *const excluded_names[] = {
	".samba4",
	".timemachine",
	"Backups.backupdb",
	NULL
};

static const char *const excluded_prefixes[] = {
	".com.apple.TimeMachine.",
	"Backups of ",
	NULL
};

static const char *const excluded_suffixes[] = {
	".sparsebundle",
	".app",
	".bundle",
	".framework",
	".photoslibrary",
	".musiclibrary",
	NULL
};

struct ancestor {
	dev_t dev;
	ino_t ino;
};

static struct counters counters;
static struct options opts;

static void
warn_errno(const char *path, const char *what)
{
	fprintf(stderr, "symlink-converter: %s: %s: %s\n", what, path,
	    strerror(errno));
}

static int
str_ends_with_ci(const char *s, const char *suffix)
{
	size_t slen = strlen(s);
	size_t xlen = strlen(suffix);

	if (xlen > slen)
		return 0;
	return strcasecmp(s + slen - xlen, suffix) == 0;
}

static int
str_starts_with_ci(const char *s, const char *prefix)
{
	return strncasecmp(s, prefix, strlen(prefix)) == 0;
}

static int
is_excluded_name(const char *name)
{
	size_t i;

	for (i = 0; excluded_names[i] != NULL; i++) {
		if (strcasecmp(name, excluded_names[i]) == 0)
			return 1;
	}
	for (i = 0; excluded_prefixes[i] != NULL; i++) {
		if (str_starts_with_ci(name, excluded_prefixes[i]))
			return 1;
	}
	for (i = 0; excluded_suffixes[i] != NULL; i++) {
		if (str_ends_with_ci(name, excluded_suffixes[i]))
			return 1;
	}
	if (strncmp(name, TEMP_PREFIX, strlen(TEMP_PREFIX)) == 0)
		return 1;
	return 0;
}

/*
 * Fold "." and ".." textually, without resolving symlinks. This matches
 * Samba's canonicalize_absolute_path(): the server decides containment the
 * same way, and using realpath() here would answer a different question
 * and disagree with it.
 */
static int
canonicalize_abs(const char *path, char *out, size_t outsz)
{
	const char *p = path;
	size_t len = 0;

	if (path[0] != '/')
		return -1;

	if (outsz < 2)
		return -1;
	out[len++] = '/';

	while (*p != '\0') {
		const char *seg;
		size_t seglen;

		while (*p == '/')
			p++;
		if (*p == '\0')
			break;

		seg = p;
		while (*p != '\0' && *p != '/')
			p++;
		seglen = (size_t)(p - seg);

		if (seglen == 1 && seg[0] == '.')
			continue;
		if (seglen == 2 && seg[0] == '.' && seg[1] == '.') {
			while (len > 1 && out[len - 1] != '/')
				len--;
			if (len > 1)
				len--;
			continue;
		}

		if (len > 1) {
			if (len + 1 >= outsz)
				return -1;
			out[len++] = '/';
		}
		if (len + seglen >= outsz)
			return -1;
		memcpy(out + len, seg, seglen);
		len += seglen;
	}

	if (len == 0)
		out[len++] = '/';
	out[len] = '\0';
	return 0;
}

/*
 * True when "child" is at or below "parent". Component-boundary aware, so
 * "/a/bc" is not below "/a/b". Mirrors Samba's subdir_of(); when it matches,
 * *relative points at the descending remainder.
 */
static int
path_under(const char *parent, const char *child, const char **relative)
{
	size_t plen = strlen(parent);

	while (plen > 1 && parent[plen - 1] == '/')
		plen--;

	if (plen == 1 && parent[0] == '/') {
		if (relative != NULL)
			*relative = child + 1;
		return 1;
	}
	if (strncmp(child, parent, plen) != 0)
		return 0;
	if (child[plen] == '\0') {
		if (relative != NULL)
			*relative = child + plen;
		return 1;
	}
	if (child[plen] != '/')
		return 0;
	if (relative != NULL)
		*relative = child + plen + 1;
	return 1;
}

static void
md5_hex(const unsigned char *data, size_t len, char *out33)
{
	MD5_CTX ctx;
	unsigned char digest[MD5_DIGEST_LENGTH];
	size_t i;

	MD5Init(&ctx);
	MD5Update(&ctx, data, (unsigned int)len);
	MD5Final(digest, &ctx);

	for (i = 0; i < MD5_DIGEST_LENGTH; i++)
		snprintf(out33 + i * 2, 3, "%02x", digest[i]);
	out33[MD5_DIGEST_LENGTH * 2] = '\0';
}

/*
 * Build the 1067-byte XSym body for a target. The MD5 covers exactly the
 * target bytes -- no newline, no NUL, no padding.
 */
static int
xsym_format(const char *target, size_t tlen, unsigned char *buf)
{
	char hex[MD5_DIGEST_LENGTH * 2 + 1];
	char lenbuf[8];
	size_t off;

	if (tlen > XSYM_MAX_TARGET)
		return -1;

	md5_hex((const unsigned char *)target, tlen, hex);

	/*
	 * Assemble the header field by field. snprintf() is deliberately
	 * kept off the buffer: it would write a terminating NUL one byte
	 * past each field, and a NUL anywhere in these 43 bytes makes the
	 * object unrecognisable to the client.
	 */
	memset(buf, XSYM_PAD_BYTE, XSYM_FILE_SIZE);
	memcpy(buf, XSYM_MAGIC, XSYM_MAGIC_LEN);

	snprintf(lenbuf, sizeof(lenbuf), "%04u", (unsigned int)tlen);
	memcpy(buf + XSYM_LEN_OFFSET, lenbuf, 4);
	buf[XSYM_LEN_OFFSET + 4] = '\n';

	memcpy(buf + XSYM_MD5_OFFSET, hex, MD5_DIGEST_LENGTH * 2);
	buf[XSYM_MD5_OFFSET + MD5_DIGEST_LENGTH * 2] = '\n';
	memcpy(buf + XSYM_HEADER_LEN, target, tlen);

	off = XSYM_HEADER_LEN + tlen;
	if (off < XSYM_FILE_SIZE)
		buf[off] = '\n';

	return 0;
}

/*
 * Parse an XSym body. Validates magic, the declared length and the MD5 --
 * a genuine 1067-byte file is possible, so size alone proves nothing. The
 * trailing newline is optional: at a 1024-byte target there is no room for
 * it.
 */
static enum outcome
xsym_parse(const unsigned char *buf, char *target, size_t targetsz,
    size_t *tlen_out)
{
	char declared[5];
	char hex[MD5_DIGEST_LENGTH * 2 + 1];
	unsigned long tlen;
	char *end;
	size_t i;

	if (memcmp(buf, XSYM_MAGIC, XSYM_MAGIC_LEN) != 0)
		return OUT_BAD_MAGIC;

	for (i = 0; i < 4; i++) {
		if (buf[XSYM_LEN_OFFSET + i] < '0' ||
		    buf[XSYM_LEN_OFFSET + i] > '9')
			return OUT_BAD_LENGTH;
		declared[i] = (char)buf[XSYM_LEN_OFFSET + i];
	}
	declared[4] = '\0';

	if (buf[XSYM_LEN_OFFSET + 4] != '\n')
		return OUT_BAD_LENGTH;

	errno = 0;
	tlen = strtoul(declared, &end, 10);
	if (errno != 0 || *end != '\0')
		return OUT_BAD_LENGTH;
	if (tlen > XSYM_MAX_TARGET)
		return OUT_BAD_LENGTH;
	if (tlen == 0)
		return OUT_EMPTY_TARGET;
	if (tlen >= targetsz)
		return OUT_BAD_LENGTH;

	md5_hex(buf + XSYM_HEADER_LEN, (size_t)tlen, hex);
	if (memcmp(buf + XSYM_MD5_OFFSET, hex, MD5_DIGEST_LENGTH * 2) != 0)
		return OUT_BAD_MD5;

	memcpy(target, buf + XSYM_HEADER_LEN, (size_t)tlen);
	target[tlen] = '\0';
	*tlen_out = (size_t)tlen;
	return OUT_CONVERTED;
}

static void
record(const char *path, enum outcome out, unsigned int notes,
    const char *target)
{
	counters.outcomes[out]++;

	if ((notes & NOTE_TARGET_DIR) != 0)
		counters.notes_target_dir++;
	if ((notes & NOTE_TARGET_MISSING) != 0)
		counters.notes_target_missing++;
	if ((notes & NOTE_SELFREF) != 0)
		counters.notes_selfref++;
	if ((notes & NOTE_CHAIN) != 0)
		counters.notes_chain++;
	if ((notes & NOTE_HARDLINK) != 0)
		counters.notes_hardlink++;
	if ((notes & NOTE_ABSOLUTE) != 0)
		counters.notes_absolute++;
	if ((notes & NOTE_REWRITTEN) != 0)
		counters.notes_rewritten++;

	printf("%s\t%s\t%s\n", outcome_names[out], path,
	    target != NULL ? target : "");
}

/*
 * Journal a pending change before it happens: a crash between the rename
 * and the record would otherwise leave a change that rollback cannot see.
 * Paths and targets are opaque bytes and may contain tabs or newlines, so
 * their lengths precede the raw bytes rather than a delimiter separating
 * them.
 */
static int
journal_write(const char *path, const char *old_target,
    const char *new_target, mode_t mode, time_t mtime)
{
	size_t plen = strlen(path);
	size_t olen = strlen(old_target);
	size_t nlen = strlen(new_target);

	if (opts.journal == NULL)
		return 0;

	if (fprintf(opts.journal, "%s\t%lu\t%lu\t%lu\t%04lo\t%ld\n",
	    opts.mode == MODE_POSIX_TO_XSYM ? "posix2xsym" : "xsym2posix",
	    (unsigned long)plen, (unsigned long)olen, (unsigned long)nlen,
	    (unsigned long)(mode & 07777), (long)mtime) < 0)
		return -1;
	if (fwrite(path, 1, plen, opts.journal) != plen)
		return -1;
	if (fwrite(old_target, 1, olen, opts.journal) != olen)
		return -1;
	if (fwrite(new_target, 1, nlen, opts.journal) != nlen)
		return -1;
	if (fputc('\n', opts.journal) == EOF)
		return -1;
	if (fflush(opts.journal) != 0)
		return -1;
	if (fsync(fileno(opts.journal)) != 0)
		return -1;
	return 0;
}

static int
make_temp_path(const char *dir, char *out, size_t outsz)
{
	static unsigned long seq;
	int n;

	n = snprintf(out, outsz, "%s/%s%ld.%lu.tmp", dir, TEMP_PREFIX,
	    (long)getpid(), seq++);
	if (n < 0 || (size_t)n >= outsz)
		return -1;
	return 0;
}

/*
 * Restore atime and mtime on the new object. lutimes() is used rather than
 * utimes() because it acts on the symlink itself: following the link would
 * stamp an unrelated file, which in the xsym-to-posix direction is somebody
 * else's data.
 *
 * Measured on the capsule: this preserves mtime when the new object is a
 * regular file (posix-to-xsym), but has no effect on a symlink -- HFS+
 * here accepts the call, reports success, and leaves the timestamp at the
 * time of creation. So a round trip returns the target bytes exactly and
 * loses the original mtime in the xsym-to-posix leg. The call is kept
 * because it costs nothing and works in the direction where it can.
 */
static void
restore_times(const char *path, time_t atime, time_t mtime)
{
	struct timeval tv[2];

	tv[0].tv_sec = atime;
	tv[0].tv_usec = 0;
	tv[1].tv_sec = mtime;
	tv[1].tv_usec = 0;

	if (lutimes(path, tv) != 0)
		warn_errno(path, "lutimes");
}

static int
write_xsym_file(const char *dir, const char *path, const char *target,
    size_t tlen, time_t atime, time_t mtime)
{
	unsigned char buf[XSYM_FILE_SIZE];
	char tmp[PATH_MAX];
	int fd;

	if (xsym_format(target, tlen, buf) != 0)
		return -1;
	if (make_temp_path(dir, tmp, sizeof(tmp)) != 0)
		return -1;

	fd = open(tmp, O_WRONLY | O_CREAT | O_EXCL, 0666);
	if (fd < 0) {
		warn_errno(tmp, "open");
		return -1;
	}
	if (write(fd, buf, sizeof(buf)) != (ssize_t)sizeof(buf)) {
		warn_errno(tmp, "write");
		close(fd);
		unlink(tmp);
		return -1;
	}
	/* The share forces 0666; open()'s mode is filtered by umask. */
	if (fchmod(fd, 0666) != 0)
		warn_errno(tmp, "fchmod");
	if (fsync(fd) != 0) {
		warn_errno(tmp, "fsync");
		close(fd);
		unlink(tmp);
		return -1;
	}
	if (close(fd) != 0) {
		warn_errno(tmp, "close");
		unlink(tmp);
		return -1;
	}
	if (rename(tmp, path) != 0) {
		warn_errno(path, "rename");
		unlink(tmp);
		return -1;
	}

	restore_times(path, atime, mtime);
	return 0;
}

static int
write_posix_symlink(const char *dir, const char *path, const char *target,
    time_t atime, time_t mtime)
{
	char tmp[PATH_MAX];

	if (make_temp_path(dir, tmp, sizeof(tmp)) != 0)
		return -1;

	if (symlink(target, tmp) != 0) {
		warn_errno(tmp, "symlink");
		return -1;
	}
	if (rename(tmp, path) != 0) {
		warn_errno(path, "rename");
		unlink(tmp);
		return -1;
	}

	restore_times(path, atime, mtime);
	return 0;
}

/*
 * Decide and, with --apply, perform the conversion for one symlink.
 *
 * Only the first two axes -- the form of the target and where it points --
 * decide anything. Whether the target is a file, a directory, missing or
 * cyclic is reported but never changes the outcome: both representations
 * store the target as a string.
 */
static void
handle_symlink(const char *dir, const char *path, const struct stat *st)
{
	char target[PATH_MAX + 1];
	char joined[PATH_MAX];
	char canon[PATH_MAX];
	const char *relative = NULL;
	const char *write_target = NULL;
	unsigned int notes = NOTE_NONE;
	ssize_t n;
	size_t tlen;
	struct stat tst;

	if (opts.mode == MODE_XSYM_TO_POSIX) {
		record(path, OUT_NOT_APPLICABLE, NOTE_NONE, NULL);
		return;
	}

	/*
	 * readlink(2) does not report truncation: a target that fills the
	 * buffer is indistinguishable from a longer one. Read into a buffer
	 * larger than any target this tool will accept, so a full read means
	 * the target is too long rather than exactly at the limit. Taking a
	 * truncated path for the whole one would write a wrong target over
	 * the original and lose it.
	 */
	n = readlink(path, target, sizeof(target) - 1);
	if (n < 0) {
		record(path, errno == EACCES ? OUT_PERMISSION_DENIED :
		    OUT_IO_ERROR, NOTE_NONE, NULL);
		return;
	}
	if ((size_t)n >= sizeof(target) - 1) {
		record(path, OUT_TARGET_TOO_LONG, NOTE_NONE, NULL);
		return;
	}
	target[n] = '\0';
	tlen = (size_t)n;

	if (tlen > XSYM_MAX_TARGET) {
		record(path, OUT_TARGET_TOO_LONG, NOTE_NONE, target);
		return;
	}

	if (tlen == 0) {
		record(path, OUT_EMPTY_TARGET, NOTE_NONE, NULL);
		return;
	}

	if (target[0] == '/')
		notes |= NOTE_ABSOLUTE;

	if (stat(path, &tst) != 0) {
		notes |= NOTE_TARGET_MISSING;
	} else {
		struct stat dst;

		if (S_ISDIR(tst.st_mode))
			notes |= NOTE_TARGET_DIR;
		/*
		 * Compare the target against the directory holding the link,
		 * not against the link itself: a symlink has its own inode,
		 * so the latter never matches. "x -> ." resolves to the
		 * containing directory.
		 */
		if (stat(dir, &dst) == 0 && tst.st_dev == dst.st_dev &&
		    tst.st_ino == dst.st_ino)
			notes |= NOTE_SELFREF;
	}

	/*
	 * Reduce the target to an absolute path before asking whether it
	 * escapes: a relative target with enough ".." leaves the share just
	 * as an absolute one does, and the containment test is only defined
	 * over absolute paths.
	 */
	if (target[0] == '/') {
		if (canonicalize_abs(target, canon, sizeof(canon)) != 0) {
			record(path, OUT_FAILED, notes, target);
			return;
		}
	} else {
		int k = snprintf(joined, sizeof(joined), "%s/%s", dir, target);

		if (k < 0 || (size_t)k >= sizeof(joined)) {
			record(path, OUT_TARGET_TOO_LONG, notes, target);
			return;
		}
		if (canonicalize_abs(joined, canon, sizeof(canon)) != 0) {
			record(path, OUT_FAILED, notes, target);
			return;
		}
	}

	{
		struct stat chain;

		if (lstat(canon, &chain) == 0 && S_ISLNK(chain.st_mode))
			notes |= NOTE_CHAIN;
	}

	/*
	 * An absolute target below the link's own directory is rewritten to
	 * the descending remainder, which is what read_symlink_reparse()
	 * does in smbd/files.c. Without it the client would look for
	 * "/Volumes/dk2/ShareRoot/..." under its own root and find nothing,
	 * so the rewrite is what keeps an in-share link working.
	 *
	 * Everything else -- absolute but not below this directory, or
	 * pointing outside the share -- is written verbatim. The AFP
	 * firmware handed such targets to the client unchanged, and an XSym
	 * body stores a string, so nothing here needs to refuse.
	 */
	write_target = target;
	if (target[0] == '/' && path_under(dir, canon, &relative) &&
	    relative != NULL && *relative != '\0') {
		write_target = relative;
		notes |= NOTE_REWRITTEN;
	}

	/*
	 * Checked again after the rewrite, not only before it: turning an
	 * absolute target into a relative one can lengthen it.
	 */
	if (strlen(write_target) > XSYM_MAX_TARGET) {
		record(path, OUT_TARGET_TOO_LONG, notes, target);
		return;
	}

	if (opts.mode == MODE_INVENTORY) {
		record(path, OUT_ALREADY, notes, write_target);
		return;
	}
	if (!opts.apply) {
		record(path, OUT_WOULD_CONVERT, notes, write_target);
		return;
	}

	if (access(dir, W_OK) != 0) {
		record(path, OUT_DIR_NOT_WRITABLE, notes, target);
		return;
	}
	if (journal_write(path, target, write_target, st->st_mode,
	    st->st_mtime) != 0) {
		record(path, OUT_FAILED, notes, target);
		return;
	}
	if (write_xsym_file(dir, path, write_target, strlen(write_target),
	    st->st_atime, st->st_mtime) != 0) {
		record(path, OUT_FAILED, notes, target);
		return;
	}

	record(path, OUT_CONVERTED, notes, write_target);
}

/*
 * A regular file is only interesting when it is exactly 1067 bytes, which
 * is the same test macOS applies before it looks inside at all.
 */
static void
handle_regular(const char *dir, const char *path, const struct stat *st)
{
	unsigned char buf[XSYM_FILE_SIZE];
	char target[XSYM_MAX_TARGET + 1];
	unsigned int notes = NOTE_NONE;
	enum outcome parsed;
	size_t tlen;
	ssize_t n;
	int fd;

	if (opts.mode == MODE_POSIX_TO_XSYM) {
		record(path, OUT_NOT_APPLICABLE, NOTE_NONE, NULL);
		return;
	}
	if (st->st_size != XSYM_FILE_SIZE) {
		record(path, OUT_NOT_XSYM_SIZE, NOTE_NONE, NULL);
		return;
	}
	if (st->st_nlink > 1)
		notes |= NOTE_HARDLINK;

	fd = open(path, O_RDONLY);
	if (fd < 0) {
		record(path, errno == EACCES ? OUT_PERMISSION_DENIED :
		    OUT_IO_ERROR, notes, NULL);
		return;
	}
	n = read(fd, buf, sizeof(buf));
	close(fd);
	if (n != (ssize_t)sizeof(buf)) {
		record(path, OUT_IO_ERROR, notes, NULL);
		return;
	}

	parsed = xsym_parse(buf, target, sizeof(target), &tlen);
	if (parsed != OUT_CONVERTED) {
		record(path, parsed, notes, NULL);
		return;
	}

	/*
	 * The target is carried over verbatim, so a round trip returns
	 * exactly the bytes it started with. Where it points is not this
	 * tool's business: both formats store the target as a string, and
	 * the AFP firmware stored it the same way.
	 */
	if (target[0] == '/')
		notes |= NOTE_ABSOLUTE;

	if (opts.mode == MODE_INVENTORY) {
		record(path, OUT_ALREADY, notes, target);
		return;
	}
	if (!opts.apply) {
		record(path, (notes & NOTE_HARDLINK) != 0 ? OUT_HARDLINKED :
		    OUT_WOULD_CONVERT, notes, target);
		return;
	}

	/*
	 * Refuse a hardlinked XSym file, but only on the write path: the
	 * reason is that rename() replaces a single directory entry, leaving
	 * the other names resolving to the original 1067-byte file, so the
	 * same inode's names would come to mean different things and no
	 * reverse migration could undo it. Deciding this earlier would rob
	 * an inventory of the object's target, which it can safely report.
	 */
	if ((notes & NOTE_HARDLINK) != 0) {
		record(path, OUT_HARDLINKED, notes, target);
		return;
	}

	if (access(dir, W_OK) != 0) {
		record(path, OUT_DIR_NOT_WRITABLE, notes, target);
		return;
	}
	if (journal_write(path, target, target, st->st_mode,
	    st->st_mtime) != 0) {
		record(path, OUT_FAILED, notes, target);
		return;
	}
	if (write_posix_symlink(dir, path, target, st->st_atime, st->st_mtime) != 0) {
		record(path, OUT_FAILED, notes, target);
		return;
	}

	record(path, OUT_CONVERTED, notes, target);
}

/*
 * Walk the tree recursively, bounded by maxdepth (256) because the depth of
 * a user's share is not known in advance. Each frame holds a few PATH_MAX
 * buffers, so the bound is what keeps the stack in hand.
 *
 * Symlinks are never descended into -- they are objects to convert, not
 * directories to enter. That alone closes every cycle a symlink can form,
 * including the measured ".fcpcache" that points at its library root.
 * The ancestor set below is for the cycles a symlink cannot cause: a
 * directory reachable from itself through a mount, or one whose identity
 * cannot be read at all.
 *
 * The set holds the ancestors of the current branch, not every directory
 * visited: a file reachable by two distinct paths must still be examined
 * along both, otherwise objects are silently skipped and the run reports a
 * completeness it does not have.
 */
static void
walk(const char *dir, dev_t root_dev, struct ancestor *ancestors,
    size_t depth, size_t maxdepth)
{
	DIR *dp;
	struct dirent *de;

	dp = opendir(dir);
	if (dp == NULL) {
		warn_errno(dir, "opendir");
		counters.outcomes[errno == EACCES ? OUT_PERMISSION_DENIED :
		    OUT_IO_ERROR]++;
		return;
	}
	counters.scanned_dirs++;

	while ((de = readdir(dp)) != NULL) {
		char path[PATH_MAX];
		struct stat st;
		int n;

		if (strcmp(de->d_name, ".") == 0 ||
		    strcmp(de->d_name, "..") == 0)
			continue;

		counters.scanned_entries++;

		if (is_excluded_name(de->d_name)) {
			counters.skipped_excluded++;
			continue;
		}

		n = snprintf(path, sizeof(path), "%s/%s", dir, de->d_name);
		if (n < 0 || (size_t)n >= sizeof(path)) {
			counters.outcomes[OUT_FAILED]++;
			continue;
		}

		if (lstat(path, &st) != 0) {
			warn_errno(path, "lstat");
			counters.outcomes[errno == EACCES ?
			    OUT_PERMISSION_DENIED : OUT_IO_ERROR]++;
			continue;
		}

		/*
		 * With --only, still descend through directories so the one
		 * named object can be reached, but act on nothing else.
		 */
		if (opts.only != NULL && !S_ISDIR(st.st_mode) &&
		    strcmp(path, opts.only) != 0)
			continue;

		if (S_ISLNK(st.st_mode)) {
			handle_symlink(dir, path, &st);
			continue;
		}
		if (S_ISREG(st.st_mode)) {
			handle_regular(dir, path, &st);
			continue;
		}
		if (!S_ISDIR(st.st_mode))
			continue;

		if (st.st_dev != root_dev)
			continue;

		if (depth >= maxdepth) {
			fprintf(stderr, "symlink-converter: depth limit at "
			    "%s\n", path);
			continue;
		}

		{
			size_t i;
			int cycle = 0;

			for (i = 0; i < depth; i++) {
				if (ancestors[i].dev == st.st_dev &&
				    ancestors[i].ino == st.st_ino) {
					cycle = 1;
					break;
				}
			}
			if (cycle) {
				fprintf(stderr, "symlink-converter: cycle at "
				    "%s\n", path);
				continue;
			}
		}

		ancestors[depth].dev = st.st_dev;
		ancestors[depth].ino = st.st_ino;
		walk(path, root_dev, ancestors, depth + 1, maxdepth);
	}

	if (closedir(dp) != 0)
		warn_errno(dir, "closedir");
}

static void
print_summary(void)
{
	int i;

	fprintf(stderr, "\n--- summary ---\n");
	fprintf(stderr, "directories scanned:  %lu\n", counters.scanned_dirs);
	fprintf(stderr, "entries seen:         %lu\n",
	    counters.scanned_entries);
	fprintf(stderr, "excluded:             %lu\n",
	    counters.skipped_excluded);

	for (i = 0; i < OUT__COUNT; i++) {
		if (counters.outcomes[i] != 0)
			fprintf(stderr, "%-28s %lu\n", outcome_names[i],
			    counters.outcomes[i]);
	}

	fprintf(stderr, "\n--- notes (reported, never decisive) ---\n");
	fprintf(stderr, "target is a directory: %lu\n",
	    counters.notes_target_dir);
	fprintf(stderr, "target missing:        %lu\n",
	    counters.notes_target_missing);
	fprintf(stderr, "self-referential:      %lu\n",
	    counters.notes_selfref);
	fprintf(stderr, "target is a symlink:   %lu\n", counters.notes_chain);
	fprintf(stderr, "hardlinked:            %lu\n",
	    counters.notes_hardlink);
	fprintf(stderr, "absolute target:       %lu\n",
	    counters.notes_absolute);
	fprintf(stderr, "rewritten to relative: %lu\n",
	    counters.notes_rewritten);
}

/*
 * Check that the walk root is inside a directory smbd actually serves.
 *
 * The share paths are read from the running config rather than taken from
 * the command line: the point of this tool is to fix up what clients see,
 * so "a share" has to mean what the server currently exports, not what the
 * operator believes it exports. It also catches the obvious slip of being
 * pointed at a system directory.
 *
 * Returns 1 when the root is inside a share, 0 when it is not, and -1 when
 * the config could not be read at all -- an unreadable config is not the
 * same answer as a root outside every share.
 */
static int
root_is_inside_a_share(const char *conf_path, const char *root)
{
	char line[PATH_MAX + 64];
	FILE *fp;
	int found = 0;

	fp = fopen(conf_path, "r");
	if (fp == NULL)
		return -1;

	while (fgets(line, sizeof(line), fp) != NULL) {
		char *p = line;
		char *end;

		while (*p == ' ' || *p == '\t')
			p++;
		if (strncmp(p, "path", 4) != 0)
			continue;
		p += 4;
		while (*p == ' ' || *p == '\t')
			p++;
		if (*p != '=')
			continue;
		p++;
		while (*p == ' ' || *p == '\t')
			p++;

		end = p + strlen(p);
		while (end > p && (end[-1] == '\n' || end[-1] == '\r' ||
		    end[-1] == ' ' || end[-1] == '\t'))
			end--;
		*end = '\0';

		if (*p != '/')
			continue;
		if (path_under(p, root, NULL)) {
			found = 1;
			break;
		}
	}

	fclose(fp);
	return found;
}

static void
usage(void)
{
	fprintf(stderr,
	    "Usage: symlink-converter --mode <mode> --root <path> [options]\n"
	    "Modes:\n"
	    "  inventory          Report what is there, write nothing\n"
	    "  posix-to-xsym      Replace POSIX symlinks with XSym files\n"
	    "  xsym-to-posix      Replace XSym files with POSIX symlinks\n"
	    "Options:\n"
	    "  --root <path>      Directory to walk; required\n"
	    "  --only <path>      Act on this one object instead of the tree\n"
	    "  --journal <path>   Record each change before it is made\n"
	    "  --config <path>    smb.conf to read share paths from\n"
	    "  --any-path         Convert outside a configured share\n"
	    "  --apply            Actually write; without it nothing changes\n"
	    "  --help             Print this text and exit\n");
}

int
main(int argc, char **argv)
{
	struct ancestor *ancestors;
	struct stat st;
	const char *journal_path = NULL;
	const char *conf_path = SMB_CONF_DEFAULT;
	char root_canon[PATH_MAX];
	char only_canon[PATH_MAX];
	const size_t maxdepth = 256;
	int i;

	opts.mode = -1;

	for (i = 1; i < argc; i++) {
		if (strcmp(argv[i], "--mode") == 0 && i + 1 < argc) {
			const char *v = argv[++i];

			if (strcmp(v, "inventory") == 0)
				opts.mode = MODE_INVENTORY;
			else if (strcmp(v, "posix-to-xsym") == 0)
				opts.mode = MODE_POSIX_TO_XSYM;
			else if (strcmp(v, "xsym-to-posix") == 0)
				opts.mode = MODE_XSYM_TO_POSIX;
			else {
				fprintf(stderr, "unknown mode: %s\n", v);
				return 2;
			}
		} else if (strcmp(argv[i], "--root") == 0 && i + 1 < argc) {
			opts.root = argv[++i];
		} else if (strcmp(argv[i], "--only") == 0 && i + 1 < argc) {
			opts.only = argv[++i];
		} else if (strcmp(argv[i], "--journal") == 0 && i + 1 < argc) {
			journal_path = argv[++i];
		} else if (strcmp(argv[i], "--config") == 0 && i + 1 < argc) {
			conf_path = argv[++i];
		} else if (strcmp(argv[i], "--any-path") == 0) {
			opts.no_share_check = 1;
		} else if (strcmp(argv[i], "--apply") == 0) {
			opts.apply = 1;
		} else if (strcmp(argv[i], "--help") == 0 ||
		    strcmp(argv[i], "-h") == 0) {
			usage();
			return 0;
		} else {
			fprintf(stderr, "unknown argument: %s\n", argv[i]);
			usage();
			return 2;
		}
	}

	if (opts.mode < 0 || opts.root == NULL) {
		usage();
		return 2;
	}
	if (opts.mode == MODE_INVENTORY && opts.apply) {
		fprintf(stderr, "inventory mode never writes; drop --apply\n");
		return 2;
	}

	/*
	 * --root may itself be a symlink, so resolve it once here and work
	 * with the resolved path from then on. Containment, by contrast, is
	 * decided textually to match the server.
	 */
	if (realpath(opts.root, root_canon) == NULL) {
		warn_errno(opts.root, "realpath");
		return 1;
	}
	opts.root = root_canon;

	if (strcmp(root_canon, "/") == 0) {
		fprintf(stderr, "refusing to operate on /\n");
		return 2;
	}

	if (!opts.no_share_check) {
		int inside = root_is_inside_a_share(conf_path, root_canon);

		if (inside < 0) {
			fprintf(stderr,
			    "cannot read %s to confirm the root is inside a "
			    "share\n", conf_path);
			fprintf(stderr,
			    "pass --config <path>, or --any-path to skip the "
			    "check\n");
			return 2;
		}
		if (inside == 0) {
			fprintf(stderr,
			    "%s is not inside any share in %s\n",
			    root_canon, conf_path);
			fprintf(stderr,
			    "pass --any-path to convert there anyway\n");
			return 2;
		}
	}
	/*
	 * --only is compared against paths the walk builds from the resolved
	 * root, so it has to be resolved too. Left raw, a path with "./", a
	 * trailing slash or a symlinked parent matches nothing and the run
	 * quietly converts zero objects while reporting success.
	 */
	if (opts.only != NULL) {
		char dirpart[PATH_MAX];
		char dircanon[PATH_MAX];
		char trimmed[PATH_MAX];
		const char *base;
		size_t dlen;
		size_t tl;
		int k;

		/*
		 * Resolve only the directory, never the final component:
		 * realpath() on the object itself would follow the very
		 * symlink we were asked to convert and leave --only pointing
		 * at its target instead.
		 */
		if (opts.only[0] != '/') {
			fprintf(stderr, "--only must be an absolute path\n");
			return 2;
		}

		/*
		 * Strip trailing slashes first. Left in place they make the
		 * final component empty, and the walk never builds a path
		 * ending in a slash, so the comparison could not match and
		 * the run would convert nothing while reporting success.
		 */
		tl = strlen(opts.only);
		while (tl > 1 && opts.only[tl - 1] == '/')
			tl--;
		if (tl >= sizeof(trimmed)) {
			fprintf(stderr, "--only path is too long\n");
			return 2;
		}
		memcpy(trimmed, opts.only, tl);
		trimmed[tl] = '\0';

		if (strcmp(trimmed, "/") == 0) {
			fprintf(stderr, "--only must name an object, not /\n");
			return 2;
		}

		base = strrchr(trimmed, '/');
		if (base == NULL || base[1] == '\0') {
			fprintf(stderr, "--only must name an object\n");
			return 2;
		}
		dlen = (size_t)(base - trimmed);
		if (dlen == 0)
			dlen = 1;
		if (dlen >= sizeof(dirpart)) {
			fprintf(stderr, "--only path is too long\n");
			return 2;
		}
		memcpy(dirpart, trimmed, dlen);
		dirpart[dlen] = '\0';

		if (realpath(dirpart, dircanon) == NULL) {
			warn_errno(dirpart, "realpath");
			return 1;
		}
		k = snprintf(only_canon, sizeof(only_canon), "%s/%s",
		    strcmp(dircanon, "/") == 0 ? "" : dircanon, base + 1);
		if (k < 0 || (size_t)k >= sizeof(only_canon)) {
			fprintf(stderr, "--only path is too long\n");
			return 2;
		}
		opts.only = only_canon;
		if (!path_under(root_canon, only_canon, NULL)) {
			fprintf(stderr, "--only is outside --root\n");
			return 2;
		}
	}

	if (lstat(root_canon, &st) != 0) {
		warn_errno(root_canon, "lstat");
		return 1;
	}
	if (!S_ISDIR(st.st_mode)) {
		fprintf(stderr, "--root is not a directory\n");
		return 2;
	}

	if (journal_path != NULL) {
		opts.journal = fopen(journal_path, "a");
		if (opts.journal == NULL) {
			warn_errno(journal_path, "fopen");
			return 1;
		}
	}

	ancestors = calloc(maxdepth, sizeof(*ancestors));
	if (ancestors == NULL) {
		fprintf(stderr, "out of memory\n");
		return 1;
	}
	ancestors[0].dev = st.st_dev;
	ancestors[0].ino = st.st_ino;

	walk(root_canon, st.st_dev, ancestors, 1, maxdepth);

	free(ancestors);
	if (opts.journal != NULL)
		fclose(opts.journal);

	print_summary();

	/*
	 * Anything that stopped the tool from reading or writing what it was
	 * pointed at is a failure, not a quiet zero. Otherwise "converted
	 * nothing because the share could not be read" is indistinguishable
	 * from "there was nothing to convert" to whatever runs this.
	 */
	if (counters.outcomes[OUT_FAILED] != 0 ||
	    counters.outcomes[OUT_IO_ERROR] != 0 ||
	    counters.outcomes[OUT_PERMISSION_DENIED] != 0 ||
	    counters.outcomes[OUT_DIR_NOT_WRITABLE] != 0)
		return 1;

	/*
	 * A refused hardlink counts only under --apply, where the operator
	 * asked for a conversion and did not get one -- the same category as
	 * a directory that could not be written. Reporting it from a survey
	 * or a dry run is an answer to a question, not a declined write, so
	 * those still exit 0.
	 */
	if (opts.apply && counters.outcomes[OUT_HARDLINKED] != 0)
		return 1;

	return 0;
}
