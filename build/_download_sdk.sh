#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"

case "$SDK_FAMILY" in
    netbsd4)
        NETBSD_GIT_URL='https://github.com/NetBSD/src.git'
        NETBSD_GIT_BRANCH='netbsd-4'
        BUILD_LABEL='NetBSD 4'
        ;;
    netbsd7)
        NETBSD_GIT_URL='https://github.com/NetBSD/src.git'
        NETBSD_GIT_BRANCH='netbsd-7'
        BUILD_LABEL='NetBSD 7'
        ;;
    *)
        echo "Unsupported SDK_FAMILY: $SDK_FAMILY"
        exit 1
        ;;
esac

mkdir -p "$BUILD_ROOT"
mkdir -p "$OUT"
cd "$BUILD_ROOT"

{
    echo "Starting $BUILD_LABEL download workflow at $(date -u)"
    echo "SDK_FAMILY=$SDK_FAMILY"
    if [ -d "$SRC/.git" ]; then
        printf 'Reusing existing git checkout at %s\n' "$SRC"
        printf 'Refreshing branch %s at %s\n' "$NETBSD_GIT_BRANCH" "$(date -u)"
        git -C "$SRC" fetch --depth 1 origin "$NETBSD_GIT_BRANCH"
        git -C "$SRC" checkout -f "$NETBSD_GIT_BRANCH"
        git -C "$SRC" reset --hard "origin/$NETBSD_GIT_BRANCH"
    else
        rm -rf "$SRC"
        printf 'Cloning %s branch %s at %s\n' "$NETBSD_GIT_URL" "$NETBSD_GIT_BRANCH" "$(date -u)"
        git clone --depth 1 --branch "$NETBSD_GIT_BRANCH" "$NETBSD_GIT_URL" "$SRC"
    fi

    commands_magic="$SRC/external/bsd/file/dist/magic/magdir/commands"
    if [ "$SDK_FAMILY" = "netbsd7" ] && [ -f "$commands_magic" ]; then
        printf 'Applying NetBSD 7 file(1) commands magic compatibility patch at %s\n' "$(date -u)"
        awk 'NR==59{$0="0\tsearch/4096\tBEGIN{\tawk script text"} {print}' \
            "$commands_magic" > "$commands_magic.new"
        mv "$commands_magic.new" "$commands_magic"
    fi

    python_magic="$SRC/external/bsd/file/dist/magic/magdir/python"
    if [ "$SDK_FAMILY" = "netbsd7" ] && [ -f "$python_magic" ]; then
        printf 'Applying NetBSD 7 file(1) magic compatibility patch at %s\n' "$(date -u)"
        awk 'NR==38{$0="0\tsearch/4096\t import \tPython script text executable"} NR==59{$0=">&0\tsearch/4096\texcept:\tPython script text executable"} NR==65{$0="0\tsearch/4096\tdef \tPython script text executable"} NR==66{next} {print}' \
            "$python_magic" > "$python_magic.new"
        mv "$python_magic.new" "$python_magic"
    fi

    windows_magic="$SRC/external/bsd/file/dist/magic/magdir/windows"
    if [ "$SDK_FAMILY" = "netbsd7" ] && [ -f "$windows_magic" ]; then
        printf 'Applying NetBSD 7 file(1) windows magic compatibility patch at %s\n' "$(date -u)"
        awk 'NR==163{$0="0\tsearch/8192\t[Version]\tWindows setup text"} {print}' \
            "$windows_magic" > "$windows_magic.new"
        mv "$windows_magic.new" "$windows_magic"
    fi

    gcc_makefile="$SRC/gnu/dist/gcc4/gcc/Makefile.in"
    if [ "$SDK_FAMILY" = "netbsd4" ] && [ -f "$gcc_makefile" ]; then
        printf 'Applying NetBSD 4 gcc pregenerated parser staging patch at %s\n' "$(date -u)"
        perl -0pi -e 's/gengtype-lex\.c : gengtype-lex\.l\n\t\@echo "NOT REBUILDING \$\@"\nNetBSD_DISABLED_gengtype-lex\.c:\n\t-\$\(FLEX\) \$\(FLEXFLAGS\) -o\$\@ \$</gengtype-lex.c : gengtype-lex.l\n\tcp -f \$(srcdir)\/gengtype-lex.c \$\@\nNetBSD_DISABLED_gengtype-lex.c:\n\t-\$(FLEX) \$(FLEXFLAGS) -o\$\@ \$</s' \
            "$gcc_makefile"
        perl -0pi -e 's/gengtype-yacc\.c gengtype-yacc\.h: gengtype-yacc\.y\n\t\@echo "NOT REBUILDING \$\@"\nNetBSD_DISABLED_gengtype-yacc\.c:\n\t-\$\(BISON\) \$\(BISONFLAGS\) -d -o gengtype-yacc\.c \$</gengtype-yacc.c gengtype-yacc.h: gengtype-yacc.y\n\tcp -f \$(srcdir)\/gengtype-yacc.c gengtype-yacc.c\n\tcp -f \$(srcdir)\/gengtype-yacc.h gengtype-yacc.h\nNetBSD_DISABLED_gengtype-yacc.c:\n\t-\$(BISON) \$(BISONFLAGS) -d -o gengtype-yacc.c \$</s' \
            "$gcc_makefile"
    fi

    magic_root="$SRC/external/bsd/file/dist/magic"
    if [ -d "$magic_root" ]; then
        printf 'Removing stale generated file(1) magic databases at %s\n' "$(date -u)"
        find "$magic_root" -type f \( -name '*.mgc' -o -name 'magic.mgc' \) -delete
    fi

    printf 'Downloaded/extracted %s sources into %s\n' "$BUILD_LABEL" "$BUILD_ROOT"
    echo "Finished $BUILD_LABEL download workflow at $(date -u)"
} >"$DOWNLOAD_LOG" 2>&1

printf 'Download complete.\n'
printf 'Log: %s\n' "$DOWNLOAD_LOG"
