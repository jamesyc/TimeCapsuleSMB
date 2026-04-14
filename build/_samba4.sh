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

has_elf_section() {
    path="$1"
    section="$2"
    # This old readelf truncates long section names in its default -S output
    # (for example .note.netbsd.ident becomes .note.netbsd.iden), which made
    # the NetBSD4 note repair reject a valid donor binary. objdump -h prints
    # the full section name, so use it for exact section-name checks.
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
    SAMBA4_NETBSD4_NOTE_ASM="$SAMBA4_BUILD/netbsd4-notes.S"
    SAMBA4_NETBSD4_NOTE_OBJ="$SAMBA4_BUILD/netbsd4-notes.o"
    SAMBA4_NETBSD4_DEFAULT_LD="$SAMBA4_BUILD/netbsd4-default.ld"
    SAMBA4_NETBSD4_KEEP_NOTES_LD="$SAMBA4_BUILD/netbsd4-keep-notes.ld"

    cat >"$SAMBA4_NETBSD4_NOTE_ASM" <<'EOF'
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

    "$TOOLDIR/bin/$TRIPLE-gcc" -c "$SAMBA4_NETBSD4_NOTE_ASM" -o "$SAMBA4_NETBSD4_NOTE_OBJ"

    "$TOOLDIR/bin/$TRIPLE-ld" --verbose | awk '
        /^====/ { seen++; next }
        seen == 1 { print }
    ' >"$SAMBA4_NETBSD4_DEFAULT_LD"

    # --gc-sections drops the crt-provided NetBSD identity notes unless the
    # linker script explicitly marks them live. Keeping them at link time is
    # required because post-link objcopy can add sections but cannot reliably
    # recreate the PT_NOTE program headers that NetBSD 4 needs to exec smbd.
    awk '
        /SIZEOF_HEADERS;/ {
            print
            print "  .note.netbsd.ident : { KEEP(*(.note.netbsd.ident)) }"
            print "  .note.netbsd.pax : { KEEP(*(.note.netbsd.pax)) }"
            next
        }
        { print }
    ' "$SAMBA4_NETBSD4_DEFAULT_LD" >"$SAMBA4_NETBSD4_KEEP_NOTES_LD"

    export SAMBA4_NETBSD4_NOTE_OBJ SAMBA4_NETBSD4_KEEP_NOTES_LD
}

validate_netbsd4_notes() {
    path="$1"

    if [ "$SDK_FAMILY" != "netbsd4" ] || [ "$SAMBA4_NETBSD4_GC_SECTIONS" != "1" ]; then
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

mkdir -p "$SAMBA4_WORK" "$SAMBA4_STAGE" "$SAMBA4_BUILD"

if [ "$SDK_FAMILY" = "netbsd4" ] && [ "$SAMBA4_NETBSD4_GC_SECTIONS" = "1" ]; then
    prepare_netbsd4_gc_note_inputs
fi

export PATH="$TOOLDIR/bin:/usr/pkg/libexec/heimdal:/usr/local/libexec/heimdal:/usr/pkg/bin:$PATH"
export TOOLDIR DESTDIR TRIPLE SYSROOT
export AR="$TOOLDIR/bin/$TRIPLE-ar"
export RANLIB="$TOOLDIR/bin/$TRIPLE-ranlib"
export STRIP="$TOOLDIR/bin/$TRIPLE-strip"

if [ "$SDK_FAMILY" = "netbsd4" ]; then
    export CC="$TOOLDIR/bin/$TRIPLE-gcc"
    export CXX="$TOOLDIR/bin/$TRIPLE-g++"
    export CPP="$TOOLDIR/bin/$TRIPLE-cpp"
    export LD="$TOOLDIR/bin/$TRIPLE-ld"
    # Keep NetBSD headers available without making them outrank Samba's own
    # embedded Heimdal headers. A leading -I$DESTDIR/usr/include causes mixed
    # system/embedded GSSAPI typedefs on NetBSD 4.
    export CFLAGS="-Os -ffunction-sections -fdata-sections -fomit-frame-pointer -fno-unwind-tables -fno-asynchronous-unwind-tables -fno-ident -fno-pie -B$DESTDIR/usr/lib -B$DESTDIR/usr/lib/csu -isystem $DESTDIR/usr/include -D_NETBSD_SOURCE -D_LARGEFILE_SOURCE -D_FILE_OFFSET_BITS=64 -D_LARGE_FILES"
    export CXXFLAGS="$CFLAGS"
    export CPPFLAGS="-isystem $DESTDIR/usr/include -D_NETBSD_SOURCE -D_LARGEFILE_SOURCE -D_FILE_OFFSET_BITS=64 -D_LARGE_FILES"
    # NetBSD 4's arm--netbsdelf linker was not configured for --sysroot.
    # Enabling --gc-sections is the main size win for the old Samba 4 binary,
    # but it can collect the NetBSD note sections from crt objects. For the
    # final build, link a tiny note object and use a generated linker script to
    # KEEP those sections so the output still has PT_NOTE headers.
    SAMBA4_NETBSD4_BASE_LDFLAGS="-static -L$DESTDIR/lib -L$DESTDIR/usr/lib -B$DESTDIR/usr/lib -B$DESTDIR/usr/lib/csu"
    SAMBA4_NETBSD4_FINAL_LDFLAGS="$SAMBA4_NETBSD4_BASE_LDFLAGS"
    SAMBA4_NETBSD4_FINAL_LINKFLAGS=""
    if [ "$SAMBA4_NETBSD4_GC_SECTIONS" = "1" ]; then
        SAMBA4_NETBSD4_FINAL_LDFLAGS="-static -L$DESTDIR/lib -L$DESTDIR/usr/lib -B$DESTDIR/usr/lib -B$DESTDIR/usr/lib/csu"
        SAMBA4_NETBSD4_FINAL_LINKFLAGS="'-Wl,--gc-sections', '-Wl,-T,$SAMBA4_NETBSD4_KEEP_NOTES_LD', '$SAMBA4_NETBSD4_NOTE_OBJ', '-static', '-L$DESTDIR/lib', '-L$DESTDIR/usr/lib', '-B$DESTDIR/usr/lib', '-B$DESTDIR/usr/lib/csu'"
    fi
    # Configure runs cross-exec probes on the Time Capsule. Those probe ELFs
    # need their native NetBSD notes too, so keep configure on the safe no-GC
    # path and add --gc-sections only to the final smbd build below.
    export LDFLAGS="$SAMBA4_NETBSD4_BASE_LDFLAGS"
else
    export CC="$TOOLDIR/bin/$TRIPLE-gcc --sysroot=$SYSROOT"
    export CXX="$TOOLDIR/bin/$TRIPLE-g++ --sysroot=$SYSROOT"
    export CPP="$TOOLDIR/bin/$TRIPLE-cpp --sysroot=$SYSROOT"
    export LD="$TOOLDIR/bin/$TRIPLE-ld --sysroot=$SYSROOT"
    export CFLAGS="-Os -ffunction-sections -fdata-sections -fomit-frame-pointer -fno-unwind-tables -fno-asynchronous-unwind-tables -fno-ident -fno-pie"
    export CXXFLAGS="-Os -ffunction-sections -fdata-sections -fomit-frame-pointer -fno-unwind-tables -fno-asynchronous-unwind-tables -fno-ident -fno-pie"
    export CPPFLAGS="-I$SYSROOT/usr/include -D_NETBSD_SOURCE -D_LARGEFILE_SOURCE -D_FILE_OFFSET_BITS=64 -D_LARGE_FILES"
    export LDFLAGS="-static -Wl,--gc-sections -L$SYSROOT/lib -L$SYSROOT/usr/lib"
fi
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
    echo "SAMBA4_NETBSD4_GC_SECTIONS=$SAMBA4_NETBSD4_GC_SECTIONS"
    echo "SAMBA4_NETBSD4_FINAL_LDFLAGS=${SAMBA4_NETBSD4_FINAL_LDFLAGS:-$LDFLAGS}"
    echo "SAMBA4_NETBSD4_FINAL_LINKFLAGS=${SAMBA4_NETBSD4_FINAL_LINKFLAGS:-}"
    echo "CFLAGS=$CFLAGS"
    echo "LDFLAGS=$LDFLAGS"
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

    if [ "$SDK_FAMILY" = "netbsd4" ]; then
        # The NetBSD 4 Time Capsule crashes inside libc gettext/citrus during
        # early smbd option handling. Samba does not need translated messages on
        # the appliance, so keep the old static binary off that libc path.
        CONFIGURE_ARGS="$CONFIGURE_ARGS --without-gettext"
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
            # Samba's lib/replace probe can add pthread globally even after the
            # direct library variables are cleared. Remove those global flags so
            # the final smbd link line does not pull in NetBSD libpthread.
            perl -0pi -e "s/'-lpthread',\\s*//g; s/'-pthread',\\s*//g; s/\\s*'\\-pthread'\\s*//g" "$cache_file"
            perl -0pi -e "s/'-D_REENTRANT',\\s*//g; s/'-D_POSIX_PTHREAD_SEMANTICS',\\s*//g" "$cache_file"
            perl -0pi -e 's/^replace_add_global_pthread = True$/replace_add_global_pthread = False/m' "$cache_file"
            perl -0pi -e "s/'HAVE_PTHREAD': '1'/'HAVE_PTHREAD': ()/g" "$cache_file"
            perl -0pi -e "s/'HAVE_PTHREAD_CREATE': 1/'HAVE_PTHREAD_CREATE': ()/g" "$cache_file"
            perl -0pi -e "s/'HAVE_PTHREAD_ATTR_INIT': 1/'HAVE_PTHREAD_ATTR_INIT': ()/g" "$cache_file"
            perl -0pi -e "s/'HAVE_LIBPTHREAD': 1/'HAVE_LIBPTHREAD': ()/g" "$cache_file"
            perl -0pi -e "s/'WITH_PTHREADPOOL': '1'/'WITH_PTHREADPOOL': ()/g" "$cache_file"
        fi
        if [ "$SDK_FAMILY" = "netbsd4" ]; then
            # The old appliance is sensitive to newer runtime support glue in
            # fully static binaries. Keep stack-protector out of the final
            # NetBSD 4 smbd until we can prove the target runtime accepts it.
            perl -0pi -e "s/'-fstack-protector',\\s*//g; s/\\s*'\\-fstack-protector'\\s*//g" "$cache_file"
            if [ "$SAMBA4_NETBSD4_GC_SECTIONS" = "1" ]; then
                # Configure probes use no-GC link flags so their NetBSD notes
                # survive cross-exec. After configure, force only the real build
                # cache to link with --gc-sections; the staged smbd is validated
                # and note-repaired below.
                # Waf's smbd link path honors LINKFLAGS for linker switches
                # and object-like extra inputs; LDFLAGS alone is not enough.
                if grep -q '^LINKFLAGS = \[' "$cache_file"; then
                    perl -0pi -e "s|^LINKFLAGS = \\[.*?\\]\$|LINKFLAGS = [$SAMBA4_NETBSD4_FINAL_LINKFLAGS]|m" "$cache_file"
                else
                    printf '%s\n' "LINKFLAGS = [$SAMBA4_NETBSD4_FINAL_LINKFLAGS]" >>"$cache_file"
                fi
                if grep -q '^LDFLAGS = ' "$cache_file"; then
                    perl -0pi -e "s/^LDFLAGS = .*$/LDFLAGS = '$SAMBA4_NETBSD4_FINAL_LDFLAGS'/m" "$cache_file"
                else
                    printf '%s\n' "LDFLAGS = '$SAMBA4_NETBSD4_FINAL_LDFLAGS'" >>"$cache_file"
                fi
            fi
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
            perl -0pi -e 's/^#define WITH_PTHREADPOOL 1$/\/\* #undef WITH_PTHREADPOOL \*\//m' "$config_header"
        fi
        perl -0pi -e 's/^#define HAVE_EXECINFO_H 1$/\/\* #undef HAVE_EXECINFO_H \*\//m' "$config_header"
        perl -0pi -e 's/^#define HAVE_BACKTRACE 1$/\/\* #undef HAVE_BACKTRACE \*\//m' "$config_header"
        perl -0pi -e 's/^#define HAVE_BACKTRACE_SYMBOLS 1$/\/\* #undef HAVE_BACKTRACE_SYMBOLS \*\//m' "$config_header"
    done

    if [ "$SDK_FAMILY" = "netbsd4" ] && [ "$SAMBA4_NETBSD4_GC_SECTIONS" = "1" ]; then
        export LDFLAGS="$SAMBA4_NETBSD4_FINAL_LDFLAGS"
        echo "Final NetBSD4 build LDFLAGS=$LDFLAGS"
    fi

    PYTHON="$PYTHON2_BIN" ./buildtools/bin/waf -v -j"$SAMBA4_JOBS" build --targets=smbd/smbd

    SAMBA4_SMBD="$(find "$SAMBA4_SRC_DIR/bin" -path '*/source3/smbd/smbd' | head -n1)"
    if [ -z "$SAMBA4_SMBD" ] || [ ! -f "$SAMBA4_SMBD" ]; then
        echo "Unable to locate built Samba 4 smbd under $SAMBA4_SRC_DIR/bin"
        exit 1
    fi

    SAMBA4_FILE_OUTPUT="$("$TOOLDIR/bin/nbfile" "$SAMBA4_SMBD" 2>&1 || true)"
    echo "$SAMBA4_FILE_OUTPUT"
    # NetBSD 4 nbfile can lack a magic database, so use program headers as the
    # authoritative static check. Dynamic ELFs have INTERP/DYNAMIC headers.
    if "$TOOLDIR/bin/$TRIPLE-objdump" -p "$SAMBA4_SMBD" | grep -Eq '^[[:space:]]+(INTERP|DYNAMIC)'; then
        echo "Samba 4 smbd has dynamic ELF headers; refusing to stage it."
        exit 1
    fi
    dump_elf_notes "built smbd" "$SAMBA4_SMBD"
    validate_netbsd4_notes "$SAMBA4_SMBD"

    mkdir -p "$SAMBA4_STAGE/sbin"
    cp "$SAMBA4_SMBD" "$SAMBA4_STAGE/sbin/smbd"
    cp "$SAMBA4_SMBD" "$SAMBA4_STAGE/sbin/smbd.stripped"
    "$STRIP" --strip-unneeded "$SAMBA4_STAGE/sbin/smbd.stripped"
    validate_netbsd4_notes "$SAMBA4_STAGE/sbin/smbd.stripped"
    dump_elf_notes "staged stripped smbd" "$SAMBA4_STAGE/sbin/smbd.stripped"
} >"$SAMBA4_LOG" 2>&1

printf 'Samba 4 build complete.\n'
printf 'Log: %s\n' "$SAMBA4_LOG"
printf 'Regular binary: %s\n' "$SAMBA4_STAGE/sbin/smbd"
printf 'Stripped binary: %s\n' "$SAMBA4_STAGE/sbin/smbd.stripped"
