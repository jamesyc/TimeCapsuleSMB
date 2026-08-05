#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"
SERVICE_STAGE="${SERVICE_STAGE:-$MDNS_STAGE}"
SERVICE_LOG="${SERVICE_LOG:-$OUT/service.log}"
SERVICE_BIN_NAME=service

TOOLDIR="$TOOLS"
DESTDIR="$OBJ/destdir.evbarm"
TRIPLE="$(select_tool_triple)"
SERVICE_SRC="$SCRIPT_DIR/native/service.sources"
SERVICE_CFLAGS="${SERVICE_CFLAGS:--Os -fomit-frame-pointer -ffunction-sections -fdata-sections -fno-unwind-tables -fno-asynchronous-unwind-tables -fno-ident}"
SERVICE_LDFLAGS="${SERVICE_LDFLAGS:--static -Wl,--gc-sections}"

if [ "$SDK_FAMILY" = "netbsd4" ]; then
    # NetBSD 4's arm--netbsdelf linker was not configured for --sysroot.
    # Keep this helper on the conservative no-GC link path so crt note
    # sections survive and the binary remains executable on the old kernel.
    SERVICE_CC_SYSROOT_FLAGS=""
    SERVICE_CFLAGS="$SERVICE_CFLAGS -B$DESTDIR/usr/lib -B$DESTDIR/usr/lib/csu"
    SERVICE_LDFLAGS="${SERVICE_LDFLAGS_NETBSD4:--static -L$DESTDIR/lib -L$DESTDIR/usr/lib -B$DESTDIR/usr/lib -B$DESTDIR/usr/lib/csu}"
else
    SERVICE_CC_SYSROOT_FLAGS="--sysroot=$DESTDIR"
fi

if [ ! -x "$TOOLDIR/bin/nbmake" ] || [ ! -d "$DESTDIR" ]; then
    echo "Missing toolchain/sysroot under $OUT"
    echo "Run $SDK_BOOTSTRAP_WRAPPER first."
    exit 1
fi

if [ ! -f "$SERVICE_SRC" ]; then
    echo "Missing source file: $SERVICE_SRC"
    exit 1
fi

mkdir -p "$SERVICE_STAGE"
mkdir -p "$(dirname "$SERVICE_LOG")"

if ! : >"$SERVICE_LOG"; then
    echo "Cannot write log file: $SERVICE_LOG"
    exit 1
fi

if ! {
    echo "SDK_FAMILY=$SDK_FAMILY"
    echo "SERVICE_SRC=$SERVICE_SRC"
    echo "SERVICE_STAGE=$SERVICE_STAGE"
    echo "SERVICE_BIN_NAME=$SERVICE_BIN_NAME"
    echo "TOOLDIR=$TOOLDIR"
    echo "DESTDIR=$DESTDIR"
    echo "TRIPLE=$TRIPLE"
    echo "SERVICE_CC_SYSROOT_FLAGS=$SERVICE_CC_SYSROOT_FLAGS"
    echo "SERVICE_CFLAGS=$SERVICE_CFLAGS"
    echo "SERVICE_LDFLAGS=$SERVICE_LDFLAGS"

    # Explicit failures are necessary here: sh suppresses errexit inside the
    # enclosing if condition, otherwise a failed compile could copy stale output.
    # Compile separate modules into a single static executable. Explicit lists
    # avoid pulling every helper into NetBSD 4's deliberately no-GC link.
    set --
    while IFS= read -r source; do
        set -- "$@" "$SCRIPT_DIR/$source"
    done <"$SERVICE_SRC"
    "$TOOLDIR/bin/$TRIPLE-gcc" \
        $SERVICE_CC_SYSROOT_FLAGS \
        $SERVICE_CFLAGS \
        -I"$DESTDIR/usr/include" \
        -D_NETBSD_SOURCE \
        -D_LARGEFILE_SOURCE \
        -D_FILE_OFFSET_BITS=64 \
        -D_LARGE_FILES \
        "$@" \
        -o "$SERVICE_STAGE/$SERVICE_BIN_NAME" \
        $SERVICE_LDFLAGS || exit 1

    cp "$SERVICE_STAGE/$SERVICE_BIN_NAME" "$SERVICE_STAGE/$SERVICE_BIN_NAME.stripped" || exit 1
    "$TOOLDIR/bin/$TRIPLE-strip" --strip-unneeded "$SERVICE_STAGE/$SERVICE_BIN_NAME.stripped" || exit 1

    "$TOOLDIR/bin/nbfile" "$SERVICE_STAGE/$SERVICE_BIN_NAME"
    "$TOOLDIR/bin/nbfile" "$SERVICE_STAGE/$SERVICE_BIN_NAME.stripped"
    "$TOOLDIR/bin/$TRIPLE-objdump" -p "$SERVICE_STAGE/$SERVICE_BIN_NAME.stripped" | sed -n '1,120p'
} >"$SERVICE_LOG" 2>&1; then
    echo "Service build failed."
    echo "Log: $SERVICE_LOG"
    exit 1
fi

printf 'Service build complete.\n'
printf 'Log: %s\n' "$SERVICE_LOG"
printf 'Regular binary: %s\n' "$SERVICE_STAGE/$SERVICE_BIN_NAME"
printf 'Stripped binary: %s\n' "$SERVICE_STAGE/$SERVICE_BIN_NAME.stripped"
