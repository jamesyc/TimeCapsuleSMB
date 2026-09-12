#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"
TELEMETRY_STAGE="${TELEMETRY_STAGE:-$MDNS_STAGE}"
TELEMETRY_LOG="${TELEMETRY_LOG:-$OUT/telemetry.log}"
TELEMETRY_BIN_NAME=telemetry

TOOLDIR="$TOOLS"
DESTDIR="$OBJ/destdir.evbarm"
TRIPLE="$(select_tool_triple)"
TELEMETRY_SRC="$SCRIPT_DIR/native/telemetry.sources"
TELEMETRY_CFLAGS="${TELEMETRY_CFLAGS:--Os -fomit-frame-pointer -ffunction-sections -fdata-sections -fno-unwind-tables -fno-asynchronous-unwind-tables -fno-ident}"
TELEMETRY_LDFLAGS="${TELEMETRY_LDFLAGS:--static -Wl,--gc-sections}"

if [ "$SDK_FAMILY" = "netbsd4" ]; then
    # NetBSD 4's arm--netbsdelf linker was not configured for --sysroot.
    # Keep this helper on the conservative no-GC link path so crt note
    # sections survive and the binary remains executable on the old kernel.
    TELEMETRY_CC_SYSROOT_FLAGS=""
    TELEMETRY_CFLAGS="$TELEMETRY_CFLAGS -B$DESTDIR/usr/lib -B$DESTDIR/usr/lib/csu"
    TELEMETRY_LDFLAGS="${TELEMETRY_LDFLAGS_NETBSD4:--static -L$DESTDIR/lib -L$DESTDIR/usr/lib -B$DESTDIR/usr/lib -B$DESTDIR/usr/lib/csu}"
else
    TELEMETRY_CC_SYSROOT_FLAGS="--sysroot=$DESTDIR"
fi

if [ ! -x "$TOOLDIR/bin/nbmake" ] || [ ! -d "$DESTDIR" ]; then
    echo "Missing toolchain/sysroot under $OUT"
    echo "Run $SDK_BOOTSTRAP_WRAPPER first."
    exit 1
fi

if [ ! -f "$TELEMETRY_SRC" ]; then
    echo "Missing source file: $TELEMETRY_SRC"
    exit 1
fi

mkdir -p "$TELEMETRY_STAGE"
mkdir -p "$(dirname "$TELEMETRY_LOG")"

if ! : >"$TELEMETRY_LOG"; then
    echo "Cannot write log file: $TELEMETRY_LOG"
    exit 1
fi

if ! {
    echo "SDK_FAMILY=$SDK_FAMILY"
    echo "TELEMETRY_SRC=$TELEMETRY_SRC"
    echo "TELEMETRY_STAGE=$TELEMETRY_STAGE"
    echo "TELEMETRY_BIN_NAME=$TELEMETRY_BIN_NAME"
    echo "TOOLDIR=$TOOLDIR"
    echo "DESTDIR=$DESTDIR"
    echo "TRIPLE=$TRIPLE"
    echo "TELEMETRY_CC_SYSROOT_FLAGS=$TELEMETRY_CC_SYSROOT_FLAGS"
    echo "TELEMETRY_CFLAGS=$TELEMETRY_CFLAGS"
    echo "TELEMETRY_LDFLAGS=$TELEMETRY_LDFLAGS"

    # Explicit failures are necessary here: sh suppresses errexit inside the
    # enclosing if condition, otherwise a failed compile could copy stale output.
    # Compile separate modules into a single static executable. Explicit lists
    # avoid pulling every helper into NetBSD 4's deliberately no-GC link.
    set --
    while IFS= read -r source; do
        set -- "$@" "$SCRIPT_DIR/$source"
    done <"$TELEMETRY_SRC"
    lane=6
    if [ "$SDK_FAMILY" = netbsd4 ]; then lane="4$NETBSD4_ABI"; fi
    "$TOOLDIR/bin/$TRIPLE-gcc" \
        $TELEMETRY_CC_SYSROOT_FLAGS \
        $TELEMETRY_CFLAGS \
        -I"$DESTDIR/usr/include" \
        -DTC_TELEMETRY_LANE=\"$lane\" \
        -Wno-sign-compare \
        -D_NETBSD_SOURCE \
        -D_LARGEFILE_SOURCE \
        -D_FILE_OFFSET_BITS=64 \
        -D_LARGE_FILES \
        "$@" \
        -o "$TELEMETRY_STAGE/$TELEMETRY_BIN_NAME" \
        $TELEMETRY_LDFLAGS || exit 1

    cp "$TELEMETRY_STAGE/$TELEMETRY_BIN_NAME" "$TELEMETRY_STAGE/$TELEMETRY_BIN_NAME.stripped" || exit 1
    "$TOOLDIR/bin/$TRIPLE-strip" --strip-unneeded "$TELEMETRY_STAGE/$TELEMETRY_BIN_NAME.stripped" || exit 1

    "$TOOLDIR/bin/nbfile" "$TELEMETRY_STAGE/$TELEMETRY_BIN_NAME"
    "$TOOLDIR/bin/nbfile" "$TELEMETRY_STAGE/$TELEMETRY_BIN_NAME.stripped"
    "$TOOLDIR/bin/$TRIPLE-objdump" -p "$TELEMETRY_STAGE/$TELEMETRY_BIN_NAME.stripped" | sed -n '1,120p'
} >"$TELEMETRY_LOG" 2>&1; then
    echo "Telemetry build failed."
    echo "Log: $TELEMETRY_LOG"
    exit 1
fi

printf 'Telemetry build complete.\n'
printf 'Log: %s\n' "$TELEMETRY_LOG"
printf 'Regular binary: %s\n' "$TELEMETRY_STAGE/$TELEMETRY_BIN_NAME"
printf 'Stripped binary: %s\n' "$TELEMETRY_STAGE/$TELEMETRY_BIN_NAME.stripped"
