#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"
. "$(dirname "$0")/_patch_helpers.sh"

PATCH_DIR="$(CDPATH= cd "$(dirname "$0")/patches/samba4x" && pwd)"

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
    patch_perl_any "Samba 4.x static service binary env helper patch" \
        "s/\\ndef SAMBA_BINARY/\\ndef APPLY_SMBD_STATIC_ENV\\(bld, taskgen\\):\\n    # Time Capsule runs smbd from a tiny RAM disk. Keep the deployed binary\\n    # fully static and apply the static link flags only to smbd; external\\n    # DCE-RPC helper binaries are intentionally not shipped because executing\\n    # them from the HFS data disk is unsafe if Apple firmware unmounts it.\\n    if getattr\\(taskgen, 'target', None\\) != 'smbd\\/smbd':\\n        return\\n    static_linkflags = TO_LIST\\(getattr\\(bld.env, 'SMBD_STATIC_LINKFLAGS', []\\)\\)\\n    static_ldflags = TO_LIST\\(getattr\\(bld.env, 'SMBD_STATIC_LDFLAGS', []\\)\\)\\n    if not static_linkflags and not static_ldflags:\\n        return\\n    taskgen.env = taskgen.env.derive\\(\\)\\n    taskgen.env.LINKFLAGS = static_linkflags\\n    taskgen.env.LDFLAGS = static_ldflags\\n    taskgen.env.LIBPATH = TO_LIST\\(getattr\\(bld.env, 'SMBD_STATIC_LIBPATH', []\\)\\)\\n    taskgen.env.SHLIB_MARKER = getattr\\(bld.env, 'SMBD_STATIC_SHLIB_MARKER', ''\\)\\n    taskgen.env.FULLSTATIC_MARKER = getattr\\(bld.env, 'SMBD_STATIC_FULLSTATIC_MARKER', '-static'\\)\\n\\ndef SAMBA_BINARY/" \
        "$SAMBA4X_SRC_DIR/buildtools/wafsamba/wafsamba.py"
    patch_require_fixed "Samba 4.x static service binary env helper patch" "if getattr(taskgen, 'target', None) != 'smbd/smbd':" \
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

    # NetBSD4 needs libc symbol shims. NetBSD6/7 use the native libc symbols.
    patch_apply_checked "Samba 4.x NetBSD4 replace compatibility patch" \
        "$PATCH_DIR/0001-netbsd4-replace-compat.patch" \
        "$SAMBA4X_SRC_DIR"
    # The Time Capsule NetBSD kernels/libcs are too old for Samba 4.24's
    # source3 VFS path to rely on the modern *at/fdopendir behavior. NetBSD6
    # reaches ENOSYS during share-root tree connect without these fallbacks, so
    # this patch is appliance-wide and is enabled by TC_SAMBA4X_VFS_AT_PATH_COMPAT.
    patch_apply_checked "Samba 4.x NetBSD source3 VFS at-path fallback patch" \
        "$PATCH_DIR/0002-netbsd-vfs-at-path-fallbacks.patch" \
        "$SAMBA4X_SRC_DIR"
    # NetBSD 6/7 also lack Linux openat2() semantics. Samba's default VFS can
    # request those constraints while connecting the share root, where returning
    # ENOSYS is not retried. Disable the unsupported constraints and continue
    # through the ordinary openat() fallback in the same call.
    patch_apply_checked "Samba 4.x NetBSD openat2 ENOSYS fallback patch" \
        "$PATCH_DIR/0017-netbsd-openat2-enosys-fallback.patch" \
        "$SAMBA4X_SRC_DIR"
    # The NetBSD4 big-endian runtime can reach SMB2 QUERY_DIRECTORY with a
    # directory handle whose stat mode is still a directory but whose cached
    # is_directory flag is false. Restore the flag from stat before rejecting
    # the query so SMB2 directory listings work on that target.
    patch_apply_checked "Samba 4.x SMB2 query directory stat fallback patch" \
        "$PATCH_DIR/0018-smb2-query-directory-stat-directory-fallback.patch" \
        "$SAMBA4X_SRC_DIR"
    # NetBSD4 lacks fdopendir(), and Samba's generic replace fallback reports
    # that as NOT_SUPPORTED during SMB2 directory enumeration. Use the default
    # VFS layer's tracked pathname for directory streams on appliance builds.
    patch_apply_checked "Samba 4.x NetBSD VFS fdopendir path fallback patch" \
        "$PATCH_DIR/0019-netbsd-vfs-fdopendir-path-fallback.patch" \
        "$SAMBA4X_SRC_DIR"
    # Finder and smbclient -L enumerate shares through IPC$ -> \PIPE\srvsvc.
    # The Time Capsule runtime copies only smbd onto the RAM disk because the
    # Apple-managed HFS disk can be unmounted later. Embed exactly srvsvc in
    # smbd and leave other DCE/RPC helpers out of the deployed artifact.
    patch_apply_checked "Samba 4.x embedded srvsvc named pipe patch" \
        "$PATCH_DIR/0020-smbd-embedded-srvsvc.patch" \
        "$SAMBA4X_SRC_DIR"

    # smbd -V exits through Samba's shared popt callback before server.c can
    # free its startup talloc frame. Patch smbd itself so both NetBSD4 and
    # NetBSD6/7 binaries print a clean version string.
    patch_apply_checked "Samba 4.x smbd early version cleanup patch" \
        "$PATCH_DIR/0003-smbd-version-no-dangling-frame.patch" \
        "$SAMBA4X_SRC_DIR"
    # tdb_open_ex() sets FD_CLOEXEC after the initial open, but tdb_reopen()
    # replaces the fd and used to lose that flag. Keep reopened TDB handles out
    # of helper execs on every target; this is not NetBSD4-specific.
    patch_apply_checked "Samba 4.x TDB reopen close-on-exec patch" \
        "$PATCH_DIR/0004-tdb-reopen-cloexec.patch" \
        "$SAMBA4X_SRC_DIR"
    # The no-pthread static appliance build aborts while registering Samba's
    # MSG_REQ_POOL_USAGE diagnostic filtered reader during smbd startup. That
    # hook only supports smbcontrol memory reports, so skip it for the appliance.
    patch_apply_checked "Samba 4.x no-pthread pool usage diagnostic skip patch" \
        "$PATCH_DIR/0005-no-pthread-skip-pool-usage.patch" \
        "$SAMBA4X_SRC_DIR"
    # cleanupd is an auxiliary smbd helper loop that aborts in the static
    # no-pthread appliance build before TCP/445 can bind. Keep notifyd enabled:
    # SMB tree connects with the default "change notify = yes" require the
    # notify-daemon registration.
    patch_apply_checked "Samba 4.x no-pthread helper daemon skip patch" \
        "$PATCH_DIR/0006-no-pthread-skip-helper-daemons.patch" \
        "$SAMBA4X_SRC_DIR"
    # The static appliance build has no winbindd. Keep in-process Unix SID and
    # legacy local mappings, but do not ask libwbclient to resolve the remaining
    # Windows SIDs during startup token construction.
    patch_apply_checked "Samba 4.x no-pthread local SID mapping patch" \
        "$PATCH_DIR/0007-no-pthread-local-sid-to-unixids.patch" \
        "$SAMBA4X_SRC_DIR"
    # Some Samba filtered messaging readers still use direct abort() for event
    # context bookkeeping assertions. In the static no-pthread appliance build,
    # recover from stale registrations instead of killing smbd during startup or
    # helper teardown.
    patch_apply_checked "Samba 4.x no-pthread messaging event context recovery patch" \
        "$PATCH_DIR/0008-no-pthread-messaging-event-context-recovery.patch" \
        "$SAMBA4X_SRC_DIR"
    # Samba's datagram messaging layer normally creates a pthreadpool-backed
    # queue for the rare case where a Unix datagram socket is full. The static
    # no-pthread appliance build must stay on the direct nonblocking path:
    # pthreadpool_tevent registers atfork/assert code that aborts notifyd and
    # client children on NetBSD Time Capsules.
    patch_apply_checked "Samba 4.x no-pthread messaging datagram direct-send patch" \
        "$PATCH_DIR/0009-no-pthread-messaging-dgm-no-pthreadpool.patch" \
        "$SAMBA4X_SRC_DIR"
    # Keep change notify available on the no-pthread appliance build, but run
    # notifyd's long-lived request on the smbd parent event loop. Forking a
    # separate notifyd helper still hits raw abort paths on these NetBSD targets.
    patch_apply_checked "Samba 4.x no-pthread in-parent notifyd patch" \
        "$PATCH_DIR/0010-no-pthread-notifyd-in-parent.patch" \
        "$SAMBA4X_SRC_DIR"
    # smbXsrv_version_global_init keeps a global db context after returning.
    # On the no-pthread static build, using a nested process-global talloc stack
    # frame in that startup helper aborts while smbd is still initializing.
    patch_apply_checked "Samba 4.x no-pthread smbXsrv version plain talloc frame patch" \
        "$PATCH_DIR/0011-no-pthread-smbxsrv-version-plain-talloc-frame.patch" \
        "$SAMBA4X_SRC_DIR"
    # The client/session SMBX global databases are normally wrapped by
    # db_open_watched(), which uses messaging filtered readers for invalidation.
    # On the no-pthread appliance build, those messaging paths can raw-abort
    # during startup. Keep the underlying locked TDBs and skip only the watched
    # cache wrapper for this single-node file server.
    patch_apply_checked "Samba 4.x no-pthread SMBX global DB watched wrapper skip patch" \
        "$PATCH_DIR/0012-no-pthread-smbxsrv-unwatched-global-dbs.patch" \
        "$SAMBA4X_SRC_DIR"
    # notifyd is a long-lived request hosted by the event loop. In no-pthread
    # appliance builds, parent it like notifydd does so tevent request creation
    # does not abort while smbd is still starting up.
    patch_apply_checked "Samba 4.x no-pthread notifyd event context owner patch" \
        "$PATCH_DIR/0014-no-pthread-notifyd-event-context-owner.patch" \
        "$SAMBA4X_SRC_DIR"
    # tevent's pooled request allocator is an optimization. In the no-pthread
    # static appliance build it aborts while notifyd creates its startup request
    # on NetBSD Time Capsules, so use ordinary talloc children for tevent
    # requests in that build.
    patch_apply_checked "Samba 4.x no-pthread tevent request plain talloc patch" \
        "$PATCH_DIR/0015-no-pthread-tevent-req-plain-talloc.patch" \
        "$SAMBA4X_SRC_DIR"
    # tevent's call-depth hooks are thread-local diagnostics. The static
    # no-pthread appliance build can abort while invoking that TLS callback
    # during notifyd startup, so make the tracking hook a no-op there.
    patch_apply_checked "Samba 4.x no-pthread tevent call-depth noop patch" \
        "$PATCH_DIR/0016-no-pthread-tevent-call-depth-noop.patch" \
        "$SAMBA4X_SRC_DIR"
    # Samba 4.24's bundled Heimdal headers can expose the same integer typedefs
    # through multiple include paths in this reduced static build. Guard them
    # explicitly; this is harmless on NetBSD 6/7 and required for the NetBSD4
    # static lane to avoid duplicate typedef errors.
    patch_perl_any "Samba 4.x Heimdal krb5 integer typedef guard patch" \
        "s/typedef Krb5Int32 krb5int32;\\ntypedef Krb5UInt32 krb5uint32;/#ifndef HEIMDAL_KRB5_INT_TYPES_DEFINED\\n#define HEIMDAL_KRB5_INT_TYPES_DEFINED\\ntypedef Krb5Int32 krb5int32;\\ntypedef Krb5UInt32 krb5uint32;\\n#endif/g" \
        "$SAMBA4X_SRC_DIR/third_party/heimdal/lib/krb5/krb5.h" \
        "$SAMBA4X_SRC_DIR/third_party/heimdal/lib/krb5/krb5_locl.h"
    patch_require_fixed "Samba 4.x Heimdal krb5 integer typedef guard patch" "HEIMDAL_KRB5_INT_TYPES_DEFINED" \
        "$SAMBA4X_SRC_DIR/third_party/heimdal/lib/krb5/krb5.h"
    patch_require_fixed "Samba 4.x Heimdal krb5 integer typedef guard patch" "HEIMDAL_KRB5_INT_TYPES_DEFINED" \
        "$SAMBA4X_SRC_DIR/third_party/heimdal/lib/krb5/krb5_locl.h"
    # The Heimdal plugin typedefs have the same duplicate-include problem in
    # the library sources. Keep the change narrow so a future Samba update fails
    # in the helper if the upstream text changes.
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
    # Samba's source3 headers are pulled into this nonshared smbd build in an
    # order that can repeat a few typedefs. Guard the typedefs instead of
    # changing include order, which is more fragile across Samba releases.
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
    # NetBSD4 headers do not define O_CLOEXEC. NetBSD 6/7 already define it;
    # this fallback only compiles if the target headers are missing the value.
    patch_perl_any "Samba 4.x missing O_CLOEXEC fallback patch" \
        "s/#include \"tdb_wrap.h\"\\n/#include \"tdb_wrap.h\"\\n\\n#ifndef O_CLOEXEC\\n#define O_CLOEXEC 0\\n#endif\\n/" \
        "$SAMBA4X_SRC_DIR/lib/tdb_wrap/tdb_wrap.c"
    patch_require_fixed "Samba 4.x missing O_CLOEXEC fallback patch" "#define O_CLOEXEC 0" \
        "$SAMBA4X_SRC_DIR/lib/tdb_wrap/tdb_wrap.c"
    # NetBSD4 lacks posix_spawn(), while NetBSD 6/7 keep the native path. The
    # fallback preserves the local named-pipe helper's fork/exec behavior rather
    # than removing that code from the file-server build.
    patch_perl_any "Samba 4.x local named pipe no-posix-spawn fallback patch" \
        "s/#include <spawn.h>/#ifdef HAVE_POSIX_SPAWN\\n#include <spawn.h>\\n#else\\nstatic int samba_fork_exec(const char *path, char *const argv[], char *const envp[], pid_t *pid)\\n{\\n\\tpid_t child = fork();\\n\\tif (child == -1) {\\n\\t\\treturn errno;\\n\\t}\\n\\tif (child == 0) {\\n\\t\\texecve(path, argv, envp);\\n\\t\\t_exit(127);\\n\\t}\\n\\t*pid = child;\\n\\treturn 0;\\n}\\n#endif/; s/ret = posix_spawn\\(&pid, argv\\[0\\], NULL, NULL, argv, environ\\);/#ifdef HAVE_POSIX_SPAWN\\n\\tret = posix_spawn(&pid, argv[0], NULL, NULL, argv, environ);\\n#else\\n\\tret = samba_fork_exec(argv[0], argv, environ, &pid);\\n#endif/" \
        "$SAMBA4X_SRC_DIR/source3/rpc_client/local_np.c"
    patch_require_fixed "Samba 4.x local named pipe no-posix-spawn fallback patch" "samba_fork_exec" \
        "$SAMBA4X_SRC_DIR/source3/rpc_client/local_np.c"
    # NetBSD4 has no spawn.h/posix_spawn(). Printing is not enabled in the
    # appliance config, but queue_process.c still compiles into the static smbd
    # target. Keep it buildable with the same fork/exec helper shape used by
    # local_np.c, while NetBSD 6/7 continue to use native posix_spawn().
    patch_apply_checked "Samba 4.x NetBSD4 print queue no-posix-spawn fallback patch" \
        "$PATCH_DIR/0024-netbsd4-print-queue-no-posix-spawn.patch" \
        "$SAMBA4X_SRC_DIR"
    # Our appliance build removes pthread support after configure. Make any
    # remaining __thread uses compile as process-global storage for that
    # no-pthread mode on both NetBSD4 and NetBSD6/7.
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
