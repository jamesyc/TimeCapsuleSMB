#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"

NETBSD7_GIT_URL='https://github.com/NetBSD/src.git'
NETBSD7_GIT_BRANCH='netbsd-7'

mkdir -p "$NETBSD7_ROOT"
mkdir -p "$OUT"
cd "$NETBSD7_ROOT"

{
    echo "Starting download workflow at $(date -u)"
    if [ -d "$SRC/.git" ]; then
        printf 'Reusing existing git checkout at %s\n' "$SRC"
        printf 'Refreshing branch %s at %s\n' "$NETBSD7_GIT_BRANCH" "$(date -u)"
        git -C "$SRC" fetch --depth 1 origin "$NETBSD7_GIT_BRANCH"
        git -C "$SRC" checkout -f "$NETBSD7_GIT_BRANCH"
        git -C "$SRC" reset --hard "origin/$NETBSD7_GIT_BRANCH"
    else
        rm -rf "$SRC"
        printf 'Cloning %s branch %s at %s\n' "$NETBSD7_GIT_URL" "$NETBSD7_GIT_BRANCH" "$(date -u)"
        git clone --depth 1 --branch "$NETBSD7_GIT_BRANCH" "$NETBSD7_GIT_URL" "$SRC"
    fi

    commands_magic="$SRC/external/bsd/file/dist/magic/magdir/commands"
    if [ -f "$commands_magic" ]; then
        printf 'Applying NetBSD 7 file(1) commands magic compatibility patch at %s\n' "$(date -u)"
        awk 'NR==59{$0="0\tsearch/4096\tBEGIN{\tawk script text"} {print}' \
            "$commands_magic" > "$commands_magic.new"
        mv "$commands_magic.new" "$commands_magic"
    fi

    python_magic="$SRC/external/bsd/file/dist/magic/magdir/python"
    if [ -f "$python_magic" ]; then
        printf 'Applying NetBSD 7 file(1) magic compatibility patch at %s\n' "$(date -u)"
        awk 'NR==38{$0="0\tsearch/4096\t import \tPython script text executable"} NR==59{$0=">&0\tsearch/4096\texcept:\tPython script text executable"} NR==65{$0="0\tsearch/4096\tdef \tPython script text executable"} NR==66{next} {print}' \
            "$python_magic" > "$python_magic.new"
        mv "$python_magic.new" "$python_magic"
    fi

    windows_magic="$SRC/external/bsd/file/dist/magic/magdir/windows"
    if [ -f "$windows_magic" ]; then
        printf 'Applying NetBSD 7 file(1) windows magic compatibility patch at %s\n' "$(date -u)"
        awk 'NR==163{$0="0\tsearch/8192\t[Version]\tWindows setup text"} {print}' \
            "$windows_magic" > "$windows_magic.new"
        mv "$windows_magic.new" "$windows_magic"
    fi

    magic_root="$SRC/external/bsd/file/dist/magic"
    if [ -d "$magic_root" ]; then
        printf 'Removing stale generated file(1) magic databases at %s\n' "$(date -u)"
        find "$magic_root" -type f \( -name '*.mgc' -o -name 'magic.mgc' \) -delete
    fi

    printf 'Downloaded/extracted NetBSD 7 sources into %s\n' "$NETBSD7_ROOT"
    echo "Finished download workflow at $(date -u)"
} >"$DOWNLOAD_LOG" 2>&1

printf 'Download complete.\n'
printf 'Log: %s\n' "$DOWNLOAD_LOG"
