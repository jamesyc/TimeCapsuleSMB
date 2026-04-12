#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"

TOOLDIR="$TOOLS"
DESTDIR="$OBJ/destdir.evbarm"
TRIPLE="$(basename "$(ls "$TOOLDIR"/bin/*-netbsdelf-*gcc | head -n1)" | sed 's/-gcc$//')"
NBNS_SRC="$SCRIPT_DIR/nbns-advertiser.c"
NBNS_CFLAGS="${NBNS_CFLAGS:--Os -fomit-frame-pointer -ffunction-sections -fdata-sections -fno-unwind-tables -fno-asynchronous-unwind-tables -fno-ident}"
NBNS_LDFLAGS="${NBNS_LDFLAGS:--static -Wl,--gc-sections}"

if [ ! -x "$TOOLDIR/bin/nbmake" ] || [ ! -d "$DESTDIR" ]; then
    echo "Missing toolchain/sysroot under $OUT"
    echo "Run $SDK_BOOTSTRAP_WRAPPER first."
    exit 1
fi

if [ ! -f "$NBNS_SRC" ]; then
    echo "Missing source file: $NBNS_SRC"
    exit 1
fi

mkdir -p "$NBNS_STAGE"
mkdir -p "$(dirname "$NBNS_LOG")"

if ! : >"$NBNS_LOG"; then
    echo "Cannot write log file: $NBNS_LOG"
    exit 1
fi

if ! {
    echo "SDK_FAMILY=$SDK_FAMILY"
    echo "NBNS_SRC=$NBNS_SRC"
    echo "NBNS_STAGE=$NBNS_STAGE"
    echo "NBNS_BIN_NAME=$NBNS_BIN_NAME"
    echo "TOOLDIR=$TOOLDIR"
    echo "DESTDIR=$DESTDIR"
    echo "TRIPLE=$TRIPLE"

    "$TOOLDIR/bin/$TRIPLE-gcc" \
        --sysroot="$DESTDIR" \
        $NBNS_CFLAGS \
        -I"$DESTDIR/usr/include" \
        -D_NETBSD_SOURCE \
        -D_LARGEFILE_SOURCE \
        -D_FILE_OFFSET_BITS=64 \
        -D_LARGE_FILES \
        "$NBNS_SRC" \
        -o "$NBNS_STAGE/$NBNS_BIN_NAME" \
        $NBNS_LDFLAGS \
        -L"$DESTDIR/lib" \
        -L"$DESTDIR/usr/lib"

    cp "$NBNS_STAGE/$NBNS_BIN_NAME" "$NBNS_STAGE/$NBNS_BIN_NAME.stripped"
    "$TOOLDIR/bin/$TRIPLE-strip" --strip-unneeded "$NBNS_STAGE/$NBNS_BIN_NAME.stripped"

    "$TOOLDIR/bin/nbfile" "$NBNS_STAGE/$NBNS_BIN_NAME"
    "$TOOLDIR/bin/nbfile" "$NBNS_STAGE/$NBNS_BIN_NAME.stripped"
} >"$NBNS_LOG" 2>&1; then
    echo "NBNS build failed."
    echo "Log: $NBNS_LOG"
    exit 1
fi

printf 'NBNS build complete.\n'
printf 'Log: %s\n' "$NBNS_LOG"
printf 'Regular binary: %s\n' "$NBNS_STAGE/$NBNS_BIN_NAME"
printf 'Stripped binary: %s\n' "$NBNS_STAGE/$NBNS_BIN_NAME.stripped"
