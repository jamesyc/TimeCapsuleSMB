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

    python_magic="$SRC/external/bsd/file/dist/magic/magdir/python"
    if [ -f "$python_magic" ]; then
        printf 'Applying NetBSD 7 file(1) magic compatibility patch at %s\n' "$(date -u)"
        awk 'NR==59{$0=">&0\tsearch/4096\texcept:\tPython script text executable"} {print}' \
            "$python_magic" > "$python_magic.new"
        mv "$python_magic.new" "$python_magic"
    fi

    printf 'Downloaded/extracted NetBSD 7 sources into %s\n' "$NETBSD7_ROOT"
    echo "Finished download workflow at $(date -u)"
} >"$DOWNLOAD_LOG" 2>&1

printf 'Download complete.\n'
printf 'Log: %s\n' "$DOWNLOAD_LOG"
