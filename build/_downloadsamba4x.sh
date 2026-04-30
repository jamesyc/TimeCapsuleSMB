#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"
. "$(dirname "$0")/_patch_helpers.sh"

mkdir -p "$OUT" "$SAMBA4X_WORK"

{
    echo "Starting Samba 4.x download workflow at $(date -u)"
    echo "SDK_FAMILY=$SDK_FAMILY"
    echo "DEVICE_FAMILY=$DEVICE_FAMILY"
    echo "NETBSD4_ABI=$NETBSD4_ABI"
    echo "SAMBA4X_VERSION=$SAMBA4X_VERSION"
    echo "SAMBA4X_GIT_URL=$SAMBA4X_GIT_URL"
    echo "SAMBA4X_GIT_REF=$SAMBA4X_GIT_REF"
    echo "SAMBA4X_SRC_DIR=$SAMBA4X_SRC_DIR"

    echo "Installing Samba 4.x host build tools on the VM."
    for pkg in bison p5-Parse-Yapp; do
        if pkg_info "$pkg" >/dev/null 2>&1; then
            echo "$pkg is already installed on the VM; skipping pkgin install."
        else
            /usr/pkg/bin/pkgin -4 -y install "$pkg"
        fi
    done

    if [ -d "$SAMBA4X_SRC_DIR/.git" ]; then
        printf 'Refreshing existing git checkout at %s\n' "$SAMBA4X_SRC_DIR"
        git -C "$SAMBA4X_SRC_DIR" fetch --depth 1 origin "$SAMBA4X_GIT_REF"
        git -C "$SAMBA4X_SRC_DIR" checkout -B "$SAMBA4X_GIT_REF" "FETCH_HEAD"
        git -C "$SAMBA4X_SRC_DIR" reset --hard "FETCH_HEAD"
    elif [ -d "$SAMBA4X_SRC_DIR" ]; then
        printf 'Removing existing non-git Samba source tree at %s\n' "$SAMBA4X_SRC_DIR"
        rm -rf "$SAMBA4X_SRC_DIR"
        git clone --depth 1 --branch "$SAMBA4X_GIT_REF" "$SAMBA4X_GIT_URL" "$SAMBA4X_SRC_DIR"
    else
        git clone --depth 1 --branch "$SAMBA4X_GIT_REF" "$SAMBA4X_GIT_URL" "$SAMBA4X_SRC_DIR"
    fi

    # Samba 4.24 tags Heimdal generators with use_hostcc=True, but the waf
    # glue only adds a define and still inherits the target compiler/env. Make
    # those task generators actually use the configured host compiler.
    patch_perl_any "Samba 4.x hostcc cflags patch" \
        "s/    if not 'EXTRA_CFLAGS' in bld\\.env:\\n        list = \\[\\]\\n    else:\\n        list = bld\\.env\\['EXTRA_CFLAGS'\\]\\n    ret\\.extend\\(list\\)/    if use_hostcc:\\n        list = TO_LIST\\(bld.env.HOST_CFLAGS\\)\\n    elif not 'EXTRA_CFLAGS' in bld.env:\\n        list = []\\n    else:\\n        list = bld.env['EXTRA_CFLAGS']\\n    ret.extend\\(list\\)/" \
        "$SAMBA4X_SRC_DIR/buildtools/wafsamba/samba_autoconf.py"
    patch_perl_any "Samba 4.x hostcc env helper patch" \
        "s/os\\.environ\\['PYTHONUNBUFFERED'\\] = '1'\\n/os.environ['PYTHONUNBUFFERED'] = '1'\\n\\ndef APPLY_HOSTCC_ENV\\(bld, taskgen\\):\\n    if not getattr\\(taskgen, 'samba_use_hostcc', False\\):\\n        return\\n    if not bld.env.HOSTCC:\\n        return\\n    taskgen.env = taskgen.env.derive\\(\\)\\n    hostcc = TO_LIST\\(bld.env.HOSTCC\\)\\n    gccdeps_cflags = \\[flag for flag in TO_LIST\\(bld.env.CFLAGS\\) if flag in \\('-MD', '-MMD'\\)\\]\\n    host_cflags = TO_LIST\\(bld.env.HOST_CFLAGS\\) + gccdeps_cflags\\n    taskgen.env.CC = hostcc\\n    taskgen.env.LINK_CC = hostcc\\n    taskgen.env.CFLAGS = host_cflags\\n    taskgen.env.CPPFLAGS = TO_LIST\\(bld.env.HOST_CPPFLAGS\\)\\n    taskgen.env.LDFLAGS = []\\n    taskgen.env.LINKFLAGS = []\\n    taskgen.env.STLIB_MARKER = []\\n    taskgen.env.SHLIB_MARKER = []\\n    taskgen.env.FULLSTATIC_MARKER = []\\n    taskgen.env.EXTRA_CFLAGS = host_cflags\\n    taskgen.env.EXTRA_LDFLAGS = []\\n\\n/" \
        "$SAMBA4X_SRC_DIR/buildtools/wafsamba/wafsamba.py"
    patch_perl_any "Samba 4.x smbd static env helper patch" \
        "s/\\ndef SAMBA_BINARY/\\ndef APPLY_SMBD_STATIC_ENV\\(bld, taskgen\\):\\n    if getattr\\(taskgen, 'target', None\\) != 'smbd\\/smbd':\\n        return\\n    static_linkflags = TO_LIST\\(getattr\\(bld.env, 'SMBD_STATIC_LINKFLAGS', []\\)\\)\\n    static_ldflags = TO_LIST\\(getattr\\(bld.env, 'SMBD_STATIC_LDFLAGS', []\\)\\)\\n    if not static_linkflags and not static_ldflags:\\n        return\\n    taskgen.env = taskgen.env.derive\\(\\)\\n    taskgen.env.LINKFLAGS = static_linkflags\\n    taskgen.env.LDFLAGS = static_ldflags\\n    taskgen.env.LIBPATH = TO_LIST\\(getattr\\(bld.env, 'SMBD_STATIC_LIBPATH', []\\)\\)\\n    taskgen.env.SHLIB_MARKER = getattr\\(bld.env, 'SMBD_STATIC_SHLIB_MARKER', ''\\)\\n    taskgen.env.FULLSTATIC_MARKER = getattr\\(bld.env, 'SMBD_STATIC_FULLSTATIC_MARKER', '-static'\\)\\n\\ndef SAMBA_BINARY/" \
        "$SAMBA4X_SRC_DIR/buildtools/wafsamba/wafsamba.py"
    patch_require_fixed "Samba 4.x smbd static env helper patch" "def APPLY_SMBD_STATIC_ENV(bld, taskgen):" \
        "$SAMBA4X_SRC_DIR/buildtools/wafsamba/wafsamba.py"
    patch_perl_any "Samba 4.x hostcc binary task patch" \
        "s/samba_ldflags  = pie_ldflags\\n        \\)/samba_ldflags  = pie_ldflags,\\n        samba_use_hostcc = use_hostcc\\n        \\)\\n    APPLY_HOSTCC_ENV\\(bld, t\\)/" \
        "$SAMBA4X_SRC_DIR/buildtools/wafsamba/wafsamba.py"
    patch_perl_any "Samba 4.x smbd static binary task patch" \
        "s/(samba_use_hostcc = use_hostcc\\n        \\)\\n    APPLY_HOSTCC_ENV\\(bld, t\\))/\\1\\n    APPLY_SMBD_STATIC_ENV(bld, t)/" \
        "$SAMBA4X_SRC_DIR/buildtools/wafsamba/wafsamba.py"
    patch_require_fixed "Samba 4.x smbd static binary task patch" "APPLY_SMBD_STATIC_ENV(bld, t)" \
        "$SAMBA4X_SRC_DIR/buildtools/wafsamba/wafsamba.py"
    patch_perl_any "Samba 4.x hostcc subsystem task patch" \
        "s/samba_builtin_subsystem = None,\\n        \\)/samba_builtin_subsystem = None,\\n        \\)\\n\\n    APPLY_HOSTCC_ENV\\(bld, t\\)/" \
        "$SAMBA4X_SRC_DIR/buildtools/wafsamba/wafsamba.py"
    patch_perl_any "Samba 4.x tevent no-pthread destructor patch" \
        "s/(\\n\\t\\/\\*\\n\\t \\* We have to coordinate with _tevent_threaded_schedule_immediate's\\n)/\\n#ifdef HAVE_PTHREAD\\1/; s/(\\n\\treturn 0;\\n\\}\\n\\nstruct tevent_threaded_context \\*tevent_threaded_context_create)/\\n#endif\\1/" \
        "$SAMBA4X_SRC_DIR/lib/tevent/tevent_threads.c"
    patch_require_fixed "Samba 4.x tevent no-pthread destructor patch" "#ifdef HAVE_PTHREAD
	/*
	 * We have to coordinate with _tevent_threaded_schedule_immediate's" \
        "$SAMBA4X_SRC_DIR/lib/tevent/tevent_threads.c"
    patch_perl_any "Samba 4.x bundled popt glob symbol patch" \
        "s/static int\\nglob_pattern_p \\(const char \\* pattern, int quote\\)/static int\\npopt_glob_pattern_p (const char * pattern, int quote)/; s/\\n}\\n#endif\\t\\/\\* !defined\\(__GLIBC__\\) \\*\\//\\n}\\n#define glob_pattern_p popt_glob_pattern_p\\n#endif\\t\\/\\* !defined(__GLIBC__) \\*\\//" \
        "$SAMBA4X_SRC_DIR/third_party/popt/poptconfig.c"
    patch_require_fixed "Samba 4.x bundled popt glob symbol patch" "popt_glob_pattern_p (const char * pattern, int quote)" \
        "$SAMBA4X_SRC_DIR/third_party/popt/poptconfig.c"
    patch_perl_any "Samba 4.x no-pthread TLS configure patch" \
        "s/    if conf\\.CONFIG_SET\\('HAVE_PTHREAD'\\) and not conf\\.CONFIG_SET\\('HAVE___THREAD'\\):\\n        conf\\.fatal\\('Missing required TLS support in pthread library'\\)/    if conf.CONFIG_SET('HAVE_PTHREAD') and not conf.CONFIG_SET('HAVE___THREAD'):\\n        conf.msg('Missing required TLS support in pthread library', 'continuing; pthread is removed after configure')/" \
        "$SAMBA4X_SRC_DIR/lib/replace/wscript"
    patch_require_fixed "Samba 4.x no-pthread TLS configure patch" "continuing; pthread is removed after configure" \
        "$SAMBA4X_SRC_DIR/lib/replace/wscript"
    patch_perl_any "Samba 4.x renameat2 no-renameat fallback patch" \
        "s/\\n\\treturn renameat\\(__oldfd, __old, __newfd, __new\\);/\\n#ifdef HAVE_RENAMEAT\\n\\treturn renameat(__oldfd, __old, __newfd, __new);\\n#else\\n\\terrno = ENOSYS;\\n\\treturn -1;\\n#endif/" \
        "$SAMBA4X_SRC_DIR/lib/replace/replace.c"
    patch_require_fixed "Samba 4.x renameat2 no-renameat fallback patch" "errno = ENOSYS;" \
        "$SAMBA4X_SRC_DIR/lib/replace/replace.c"
    patch_perl_any "Samba 4.x missing at-family constants patch" \
        "s/#ifndef O_ACCMODE\\n#define O_ACCMODE \\(O_RDONLY \\| O_WRONLY \\| O_RDWR\\)\\n#endif\\n/#ifndef O_ACCMODE\\n#define O_ACCMODE (O_RDONLY | O_WRONLY | O_RDWR)\\n#endif\\n\\n#ifndef O_DIRECTORY\\n#define O_DIRECTORY 0\\n#endif\\n#ifndef AT_FDCWD\\n#define AT_FDCWD -100\\n#endif\\n#ifndef AT_REMOVEDIR\\n#define AT_REMOVEDIR 0x01\\n#endif\\n#ifndef AT_SYMLINK_NOFOLLOW\\n#define AT_SYMLINK_NOFOLLOW 0x02\\n#endif\\n\\n#ifndef HAVE_OPENAT\\nint openat(int dirfd, const char *path, int flags, ...);\\nint mkdirat(int dirfd, const char *path, mode_t mode);\\nint unlinkat(int dirfd, const char *path, int flags);\\nint symlinkat(const char *target, int newdirfd, const char *linkpath);\\nssize_t readlinkat(int dirfd, const char *path, char *buf, size_t bufsiz);\\nint linkat(int olddirfd, const char *oldpath, int newdirfd, const char *newpath, int flags);\\n#endif\\n/" \
        "$SAMBA4X_SRC_DIR/lib/replace/system/filesys.h"
    patch_require_fixed "Samba 4.x missing at-family constants patch" "int openat(int dirfd" \
        "$SAMBA4X_SRC_DIR/lib/replace/system/filesys.h"
    patch_perl_any "Samba 4.x missing at-family syscall fallback patch" \
        "s/#ifdef _WIN32\\n#define mkdir\\(d,m\\) _mkdir\\(d\\)\\n#endif\\n/#ifdef _WIN32\\n#define mkdir(d,m) _mkdir(d)\\n#endif\\n\\n#ifndef HAVE_OPENAT\\nstatic int rep_at_path_is_direct(int dirfd, const char *path)\\n{\\n\\treturn dirfd == AT_FDCWD || (path != NULL && path[0] == '\\/');\\n}\\n\\nint openat(int dirfd, const char *path, int flags, ...)\\n{\\n\\tmode_t mode = 0;\\n\\tif ((flags & O_CREAT) != 0) {\\n\\t\\tva_list ap;\\n\\t\\tva_start(ap, flags);\\n\\t\\tmode = va_arg(ap, mode_t);\\n\\t\\tva_end(ap);\\n\\t}\\n\\tif (!rep_at_path_is_direct(dirfd, path)) {\\n\\t\\terrno = ENOSYS;\\n\\t\\treturn -1;\\n\\t}\\n\\treturn open(path, flags, mode);\\n}\\n\\nint mkdirat(int dirfd, const char *path, mode_t mode)\\n{\\n\\tif (!rep_at_path_is_direct(dirfd, path)) {\\n\\t\\terrno = ENOSYS;\\n\\t\\treturn -1;\\n\\t}\\n\\treturn mkdir(path, mode);\\n}\\n\\nint unlinkat(int dirfd, const char *path, int flags)\\n{\\n\\tif (!rep_at_path_is_direct(dirfd, path)) {\\n\\t\\terrno = ENOSYS;\\n\\t\\treturn -1;\\n\\t}\\n\\tif ((flags & AT_REMOVEDIR) != 0) {\\n\\t\\treturn rmdir(path);\\n\\t}\\n\\treturn unlink(path);\\n}\\n\\nint symlinkat(const char *target, int newdirfd, const char *linkpath)\\n{\\n\\tif (!rep_at_path_is_direct(newdirfd, linkpath)) {\\n\\t\\terrno = ENOSYS;\\n\\t\\treturn -1;\\n\\t}\\n\\treturn symlink(target, linkpath);\\n}\\n\\nssize_t readlinkat(int dirfd, const char *path, char *buf, size_t bufsiz)\\n{\\n\\tif (!rep_at_path_is_direct(dirfd, path)) {\\n\\t\\terrno = ENOSYS;\\n\\t\\treturn -1;\\n\\t}\\n\\treturn readlink(path, buf, bufsiz);\\n}\\n\\nint linkat(int olddirfd, const char *oldpath, int newdirfd, const char *newpath, int flags)\\n{\\n\\tif (flags != 0 ||\\n\\t    !rep_at_path_is_direct(olddirfd, oldpath) ||\\n\\t    !rep_at_path_is_direct(newdirfd, newpath)) {\\n\\t\\terrno = ENOSYS;\\n\\t\\treturn -1;\\n\\t}\\n\\treturn link(oldpath, newpath);\\n}\\n#endif\\n/" \
        "$SAMBA4X_SRC_DIR/lib/replace/replace.c"
    patch_require_fixed "Samba 4.x missing at-family syscall fallback patch" "rep_at_path_is_direct" \
        "$SAMBA4X_SRC_DIR/lib/replace/replace.c"
    patch_perl_any "Samba 4.x NetBSD4 missing libc fallback patch" \
        "s/#endif\\n\\nvoid replace_dummy\\(void\\);/#endif\\n\\n#ifdef TC_SAMBA4X_NETBSD4_COMPAT\\nssize_t getline(char **lineptr, size_t *n, FILE *stream)\\n{\\n\\tint c = 0;\\n\\tsize_t pos = 0;\\n\\tchar *new_line = NULL;\\n\\tsize_t new_size = 0;\\n\\n\\tif (lineptr == NULL || n == NULL || stream == NULL) {\\n\\t\\terrno = EINVAL;\\n\\t\\treturn -1;\\n\\t}\\n\\tif (*lineptr == NULL || *n == 0) {\\n\\t\\t*n = 128;\\n\\t\\t*lineptr = malloc(*n);\\n\\t\\tif (*lineptr == NULL) {\\n\\t\\t\\treturn -1;\\n\\t\\t}\\n\\t}\\n\\n\\twhile ((c = fgetc(stream)) != EOF) {\\n\\t\\tif (pos + 1 >= *n) {\\n\\t\\t\\tnew_size = *n * 2;\\n\\t\\t\\tif (new_size <= *n) {\\n\\t\\t\\t\\terrno = ENOMEM;\\n\\t\\t\\t\\treturn -1;\\n\\t\\t\\t}\\n\\t\\t\\tnew_line = realloc(*lineptr, new_size);\\n\\t\\t\\tif (new_line == NULL) {\\n\\t\\t\\t\\treturn -1;\\n\\t\\t\\t}\\n\\t\\t\\t*lineptr = new_line;\\n\\t\\t\\t*n = new_size;\\n\\t\\t}\\n\\t\\t(*lineptr)[pos++] = (char)c;\\n\\t\\tif (c == '\\\\n') {\\n\\t\\t\\tbreak;\\n\\t\\t}\\n\\t}\\n\\tif (pos == 0 && c == EOF) {\\n\\t\\treturn -1;\\n\\t}\\n\\t(*lineptr)[pos] = '\\\\0';\\n\\treturn (ssize_t)pos;\\n}\\n\\nint fstatat(int dirfd, const char *path, struct stat *buf, int flags)\\n{\\n\\tif (!rep_at_path_is_direct(dirfd, path)) {\\n\\t\\terrno = ENOSYS;\\n\\t\\treturn -1;\\n\\t}\\n\\tif ((flags & ~AT_SYMLINK_NOFOLLOW) != 0) {\\n\\t\\terrno = ENOSYS;\\n\\t\\treturn -1;\\n\\t}\\n\\tif ((flags & AT_SYMLINK_NOFOLLOW) != 0) {\\n\\t\\treturn lstat(path, buf);\\n\\t}\\n\\treturn stat(path, buf);\\n}\\n\\nstatic void rep_timespecs_to_timevals(const struct timespec times[2], struct timeval tv[2])\\n{\\n\\ttv[0].tv_sec = times[0].tv_sec;\\n\\ttv[0].tv_usec = times[0].tv_nsec \\/ 1000;\\n\\ttv[1].tv_sec = times[1].tv_sec;\\n\\ttv[1].tv_usec = times[1].tv_nsec \\/ 1000;\\n}\\n\\nint futimens(int fd, const struct timespec times[2])\\n{\\n#ifdef HAVE_FUTIMES\\n\\tstruct timeval tv[2];\\n\\tstruct timeval *tvp = NULL;\\n\\tif (times != NULL) {\\n\\t\\trep_timespecs_to_timevals(times, tv);\\n\\t\\ttvp = tv;\\n\\t}\\n\\treturn futimes(fd, tvp);\\n#else\\n\\tif (times == NULL) {\\n\\t\\treturn 0;\\n\\t}\\n\\terrno = ENOSYS;\\n\\treturn -1;\\n#endif\\n}\\n\\nint utimensat(int dirfd, const char *path, const struct timespec times[2], int flags)\\n{\\n\\tstruct timeval tv[2];\\n\\tstruct timeval *tvp = NULL;\\n\\tif (!rep_at_path_is_direct(dirfd, path)) {\\n\\t\\terrno = ENOSYS;\\n\\t\\treturn -1;\\n\\t}\\n\\tif ((flags & ~AT_SYMLINK_NOFOLLOW) != 0) {\\n\\t\\terrno = ENOSYS;\\n\\t\\treturn -1;\\n\\t}\\n\\tif (times != NULL) {\\n\\t\\trep_timespecs_to_timevals(times, tv);\\n\\t\\ttvp = tv;\\n\\t}\\n\\tif ((flags & AT_SYMLINK_NOFOLLOW) != 0) {\\n#ifdef HAVE_LUTIMES\\n\\t\\treturn lutimes(path, tvp);\\n#else\\n\\t\\terrno = ENOSYS;\\n\\t\\treturn -1;\\n#endif\\n\\t}\\n\\treturn utimes(path, tvp);\\n}\\n\\nvoid arc4random_buf(void *buf, size_t n)\\n{\\n\\tunsigned char *p = buf;\\n\\tsize_t done = 0;\\n\\tint fd = open(\"\\/dev\\/urandom\", O_RDONLY);\\n\\tif (fd != -1) {\\n\\t\\twhile (done < n) {\\n\\t\\t\\tssize_t ret = read(fd, p + done, n - done);\\n\\t\\t\\tif (ret <= 0) {\\n\\t\\t\\t\\tbreak;\\n\\t\\t\\t}\\n\\t\\t\\tdone += ret;\\n\\t\\t}\\n\\t\\tclose(fd);\\n\\t\\tif (done == n) {\\n\\t\\t\\treturn;\\n\\t\\t}\\n\\t}\\n\\twhile (done < n) {\\n\\t\\tp[done++] = (unsigned char)random();\\n\\t}\\n}\\n#endif\\n\\nvoid replace_dummy(void);/" \
        "$SAMBA4X_SRC_DIR/lib/replace/replace.c"
    patch_require_fixed "Samba 4.x NetBSD4 missing libc fallback patch" "arc4random_buf" \
        "$SAMBA4X_SRC_DIR/lib/replace/replace.c"
    patch_perl_any "Samba 4.x Heimdal krb5 integer typedef guard patch" \
        "s/typedef Krb5Int32 krb5int32;\\ntypedef Krb5UInt32 krb5uint32;/#ifndef HEIMDAL_KRB5_INT_TYPES_DEFINED\\n#define HEIMDAL_KRB5_INT_TYPES_DEFINED\\ntypedef Krb5Int32 krb5int32;\\ntypedef Krb5UInt32 krb5uint32;\\n#endif/g" \
        "$SAMBA4X_SRC_DIR/third_party/heimdal/lib/krb5/krb5.h" \
        "$SAMBA4X_SRC_DIR/third_party/heimdal/lib/krb5/krb5_locl.h"
    patch_require_fixed "Samba 4.x Heimdal krb5 integer typedef guard patch" "HEIMDAL_KRB5_INT_TYPES_DEFINED" \
        "$SAMBA4X_SRC_DIR/third_party/heimdal/lib/krb5/krb5.h"
    patch_require_fixed "Samba 4.x Heimdal krb5 integer typedef guard patch" "HEIMDAL_KRB5_INT_TYPES_DEFINED" \
        "$SAMBA4X_SRC_DIR/third_party/heimdal/lib/krb5/krb5_locl.h"
    patch_perl_any "Samba 4.x Heimdal plugin typedef guard patch" \
        "s/typedef struct heim_pcontext_s \\*heim_pcontext;/#ifndef HEIMDAL_HEIM_PCONTEXT_TYPEDEF\\n#define HEIMDAL_HEIM_PCONTEXT_TYPEDEF\\ntypedef struct heim_pcontext_s *heim_pcontext;\\n#endif/g; s/typedef uintptr_t\\n\\(HEIM_LIB_CALL \\*heim_get_instance_func_t\\)\\(const char \\*\\);/#ifndef HEIMDAL_HEIM_GET_INSTANCE_TYPEDEF\\n#define HEIMDAL_HEIM_GET_INSTANCE_TYPEDEF\\ntypedef uintptr_t\\n(HEIM_LIB_CALL *heim_get_instance_func_t)(const char *);\\n#endif/g" \
        "$SAMBA4X_SRC_DIR/third_party/heimdal/lib/base/heimbase.h" \
        "$SAMBA4X_SRC_DIR/third_party/heimdal/lib/base/common_plugin.h" \
        "$SAMBA4X_SRC_DIR/third_party/heimdal/lib/base/log.c"
    patch_require_fixed "Samba 4.x Heimdal plugin typedef guard patch" "HEIMDAL_HEIM_PCONTEXT_TYPEDEF" \
        "$SAMBA4X_SRC_DIR/third_party/heimdal/lib/base/heimbase.h"
    patch_require_fixed "Samba 4.x Heimdal plugin typedef guard patch" "HEIMDAL_HEIM_GET_INSTANCE_TYPEDEF" \
        "$SAMBA4X_SRC_DIR/third_party/heimdal/lib/base/common_plugin.h"
    patch_require_fixed "Samba 4.x Heimdal plugin typedef guard patch" "HEIMDAL_HEIM_PCONTEXT_TYPEDEF" \
        "$SAMBA4X_SRC_DIR/third_party/heimdal/lib/base/log.c"
    patch_perl_any "Samba 4.x source3 SMB_STRUCT_STAT typedef guard patch" \
        "s/typedef struct stat_ex SMB_STRUCT_STAT;/#ifndef SAMBA_SMB_STRUCT_STAT_TYPEDEF\\n#define SAMBA_SMB_STRUCT_STAT_TYPEDEF\\ntypedef struct stat_ex SMB_STRUCT_STAT;\\n#endif/g" \
        "$SAMBA4X_SRC_DIR/source3/include/includes.h" \
        "$SAMBA4X_SRC_DIR/source3/param/loadparm.h"
    patch_require_fixed "Samba 4.x source3 SMB_STRUCT_STAT typedef guard patch" "SAMBA_SMB_STRUCT_STAT_TYPEDEF" \
        "$SAMBA4X_SRC_DIR/source3/include/includes.h"
    patch_require_fixed "Samba 4.x source3 SMB_STRUCT_STAT typedef guard patch" "SAMBA_SMB_STRUCT_STAT_TYPEDEF" \
        "$SAMBA4X_SRC_DIR/source3/param/loadparm.h"
    patch_perl_any "Samba 4.x source3 files_struct forward typedef guard patch" \
        "s/typedef struct files_struct files_struct;/#ifndef SAMBA_FILES_STRUCT_TYPEDEF\\n#define SAMBA_FILES_STRUCT_TYPEDEF\\ntypedef struct files_struct files_struct;\\n#endif/" \
        "$SAMBA4X_SRC_DIR/source3/param/loadparm.h"
    patch_perl_any "Samba 4.x source3 files_struct vfs typedef split patch" \
        "s/struct files_struct;\\n/struct files_struct;\\n#ifndef SAMBA_FILES_STRUCT_TYPEDEF\\n#define SAMBA_FILES_STRUCT_TYPEDEF\\ntypedef struct files_struct files_struct;\\n#endif\\n/" \
        "$SAMBA4X_SRC_DIR/source3/include/vfs.h"
    patch_perl_any "Samba 4.x source3 files_struct vfs definition patch" \
        "s/typedef struct files_struct \\{/struct files_struct {/" \
        "$SAMBA4X_SRC_DIR/source3/include/vfs.h"
    patch_perl_any "Samba 4.x source3 files_struct vfs terminator patch" \
        "s/\\n} files_struct;\\n/\\n};\\n/" \
        "$SAMBA4X_SRC_DIR/source3/include/vfs.h"
    patch_require_fixed "Samba 4.x source3 files_struct typedef guard patch" "SAMBA_FILES_STRUCT_TYPEDEF" \
        "$SAMBA4X_SRC_DIR/source3/param/loadparm.h"
    patch_require_fixed "Samba 4.x source3 files_struct typedef guard patch" "SAMBA_FILES_STRUCT_TYPEDEF" \
        "$SAMBA4X_SRC_DIR/source3/include/vfs.h"
    patch_perl_any "Samba 4.x missing O_CLOEXEC fallback patch" \
        "s/#include \"tdb_wrap.h\"\\n/#include \"tdb_wrap.h\"\\n\\n#ifndef O_CLOEXEC\\n#define O_CLOEXEC 0\\n#endif\\n/" \
        "$SAMBA4X_SRC_DIR/lib/tdb_wrap/tdb_wrap.c"
    patch_require_fixed "Samba 4.x missing O_CLOEXEC fallback patch" "#define O_CLOEXEC 0" \
        "$SAMBA4X_SRC_DIR/lib/tdb_wrap/tdb_wrap.c"
    patch_perl_any "Samba 4.x local named pipe no-posix-spawn fallback patch" \
        "s/#include <spawn.h>/#ifdef HAVE_POSIX_SPAWN\\n#include <spawn.h>\\n#else\\nstatic int samba_fork_exec(const char *path, char *const argv[], char *const envp[], pid_t *pid)\\n{\\n\\tpid_t child = fork();\\n\\tif (child == -1) {\\n\\t\\treturn errno;\\n\\t}\\n\\tif (child == 0) {\\n\\t\\texecve(path, argv, envp);\\n\\t\\t_exit(127);\\n\\t}\\n\\t*pid = child;\\n\\treturn 0;\\n}\\n#endif/; s/ret = posix_spawn\\(&pid, argv\\[0\\], NULL, NULL, argv, environ\\);/#ifdef HAVE_POSIX_SPAWN\\n\\tret = posix_spawn(&pid, argv[0], NULL, NULL, argv, environ);\\n#else\\n\\tret = samba_fork_exec(argv[0], argv, environ, &pid);\\n#endif/" \
        "$SAMBA4X_SRC_DIR/source3/rpc_client/local_np.c"
    patch_require_fixed "Samba 4.x local named pipe no-posix-spawn fallback patch" "samba_fork_exec" \
        "$SAMBA4X_SRC_DIR/source3/rpc_client/local_np.c"
    patch_perl_any "Samba 4.x no-thread keyword fallback patch" \
        "s/#define HAVE___THREAD/#define __thread\\n#define HAVE___THREAD/" \
        "$SAMBA4X_SRC_DIR/lib/replace/replace.h"
    patch_require_fixed "Samba 4.x no-thread keyword fallback patch" "#define __thread" \
        "$SAMBA4X_SRC_DIR/lib/replace/replace.h"

    git -C "$SAMBA4X_SRC_DIR" rev-parse --short HEAD
    git -C "$SAMBA4X_SRC_DIR" log -1 --format='%H%n%cd%n%s' --date=iso
    echo "Finished Samba 4.x download workflow at $(date -u)"
} >"$SAMBA4X_DOWNLOAD_LOG" 2>&1

printf 'Samba 4.x download complete.\n'
printf 'Log: %s\n' "$SAMBA4X_DOWNLOAD_LOG"
