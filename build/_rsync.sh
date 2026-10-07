#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"
. "$(dirname "$0")/_data_segment_check.sh"
. "$(dirname "$0")/_netbsd4_notes.sh"

TOOLDIR="$TOOLS"
DESTDIR="$OBJ/destdir.evbarm"
TRIPLE="$(select_tool_triple)"
SYSROOT="$DESTDIR"

if [ ! -x "$TOOLDIR/bin/nbmake" ] || [ ! -d "$DESTDIR" ]; then
    echo "Missing toolchain/sysroot under $OUT"
    echo "Run $SDK_BOOTSTRAP_WRAPPER first."
    exit 1
fi

if [ ! -x "$RSYNC_SRC_DIR/configure" ]; then
    echo "Missing rsync source tree at $RSYNC_SRC_DIR"
    echo "Run ./build/downloadrsync.sh first."
    exit 1
fi

export PATH="$TOOLDIR/bin:/usr/pkg/bin:$PATH"
export AR="$TOOLDIR/bin/$TRIPLE-ar"
export RANLIB="$TOOLDIR/bin/$TRIPLE-ranlib"
export STRIP="$TOOLDIR/bin/$TRIPLE-strip"

RSYNC_CFLAGS="${RSYNC_CFLAGS:--Os -ffunction-sections -fdata-sections -fomit-frame-pointer -fno-unwind-tables -fno-asynchronous-unwind-tables -fno-ident -fno-pie -fcommon -D_NETBSD_SOURCE -D_LARGEFILE_SOURCE -D_FILE_OFFSET_BITS=64 -D_LARGE_FILES}"
RSYNC_CPPFLAGS="${RSYNC_CPPFLAGS:--D_NETBSD_SOURCE -D_LARGEFILE_SOURCE -D_FILE_OFFSET_BITS=64 -D_LARGE_FILES}"

if [ "$SDK_FAMILY" = "netbsd4" ]; then
    # NetBSD 4's cross linker does not honor --sysroot. Explicit -B/-L paths
    # keep startup objects and libc from the target sysroot while preserving a
    # fully static rsync binary for devices that cannot load dynamic binaries.
    export CC="$TOOLDIR/bin/$TRIPLE-gcc"
    RSYNC_CFLAGS="$RSYNC_CFLAGS -B$DESTDIR/usr/lib -B$DESTDIR/usr/lib/csu -isystem $DESTDIR/usr/include"
    RSYNC_CPPFLAGS="$RSYNC_CPPFLAGS -isystem $DESTDIR/usr/include"
    RSYNC_LDFLAGS="${RSYNC_LDFLAGS_NETBSD4:--static -L$DESTDIR/lib -L$DESTDIR/usr/lib -B$DESTDIR/usr/lib -B$DESTDIR/usr/lib/csu}"
else
    export CC="$TOOLDIR/bin/$TRIPLE-gcc --sysroot=$SYSROOT"
    RSYNC_CPPFLAGS="$RSYNC_CPPFLAGS -I$SYSROOT/usr/include"
    RSYNC_LDFLAGS="${RSYNC_LDFLAGS:--static -Wl,--gc-sections -L$SYSROOT/lib -L$SYSROOT/usr/lib}"
fi

export CFLAGS="$RSYNC_CFLAGS"
export CPPFLAGS="$RSYNC_CPPFLAGS"
export LDFLAGS="$RSYNC_LDFLAGS"

RSYNC_MAKE="${RSYNC_MAKE:-gmake}"
# Every build starts from an empty build directory: make keeps an object
# whose source did not change, so objects compiled with other flags would be
# linked into the new binary, and an rsync built with other link flags would
# not be relinked. RSYNC_BUILD can be overridden, so refuse a directory that
# holds the source or the staged output rather than delete it.
case "$RSYNC_BUILD" in
    ""|/|"$RSYNC_SRC_DIR"|"$RSYNC_SRC_DIR"/*|"$RSYNC_STAGE"|"$RSYNC_STAGE"/*)
        echo "Refusing to empty RSYNC_BUILD=$RSYNC_BUILD (source or stage directory)"
        exit 1
        ;;
esac
case "$RSYNC_SRC_DIR/" in
    "$RSYNC_BUILD"/*)
        echo "Refusing to empty RSYNC_BUILD=$RSYNC_BUILD, which holds RSYNC_SRC_DIR"
        exit 1
        ;;
esac
rm -rf "$RSYNC_BUILD"
mkdir -p "$RSYNC_WORK" "$RSYNC_BUILD" "$RSYNC_STAGE" "$RSYNC_STAGE/bin" "$(dirname "$RSYNC_LOG")"

{
    echo "SDK_FAMILY=$SDK_FAMILY"
    echo "DEVICE_FAMILY=$DEVICE_FAMILY"
    echo "NETBSD4_ABI=$NETBSD4_ABI"
    echo "RSYNC_VERSION=$RSYNC_VERSION"
    echo "RSYNC_SRC_DIR=$RSYNC_SRC_DIR"
    echo "RSYNC_BUILD=$RSYNC_BUILD"
    echo "RSYNC_STAGE=$RSYNC_STAGE"
    echo "RSYNC_BIN_NAME=$RSYNC_BIN_NAME"
    echo "TOOLDIR=$TOOLDIR"
    echo "DESTDIR=$DESTDIR"
    echo "TRIPLE=$TRIPLE"
    echo "HOST_ALIAS=$HOST_ALIAS"
    echo "CC=$CC"
    echo "AR=$AR"
    echo "RANLIB=$RANLIB"
    echo "STRIP=$STRIP"
    echo "CFLAGS=$CFLAGS"
    echo "CPPFLAGS=$CPPFLAGS"
    echo "LDFLAGS=$LDFLAGS"

    cd "$RSYNC_BUILD"
    "$RSYNC_SRC_DIR/configure" \
        --host="$HOST_ALIAS" \
        --prefix="$RSYNC_STAGE" \
        --with-included-zlib \
        --with-included-popt \
        --disable-openssl \
        --disable-xxhash \
        --disable-zstd \
        --disable-lz4 \
        --disable-acl-support \
        --disable-xattr-support \
        --disable-iconv-open \
        --disable-iconv \
        --disable-locale \
        --disable-md2man

    if [ "$SDK_FAMILY" = "netbsd4" ]; then
        # --gc-sections and the NetBSD notes it would drop (_netbsd4_notes.sh)
        # go only on make's final link, as on NetBSD 6 below.
        netbsd4_keep_notes_inputs "$RSYNC_BUILD/netbsd4-notes" || exit 1
        "$RSYNC_MAKE" -j"$RSYNC_JOBS" LDFLAGS="$LDFLAGS $NETBSD4_KEEP_NOTES_LDFLAGS"
    else
        # rsync's workers keep running the forked image, so on NetBSD 6 every
        # fork() goes through the fork repair (_data_segment_check.sh; the
        # source is Samba patch 0070's overlay file). Its object and wrappers
        # go only on make's final link: configure links its probes with
        # LDFLAGS, and a probe calling fork or mmap would fail to link.
        $CC $CPPFLAGS $CFLAGS $TC_FORK_REPAIR_CFLAGS \
            -c "$SCRIPT_DIR/patches/samba4x/overlay/lib/replace/tc_fork_repair.c" \
            -o "$RSYNC_BUILD/tc_fork_repair.o"
        "$RSYNC_MAKE" -j"$RSYNC_JOBS" \
            LDFLAGS="$LDFLAGS $TC_FORK_REPAIR_LDFLAGS $RSYNC_BUILD/tc_fork_repair.o"
    fi

    if [ ! -f "$RSYNC_BUILD/rsync" ]; then
        echo "Unable to locate built rsync at $RSYNC_BUILD/rsync"
        exit 1
    fi

    # rsync patch 0003's constructor keeps .data writes on Apple's kernels.
    verify_data_faultahead "$RSYNC_BUILD/rsync" tc_disable_data_faultahead || exit 1
    if [ "$SDK_FAMILY" = "netbsd4" ]; then
        netbsd4_require_notes "$RSYNC_BUILD/rsync" || exit 1
    else
        verify_fork_repair "$RSYNC_BUILD/rsync" || exit 1
    fi
    cp "$RSYNC_BUILD/rsync" "$RSYNC_STAGE/bin/$RSYNC_BIN_NAME"
    cp "$RSYNC_STAGE/bin/$RSYNC_BIN_NAME" "$RSYNC_STAGE/$RSYNC_BIN_NAME.stripped"
    "$STRIP" --strip-unneeded "$RSYNC_STAGE/$RSYNC_BIN_NAME.stripped"

    "$TOOLDIR/bin/nbfile" "$RSYNC_STAGE/bin/$RSYNC_BIN_NAME" 2>&1 || true
    "$TOOLDIR/bin/nbfile" "$RSYNC_STAGE/$RSYNC_BIN_NAME.stripped" 2>&1 || true
    if "$TOOLDIR/bin/$TRIPLE-objdump" -p "$RSYNC_STAGE/$RSYNC_BIN_NAME.stripped" | grep -Eq '^[[:space:]]+(INTERP|DYNAMIC)'; then
        echo "rsync has dynamic ELF headers; refusing to stage it."
        exit 1
    fi
    "$TOOLDIR/bin/$TRIPLE-objdump" -p "$RSYNC_STAGE/$RSYNC_BIN_NAME.stripped" | sed -n '1,120p'
} >"$RSYNC_LOG" 2>&1

printf 'rsync build complete.\n'
printf 'Log: %s\n' "$RSYNC_LOG"
printf 'Regular binary: %s\n' "$RSYNC_STAGE/bin/$RSYNC_BIN_NAME"
printf 'Stripped binary: %s\n' "$RSYNC_STAGE/$RSYNC_BIN_NAME.stripped"
