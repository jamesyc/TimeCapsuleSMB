#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"

TOOLDIR="$TOOLS"
DESTDIR="$OBJ/destdir.evbarm"
TRIPLE="$(select_tool_triple)"
DISCOVERY_SRC="${DISCOVERY_SRC:-$SCRIPT_DIR/native/discovery.sources}"
# -D_DNS_SD_LIBDISPATCH=0: the vendored Apple dns_sd client stub
# (build/native/dnssd) must not assume libdispatch/GCD on NetBSD.
DISCOVERY_CFLAGS="${DISCOVERY_CFLAGS:--Os -fomit-frame-pointer -ffunction-sections -fdata-sections -fno-unwind-tables -fno-asynchronous-unwind-tables -fno-ident -D_DNS_SD_LIBDISPATCH=0}"
DISCOVERY_LDFLAGS="${DISCOVERY_LDFLAGS:--static -Wl,--gc-sections}"

if [ "$SDK_FAMILY" = "netbsd4" ]; then
    # NetBSD 4's arm--netbsdelf linker was not configured for --sysroot.
    # Keep this helper on the conservative no-GC link path so crt note
    # sections survive and the binary remains executable on the old kernel.
    DISCOVERY_CC_SYSROOT_FLAGS=""
    DISCOVERY_CFLAGS="$DISCOVERY_CFLAGS -B$DESTDIR/usr/lib -B$DESTDIR/usr/lib/csu"
    DISCOVERY_LDFLAGS="${DISCOVERY_LDFLAGS_NETBSD4:--static -L$DESTDIR/lib -L$DESTDIR/usr/lib -B$DESTDIR/usr/lib -B$DESTDIR/usr/lib/csu}"
else
    DISCOVERY_CC_SYSROOT_FLAGS="--sysroot=$DESTDIR"
fi

if [ ! -x "$TOOLDIR/bin/nbmake" ] || [ ! -d "$DESTDIR" ]; then
    echo "Missing toolchain/sysroot under $OUT"
    echo "Run $SDK_BOOTSTRAP_WRAPPER first."
    exit 1
fi

if [ ! -f "$DISCOVERY_SRC" ]; then
    echo "Missing source file: $DISCOVERY_SRC"
    exit 1
fi

mkdir -p "$DISCOVERY_STAGE"
mkdir -p "$(dirname "$DISCOVERY_LOG")"

if ! : >"$DISCOVERY_LOG"; then
    echo "Cannot write log file: $DISCOVERY_LOG"
    exit 1
fi

if ! {
    echo "SDK_FAMILY=$SDK_FAMILY"
    echo "DISCOVERY_SRC=$DISCOVERY_SRC"
    echo "DISCOVERY_STAGE=$DISCOVERY_STAGE"
    echo "DISCOVERY_BIN_NAME=$DISCOVERY_BIN_NAME"
    echo "TOOLDIR=$TOOLDIR"
    echo "DESTDIR=$DESTDIR"
    echo "TRIPLE=$TRIPLE"
    echo "DISCOVERY_CC_SYSROOT_FLAGS=$DISCOVERY_CC_SYSROOT_FLAGS"
    echo "DISCOVERY_CFLAGS=$DISCOVERY_CFLAGS"
    echo "DISCOVERY_LDFLAGS=$DISCOVERY_LDFLAGS"

    # Explicit failures are necessary here: sh suppresses errexit inside the
    # enclosing if condition, otherwise a failed compile could copy stale output.
    # Compile separate modules into a single static executable. Explicit lists
    # avoid pulling every helper into NetBSD 4's deliberately no-GC link.
    set --
    while IFS= read -r source; do
        set -- "$@" "$SCRIPT_DIR/$source"
    done <"$DISCOVERY_SRC"
    "$TOOLDIR/bin/$TRIPLE-gcc" \
        $DISCOVERY_CC_SYSROOT_FLAGS \
        $DISCOVERY_CFLAGS \
        -I"$DESTDIR/usr/include" \
        -D_NETBSD_SOURCE \
        -D_LARGEFILE_SOURCE \
        -D_FILE_OFFSET_BITS=64 \
        -D_LARGE_FILES \
        "$@" \
        -o "$DISCOVERY_STAGE/$DISCOVERY_BIN_NAME" \
        $DISCOVERY_LDFLAGS || exit 1

    cp "$DISCOVERY_STAGE/$DISCOVERY_BIN_NAME" "$DISCOVERY_STAGE/$DISCOVERY_BIN_NAME.stripped" || exit 1
    "$TOOLDIR/bin/$TRIPLE-strip" --strip-unneeded "$DISCOVERY_STAGE/$DISCOVERY_BIN_NAME.stripped" || exit 1

    "$TOOLDIR/bin/nbfile" "$DISCOVERY_STAGE/$DISCOVERY_BIN_NAME"
    "$TOOLDIR/bin/nbfile" "$DISCOVERY_STAGE/$DISCOVERY_BIN_NAME.stripped"
    "$TOOLDIR/bin/$TRIPLE-objdump" -p "$DISCOVERY_STAGE/$DISCOVERY_BIN_NAME.stripped" | sed -n '1,120p'
} >"$DISCOVERY_LOG" 2>&1; then
    echo "${DISCOVERY_BUILD_LABEL:-discoveryd} build failed."
    echo "Log: $DISCOVERY_LOG"
    exit 1
fi

printf '%s build complete.\n' "${DISCOVERY_BUILD_LABEL:-discoveryd}"
printf 'Log: %s\n' "$DISCOVERY_LOG"
printf 'Regular binary: %s\n' "$DISCOVERY_STAGE/$DISCOVERY_BIN_NAME"
printf 'Stripped binary: %s\n' "$DISCOVERY_STAGE/$DISCOVERY_BIN_NAME.stripped"
