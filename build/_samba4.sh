#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"

TOOLDIR="$TOOLS"
DESTDIR="$OBJ/destdir.evbarm"
TRIPLE="$(basename "$(ls "$TOOLDIR"/bin/*-netbsdelf-*gcc | head -n1)" | sed 's/-gcc$//')"
SYSROOT="$DESTDIR"

if [ ! -x "$TOOLDIR/bin/nbmake" ] || [ ! -d "$DESTDIR" ]; then
    echo "Missing toolchain/sysroot under $OUT"
    echo "Run $SDK_BOOTSTRAP_WRAPPER first."
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
SAMBA4_STATIC_MODULES='vfs_catia,vfs_fruit,vfs_streams_xattr,vfs_xattr_tdb,vfs_acl_xattr'

{
    echo "SDK_FAMILY=$SDK_FAMILY"
    echo "DEVICE_FAMILY=$DEVICE_FAMILY"
    echo "SAMBA4_VERSION=$SAMBA4_VERSION"
    echo "TOOLDIR=$TOOLDIR"
    echo "DESTDIR=$DESTDIR"
    echo "TRIPLE=$TRIPLE"
    echo "SYSROOT=$SYSROOT"
    echo "WORK=$SAMBA4_WORK"
    echo "STAGE=$SAMBA4_STAGE"
    echo "SRC_DIR=$SAMBA4_SRC_DIR"
    echo "HOST_ALIAS=$SAMBA4_HOST_ALIAS"
    echo "STATIC_MODULES=$SAMBA4_STATIC_MODULES"
    echo "CROSS_EXECUTE=$CROSS_EXECUTE"

    if [ ! -f "$SAMBA4_SRC_DIR/configure" ]; then
        echo "Missing Samba 4 source tree at $SAMBA4_SRC_DIR"
        echo "Run $SAMBA_DOWNLOAD_WRAPPER first."
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

    # Force IPC$ to be non-guest. macOS Time Machine currently sees
    # SupportsGuest=1 from Samba 4.8 and then attempts the wrong auth flow.
    # Keeping this patch in the build script makes the experiment reproducible.
    perl -0pi -e 's/lp_add_ipc\("IPC\\\$", \(lp_restrict_anonymous\(\) < 2\)\);/lp_add_ipc("IPC\$", false);/' \
        "$SAMBA4_SRC_DIR/source3/param/loadparm.c"

    CONFIGURE_ARGS="\
      --cross-compile \
      --cross-execute=$CROSS_EXECUTE \
      --hostcc=$HOST_CC \
      --prefix=$SAMBA4_STAGE \
      --bundled-libraries=!asn1_compile,!compile_et \
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
      --nonshared-binary=smbd/smbd"

    if [ -n "$SAMBA4_STATIC_MODULES" ]; then
        CONFIGURE_ARGS="$CONFIGURE_ARGS --with-static-modules=$SAMBA4_STATIC_MODULES"
    fi

    # NetBSD 6 on the Time Capsule does not expose the POSIX ACL API Samba
    # probes for in configure (`sys/acl.h`, libacl). We use the acl_xattr VFS
    # module to provide Windows ACL semantics via xattrs/tdb instead of native
    # filesystem ACLs.
    #
    # Intentionally keep the Time Machine VFS stack static during experiments.
    # The device does not have a normal shared-module runtime tree, and the
    # earlier fruit test failed because smbd tried to dlopen streams_xattr.so.
    eval "PYTHON=\"$PYTHON2_BIN\" ./configure $CONFIGURE_ARGS"

    for cache_file in "$SAMBA4_SRC_DIR"/bin/c4che/*.py; do
        [ -f "$cache_file" ] || continue
        perl -0pi -e 's/^ENABLE_PIE = True$/ENABLE_PIE = False/m' "$cache_file"
        perl -0pi -e 's/^HAVE_POSIX_FALLOCATE = .*$/HAVE_POSIX_FALLOCATE = ()/m' "$cache_file"
        perl -0pi -e 's/^_POSIX_FALLOCATE_CAPABLE_LIBC = .*$/_POSIX_FALLOCATE_CAPABLE_LIBC = ()/m' "$cache_file"
        if [ "$NO_PTHREADS" = "1" ]; then
            perl -0pi -e 's/^HAVE_PTHREAD = .*$/HAVE_PTHREAD = ()/m' "$cache_file"
            perl -0pi -e 's/^HAVE_PTHREAD_CREATE = .*$/HAVE_PTHREAD_CREATE = ()/m' "$cache_file"
            perl -0pi -e 's/^HAVE_PTHREAD_ATTR_INIT = .*$/HAVE_PTHREAD_ATTR_INIT = ()/m' "$cache_file"
            perl -0pi -e 's/^HAVE_LIBPTHREAD = .*$/HAVE_LIBPTHREAD = ()/m' "$cache_file"
            perl -0pi -e 's/^WITH_PTHREADPOOL = .*$/WITH_PTHREADPOOL = ()/m' "$cache_file"
            perl -0pi -e "s/^LIB_pthread = \\['pthread'\\]$/LIB_pthread = []/m" "$cache_file"
            perl -0pi -e "s/^LIB_PTHREAD = 'pthread'$/LIB_PTHREAD = ''/m" "$cache_file"
            perl -0pi -e "s/'pthread': 'SYSLIB'/'pthread': 'EMPTY'/g" "$cache_file"
        fi
        # Keep the optional execinfo/backtrace feature disabled in the
        # generated cache too. The source-tree patches remove the direct deps,
        # and these cache edits stop generated config defines from reviving the
        # code paths during a clean rebuild.
        perl -0pi -e 's/^HAVE_BACKTRACE = .*$/HAVE_BACKTRACE = ()/m' "$cache_file"
        perl -0pi -e 's/^HAVE_BACKTRACE_SYMBOLS = .*$/HAVE_BACKTRACE_SYMBOLS = ()/m' "$cache_file"
        perl -0pi -e 's/^HAVE_EXECINFO_H = .*$/HAVE_EXECINFO_H = ()/m' "$cache_file"
        if ! grep -q '^FULLSTATIC = ' "$cache_file"; then
            perl -0pi -e 's/^(FULLSTATIC_MARKER = .*)$/$1\nFULLSTATIC = True/m' "$cache_file"
        fi
        grep -q '^FULLSTATIC = ' "$cache_file" || printf 'FULLSTATIC = True\n' >>"$cache_file"
    done

    for config_header in \
        "$SAMBA4_SRC_DIR/bin/default/include/config.h" \
        "$SAMBA4_SRC_DIR/bin/default/source3/include/config.h" \
        "$SAMBA4_SRC_DIR/bin/default/source4/include/config.h"
    do
        [ -f "$config_header" ] || continue
        perl -0pi -e 's/^#define HAVE_POSIX_FALLOCATE 1$/\/\* #undef HAVE_POSIX_FALLOCATE \*\//m' "$config_header"
        perl -0pi -e 's/^#define _POSIX_FALLOCATE_CAPABLE_LIBC 1$/\/\* #undef _POSIX_FALLOCATE_CAPABLE_LIBC \*\//m' "$config_header"
        if [ "$NO_PTHREADS" = "1" ]; then
            perl -0pi -e 's/^#define HAVE_PTHREAD 1$/\/\* #undef HAVE_PTHREAD \*\//m' "$config_header"
            perl -0pi -e 's/^#define HAVE_PTHREAD_CREATE 1$/\/\* #undef HAVE_PTHREAD_CREATE \*\//m' "$config_header"
            perl -0pi -e 's/^#define HAVE_PTHREAD_ATTR_INIT 1$/\/\* #undef HAVE_PTHREAD_ATTR_INIT \*\//m' "$config_header"
            perl -0pi -e 's/^#define HAVE_LIBPTHREAD 1$/\/\* #undef HAVE_LIBPTHREAD \*\//m' "$config_header"
            perl -0pi -e 's/^#define WITH_PTHREADPOOL "1"$/\/\* #undef WITH_PTHREADPOOL \*\//m' "$config_header"
        fi
        perl -0pi -e 's/^#define HAVE_EXECINFO_H 1$/\/\* #undef HAVE_EXECINFO_H \*\//m' "$config_header"
        perl -0pi -e 's/^#define HAVE_BACKTRACE 1$/\/\* #undef HAVE_BACKTRACE \*\//m' "$config_header"
        perl -0pi -e 's/^#define HAVE_BACKTRACE_SYMBOLS 1$/\/\* #undef HAVE_BACKTRACE_SYMBOLS \*\//m' "$config_header"
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
