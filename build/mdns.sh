#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"

TOOLDIR="$TOOLS"
DESTDIR="$OBJ/destdir.evbarm"
TRIPLE="$(basename "$(ls "$TOOLDIR"/bin/*-netbsdelf-*gcc | head -n1)" | sed 's/-gcc$//')"
MDNS_SRC="$SCRIPT_DIR/mdns-advertiser.c"
MDNS_CFLAGS="${MDNS_CFLAGS:--Os -fomit-frame-pointer -ffunction-sections -fdata-sections -fno-unwind-tables -fno-asynchronous-unwind-tables -fno-ident}"
MDNS_LDFLAGS="${MDNS_LDFLAGS:--static -Wl,--gc-sections}"

if [ "$SDK_FAMILY" = "netbsd4" ]; then
    # NetBSD 4's arm--netbsdelf linker was not configured for --sysroot.
    # Keep this helper on the conservative no-GC link path so crt note
    # sections survive and the binary remains executable on the old kernel.
    MDNS_CC_SYSROOT_FLAGS=""
    MDNS_CFLAGS="$MDNS_CFLAGS -B$DESTDIR/usr/lib -B$DESTDIR/usr/lib/csu"
    MDNS_LDFLAGS="${MDNS_LDFLAGS_NETBSD4:--static -L$DESTDIR/lib -L$DESTDIR/usr/lib -B$DESTDIR/usr/lib -B$DESTDIR/usr/lib/csu}"
else
    MDNS_CC_SYSROOT_FLAGS="--sysroot=$DESTDIR"
fi

if [ ! -x "$TOOLDIR/bin/nbmake" ] || [ ! -d "$DESTDIR" ]; then
    echo "Missing toolchain/sysroot under $OUT"
    echo "Run $SDK_BOOTSTRAP_WRAPPER first."
    exit 1
fi

if [ ! -f "$MDNS_SRC" ]; then
    echo "Missing source file: $MDNS_SRC"
    exit 1
fi

mkdir -p "$MDNS_STAGE"
mkdir -p "$(dirname "$MDNS_LOG")"

if ! : >"$MDNS_LOG"; then
    echo "Cannot write log file: $MDNS_LOG"
    exit 1
fi

if ! {
    echo "SDK_FAMILY=$SDK_FAMILY"
    echo "MDNS_SRC=$MDNS_SRC"
    echo "MDNS_STAGE=$MDNS_STAGE"
    echo "MDNS_BIN_NAME=$MDNS_BIN_NAME"
    echo "TOOLDIR=$TOOLDIR"
    echo "DESTDIR=$DESTDIR"
    echo "TRIPLE=$TRIPLE"
    echo "MDNS_CC_SYSROOT_FLAGS=$MDNS_CC_SYSROOT_FLAGS"
    echo "MDNS_CFLAGS=$MDNS_CFLAGS"
    echo "MDNS_LDFLAGS=$MDNS_LDFLAGS"

    "$TOOLDIR/bin/$TRIPLE-gcc" \
        $MDNS_CC_SYSROOT_FLAGS \
        $MDNS_CFLAGS \
        -I"$DESTDIR/usr/include" \
        -D_NETBSD_SOURCE \
        -D_LARGEFILE_SOURCE \
        -D_FILE_OFFSET_BITS=64 \
        -D_LARGE_FILES \
        "$MDNS_SRC" \
        -o "$MDNS_STAGE/$MDNS_BIN_NAME" \
        $MDNS_LDFLAGS

    cp "$MDNS_STAGE/$MDNS_BIN_NAME" "$MDNS_STAGE/$MDNS_BIN_NAME.stripped"
    "$TOOLDIR/bin/$TRIPLE-strip" --strip-unneeded "$MDNS_STAGE/$MDNS_BIN_NAME.stripped"

    "$TOOLDIR/bin/nbfile" "$MDNS_STAGE/$MDNS_BIN_NAME"
    "$TOOLDIR/bin/nbfile" "$MDNS_STAGE/$MDNS_BIN_NAME.stripped"
    "$TOOLDIR/bin/$TRIPLE-objdump" -p "$MDNS_STAGE/$MDNS_BIN_NAME.stripped" | sed -n '1,120p'
} >"$MDNS_LOG" 2>&1; then
    echo "mDNS build failed."
    echo "Log: $MDNS_LOG"
    exit 1
fi

printf 'mDNS build complete.\n'
printf 'Log: %s\n' "$MDNS_LOG"
printf 'Regular binary: %s\n' "$MDNS_STAGE/$MDNS_BIN_NAME"
printf 'Stripped binary: %s\n' "$MDNS_STAGE/$MDNS_BIN_NAME.stripped"
