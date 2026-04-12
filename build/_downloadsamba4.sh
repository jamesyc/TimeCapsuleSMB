#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"

mkdir -p "$OUT" "$SAMBA4_WORK"

{
    echo "Starting Samba 4 download workflow at $(date -u)"
    echo "SDK_FAMILY=$SDK_FAMILY"
    echo "DEVICE_FAMILY=$DEVICE_FAMILY"
    echo "SAMBA4_VERSION=$SAMBA4_VERSION"
    echo "SAMBA4_GIT_URL=$SAMBA4_GIT_URL"
    echo "SAMBA4_GIT_REF=$SAMBA4_GIT_REF"
    echo "SAMBA4_SRC_DIR=$SAMBA4_SRC_DIR"

    # Samba 4.2 waf expects a host-side Python 2 environment on the VM.
    echo "This part is installing Python 2.7 on the VM with pkgin so Samba 4 waf can find the host interpreter and headers."
    if pkg_info python27 >/dev/null 2>&1; then
        echo "python27 is already installed on the VM; skipping pkgin install."
    else
        /usr/pkg/bin/pkgin -4 -y install python27
    fi

    if [ -d "$SAMBA4_SRC_DIR/.git" ]; then
        printf 'Refreshing existing git checkout at %s\n' "$SAMBA4_SRC_DIR"
        git -C "$SAMBA4_SRC_DIR" fetch --depth 1 origin "$SAMBA4_GIT_REF"
        git -C "$SAMBA4_SRC_DIR" checkout -B "$SAMBA4_GIT_REF" "FETCH_HEAD"
        git -C "$SAMBA4_SRC_DIR" reset --hard "FETCH_HEAD"
    elif [ -d "$SAMBA4_SRC_DIR" ]; then
        printf 'Removing existing non-git Samba source tree at %s\n' "$SAMBA4_SRC_DIR"
        rm -rf "$SAMBA4_SRC_DIR"
        git clone --depth 1 --branch "$SAMBA4_GIT_REF" "$SAMBA4_GIT_URL" "$SAMBA4_SRC_DIR"
    else
        git clone --depth 1 --branch "$SAMBA4_GIT_REF" "$SAMBA4_GIT_URL" "$SAMBA4_SRC_DIR"
    fi

    perl -0pi -e 's/perl_inc = read_perl_config_var\('\''print "\@INC"'\''\)\n(?:\s*if '\''\.'\'' in perl_inc:\n)*(?:\s*perl_inc\.remove\('\''\.'\''\)\n)?/perl_inc = read_perl_config_var('\''print "\@INC"'\'')\n    if '\''.'\'' in perl_inc:\n        perl_inc.remove('\''.'\'')\n/s' \
        "$SAMBA4_SRC_DIR/buildtools/wafsamba/samba_perl.py"
    perl -0pi -e 's/#ifndef PRINT_MAX_JOBID/#include <time.h>\n\n#ifndef PRINT_MAX_JOBID/' \
        "$SAMBA4_SRC_DIR/lib/param/loadparm.h"
    perl -0pi -e 's/conf\.SAMBA_CHECK_PYTHON_HEADERS\(mandatory=True\)/conf.SAMBA_CHECK_PYTHON_HEADERS(mandatory=False)/g' \
        "$SAMBA4_SRC_DIR/wscript" \
        "$SAMBA4_SRC_DIR/ctdb/wscript" \
        "$SAMBA4_SRC_DIR/lib/ldb/wscript"
    perl -0pi -e 's/conf\.SAMBA_CHECK_PYTHON_HEADERS\(mandatory=\(not conf\.env\.disable_python\)\)/conf.SAMBA_CHECK_PYTHON_HEADERS(mandatory=False)/g' \
        "$SAMBA4_SRC_DIR/wscript" \
        "$SAMBA4_SRC_DIR/lib/ldb/wscript"
    perl -0pi -e 's/conf\.SAMBA_CHECK_PYTHON_HEADERS\(mandatory=not conf\.env\.disable_python\)/conf.SAMBA_CHECK_PYTHON_HEADERS(mandatory=False)/g' \
        "$SAMBA4_SRC_DIR/lib/ldb/wscript"
    perl -0pi -e 's/conf\.SAMBA_CHECK_PYTHON_HEADERS\(mandatory=False\)\n/conf.SAMBA_CHECK_PYTHON_HEADERS(mandatory=False)\n    conf.env.disable_python = not conf.env.HAVE_PYTHON_H\n/' \
        "$SAMBA4_SRC_DIR/wscript"
    perl -0pi -e 's/enabled=enabled\)/enabled=(enabled and not bld.env.disable_python and bld.CONFIG_SET('\''HAVE_PYTHON_H'\'')))/' \
        "$SAMBA4_SRC_DIR/buildtools/wafsamba/samba_python.py"
    # dynconfig.c includes replace.h, which includes generated config.h. On a
    # clean waf tree that file lives under bin/default/include, so dynconfig
    # needs the "include" build path explicitly or the compile dies on
    # "config.h: No such file or directory".
    perl -0pi -e "s/deps='replace',\\n/deps='replace',\\n                        includes='include',\\n/" \
        "$SAMBA4_SRC_DIR/dynconfig/wscript"

    awk '
        BEGIN { wrap = 0 }
        !wrap && /^bld\.SAMBA_SUBSYSTEM\('\''pyrpc_util'\''/ {
            print "if not bld.env.disable_python:"
            wrap = 1
        }
        {
            if (wrap) {
                print "    " $0
                if ($0 ~ /^\t\)$/) {
                    print "else:"
                    print "    bld.SAMBA_SUBSYSTEM('\''pyrpc_util'\'', source='\'''\'')"
                    wrap = 0
                }
            } else {
                print
            }
        }
    ' "$SAMBA4_SRC_DIR/source4/librpc/wscript_build" >"$SAMBA4_SRC_DIR/source4/librpc/wscript_build.tmp"
    mv "$SAMBA4_SRC_DIR/source4/librpc/wscript_build.tmp" "$SAMBA4_SRC_DIR/source4/librpc/wscript_build"

    awk '
        BEGIN { wrap = 0 }
        !wrap && /^bld\.SAMBA_SUBSYSTEM\('\''PROVISION'\''/ {
            print "if not bld.env.disable_python:"
            wrap = 1
        }
        {
            if (wrap) {
                print "    " $0
                if ($0 ~ /^\t\)$/) {
                    print "else:"
                    print "    bld.SAMBA_SUBSYSTEM('\''PROVISION'\'', source='\'''\'')"
                    wrap = 0
                }
            } else {
                print
            }
        }
    ' "$SAMBA4_SRC_DIR/source4/param/wscript_build" >"$SAMBA4_SRC_DIR/source4/param/wscript_build.tmp"
    mv "$SAMBA4_SRC_DIR/source4/param/wscript_build.tmp" "$SAMBA4_SRC_DIR/source4/param/wscript_build"

    awk '
        BEGIN { wrap = 0 }
        !wrap && /^bld\.SAMBA_SUBSYSTEM\('\''pyparam_util'\''/ {
            print "if not bld.env.disable_python:"
            wrap = 1
        }
        {
            if (wrap) {
                print "    " $0
                if ($0 ~ /^\t\)$/) {
                    print "else:"
                    print "    bld.SAMBA_SUBSYSTEM('\''pyparam_util'\'', source='\'''\'')"
                    wrap = 0
                }
            } else {
                print
            }
        }
    ' "$SAMBA4_SRC_DIR/source4/param/wscript_build" >"$SAMBA4_SRC_DIR/source4/param/wscript_build.tmp"
    mv "$SAMBA4_SRC_DIR/source4/param/wscript_build.tmp" "$SAMBA4_SRC_DIR/source4/param/wscript_build"

    cat >"$SAMBA4_SRC_DIR/python/wscript_build" <<'EOF'
#!/usr/bin/env python

if not bld.env.disable_python:
    bld.SAMBA_LIBRARY('samba_python',
        source=[],
        deps='LIBPYTHON pytalloc-util pyrpc_util',
        grouping_library=True,
        private_library=True,
        pyembed=True)

    bld.SAMBA_SUBSYSTEM('LIBPYTHON',
        source='modules.c',
        public_deps='',
        init_function_sentinel='{NULL,NULL}',
        deps='talloc',
        pyext=True,
        )

    bld.SAMBA_PYTHON('python_uuid',
        source='uuidmodule.c',
        deps='ndr',
        realname='uuid.so',
        enabled = float(bld.env.PYTHON_VERSION) <= 2.4
        )

    bld.SAMBA_PYTHON('python_glue',
        source='pyglue.c',
        deps='pyparam_util samba-util netif pytalloc-util',
        realname='samba/_glue.so'
        )

    bld.SAMBA_SCRIPT('samba_python_files',
        pattern='samba/**/*.py',
        installdir='python')

    bld.INSTALL_WILDCARD('${PYTHONARCHDIR}', 'samba/**/*.py', flat=False)
EOF

    perl -0pi -e "s/SRC = '''tevent\\.c tevent_debug\\.c tevent_fd\\.c tevent_immediate\\.c\\n             tevent_queue\\.c tevent_req\\.c\\n             tevent_poll\\.c tevent_threads\\.c\\n             tevent_signal\\.c tevent_standard\\.c tevent_timed\\.c tevent_util\\.c tevent_wakeup\\.c'''/SRC = '''tevent.c tevent_debug.c tevent_fd.c tevent_immediate.c\\n             tevent_queue.c tevent_req.c\\n             tevent_poll.c\\n             tevent_signal.c tevent_standard.c tevent_timed.c tevent_util.c tevent_wakeup.c'''\\n\\n    if bld.CONFIG_SET('HAVE_PTHREAD'):\\n        SRC += ' tevent_threads.c'/s" \
        "$SAMBA4_SRC_DIR/lib/tevent/wscript"
    perl -0pi -e "s/\\n\\ttevent_poll_init\\(\\);\\n\\ttevent_poll_mt_init\\(\\);/\\n\\ttevent_poll_init();\\n#ifdef HAVE_PTHREAD\\n\\ttevent_poll_mt_init();\\n#endif/s" \
        "$SAMBA4_SRC_DIR/lib/tevent/tevent.c"
    perl -0pi -e "s/\\n\\tif \\(ev->threaded_contexts != NULL\\) \\{\\n\\t\\ttevent_common_threaded_activate_immediate\\(ev\\);\\n\\t\\}/\\n#ifdef HAVE_PTHREAD\\n\\tif (ev->threaded_contexts != NULL) {\\n\\t\\ttevent_common_threaded_activate_immediate(ev);\\n\\t}\\n#endif/s" \
        "$SAMBA4_SRC_DIR/lib/tevent/tevent_poll.c"
    perl -0pi -e "s/\\n\\tif \\(ev->threaded_contexts != NULL\\) \\{\\n\\t\\ttevent_common_threaded_activate_immediate\\(ev\\);\\n\\t\\}/\\n#ifdef HAVE_PTHREAD\\n\\tif (ev->threaded_contexts != NULL) {\\n\\t\\ttevent_common_threaded_activate_immediate(ev);\\n\\t}\\n#endif/s" \
        "$SAMBA4_SRC_DIR/lib/tevent/tevent_epoll.c"
    perl -0pi -e "s/\\n\\tif \\(ev->threaded_contexts != NULL\\) \\{\\n\\t\\ttevent_common_threaded_activate_immediate\\(ev\\);\\n\\t\\}/\\n#ifdef HAVE_PTHREAD\\n\\tif (ev->threaded_contexts != NULL) {\\n\\t\\ttevent_common_threaded_activate_immediate(ev);\\n\\t}\\n#endif/s" \
        "$SAMBA4_SRC_DIR/lib/tevent/tevent_port.c"

    # The AirPort's NetBSD userland aborts in malloc when the Samba build pulls
    # in libpthread-backed code paths. Keep the old NO_PTHREADS behavior by
    # stripping pthread dependencies from Samba's wscript graph before
    # configure; the generated cache is forced off later in _samba4.sh.
    if [ "$NO_PTHREADS" = "1" ]; then
        perl -0pi -e "s/tevent execinfo pthread strv/tevent execinfo strv/" \
            "$SAMBA4_SRC_DIR/lib/util/wscript_build"
        perl -0pi -e "s/public_deps='talloc tevent execinfo pthread/public_deps='talloc tevent execinfo/" \
            "$SAMBA4_SRC_DIR/lib/util/wscript_build"
        perl -0pi -e "s/deps='replace socket-blocking sys_rw pthread'/deps='replace socket-blocking sys_rw'/" \
            "$SAMBA4_SRC_DIR/lib/pthreadpool/wscript_build"
        perl -0pi -e "s/public_deps='replace pthread'/public_deps='replace'/" \
            "$SAMBA4_SRC_DIR/lib/pthreadpool/wscript_build"
    fi

    # The NetBSD 7 static libexecinfo archive depends on libelf, but this old
    # Samba 4.8 waf setup does not model that transitive dependency. Rather
    # than patch generated link lines repeatedly, turn off the optional
    # backtrace/execinfo feature at the source-tree level for reproducible
    # static cross-builds.
    perl -0pi -e "s/tevent execinfo pthread strv/tevent pthread strv/" \
        "$SAMBA4_SRC_DIR/lib/util/wscript_build"
    perl -0pi -e "s/public_deps='talloc tevent execinfo pthread/public_deps='talloc tevent pthread/" \
        "$SAMBA4_SRC_DIR/lib/util/wscript_build"
    perl -0pi -e "s/ deps='roken wind asn1 hx509 hcrypto com_err HEIMDAL_CONFIG heimbase execinfo samba_intl',/ deps='roken wind asn1 hx509 hcrypto com_err HEIMDAL_CONFIG heimbase samba_intl',/" \
        "$SAMBA4_SRC_DIR/source4/heimdal_build/wscript_build"

    # NetBSD/HFS + fruit/streams_xattr/xattr_tdb hits a Samba bug where a
    # missing TDB record is treated as corruption instead of "no xattrs yet".
    # That bubbles up as EINVAL from listxattr/getxattr and breaks SMB
    # rename/delete paths. Patch xattr_tdb to treat NOT_FOUND as an empty set.
    perl -0pi -e 's/\tstatus = dbwrap_fetch\(db, frame, key, &data\);\n\tif \(!NT_STATUS_IS_OK\(status\)\) \{\n\t\treturn NT_STATUS_INTERNAL_DB_CORRUPTION;\n\t\}/\tstatus = dbwrap_fetch(db, frame, key, \&data);\n\tif (NT_STATUS_EQUAL(status, NT_STATUS_NOT_FOUND)) {\n\t\t*presult = talloc_zero(mem_ctx, struct tdb_xattrs);\n\t\tif (*presult == NULL) {\n\t\t\treturn NT_STATUS_NO_MEMORY;\n\t\t}\n\t\treturn NT_STATUS_OK;\n\t}\n\tif (!NT_STATUS_IS_OK(status)) {\n\t\treturn NT_STATUS_INTERNAL_DB_CORRUPTION;\n\t}/' \
        "$SAMBA4_SRC_DIR/source3/lib/xattr_tdb.c"

    git -C "$SAMBA4_SRC_DIR" rev-parse --short HEAD
    git -C "$SAMBA4_SRC_DIR" log -1 --format='%H%n%cd%n%s' --date=iso
    echo "Finished Samba 4 download workflow at $(date -u)"
} >"$SAMBA4_DOWNLOAD_LOG" 2>&1

printf 'Samba 4 download complete.\n'
printf 'Log: %s\n' "$SAMBA4_DOWNLOAD_LOG"
