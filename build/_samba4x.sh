#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"
. "$(dirname "$0")/_patch_helpers.sh"

GNUTLS_PATCH_DIR="$(CDPATH= cd "$(dirname "$0")/patches/gnutls" && pwd)"
TOOLDIR="$TOOLS"
DESTDIR="$OBJ/destdir.evbarm"
TRIPLE="$(select_tool_triple)"
SYSROOT="$DESTDIR"

if [ ! -x "$TOOLDIR/bin/nbmake" ] || [ ! -d "$DESTDIR" ]; then
    echo "Missing toolchain/sysroot under $OUT"
    echo "Run $SDK_BOOTSTRAP_WRAPPER first."
    exit 1
fi

pick_python3() {
    for candidate in \
        "${PYTHON3:-}" \
        /usr/pkg/bin/python3.12 \
        /usr/pkg/bin/python3.11 \
        /usr/pkg/bin/python3.10 \
        /usr/pkg/bin/python3.9 \
        /usr/pkg/bin/python3.8 \
        /usr/pkg/bin/python3 \
        /usr/bin/python3 \
        python3.12 \
        python3.11 \
        python3.10 \
        python3.9 \
        python3.8 \
        python3
    do
        if [ -n "$candidate" ] && command -v "$candidate" >/dev/null 2>&1; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

has_elf_section() {
    path="$1"
    section="$2"
    "$TOOLDIR/bin/$TRIPLE-objdump" -h "$path" 2>/dev/null | \
        awk '{print $2}' | grep -Fx "$section" >/dev/null 2>&1
}

dump_elf_notes() {
    label="$1"
    path="$2"

    echo "===== $label ELF notes/headers ====="
    "$TOOLDIR/bin/$TRIPLE-readelf" -S "$path" 2>&1 | \
        grep -E 'note\.netbsd|Name|Section Headers' || true
    "$TOOLDIR/bin/$TRIPLE-objdump" -p "$path" 2>&1 | sed -n '1,80p'
}

prepare_netbsd4_gc_note_inputs() {
    SAMBA4X_NETBSD4_NOTE_ASM="$SAMBA4X_BUILD/netbsd4-notes.S"
    SAMBA4X_NETBSD4_NOTE_OBJ="$SAMBA4X_BUILD/netbsd4-notes.o"
    SAMBA4X_NETBSD4_DEFAULT_LD="$SAMBA4X_BUILD/netbsd4-default.ld"
    SAMBA4X_NETBSD4_KEEP_NOTES_LD="$SAMBA4X_BUILD/netbsd4-keep-notes.ld"

    cat >"$SAMBA4X_NETBSD4_NOTE_ASM" <<'EOF'
    .section .note.netbsd.ident,"a",%note
    .balign 4
    .long 7
    .long 4
    .long 1
    .asciz "NetBSD"
    .balign 4
    .long 0x17d78403

    .section .note.netbsd.pax,"a",%note
    .balign 4
    .long 4
    .long 4
    .long 3
    .asciz "PaX"
    .balign 4
    .long 0
EOF

    "$TOOLDIR/bin/$TRIPLE-gcc" -c "$SAMBA4X_NETBSD4_NOTE_ASM" -o "$SAMBA4X_NETBSD4_NOTE_OBJ"

    "$TOOLDIR/bin/$TRIPLE-ld" --verbose | awk '
        /^====/ { seen++; next }
        seen == 1 { print }
    ' >"$SAMBA4X_NETBSD4_DEFAULT_LD"

    awk '
        /SIZEOF_HEADERS;/ {
            print
            print "  .note.netbsd.ident : { KEEP(*(.note.netbsd.ident)) }"
            print "  .note.netbsd.pax : { KEEP(*(.note.netbsd.pax)) }"
            next
        }
        { print }
    ' "$SAMBA4X_NETBSD4_DEFAULT_LD" >"$SAMBA4X_NETBSD4_KEEP_NOTES_LD"

    export SAMBA4X_NETBSD4_NOTE_OBJ SAMBA4X_NETBSD4_KEEP_NOTES_LD
}

validate_netbsd4_notes() {
    path="$1"

    if [ "$SDK_FAMILY" != "netbsd4" ] || [ "$SAMBA4X_NETBSD4_GC_SECTIONS" != "1" ]; then
        return 0
    fi

    if has_elf_section "$path" ".note.netbsd.ident" &&
       has_elf_section "$path" ".note.netbsd.pax"; then
        echo "NetBSD note sections are present in $path"
        return 0
    fi

    echo "NetBSD note sections are missing from $path"
    dump_elf_notes "missing-note smbd" "$path"
    exit 1
}

set_waf_cache_value() {
    cache_file="$1"
    name="$2"
    value="$3"
    desc="Samba 4.x waf cache $name"

    if grep -Fqx "$name = $value" "$cache_file"; then
        return 0
    fi
    if grep -q "^$name = " "$cache_file"; then
        patch_perl "$desc" "s|^$name = .*\$|$name = $value|m" "$cache_file"
    else
        printf '%s = %s\n' "$name" "$value" >>"$cache_file"
    fi
    patch_require_fixed "$desc" "$name = $value" "$cache_file"
}

remove_waf_cache_fixed_text() {
    cache_file="$1"
    text="$2"
    expr="$3"
    desc="$4"

    if grep -F -q "$text" "$cache_file"; then
        patch_perl "$desc" "$expr" "$cache_file"
    fi
    if grep -F -q "$text" "$cache_file"; then
        patch_fail "$desc: forbidden cache text still present in $cache_file"
    fi
}

undef_config_symbol() {
    config_header="$1"
    symbol="$2"
    desc="Samba 4.x config header undef $symbol"

    if grep -E -q "^#define[[:space:]]+$symbol([[:space:]]|$)" "$config_header"; then
        patch_perl "$desc" "s|^#define[ \t]+$symbol([ \t]+[^\n]*)?$|/* #undef $symbol */|m" "$config_header"
    fi
    if grep -E -q "^#define[[:space:]]+$symbol([[:space:]]|$)" "$config_header"; then
        patch_fail "$desc: define still present in $config_header"
    fi
}

download_samba4x_archive() {
    url="$1"
    archive="$2"

    mkdir -p "$SAMBA4X_BUILD/distfiles"
    path="$SAMBA4X_BUILD/distfiles/$archive"
    if [ -f "$path" ]; then
        printf '%s\n' "$path"
        return 0
    fi

    tmp="$path.tmp.$$"
    rm -f "$tmp"
    curl -fL "$url" -o "$tmp"
    mv "$tmp" "$path"
    printf '%s\n' "$path"
}

extract_samba4x_archive() {
    archive="$1"
    dirname="$2"

    rm -rf "$SAMBA4X_BUILD/$dirname"
    case "$archive" in
        *.tar.gz|*.tgz)
            tar -xzf "$archive" -C "$SAMBA4X_BUILD"
            ;;
        *.tar.xz)
            tar -xJf "$archive" -C "$SAMBA4X_BUILD"
            ;;
        *)
            echo "Unsupported Samba4X dependency archive: $archive"
            exit 1
            ;;
    esac
}

find_samba4x_gmp_header() {
    gmp_arch=
    case "$BUILD_MACHINE_ARCH" in
        earm*)
            gmp_arch=earm
            ;;
        arm|armeb)
            gmp_arch="$BUILD_MACHINE_ARCH"
            ;;
    esac

    for candidate in \
        "$BUILD_SRC/external/lgpl3/gmp/lib/libgmp/arch/$gmp_arch/gmp.h" \
        "$BUILD_SRC/external/lgpl3/gmp/lib/libgmp/arch/$BUILD_MACHINE_ARCH/gmp.h" \
        "$BUILD_SRC/external/lgpl3/gmp/lib/libgmp/arch/earm/gmp.h" \
        "$BUILD_SRC/external/lgpl3/gmp/lib/libgmp/arch/arm/gmp.h" \
        "$BUILD_SRC/external/lgpl3/gmp/lib/libgmp/arch/armeb/gmp.h"
    do
        if [ -n "$candidate" ] && [ -f "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    echo "Unable to find NetBSD target gmp.h under $BUILD_SRC/external/lgpl3/gmp" >&2
    return 1
}

install_samba4x_target_gmp() {
    gmp_lib="$OBJ/external/lgpl3/gmp/lib/libgmp/libgmp.a"
    if ! gmp_header="$(find_samba4x_gmp_header)"; then
        return 1
    fi

    if [ ! -f "$gmp_lib" ]; then
        echo "Unable to find NetBSD target libgmp.a at $gmp_lib"
        return 1
    fi

    mkdir -p "$SAMBA4X_DEPS/lib" "$SAMBA4X_DEPS/include" "$SAMBA4X_DEPS/lib/pkgconfig"
    cp "$gmp_lib" "$SAMBA4X_DEPS/lib/libgmp.a"
    cp "$gmp_header" "$SAMBA4X_DEPS/include/gmp.h"
    gmp_mparam="$(dirname "$gmp_header")/gmp-mparam.h"
    if [ -f "$gmp_mparam" ]; then
        cp "$gmp_mparam" "$SAMBA4X_DEPS/include/gmp-mparam.h"
    fi
    write_samba4x_gmp_pc "6.1.0"
}

write_samba4x_gmp_pc() {
    version="$1"
    cat >"$SAMBA4X_DEPS/lib/pkgconfig/gmp.pc" <<EOF
prefix=$SAMBA4X_DEPS
exec_prefix=\${prefix}
libdir=\${prefix}/lib
includedir=\${prefix}/include

Name: GNU MP
Description: GNU Multiple Precision Arithmetic Library
Version: $version
Libs: -L\${libdir} -lgmp
Cflags: -I\${includedir}
EOF
}

build_samba4x_gmp() {
    stamp="$SAMBA4X_DEPS/.stamp-gmp-$SAMBA4X_GMP_VERSION"
    if [ -f "$stamp" ] && [ -f "$SAMBA4X_DEPS/lib/libgmp.a" ]; then
        echo "GMP $SAMBA4X_GMP_VERSION already built."
        write_samba4x_gmp_pc "$SAMBA4X_GMP_VERSION"
        return 0
    fi

    archive="$(download_samba4x_archive "$SAMBA4X_GMP_URL" "gmp-$SAMBA4X_GMP_VERSION.tar.xz")"
    extract_samba4x_archive "$archive" "gmp-$SAMBA4X_GMP_VERSION"
    cd "$SAMBA4X_BUILD/gmp-$SAMBA4X_GMP_VERSION"
    env CC="$CC" CXX="$CXX" AR="$AR" RANLIB="$RANLIB" \
        CPPFLAGS="$CPPFLAGS" CFLAGS="$CFLAGS" LDFLAGS="$LDFLAGS" \
        ./configure \
            --host="$SAMBA4X_HOST_ALIAS" \
            --prefix="$SAMBA4X_DEPS" \
            --disable-shared \
            --enable-static \
            --disable-assembly
    gmake -j"$SAMBA4X_JOBS" DESTDIR= install
    write_samba4x_gmp_pc "$SAMBA4X_GMP_VERSION"
    touch "$stamp"
}

install_samba4x_sysroot_pkg_config() {
    mkdir -p "$SAMBA4X_DEPS/lib/pkgconfig"

    if [ ! -f "$SYSROOT/usr/include/zlib.h" ] || [ ! -f "$SYSROOT/usr/lib/libz.a" ]; then
        echo "Unable to find NetBSD target zlib headers/library under $SYSROOT/usr"
        exit 1
    fi
    cat >"$SAMBA4X_DEPS/lib/pkgconfig/zlib.pc" <<EOF
prefix=$SYSROOT/usr
exec_prefix=\${prefix}
libdir=\${prefix}/lib
includedir=\${prefix}/include

Name: zlib
Description: zlib compression library
Version: 1.2.8
Libs: -L\${libdir} -lz
Cflags: -I\${includedir}
EOF
}

build_samba4x_nettle() {
    stamp="$SAMBA4X_DEPS/.stamp-nettle-$SAMBA4X_NETTLE_VERSION-system-gmp"
    if [ -f "$stamp" ] &&
       [ -f "$SAMBA4X_DEPS/lib/libnettle.a" ] &&
       [ -f "$SAMBA4X_DEPS/lib/libhogweed.a" ]; then
        echo "nettle $SAMBA4X_NETTLE_VERSION already built."
        return 0
    fi

    archive="$(download_samba4x_archive "$SAMBA4X_NETTLE_URL" "nettle-$SAMBA4X_NETTLE_VERSION.tar.gz")"
    extract_samba4x_archive "$archive" "nettle-$SAMBA4X_NETTLE_VERSION"
    cd "$SAMBA4X_BUILD/nettle-$SAMBA4X_NETTLE_VERSION"
    env PKG_CONFIG_PATH="$SAMBA4X_DEPS/lib/pkgconfig" \
        PKG_CONFIG_LIBDIR="$SAMBA4X_DEPS/lib/pkgconfig" \
        PKG_CONFIG_SYSROOT_DIR= \
        CC="$CC" CXX="$CXX" AR="$AR" RANLIB="$RANLIB" \
        CPPFLAGS="$CPPFLAGS" CFLAGS="$CFLAGS" LDFLAGS="$LDFLAGS" \
        ./configure \
            --host="$SAMBA4X_HOST_ALIAS" \
            --prefix="$SAMBA4X_DEPS" \
            --disable-shared \
            --enable-static \
            --disable-assembler
    gmake -j"$SAMBA4X_JOBS" DESTDIR= install
    touch "$stamp"
}

build_samba4x_libtasn1() {
    stamp="$SAMBA4X_DEPS/.stamp-libtasn1-$SAMBA4X_LIBTASN1_VERSION"
    if [ -f "$stamp" ] && [ -f "$SAMBA4X_DEPS/lib/libtasn1.a" ]; then
        echo "libtasn1 $SAMBA4X_LIBTASN1_VERSION already built."
        return 0
    fi

    archive="$(download_samba4x_archive "$SAMBA4X_LIBTASN1_URL" "libtasn1-$SAMBA4X_LIBTASN1_VERSION.tar.gz")"
    extract_samba4x_archive "$archive" "libtasn1-$SAMBA4X_LIBTASN1_VERSION"
    cd "$SAMBA4X_BUILD/libtasn1-$SAMBA4X_LIBTASN1_VERSION"
    env PKG_CONFIG_PATH="$SAMBA4X_DEPS/lib/pkgconfig" \
        PKG_CONFIG_LIBDIR="$SAMBA4X_DEPS/lib/pkgconfig" \
        PKG_CONFIG_SYSROOT_DIR= \
        CC="$CC" CXX="$CXX" AR="$AR" RANLIB="$RANLIB" \
        CPPFLAGS="$CPPFLAGS" CFLAGS="$CFLAGS" LDFLAGS="$LDFLAGS" \
        ./configure \
            --host="$SAMBA4X_HOST_ALIAS" \
            --prefix="$SAMBA4X_DEPS" \
            --disable-shared \
            --enable-static \
            --disable-doc
    gmake -j"$SAMBA4X_JOBS" -C lib DESTDIR= install
    touch "$stamp"
}

rewrite_samba4x_gnutls_pc() {
    pc="$SAMBA4X_DEPS/lib/pkgconfig/gnutls.pc"
    if [ ! -f "$pc" ]; then
        echo "Unable to find generated gnutls.pc at $pc"
        exit 1
    fi

    static_libs='Libs: -L${libdir} -lgnutls'
    if [ -f "$SAMBA4X_DEPS/lib/libunistring.a" ]; then
        static_libs="$static_libs -lunistring"
    fi
    static_libs="$static_libs -ltasn1 -lhogweed -lnettle -lgmp"
    awk -v libs="$static_libs" '
        /^Libs:/ {
            print libs
            replaced = 1
            next
        }
        { print }
        END { if (replaced != 1) exit 1 }
    ' "$pc" >"$pc.tmp"
    mv "$pc.tmp" "$pc"
}

build_samba4x_gnutls() {
    gnutls_stamp_suffix="system-nettle-oaep-no-thread-local"
    stamp="$SAMBA4X_DEPS/.stamp-gnutls-$SAMBA4X_GNUTLS_VERSION-$gnutls_stamp_suffix"
    if [ -f "$stamp" ] && [ -f "$SAMBA4X_DEPS/lib/libgnutls.a" ]; then
        echo "GnuTLS $SAMBA4X_GNUTLS_VERSION already built."
        rewrite_samba4x_gnutls_pc
        return 0
    fi

    archive="$(download_samba4x_archive "$SAMBA4X_GNUTLS_URL" "gnutls-$SAMBA4X_GNUTLS_VERSION.tar.xz")"
    extract_samba4x_archive "$archive" "gnutls-$SAMBA4X_GNUTLS_VERSION"
    cd "$SAMBA4X_BUILD/gnutls-$SAMBA4X_GNUTLS_VERSION"
    # GnuTLS assumes glibc-style <byteswap.h>. NetBSD 6/7 and NetBSD4 expose
    # the target helpers through <sys/bswap.h> with bswap16/32/64 names.
    patch_apply_checked "GnuTLS NetBSD bswap include patch" \
        "$GNUTLS_PATCH_DIR/0001-netbsd-bswap-include.patch" \
        .
    # All Time Capsule Samba4X lanes are static no-pthread appliance builds.
    # GnuTLS keeps PRNG and FIPS state in C11 thread-local globals by default;
    # on these old NetBSD targets that pulls in runtime TLS behavior that is not
    # needed for a single-threaded smbd and can abort when the first real random
    # buffer is requested during session setup. Make those globals ordinary
    # statics for every no-pthread lane so NetBSD6/7 and NetBSD4 behave the same.
    patch_apply_checked "GnuTLS no-pthread random TLS patch" \
        "$GNUTLS_PATCH_DIR/0002-no-pthread-random-static-state.patch" \
        .
    patch_apply_checked "GnuTLS no-pthread FIPS TLS patch" \
        "$GNUTLS_PATCH_DIR/0003-no-pthread-fips-static-state.patch" \
        .
    env PKG_CONFIG_PATH="$SAMBA4X_DEPS/lib/pkgconfig" \
        PKG_CONFIG_LIBDIR="$SAMBA4X_DEPS/lib/pkgconfig" \
        PKG_CONFIG_SYSROOT_DIR= \
        CC="$CC" CXX="$CXX" AR="$AR" RANLIB="$RANLIB" \
        CPPFLAGS="$CPPFLAGS" CFLAGS="$CFLAGS" LDFLAGS="$LDFLAGS" \
        ac_cv_func_nettle_rsa_oaep_sha256_encrypt=yes \
        ./configure \
            --host="$SAMBA4X_HOST_ALIAS" \
            --prefix="$SAMBA4X_DEPS" \
            --disable-shared \
            --enable-static \
            --disable-doc \
            --disable-tools \
            --disable-tests \
            --disable-cxx \
            --disable-nls \
            --without-p11-kit \
            --without-idn \
            --without-tpm \
            --without-zlib \
            --without-brotli \
            --without-zstd \
            --with-included-unistring
    gmake -j"$SAMBA4X_JOBS" -C gl
    gmake -j"$SAMBA4X_JOBS" -C lib DESTDIR= install
    rewrite_samba4x_gnutls_pc
    touch "$stamp"
}

prepare_samba4x_deps() {
    echo "Preparing Samba4X static dependencies under $SAMBA4X_DEPS"
    mkdir -p "$SAMBA4X_DEPS" "$SAMBA4X_DEPS/lib" "$SAMBA4X_DEPS/include" "$SAMBA4X_DEPS/lib/pkgconfig"
    if install_samba4x_target_gmp; then
        echo "Using NetBSD target GMP from $OBJ"
    else
        echo "NetBSD target GMP is unavailable; building GMP $SAMBA4X_GMP_VERSION."
        build_samba4x_gmp
    fi
    install_samba4x_sysroot_pkg_config
    build_samba4x_nettle
    build_samba4x_libtasn1
    build_samba4x_gnutls
}

mkdir -p "$SAMBA4X_WORK" "$SAMBA4X_STAGE" "$SAMBA4X_BUILD" "$SAMBA4X_DEPS" "$SAMBA4X_STAGE/sbin"
MAP_FILE="$SAMBA4X_STAGE/sbin/smbd4x.map"

if [ "$SDK_FAMILY" = "netbsd4" ] && [ "$SAMBA4X_NETBSD4_GC_SECTIONS" = "1" ]; then
    prepare_netbsd4_gc_note_inputs
fi

export PATH="$TOOLDIR/bin:/usr/pkg/libexec/heimdal:/usr/local/libexec/heimdal:/usr/pkg/bin:$PATH"
export TOOLDIR DESTDIR TRIPLE SYSROOT
export AR="$TOOLDIR/bin/$TRIPLE-ar"
export RANLIB="$TOOLDIR/bin/$TRIPLE-ranlib"
export STRIP="$TOOLDIR/bin/$TRIPLE-strip"
export CROSS_EXEC_REMOTE_DIR="$SAMBA4X_CROSS_EXEC_REMOTE_DIR"

if [ "$SDK_FAMILY" = "netbsd4" ]; then
    export CC="$TOOLDIR/bin/$TRIPLE-gcc"
    export CXX="$TOOLDIR/bin/$TRIPLE-g++"
    export CPP="$TOOLDIR/bin/$TRIPLE-cpp"
    export LD="$TOOLDIR/bin/$TRIPLE-ld"
    export CFLAGS="-Os -ffunction-sections -fdata-sections -fomit-frame-pointer -fno-unwind-tables -fno-asynchronous-unwind-tables -fno-ident -fno-pie -fcommon -B$DESTDIR/usr/lib -B$DESTDIR/usr/lib/csu -isystem $SAMBA4X_DEPS/include -isystem $DESTDIR/usr/include -D_NETBSD_SOURCE -D_LARGEFILE_SOURCE -D_FILE_OFFSET_BITS=64 -D_LARGE_FILES -DTC_SAMBA4X_NETBSD4_COMPAT=1 -DTC_SAMBA4X_VFS_AT_PATH_COMPAT=1 -DTC_SAMBA4X_EMBEDDED_SRVSVC=1"
    export CXXFLAGS="$CFLAGS"
    export CPPFLAGS="-isystem $SAMBA4X_DEPS/include -isystem $DESTDIR/usr/include -D_NETBSD_SOURCE -D_LARGEFILE_SOURCE -D_FILE_OFFSET_BITS=64 -D_LARGE_FILES -DTC_SAMBA4X_NETBSD4_COMPAT=1 -DTC_SAMBA4X_VFS_AT_PATH_COMPAT=1 -DTC_SAMBA4X_EMBEDDED_SRVSVC=1"
    SAMBA4X_NETBSD4_BASE_LDFLAGS="-Wl,-Bstatic -static -L$SAMBA4X_DEPS/lib -L$DESTDIR/lib -L$DESTDIR/usr/lib -B$DESTDIR/usr/lib -B$DESTDIR/usr/lib/csu"
    SAMBA4X_SHARED_LDFLAGS_LIST="'-L$SAMBA4X_DEPS/lib', '-L$DESTDIR/lib', '-L$DESTDIR/usr/lib', '-B$DESTDIR/usr/lib', '-B$DESTDIR/usr/lib/csu'"
    SAMBA4X_NETBSD4_FINAL_LDFLAGS="$SAMBA4X_NETBSD4_BASE_LDFLAGS"
    SAMBA4X_FINAL_LDFLAGS_LIST="'-Wl,-Bstatic', '-static', '-L$SAMBA4X_DEPS/lib', '-L$DESTDIR/lib', '-L$DESTDIR/usr/lib', '-B$DESTDIR/usr/lib', '-B$DESTDIR/usr/lib/csu'"
    SAMBA4X_NETBSD4_FINAL_LINKFLAGS="$SAMBA4X_FINAL_LDFLAGS_LIST"
    if [ "$SAMBA4X_NETBSD4_GC_SECTIONS" = "1" ]; then
        SAMBA4X_NETBSD4_FINAL_LINKFLAGS="'-Wl,-Bstatic', '-static', '-Wl,--gc-sections', '-Wl,-Map=$MAP_FILE', '-Wl,-T,$SAMBA4X_NETBSD4_KEEP_NOTES_LD', '$SAMBA4X_NETBSD4_NOTE_OBJ', '-L$SAMBA4X_DEPS/lib', '-L$DESTDIR/lib', '-L$DESTDIR/usr/lib', '-B$DESTDIR/usr/lib', '-B$DESTDIR/usr/lib/csu'"
    fi
    SAMBA4X_FINAL_LINKFLAGS="$SAMBA4X_NETBSD4_FINAL_LINKFLAGS"
    export LDFLAGS="$SAMBA4X_NETBSD4_BASE_LDFLAGS"
else
    export CC="$TOOLDIR/bin/$TRIPLE-gcc --sysroot=$SYSROOT"
    export CXX="$TOOLDIR/bin/$TRIPLE-g++ --sysroot=$SYSROOT"
    export CPP="$TOOLDIR/bin/$TRIPLE-cpp --sysroot=$SYSROOT"
    export LD="$TOOLDIR/bin/$TRIPLE-ld --sysroot=$SYSROOT"
    export CFLAGS="-Os -ffunction-sections -fdata-sections -fomit-frame-pointer -fno-unwind-tables -fno-asynchronous-unwind-tables -fno-ident -fno-pie -fcommon -I$SAMBA4X_DEPS/include -DTC_SAMBA4X_VFS_AT_PATH_COMPAT=1 -DTC_SAMBA4X_EMBEDDED_SRVSVC=1"
    export CXXFLAGS="$CFLAGS"
    export CPPFLAGS="-I$SAMBA4X_DEPS/include -I$SYSROOT/usr/include -D_NETBSD_SOURCE -D_LARGEFILE_SOURCE -D_FILE_OFFSET_BITS=64 -D_LARGE_FILES -DTC_SAMBA4X_VFS_AT_PATH_COMPAT=1 -DTC_SAMBA4X_EMBEDDED_SRVSVC=1"
    SAMBA4X_SHARED_LDFLAGS_LIST="'-L$SAMBA4X_DEPS/lib', '-L$SYSROOT/lib', '-L$SYSROOT/usr/lib'"
    SAMBA4X_FINAL_LDFLAGS_LIST="'-Wl,-Bstatic', '-static', '-Wl,--gc-sections', '-Wl,-Map=$MAP_FILE', '-L$SAMBA4X_DEPS/lib', '-L$SYSROOT/lib', '-L$SYSROOT/usr/lib'"
    SAMBA4X_FINAL_LINKFLAGS="$SAMBA4X_FINAL_LDFLAGS_LIST"
    export LDFLAGS="-Wl,-Bstatic -static -Wl,--gc-sections -Wl,-Map=$MAP_FILE -L$SAMBA4X_DEPS/lib -L$SYSROOT/lib -L$SYSROOT/usr/lib"
fi
export PKG_CONFIG_DIR=
export PKG_CONFIG_PATH="$SAMBA4X_DEPS/lib/pkgconfig"
export PKG_CONFIG_LIBDIR="$SAMBA4X_DEPS/lib/pkgconfig"
export PKG_CONFIG_SYSROOT_DIR=

CROSS_EXECUTE="$(cd "$(dirname "$0")" && pwd)/samba4-cross-exec.sh"
SAMBA4X_STATIC_MODULES='vfs_catia,vfs_fruit,vfs_streams_xattr,vfs_xattr_tdb,vfs_acl_xattr'

{
    echo "SDK_FAMILY=$SDK_FAMILY"
    echo "DEVICE_FAMILY=$DEVICE_FAMILY"
    echo "NETBSD4_ABI=$NETBSD4_ABI"
    echo "SAMBA4X_VERSION=$SAMBA4X_VERSION"
    echo "TOOLDIR=$TOOLDIR"
    echo "DESTDIR=$DESTDIR"
    echo "TRIPLE=$TRIPLE"
    echo "SYSROOT=$SYSROOT"
    echo "WORK=$SAMBA4X_WORK"
    echo "STAGE=$SAMBA4X_STAGE"
    echo "DEPS=$SAMBA4X_DEPS"
    echo "SRC_DIR=$SAMBA4X_SRC_DIR"
    echo "HOST_ALIAS=$SAMBA4X_HOST_ALIAS"
    echo "NETTLE_VERSION=$SAMBA4X_NETTLE_VERSION"
    echo "LIBTASN1_VERSION=$SAMBA4X_LIBTASN1_VERSION"
    echo "GNUTLS_VERSION=$SAMBA4X_GNUTLS_VERSION"
    echo "STATIC_MODULES=$SAMBA4X_STATIC_MODULES"
    echo "SAMBA4X_NETBSD4_GC_SECTIONS=$SAMBA4X_NETBSD4_GC_SECTIONS"
    echo "SAMBA4X_NETBSD4_FINAL_LDFLAGS=${SAMBA4X_NETBSD4_FINAL_LDFLAGS:-$LDFLAGS}"
    echo "SAMBA4X_NETBSD4_FINAL_LINKFLAGS=${SAMBA4X_NETBSD4_FINAL_LINKFLAGS:-}"
    echo "SAMBA4X_SHARED_LDFLAGS_LIST=$SAMBA4X_SHARED_LDFLAGS_LIST"
    echo "SAMBA4X_FINAL_LDFLAGS_LIST=$SAMBA4X_FINAL_LDFLAGS_LIST"
    echo "SAMBA4X_FINAL_LINKFLAGS=$SAMBA4X_FINAL_LINKFLAGS"
    echo "MAP_FILE=$MAP_FILE"
    echo "CFLAGS=$CFLAGS"
    echo "CPPFLAGS=$CPPFLAGS"
    echo "LDFLAGS=$LDFLAGS"
    echo "PKG_CONFIG_LIBDIR=$PKG_CONFIG_LIBDIR"
    echo "CROSS_EXECUTE=$CROSS_EXECUTE"
    echo "CROSS_EXEC_REMOTE_DIR=$CROSS_EXEC_REMOTE_DIR"

    if [ ! -f "$SAMBA4X_SRC_DIR/configure" ]; then
        echo "Missing Samba 4.x source tree at $SAMBA4X_SRC_DIR"
        echo "Run $SAMBA_DOWNLOAD_WRAPPER first."
        exit 1
    fi

    PYTHON3_BIN="$(pick_python3)" || {
        echo "Unable to find a Python 3 interpreter on this VM."
        exit 1
    }
    echo "PYTHON3_BIN=$PYTHON3_BIN"

    prepare_samba4x_deps

    mkdir -p "$SAMBA4X_BUILD"
    cd "$SAMBA4X_SRC_DIR"
    PYTHONHASHSEED=1 "$PYTHON3_BIN" ./buildtools/bin/waf distclean >/dev/null 2>&1 || true

    CONFIGURE_ARGS="\
      --cross-compile \
      --cross-execute=$CROSS_EXECUTE \
      --hostcc=$HOST_CC \
      --prefix=$SAMBA4X_STAGE \
      --without-pie \
      --disable-python \
      --disable-avahi \
      --without-acl-support \
      --without-ad-dc \
      --without-ads \
      --without-automount \
      --without-dmapi \
      --without-ldap \
      --without-json \
      --without-gettext \
      --without-pam \
      --disable-cups \
      --disable-iprint \
      --without-winbind \
      --without-utmp \
      --without-syslog \
      --nonshared-binary=smbd/smbd"

    if [ -n "$SAMBA4X_STATIC_MODULES" ]; then
        CONFIGURE_ARGS="$CONFIGURE_ARGS --with-static-modules=$SAMBA4X_STATIC_MODULES"
    fi
    if [ "$SDK_FAMILY" = "netbsd4" ]; then
        CONFIGURE_ARGS="$CONFIGURE_ARGS --disable-fault-handling --without-libarchive"
    fi

    eval "PYTHON=\"$PYTHON3_BIN\" ./configure $CONFIGURE_ARGS"

    for cache_file in "$SAMBA4X_SRC_DIR"/bin/c4che/*.py; do
        [ -f "$cache_file" ] || continue
        # Waf configure runs on the VM while targeting the Time Capsule. Keep
        # these generated cache values deterministic and target-safe: smbd must
        # be static, all Time Capsule targets are no-pthread, and NetBSD4 also
        # needs stack-protector detections scrubbed after configure.
        set_waf_cache_value "$cache_file" "HOST_CFLAGS" "'$HOST_CFLAGS'"
        set_waf_cache_value "$cache_file" "HOST_CPPFLAGS" "'$HOST_CPPFLAGS'"
        set_waf_cache_value "$cache_file" "ENABLE_PIE" "False"
        set_waf_cache_value "$cache_file" "HAVE_POSIX_FALLOCATE" "()"
        set_waf_cache_value "$cache_file" "_POSIX_FALLOCATE_CAPABLE_LIBC" "()"
        set_waf_cache_value "$cache_file" "LDFLAGS" "[$SAMBA4X_SHARED_LDFLAGS_LIST]"
        set_waf_cache_value "$cache_file" "SMBD_STATIC_LINKFLAGS" "[$SAMBA4X_FINAL_LINKFLAGS]"
        set_waf_cache_value "$cache_file" "SMBD_STATIC_LDFLAGS" "[$SAMBA4X_FINAL_LDFLAGS_LIST]"
        set_waf_cache_value "$cache_file" "SMBD_STATIC_LIBPATH" "[]"
        set_waf_cache_value "$cache_file" "SMBD_STATIC_SHLIB_MARKER" "''"
        set_waf_cache_value "$cache_file" "SMBD_STATIC_FULLSTATIC_MARKER" "'-static'"
        # Make the extra srvsvc sources/deps in the smbd-base build explicit.
        # The matching C macro is in CFLAGS/CPPFLAGS above so the pipe handler
        # code and Waf dependency graph stay in sync.
        set_waf_cache_value "$cache_file" "TC_SAMBA4X_EMBEDDED_SRVSVC" "True"
        # Apple Time Capsule kernels do not support the pthread behavior Samba
        # 4.24 expects. Remove pthread results unconditionally for the Samba4x
        # appliance build so NetBSD4 and NetBSD6/7 use the same no-pthread path.
        set_waf_cache_value "$cache_file" "HAVE_PTHREAD" "()"
        set_waf_cache_value "$cache_file" "HAVE_PTHREAD_CREATE" "()"
        set_waf_cache_value "$cache_file" "HAVE_PTHREAD_ATTR_INIT" "()"
        set_waf_cache_value "$cache_file" "HAVE_LIBPTHREAD" "()"
        set_waf_cache_value "$cache_file" "WITH_PTHREADPOOL" "()"
        set_waf_cache_value "$cache_file" "LIB_pthread" "[]"
        set_waf_cache_value "$cache_file" "LIB_PTHREAD" "''"
        set_waf_cache_value "$cache_file" "replace_add_global_pthread" "False"
        remove_waf_cache_fixed_text "$cache_file" "'pthread': 'SYSLIB'" "s/'pthread': 'SYSLIB'/'pthread': 'EMPTY'/g" "Samba 4.x waf cache pthread syslib removal"
        remove_waf_cache_fixed_text "$cache_file" "'-lpthread'" "s/'-lpthread',\\s*//g; s/\\s*'\\-lpthread'\\s*//g" "Samba 4.x waf cache -lpthread removal"
        remove_waf_cache_fixed_text "$cache_file" "'-pthread'" "s/'-pthread',\\s*//g; s/\\s*'\\-pthread'\\s*//g" "Samba 4.x waf cache -pthread removal"
        remove_waf_cache_fixed_text "$cache_file" "'-D_REENTRANT'" "s/'-D_REENTRANT',\\s*//g; s/\\s*'\\-D_REENTRANT'\\s*//g" "Samba 4.x waf cache reentrant define removal"
        remove_waf_cache_fixed_text "$cache_file" "'-D_POSIX_PTHREAD_SEMANTICS'" "s/'-D_POSIX_PTHREAD_SEMANTICS',\\s*//g; s/\\s*'\\-D_POSIX_PTHREAD_SEMANTICS'\\s*//g" "Samba 4.x waf cache pthread semantics define removal"
        remove_waf_cache_fixed_text "$cache_file" "'HAVE_PTHREAD': '1'" "s/'HAVE_PTHREAD': '1'/'HAVE_PTHREAD': ()/g" "Samba 4.x waf cache HAVE_PTHREAD dict removal"
        remove_waf_cache_fixed_text "$cache_file" "'HAVE_PTHREAD_CREATE': 1" "s/'HAVE_PTHREAD_CREATE': 1/'HAVE_PTHREAD_CREATE': ()/g" "Samba 4.x waf cache HAVE_PTHREAD_CREATE dict removal"
        remove_waf_cache_fixed_text "$cache_file" "'HAVE_PTHREAD_ATTR_INIT': 1" "s/'HAVE_PTHREAD_ATTR_INIT': 1/'HAVE_PTHREAD_ATTR_INIT': ()/g" "Samba 4.x waf cache HAVE_PTHREAD_ATTR_INIT dict removal"
        remove_waf_cache_fixed_text "$cache_file" "'HAVE_LIBPTHREAD': 1" "s/'HAVE_LIBPTHREAD': 1/'HAVE_LIBPTHREAD': ()/g" "Samba 4.x waf cache HAVE_LIBPTHREAD dict removal"
        remove_waf_cache_fixed_text "$cache_file" "'WITH_PTHREADPOOL': '1'" "s/'WITH_PTHREADPOOL': '1'/'WITH_PTHREADPOOL': ()/g" "Samba 4.x waf cache WITH_PTHREADPOOL dict removal"
        if [ "$SDK_FAMILY" = "netbsd4" ]; then
            # NetBSD4's old static libc/toolchain combination does not support
            # the stack protector runtime expected by newer Samba configure
            # probes. NetBSD6/7 keep the normal detection.
            remove_waf_cache_fixed_text "$cache_file" "'-fstack-protector'" "s/'-fstack-protector',\\s*//g; s/\\s*'\\-fstack-protector'\\s*//g" "Samba 4.x waf cache NetBSD4 stack protector removal"
        fi
        set_waf_cache_value "$cache_file" "HAVE_BACKTRACE" "()"
        set_waf_cache_value "$cache_file" "HAVE_BACKTRACE_SYMBOLS" "()"
        set_waf_cache_value "$cache_file" "HAVE_EXECINFO_H" "()"
        # The Time Capsule static appliance binary runs on old NetBSD libc.
        # Samba only uses process titles for ps/debug labels, while NetBSD's
        # libc setproctitle path aborts in smbd helper children during startup.
        # Force lib/replace's no-op fallback so notifyd/cleanupd can start.
        set_waf_cache_value "$cache_file" "HAVE_SETPROCTITLE" "()"
        set_waf_cache_value "$cache_file" "HAVE_SETPROCTITLE_INIT" "()"
        set_waf_cache_value "$cache_file" "FULLSTATIC" "True"
    done

    for config_header in \
        "$SAMBA4X_SRC_DIR/bin/default/include/config.h" \
        "$SAMBA4X_SRC_DIR/bin/default/source3/include/config.h" \
        "$SAMBA4X_SRC_DIR/bin/default/source4/include/config.h"
    do
        [ -f "$config_header" ] || continue
        # The generated config headers mirror waf's cache. Keep them in sync
        # so all Time Capsule targets compile the same static no-pthread
        # appliance path instead of accidentally re-enabling VM-host detections.
        undef_config_symbol "$config_header" "HAVE_POSIX_FALLOCATE"
        undef_config_symbol "$config_header" "_POSIX_FALLOCATE_CAPABLE_LIBC"
        undef_config_symbol "$config_header" "HAVE_PTHREAD"
        undef_config_symbol "$config_header" "HAVE_PTHREAD_CREATE"
        undef_config_symbol "$config_header" "HAVE_PTHREAD_ATTR_INIT"
        undef_config_symbol "$config_header" "HAVE_LIBPTHREAD"
        undef_config_symbol "$config_header" "WITH_PTHREADPOOL"
        undef_config_symbol "$config_header" "HAVE_EXECINFO_H"
        undef_config_symbol "$config_header" "HAVE_BACKTRACE"
        undef_config_symbol "$config_header" "HAVE_BACKTRACE_SYMBOLS"
        # Keep config.h aligned with the waf cache override above. This trades
        # cosmetic process titles for reliable startup on NetBSD4 and 6/7.
        undef_config_symbol "$config_header" "HAVE_SETPROCTITLE"
        undef_config_symbol "$config_header" "HAVE_SETPROCTITLE_INIT"
    done

    if [ "$SDK_FAMILY" = "netbsd4" ] && [ "$SAMBA4X_NETBSD4_GC_SECTIONS" = "1" ]; then
        export LDFLAGS="$SAMBA4X_NETBSD4_FINAL_LDFLAGS"
        echo "Final NetBSD4 build LDFLAGS=$LDFLAGS"
    fi

    PYTHONHASHSEED=1 "$PYTHON3_BIN" ./buildtools/bin/waf -v -j"$SAMBA4X_JOBS" build --targets=smbd/smbd

    stage_samba4x_binary() {
        label=$1
        source_glob=$2
        stage_path=$3
        stripped_path=$4

        matches_file="$SAMBA4X_BUILD/stage-$label-matches.$$"
        find "$SAMBA4X_SRC_DIR/bin" -path "$source_glob" -type f >"$matches_file"
        match_count="$(wc -l <"$matches_file" | tr -d ' ')"
        if [ "$match_count" = "0" ]; then
            rm -f "$matches_file"
            echo "Unable to locate built Samba 4.x $label under $SAMBA4X_SRC_DIR/bin"
            exit 1
        fi
        if [ "$match_count" != "1" ]; then
            echo "Found multiple built Samba 4.x $label candidates under $SAMBA4X_SRC_DIR/bin:"
            sed 's/^/  /' "$matches_file"
            rm -f "$matches_file"
            exit 1
        fi
        built_path="$(sed -n '1p' "$matches_file")"
        rm -f "$matches_file"

        "$TOOLDIR/bin/nbfile" "$built_path" 2>&1 || true
        if "$TOOLDIR/bin/$TRIPLE-objdump" -p "$built_path" | grep -Eq '^[[:space:]]+(INTERP|DYNAMIC)'; then
            echo "Samba 4.x $label has dynamic ELF headers; refusing to stage it."
            exit 1
        fi
        dump_elf_notes "built $label" "$built_path"
        validate_netbsd4_notes "$built_path"

        mkdir -p "$(dirname "$stage_path")"
        cp "$built_path" "$stage_path"
        cp "$built_path" "$stripped_path"
        "$STRIP" --strip-unneeded "$stripped_path"
        validate_netbsd4_notes "$stripped_path"
        dump_elf_notes "staged stripped $label" "$stripped_path"
    }

    # Stage only smbd. The download patch embeds the minimal srvsvc endpoint
    # required for Finder/smbclient share enumeration, while omitting the larger
    # external samba-dcerpcd/rpcd_* helper stack that cannot safely live on the
    # Apple-managed HFS disk after boot.
    stage_samba4x_binary "smbd" '*/source3/smbd/smbd' \
        "$SAMBA4X_STAGE/sbin/smbd" \
        "$SAMBA4X_STAGE/sbin/smbd.stripped"
} >"$SAMBA4X_LOG" 2>&1

printf 'Samba 4.x build complete.\n'
printf 'Log: %s\n' "$SAMBA4X_LOG"
printf 'Regular binary: %s\n' "$SAMBA4X_STAGE/sbin/smbd"
printf 'Stripped binary: %s\n' "$SAMBA4X_STAGE/sbin/smbd.stripped"
