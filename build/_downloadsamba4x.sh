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
    patch_apply_checked "Samba 4.x hostcc waf env patch" \
        "$PATCH_DIR/0026-waf-hostcc-env.patch" \
        "$SAMBA4X_SRC_DIR"
    # Time Capsule deploys only smbd to the RAM disk. Keep static link flags
    # scoped to smbd instead of leaking them into host tools or helper binaries.
    patch_apply_checked "Samba 4.x static smbd waf env patch" \
        "$PATCH_DIR/0027-waf-static-smbd-env.patch" \
        "$SAMBA4X_SRC_DIR"
    # The no-pthread appliance build does not create tevent threaded contexts,
    # so avoid compiling destructor pthread coordination that assumes pthreads.
    patch_apply_checked "Samba 4.x no-pthread tevent threaded destructor patch" \
        "$PATCH_DIR/0028-no-pthread-tevent-threaded-destructor.patch" \
        "$SAMBA4X_SRC_DIR"
    # Samba's bundled popt collides with libc glob_pattern_p on these static
    # NetBSD lanes. Rename the bundled fallback symbol locally.
    patch_apply_checked "Samba 4.x bundled popt glob symbol patch" \
        "$PATCH_DIR/0029-popt-glob-symbol-rename.patch" \
        "$SAMBA4X_SRC_DIR"
    # Configure sees pthreads on the build VM before we scrub them from the
    # target cache. Do not fail this transient TLS check in appliance builds.
    patch_apply_checked "Samba 4.x no-pthread TLS configure patch" \
        "$PATCH_DIR/0030-no-pthread-tls-configure-warning.patch" \
        "$SAMBA4X_SRC_DIR"

    # NetBSD4 needs libc symbol shims. NetBSD6/7 use the native libc symbols.
    patch_apply_checked "Samba 4.x NetBSD4 replace compatibility patch" \
        "$PATCH_DIR/0001-netbsd4-replace-compat.patch" \
        "$SAMBA4X_SRC_DIR"
    # The Time Capsule runtime kernels are older than the NetBSD SDKs used to
    # compile Samba 4.24. Even the NetBSD 6 appliance binary is built with the
    # NetBSD 7 SDK, so source3 must not rely on runtime *at syscalls being
    # present. Keep the path-aware VFS fallback enabled for every appliance
    # build through TC_SAMBA4X_VFS_AT_PATH_COMPAT.
    patch_apply_checked "Samba 4.x NetBSD source3 VFS at-path fallback patch" \
        "$PATCH_DIR/0002-netbsd-vfs-at-path-fallbacks.patch" \
        "$SAMBA4X_SRC_DIR"
    # NetBSD4 runtimes can reach SMB2 QUERY_DIRECTORY with a directory handle
    # whose stat mode is still a directory but whose cached is_directory flag is
    # false. Restore the flag from stat before rejecting the query so SMB2
    # directory listings work on those targets.
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
    # cleanupd is linked into smbd and cleans dead child messaging state, but
    # the no-pthread appliance build cannot safely fork Samba's cleanup helper.
    # Host cleanupd in the smbd parent event loop instead of disabling it.
    patch_apply_checked "Samba 4.x no-pthread in-parent cleanupd patch" \
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
    # On no-pthread appliance builds, parent that retained DB state to an
    # explicit named context instead of Samba's process-global talloc stack.
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
    # Samba 4.24 scavenges disconnected durable handles via smbd/scavenger.c.
    # The no-pthread appliance build avoids helper daemon forks, so run those
    # timeout timers on the smbd parent event loop to keep smbXsrv_open_global
    # records from lingering until the lock ramdisk fills.
    patch_apply_checked "Samba 4.x no-pthread in-parent scavenger patch" \
        "$PATCH_DIR/0025-no-pthread-scavenger-in-parent.patch" \
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
    # Samba 4.24's bundled Heimdal headers can expose the same typedefs through
    # multiple include paths in this reduced static build. Guard them explicitly
    # so duplicate typedef failures remain narrow and reviewable.
    patch_apply_checked "Samba 4.x Heimdal typedef guard patch" \
        "$PATCH_DIR/0031-heimdal-typedef-guards.patch" \
        "$SAMBA4X_SRC_DIR"
    # Samba's source3 headers are pulled into this nonshared smbd build in an
    # order that can repeat a few typedefs. Guard the typedefs instead of
    # changing include order, which is more fragile across Samba releases.
    patch_apply_checked "Samba 4.x source3 typedef guard patch" \
        "$PATCH_DIR/0032-source3-typedef-guards.patch" \
        "$SAMBA4X_SRC_DIR"
    # NetBSD4 headers do not define O_CLOEXEC. NetBSD 6/7 already define it;
    # this fallback only compiles if the target headers are missing the value.
    patch_apply_checked "Samba 4.x missing O_CLOEXEC fallback patch" \
        "$PATCH_DIR/0033-tdb-wrap-o-cloexec-fallback.patch" \
        "$SAMBA4X_SRC_DIR"
    # NetBSD4 lacks posix_spawn(), while NetBSD 6/7 keep the native path. The
    # fallback preserves the local named-pipe helper's fork/exec behavior rather
    # than removing that code from the file-server build.
    patch_apply_checked "Samba 4.x local named pipe no-posix-spawn fallback patch" \
        "$PATCH_DIR/0034-local-np-no-posix-spawn-fallback.patch" \
        "$SAMBA4X_SRC_DIR"
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
    patch_apply_checked "Samba 4.x no-thread keyword fallback patch" \
        "$PATCH_DIR/0035-no-pthread-thread-keyword-fallback.patch" \
        "$SAMBA4X_SRC_DIR"

    git -C "$SAMBA4X_SRC_DIR" rev-parse --short HEAD
    git -C "$SAMBA4X_SRC_DIR" log -1 --format='%H%n%cd%n%s' --date=iso
    echo "Finished Samba 4.x download workflow at $(date -u)"
} >"$SAMBA4X_DOWNLOAD_LOG" 2>&1

printf 'Samba 4.x download complete.\n'
printf 'Log: %s\n' "$SAMBA4X_DOWNLOAD_LOG"
