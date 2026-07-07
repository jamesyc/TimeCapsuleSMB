#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"
. "$(dirname "$0")/_patch_helpers.sh"

PATCH_DIR="$(CDPATH= cd "$(dirname "$0")/patches/rsync" && pwd)"

mkdir -p "$OUT" "$RSYNC_SOURCE_WORK"

{
    echo "Starting rsync download workflow at $(date -u)"
    echo "SDK_FAMILY=$SDK_FAMILY"
    echo "NETBSD4_ABI=$NETBSD4_ABI"
    echo "RSYNC_VERSION=$RSYNC_VERSION"
    echo "RSYNC_URL=$RSYNC_URL"
    echo "RSYNC_SOURCE_WORK=$RSYNC_SOURCE_WORK"
    echo "RSYNC_SRC_DIR=$RSYNC_SRC_DIR"

    archive="$RSYNC_SOURCE_WORK/$RSYNC_ARCHIVE_NAME"
    tmp="$archive.tmp.$$"
    rm -f "$tmp"
    curl -fL "$RSYNC_URL" -o "$tmp"
    mv "$tmp" "$archive"

    rm -rf "$RSYNC_SRC_DIR"
    tar -xzf "$archive" -C "$RSYNC_SOURCE_WORK"

    if [ ! -f "$RSYNC_SRC_DIR/configure" ] && [ -f "$RSYNC_SRC_DIR/configure.sh" ]; then
        # rsync 3.4.x ships configure.sh as the generated autoconf entrypoint.
        # Keep a stable configure path so the build helper does not need
        # version-specific entrypoint logic.
        cp "$RSYNC_SRC_DIR/configure.sh" "$RSYNC_SRC_DIR/configure"
        chmod +x "$RSYNC_SRC_DIR/configure"
    fi

    if [ ! -x "$RSYNC_SRC_DIR/configure" ]; then
        echo "Downloaded rsync source is missing an executable configure script: $RSYNC_SRC_DIR"
        exit 1
    fi

    patch_apply_series "rsync" "$PATCH_DIR/series" "$RSYNC_SRC_DIR"

    echo "Finished rsync download workflow at $(date -u)"
} >"$RSYNC_DOWNLOAD_LOG" 2>&1

printf 'rsync download complete.\n'
printf 'Log: %s\n' "$RSYNC_DOWNLOAD_LOG"
