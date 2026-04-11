#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"

TOOLDIR="$TOOLS"
DESTDIR="$OBJ/destdir.evbarm"
TRIPLE="$(basename "$(ls "$TOOLDIR"/bin/*-netbsdelf-*gcc | head -n1)" | sed 's/-gcc$//')"
MDNS_SRC="$SCRIPT_DIR/mdns-advertiser.c"
MDNS_STAGE="${MDNS_STAGE:-/root/tc-stage-mdns}"
MDNS_LOG="${MDNS_LOG:-$OUT/mdns.log}"
MDNS_BIN_NAME="${MDNS_BIN_NAME:-mdns-smbd-advertiser}"
MDNS_CFLAGS="${MDNS_CFLAGS:--Os -fomit-frame-pointer -ffunction-sections -fdata-sections -fno-unwind-tables -fno-asynchronous-unwind-tables -fno-ident}"
MDNS_LDFLAGS="${MDNS_LDFLAGS:--static -Wl,--gc-sections}"

if [ ! -x "$TOOLDIR/bin/nbmake" ] || [ ! -d "$DESTDIR" ]; then
    echo "Missing toolchain/sysroot under $OUT"
    echo "Run build/bootstrap.sh first."
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
    echo "MDNS_SRC=$MDNS_SRC"
    echo "MDNS_STAGE=$MDNS_STAGE"
    echo "MDNS_BIN_NAME=$MDNS_BIN_NAME"
    echo "TOOLDIR=$TOOLDIR"
    echo "DESTDIR=$DESTDIR"
    echo "TRIPLE=$TRIPLE"

    "$TOOLDIR/bin/$TRIPLE-gcc" \
        --sysroot="$DESTDIR" \
        $MDNS_CFLAGS \
        -I"$DESTDIR/usr/include" \
        -D_NETBSD_SOURCE \
        -D_LARGEFILE_SOURCE \
        -D_FILE_OFFSET_BITS=64 \
        -D_LARGE_FILES \
        "$MDNS_SRC" \
        -o "$MDNS_STAGE/$MDNS_BIN_NAME" \
        $MDNS_LDFLAGS \
        -L"$DESTDIR/lib" \
        -L"$DESTDIR/usr/lib"

    cp "$MDNS_STAGE/$MDNS_BIN_NAME" "$MDNS_STAGE/$MDNS_BIN_NAME.stripped"
    "$TOOLDIR/bin/$TRIPLE-strip" --strip-unneeded "$MDNS_STAGE/$MDNS_BIN_NAME.stripped"

    "$TOOLDIR/bin/nbfile" "$MDNS_STAGE/$MDNS_BIN_NAME"
    "$TOOLDIR/bin/nbfile" "$MDNS_STAGE/$MDNS_BIN_NAME.stripped"
} >"$MDNS_LOG" 2>&1; then
    echo "mDNS build failed."
    echo "Log: $MDNS_LOG"
    exit 1
fi

printf 'mDNS build complete.\n'
printf 'Log: %s\n' "$MDNS_LOG"
printf 'Regular binary: %s\n' "$MDNS_STAGE/$MDNS_BIN_NAME"
printf 'Stripped binary: %s\n' "$MDNS_STAGE/$MDNS_BIN_NAME.stripped"
