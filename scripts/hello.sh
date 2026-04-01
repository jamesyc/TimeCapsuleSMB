#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"

expect_probe_format() {
    probe_file_output=$("$TOOLDIR/bin/nbfile" "$PROBE_BIN")
    probe_flags_output=$("$TOOLDIR/bin/$TRIPLE-objdump" -p "$PROBE_BIN")

    printf '%s\n' "$probe_file_output"
    printf '%s\n' "$probe_flags_output" | sed -n '1,120p'

    if ! printf '%s\n' "$probe_file_output" | grep -F 'ELF 32-bit LSB executable' >/dev/null ||
       ! printf '%s\n' "$probe_file_output" | grep -F 'ARM' >/dev/null ||
       ! printf '%s\n' "$probe_file_output" | grep -F 'statically linked' >/dev/null; then
        echo "Probe format is fundamentally wrong."
        return 1
    fi

    if ! printf '%s\n' "$probe_file_output" | grep -F 'EABI4' >/dev/null ||
       ! printf '%s\n' "$probe_file_output" | grep -F 'for NetBSD 6.0' >/dev/null ||
       ! printf '%s\n' "$probe_flags_output" | grep -F 'private flags = 4000002' >/dev/null; then
        echo "WARNING: probe does not match the original EABI4 / NetBSD 6.0 expectation."
    fi
}

if [ ! -x "$TOOLS/bin/nbmake" ] || [ ! -d "$OBJ/destdir.evbarm" ]; then
    echo "Missing toolchain/sysroot under $OUT"
    echo "Run scripts/bootstrap.sh first."
    exit 1
fi

TOOLDIR="$TOOLS"
DESTDIR="$OBJ/destdir.evbarm"
TRIPLE="$(basename "$(ls "$TOOLDIR"/bin/*-netbsdelf-*gcc | head -n1)" | sed 's/-gcc$//')"
SYSROOT="$DESTDIR"

export TOOLDIR DESTDIR TRIPLE SYSROOT
export PATH="$TOOLDIR/bin:$PATH"
export CC="$TOOLDIR/bin/$TRIPLE-gcc --sysroot=$SYSROOT"
export CXX="$TOOLDIR/bin/$TRIPLE-g++ --sysroot=$SYSROOT"
export CPP="$TOOLDIR/bin/$TRIPLE-cpp --sysroot=$SYSROOT"
export AR="$TOOLDIR/bin/$TRIPLE-ar"
export RANLIB="$TOOLDIR/bin/$TRIPLE-ranlib"
export STRIP="$TOOLDIR/bin/$TRIPLE-strip"
export LD="$TOOLDIR/bin/$TRIPLE-ld --sysroot=$SYSROOT"

mkdir -p "$PROBE_DIR"
cat >"$PROBE_SRC" <<'EOF'
#include <stdio.h>

int
main(void)
{
    printf("hello from static probe\n");
    return 0;
}
EOF

{
    echo "TOOLDIR=$TOOLDIR"
    echo "DESTDIR=$DESTDIR"
    echo "TRIPLE=$TRIPLE"
    echo "SYSROOT=$SYSROOT"

    "$TOOLDIR/bin/$TRIPLE-gcc" --sysroot="$DESTDIR" \
      -B"$DESTDIR/usr/lib" -B"$DESTDIR/usr/lib/csu" \
      -static \
      -o "$PROBE_BIN" "$PROBE_SRC"

    expect_probe_format

    scp -O $TC_SSH_OPTS "$PROBE_BIN" "$TC_HOST:/tmp/hello-clean"
    ssh $TC_SSH_OPTS "$TC_HOST" \
      'chmod +x /tmp/hello-clean && /tmp/hello-clean'
} >"$HELLO_LOG" 2>&1

printf 'Hello probe run complete.\n'
printf 'Log: %s\n' "$HELLO_LOG"
