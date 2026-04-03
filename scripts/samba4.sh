#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"

TOOLDIR="$TOOLS"
DESTDIR="$OBJ/destdir.evbarm"
TRIPLE="$(basename "$(ls "$TOOLDIR"/bin/*-netbsdelf-*gcc | head -n1)" | sed 's/-gcc$//')"
SYSROOT="$DESTDIR"

if [ ! -x "$TOOLDIR/bin/nbmake" ] || [ ! -d "$DESTDIR" ]; then
    echo "Missing toolchain/sysroot under $OUT"
    echo "Run scripts/bootstrap.sh first."
    exit 1
fi

pick_python2() {
    for candidate in \
        "${PYTHON2:-}" \
        /usr/pkg/bin/python2.7 \
        /usr/pkg/bin/python2 \
        /usr/bin/python2.7 \
        /usr/bin/python2 \
        python2.7 \
        python2
    do
        if [ -n "$candidate" ] && command -v "$candidate" >/dev/null 2>&1; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

mkdir -p "$SAMBA4_WORK" "$SAMBA4_STAGE" "$SAMBA4_BUILD"

export PATH="$TOOLDIR/bin:/usr/pkg/libexec/heimdal:/usr/local/libexec/heimdal:/usr/pkg/bin:$PATH"
export TOOLDIR DESTDIR TRIPLE SYSROOT
export CC="$TOOLDIR/bin/$TRIPLE-gcc --sysroot=$SYSROOT"
export CXX="$TOOLDIR/bin/$TRIPLE-g++ --sysroot=$SYSROOT"
export CPP="$TOOLDIR/bin/$TRIPLE-cpp --sysroot=$SYSROOT"
export AR="$TOOLDIR/bin/$TRIPLE-ar"
export RANLIB="$TOOLDIR/bin/$TRIPLE-ranlib"
export STRIP="$TOOLDIR/bin/$TRIPLE-strip"
export LD="$TOOLDIR/bin/$TRIPLE-ld --sysroot=$SYSROOT"
export CFLAGS="-Os -ffunction-sections -fdata-sections -fomit-frame-pointer -fno-unwind-tables -fno-asynchronous-unwind-tables -fno-ident -fno-pie"
export CXXFLAGS="-Os -ffunction-sections -fdata-sections -fomit-frame-pointer -fno-unwind-tables -fno-asynchronous-unwind-tables -fno-ident -fno-pie"
export CPPFLAGS="-I$SYSROOT/usr/include -D_NETBSD_SOURCE -D_LARGEFILE_SOURCE -D_FILE_OFFSET_BITS=64 -D_LARGE_FILES"
export LDFLAGS="-static -Wl,--gc-sections -L$SYSROOT/lib -L$SYSROOT/usr/lib"
export PKG_CONFIG_DIR=
export PKG_CONFIG_PATH=
export PKG_CONFIG_LIBDIR=
export PKG_CONFIG_SYSROOT_DIR="$SYSROOT"

CROSS_EXECUTE="$(cd "$(dirname "$0")" && pwd)/samba4-cross-exec.sh"

{
    echo "SAMBA4_VERSION=$SAMBA4_VERSION"
    echo "TOOLDIR=$TOOLDIR"
    echo "DESTDIR=$DESTDIR"
    echo "TRIPLE=$TRIPLE"
    echo "SYSROOT=$SYSROOT"
    echo "WORK=$SAMBA4_WORK"
    echo "STAGE=$SAMBA4_STAGE"
    echo "SRC_DIR=$SAMBA4_SRC_DIR"
    echo "CROSS_EXECUTE=$CROSS_EXECUTE"

    if [ ! -f "$SAMBA4_SRC_DIR/configure" ]; then
        echo "Missing Samba 4 source tree at $SAMBA4_SRC_DIR"
        echo "Run ./scripts/downloadsamba4.sh first."
        exit 1
    fi

    PYTHON2_BIN="$(pick_python2)" || {
        echo "Unable to find a Python 2 interpreter on this VM."
        exit 1
    }
    echo "PYTHON2_BIN=$PYTHON2_BIN"

    mkdir -p "$SAMBA4_BUILD"
    cd "$SAMBA4_SRC_DIR"
    PYTHON="$PYTHON2_BIN" ./buildtools/bin/waf distclean >/dev/null 2>&1 || true

    PYTHON="$PYTHON2_BIN" ./configure \
      --cross-compile \
      --cross-execute="$CROSS_EXECUTE" \
      --hostcc="$HOST_CC" \
      --prefix="$SAMBA4_STAGE" \
      --bundled-libraries='!asn1_compile,!compile_et' \
      --without-pie \
      --without-acl-support \
      --without-ad-dc \
      --without-ads \
      --without-ldap \
      --without-pam \
      --disable-cups \
      --without-winbind \
      --without-utmp \
      --without-syslog \
      --nonshared-binary=smbd/smbd

    for cache_file in "$SAMBA4_SRC_DIR"/bin/c4che/*.py; do
        [ -f "$cache_file" ] || continue
        perl -0pi -e 's/^ENABLE_PIE = True$/ENABLE_PIE = False/m' "$cache_file"
        if ! grep -q '^FULLSTATIC = ' "$cache_file"; then
            perl -0pi -e 's/^(FULLSTATIC_MARKER = .*)$/$1\nFULLSTATIC = True/m' "$cache_file"
        fi
        grep -q '^FULLSTATIC = ' "$cache_file" || printf 'FULLSTATIC = True\n' >>"$cache_file"
    done

    PYTHON="$PYTHON2_BIN" ./buildtools/bin/waf -v -j"$SAMBA4_JOBS" build --targets=smbd/smbd

    SAMBA4_SMBD="$(find "$SAMBA4_SRC_DIR/bin" -path '*/source3/smbd/smbd' | head -n1)"
    if [ -z "$SAMBA4_SMBD" ] || [ ! -f "$SAMBA4_SMBD" ]; then
        echo "Unable to locate built Samba 4 smbd under $SAMBA4_SRC_DIR/bin"
        exit 1
    fi

    SAMBA4_FILE_OUTPUT="$("$TOOLDIR/bin/nbfile" "$SAMBA4_SMBD")"
    echo "$SAMBA4_FILE_OUTPUT"
    case "$SAMBA4_FILE_OUTPUT" in
        *"statically linked"*)
            ;;
        *)
            echo "Samba 4 smbd is not statically linked; refusing to stage it."
            exit 1
            ;;
    esac

    mkdir -p "$SAMBA4_STAGE/sbin"
    cp "$SAMBA4_SMBD" "$SAMBA4_STAGE/sbin/smbd"
    cp "$SAMBA4_SMBD" "$SAMBA4_STAGE/sbin/smbd.stripped"
    "$STRIP" --strip-unneeded "$SAMBA4_STAGE/sbin/smbd.stripped"
} >"$SAMBA4_LOG" 2>&1

printf 'Samba 4 build complete.\n'
printf 'Log: %s\n' "$SAMBA4_LOG"
printf 'Regular binary: %s\n' "$SAMBA4_STAGE/sbin/smbd"
printf 'Stripped binary: %s\n' "$SAMBA4_STAGE/sbin/smbd.stripped"
