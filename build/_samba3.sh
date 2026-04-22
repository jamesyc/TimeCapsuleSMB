#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"

dump_elf_diagnostics() {
    label="$1"
    path="$2"

    echo "===== $label ====="
    "$TOOLDIR/bin/$TRIPLE-readelf" -h -l "$path" | sed -n '1,140p'
    "$TOOLDIR/bin/$TRIPLE-readelf" -S "$path" | sed -n '1,140p'
    "$TOOLDIR/bin/$TRIPLE-readelf" -d "$path" 2>&1 | sed -n '1,80p'
    "$TOOLDIR/bin/$TRIPLE-objdump" -p "$path" | sed -n '1,120p'
    dd if="$path" bs=64 count=1 2>/dev/null | od -Ax -tx1
}

has_netbsd_notes() {
    path="$1"
    "$TOOLDIR/bin/$TRIPLE-readelf" -S "$path" 2>/dev/null | \
        grep -F '.note.netbsd.iden' >/dev/null 2>&1
}

TOOLDIR="$TOOLS"
DESTDIR="$OBJ/destdir.evbarm"
TRIPLE="$(select_tool_triple)"
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

mkdir -p "$SAMBA3_WORK" "$SAMBA3_STAGE" "$SAMBA3_BUILD"

export PATH="$TOOLDIR/bin:/usr/pkg/libexec/heimdal:/usr/local/libexec/heimdal:/usr/pkg/bin:$PATH"
export TOOLDIR DESTDIR TRIPLE SYSROOT
export CC="$TOOLDIR/bin/$TRIPLE-gcc"
export CXX="$TOOLDIR/bin/$TRIPLE-g++"
export CPP="$TOOLDIR/bin/$TRIPLE-cpp"
export AR="$TOOLDIR/bin/$TRIPLE-ar"
export RANLIB="$TOOLDIR/bin/$TRIPLE-ranlib"
export STRIP="$TOOLDIR/bin/$TRIPLE-strip"
export LD="$TOOLDIR/bin/$TRIPLE-ld"
export CFLAGS="-Os -fomit-frame-pointer -ffunction-sections -fdata-sections -fno-unwind-tables -fno-asynchronous-unwind-tables -fno-ident -fno-pie -B$DESTDIR/usr/lib -B$DESTDIR/usr/lib/csu -I$DESTDIR/usr/include -D_NETBSD_SOURCE -D_LARGEFILE_SOURCE -D_FILE_OFFSET_BITS=64 -D_LARGE_FILES"
export CXXFLAGS="$CFLAGS"
export CPPFLAGS="-I$DESTDIR/usr/include -D_NETBSD_SOURCE -D_LARGEFILE_SOURCE -D_FILE_OFFSET_BITS=64 -D_LARGE_FILES"
# Do not add --gc-sections here. On NetBSD 4 it garbage-collects the
# .note.netbsd.ident/.note.netbsd.pax sections contributed by crti.o, and the
# Time Capsule kernel then rejects the ELF as a non-native executable.
export LDFLAGS="-static -L$DESTDIR/lib -L$DESTDIR/usr/lib -B$DESTDIR/usr/lib -B$DESTDIR/usr/lib/csu"
export LIBS="-lintl"
export ac_cv_func_memmove=yes
export ac_cv_func_strcasecmp=yes
export ac_cv_func_strncasecmp=yes
export ac_cv_file__proc_sys_kernel_core_pattern=no
export libreplace_cv_HAVE_GETADDRINFO=no
export libreplace_cv_READDIR_GETDIRENTRIES=no
export libreplace_cv_READDIR_GETDENTS=no
export samba_cv_CC_NEGATIVE_ENUM_VALUES=yes
export samba_cv_HAVE_GETTIMEOFDAY_TZ=yes

SAMBA3_SOURCE3_DIR="$SAMBA3_SRC_DIR/source3"
SAMBA3_LINK_PLAN="$SAMBA3_BUILD/smbd.linkplan"
SAMBA3_TC_SHELL="$SAMBA3_BUILD/timecapsule-bin-sh"

{
    echo "SDK_FAMILY=$SDK_FAMILY"
    echo "DEVICE_FAMILY=$DEVICE_FAMILY"
    echo "SAMBA3_VERSION=$SAMBA3_VERSION"
    echo "TOOLDIR=$TOOLDIR"
    echo "DESTDIR=$DESTDIR"
    echo "TRIPLE=$TRIPLE"
    echo "SYSROOT=$SYSROOT"
    echo "WORK=$SAMBA3_WORK"
    echo "STAGE=$SAMBA3_STAGE"
    echo "SRC_DIR=$SAMBA3_SRC_DIR"
    echo "SOURCE3_DIR=$SAMBA3_SOURCE3_DIR"
    echo "HOST_ALIAS=$SAMBA3_HOST_ALIAS"

    if [ ! -d "$SAMBA3_SOURCE3_DIR" ]; then
        echo "Missing Samba 3 source tree at $SAMBA3_SRC_DIR"
        echo "Run $SAMBA_DOWNLOAD_WRAPPER first."
        exit 1
    fi

    PYTHON2_BIN="$(pick_python2)" || {
        echo "Unable to find a Python 2 interpreter on this VM."
        exit 1
    }
    echo "PYTHON2_BIN=$PYTHON2_BIN"

    mkdir -p "$SAMBA3_BUILD"
    cd "$SAMBA3_SOURCE3_DIR"
    make distclean >/dev/null 2>&1 || true
    # Samba 3.6.x bundles pidl code that predates modern Perl rejecting
    # defined(@array). Patch the two offending call sites in-place so the
    # release tarball can still regenerate or validate generated IDL stubs.
    perl -0pi -e 's/if \\(defined\\(@\\$podl\\)\\) \\{/if (\\$podl) {/g' \
        "$SAMBA3_SRC_DIR/pidl/lib/Parse/Pidl/ODL.pm"
    perl -0pi -e 's/defined \\@\\$pidl/defined(\\$pidl)/g' \
        "$SAMBA3_SRC_DIR/pidl/pidl"

    if [ ! -f "$SAMBA3_SOURCE3_DIR/configure" ]; then
        if [ ! -x "$SAMBA3_SOURCE3_DIR/autogen.sh" ]; then
            echo "Missing Samba 3 configure script and autogen helper."
            exit 1
        fi
        ./autogen.sh
    fi

    CONFIGURE_ARGS="\
      --host=$TRIPLE \
      --build=$SAMBA3_HOST_ALIAS \
      --prefix=$SAMBA3_STAGE \
      --without-ads \
      --without-ldap \
      --without-krb5 \
      --without-pam \
      --without-winbind \
      --disable-cups \
      --without-utmp \
      --without-syslog \
      --without-acl-support \
      --disable-shared \
      --enable-static \
      --with-pic=no"

    PYTHON="$PYTHON2_BIN" ./configure $CONFIGURE_ARGS
    # Samba's configure enables PIE on this toolchain. Leaving -pie/-fPIE in the
    # generated Makefile produced ET_DYN output with /usr/lib/ld.so.1, which the
    # NetBSD 4 Time Capsule cannot run. Scrub those flags after configure so the
    # final smbd link remains static ET_EXEC while keeping the NetBSD ELF notes.
    perl -0pi -e 's/(^LDFLAGS\s*=\s*)-pie\s+/$1/mg; s/\s-pie(\s|$)/$1/g; s/(^LDSHFLAGS\s*=\s*)-fPIE\s+/$1/mg; s/\s-fPIE(\s|$)/$1/g' \
        "$SAMBA3_SOURCE3_DIR/Makefile"
    gmake -n bin/smbd >"$SAMBA3_LINK_PLAN" 2>&1 || true
    echo "===== planned smbd link ====="
    awk '
        /Linking bin\/smbd/ { capture=1 }
        capture { print }
        capture && /-o bin\/smbd([[:space:]]|$)/ { exit }
    ' "$SAMBA3_LINK_PLAN" | sed -n '1,160p'

    gmake -j"$SAMBA3_JOBS"

    SAMBA3_SMBD=""
    for candidate in \
        "$SAMBA3_SOURCE3_DIR/bin/smbd" \
        "$SAMBA3_SOURCE3_DIR/smbd/smbd" \
        "$SAMBA3_SRC_DIR/bin/smbd"
    do
        if [ -f "$candidate" ]; then
            SAMBA3_SMBD="$candidate"
            break
        fi
    done
    if [ -z "$SAMBA3_SMBD" ]; then
        SAMBA3_SMBD="$(find "$SAMBA3_SOURCE3_DIR" -path '*/smbd' -type f | head -n1)"
    fi
    if [ -z "$SAMBA3_SMBD" ] || [ ! -f "$SAMBA3_SMBD" ]; then
        echo "Unable to locate built Samba 3 smbd under $SAMBA3_SRC_DIR"
        exit 1
    fi

    SAMBA3_FILE_OUTPUT="$("$TOOLDIR/bin/nbfile" "$SAMBA3_SMBD" 2>/dev/null || true)"
    if [ -n "$SAMBA3_FILE_OUTPUT" ]; then
        echo "$SAMBA3_FILE_OUTPUT"
    else
        echo "WARNING: nbfile could not identify the Samba 3 binary."
    fi
    if [ -n "$SAMBA3_FILE_OUTPUT" ]; then
        case "$SAMBA3_FILE_OUTPUT" in
            *"statically linked"*)
                ;;
            *)
                echo "Samba 3 smbd is not statically linked; refusing to stage it."
                exit 1
                ;;
        esac
    fi

    mkdir -p "$SAMBA3_STAGE/sbin"
    cp "$SAMBA3_SMBD" "$SAMBA3_STAGE/sbin/smbd"
    cp "$SAMBA3_SMBD" "$SAMBA3_STAGE/sbin/smbd.stripped"
    "$STRIP" --strip-unneeded "$SAMBA3_STAGE/sbin/smbd.stripped"

    if [ -f "$OUT/probe/hello" ]; then
        dump_elf_diagnostics "hello probe" "$OUT/probe/hello"
    else
        echo "WARNING: hello probe not found at $OUT/probe/hello"
    fi
    dump_elf_diagnostics "samba3 smbd" "$SAMBA3_STAGE/sbin/smbd"
    dump_elf_diagnostics "samba3 smbd stripped" "$SAMBA3_STAGE/sbin/smbd.stripped"

    if [ -f "$OUT/probe/hello" ]; then
        if has_netbsd_notes "$OUT/probe/hello"; then
            echo "hello probe has NetBSD ELF notes"
        else
            echo "hello probe is missing NetBSD ELF notes"
        fi
    fi
    if has_netbsd_notes "$SAMBA3_STAGE/sbin/smbd"; then
        echo "samba3 smbd has NetBSD ELF notes"
    else
        echo "samba3 smbd is missing NetBSD ELF notes"
    fi

    if tc_ssh "$TC_HOST" 'cat /bin/sh' >"$SAMBA3_TC_SHELL"; then
        dump_elf_diagnostics "time capsule /bin/sh" "$SAMBA3_TC_SHELL"
        if has_netbsd_notes "$SAMBA3_TC_SHELL"; then
            echo "time capsule /bin/sh has NetBSD ELF notes"
        else
            echo "time capsule /bin/sh is missing NetBSD ELF notes"
        fi
    else
        echo "WARNING: unable to fetch /bin/sh from the time capsule"
    fi
} >"$SAMBA3_LOG" 2>&1

printf 'Samba 3 build complete.\n'
printf 'Log: %s\n' "$SAMBA3_LOG"
printf 'Regular binary: %s\n' "$SAMBA3_STAGE/sbin/smbd"
printf 'Stripped binary: %s\n' "$SAMBA3_STAGE/sbin/smbd.stripped"
