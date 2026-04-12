#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"

dump_elf_diagnostics() {
    label="$1"
    path="$2"

    echo "===== $label ====="
    "$TOOLDIR/bin/$TRIPLE-readelf" -h -l "$path" | sed -n '1,120p'
    "$TOOLDIR/bin/$TRIPLE-readelf" -S "$path" | sed -n '1,120p'
    "$TOOLDIR/bin/$TRIPLE-readelf" -d "$path" 2>&1 | sed -n '1,80p'
    "$TOOLDIR/bin/$TRIPLE-objdump" -p "$path" | sed -n '1,120p'
}

expect_probe_format() {
    probe_flags_output=$("$TOOLDIR/bin/$TRIPLE-objdump" -p "$PROBE_BIN")
    if probe_file_output=$("$TOOLDIR/bin/nbfile" "$PROBE_BIN" 2>/dev/null); then
        :
    else
        probe_file_output=""
        echo "WARNING: nbfile could not identify the probe binary; continuing with objdump-only validation."
    fi

    if [ -n "$probe_file_output" ]; then
        printf '%s\n' "$probe_file_output"
    fi
    printf '%s\n' "$probe_flags_output" | sed -n '1,120p'

    if [ -n "$probe_file_output" ]; then
        if ! printf '%s\n' "$probe_file_output" | grep -F 'ELF 32-bit LSB executable' >/dev/null ||
           ! printf '%s\n' "$probe_file_output" | grep -F 'ARM' >/dev/null ||
           ! printf '%s\n' "$probe_file_output" | grep -F 'statically linked' >/dev/null; then
            echo "Probe format is fundamentally wrong."
            return 1
        fi
    fi

    if [ -z "$probe_file_output" ] ||
       ! printf '%s\n' "$probe_file_output" | grep -F "$BUILD_EXPECT_EABI" >/dev/null ||
       ! printf '%s\n' "$probe_file_output" | grep -F "for $BUILD_EXPECT_OS_RELEASE" >/dev/null ||
       ! printf '%s\n' "$probe_flags_output" | grep -F 'private flags = 4000002' >/dev/null; then
        echo "WARNING: probe does not match the expected $BUILD_EXPECT_EABI / $BUILD_EXPECT_OS_RELEASE format."
    fi
}

if [ ! -x "$TOOLS/bin/nbmake" ] || [ ! -d "$OBJ/destdir.evbarm" ]; then
    echo "Missing toolchain/sysroot under $OUT"
    echo "Run $SDK_BOOTSTRAP_WRAPPER first."
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
    echo "SDK_FAMILY=$SDK_FAMILY"
    echo "DEVICE_FAMILY=$DEVICE_FAMILY"
    echo "TOOLDIR=$TOOLDIR"
    echo "DESTDIR=$DESTDIR"
    echo "TRIPLE=$TRIPLE"
    echo "SYSROOT=$SYSROOT"

    "$TOOLDIR/bin/$TRIPLE-gcc" \
      -B"$DESTDIR/usr/lib" -B"$DESTDIR/usr/lib/csu" \
      -I"$DESTDIR/usr/include" \
      -D_NETBSD_SOURCE \
      -D_LARGEFILE_SOURCE \
      -D_FILE_OFFSET_BITS=64 \
      -D_LARGE_FILES \
      -static \
      -L"$DESTDIR/lib" \
      -L"$DESTDIR/usr/lib" \
      -o "$PROBE_BIN" "$PROBE_SRC"

    expect_probe_format
    dump_elf_diagnostics "hello probe" "$PROBE_BIN"

    cat "$PROBE_BIN" | tc_ssh "$TC_HOST" 'cat > /tmp/hello-clean'
    tc_ssh "$TC_HOST" \
      'chmod +x /tmp/hello-clean && /tmp/hello-clean'
} >"$HELLO_LOG" 2>&1

printf 'Hello probe run complete.\n'
printf 'Log: %s\n' "$HELLO_LOG"
